"""Model prices, and the arithmetic that turns token counts into dollars.

Prices change. This table is dated, and every figure it produces carries that
date forward, because a cost report that does not say when its prices were
current is a number with a hidden expiry.

Two parts of this are easy to get wrong, and both run to real money:

**Cache reads are priced per model.** They are not a tenth of input
everywhere: Fable 5.1 and Mythos 5.1 read the cache at 0.025x input, Opus 5.5
at 0.05x, everything else at 0.1x. A long agentic session is 95%+ cache reads
by token count, so a single global multiplier is the largest error a cost
tool can make. On the machine this was measured on, reading Fable 5.1's cache
at 0.1x overstated its spend by 47%.

**Cache writes have two prices.** A 5-minute cache write costs 1.25x the base
input rate; a 1-hour cache write costs 2x. Tools that apply a single
cache-write multiplier are wrong for whichever TTL they did not pick, and on a
long agentic session — where 1-hour writes dominate — that error runs to real
money. The session logs record the two separately
(``ephemeral_5m_input_tokens`` and ``ephemeral_1h_input_tokens``), so there is
no excuse for blending them.

Override any of this with a JSON file:

    {"prices": {"my-model": {"input": 3.0, "output": 15.0, "cache_read": 0.3}}}

passed as ``--prices path.json``. ``cache_read`` is optional (``cached_input``
is accepted as a synonym, since that is what OpenAI calls it) and defaults to
a tenth of ``input``. Unknown models are never guessed at — they are reported
as unpriced, and their tokens are excluded from the total rather than silently
costed at zero.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_WRITE_5M_MULTIPLIER",
    "FAST_MODE_PRICES",
    "PRICES",
    "PRICES_AS_OF",
    "PricingError",
    "Usage",
    "load_price_overrides",
    "model_rates",
    "price_usage",
    "resolve_model",
    "uncached_equivalent",
]

#: The date these prices were verified. Printed on every report.
PRICES_AS_OF = "2026-09-25"

#: US dollars per million tokens. ``cache_read`` is stated for every model
#: rather than derived, because it is the rate that dominates the bill and the
#: one that differs between models.
PRICES: Dict[str, Dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10},
}

#: Fast mode is a different price for the same model, so it is keyed
#: separately rather than folded into the base entry. A fast turn on a model
#: with no entry here is reported as unpriced, not costed at the standard rate.
#:
#: The published rates give fast-mode input and output only. Cache reads apply
#: the model's own read ratio to the fast input rate, the same way the
#: cache-write multipliers do: 0.1x for Opus 5 and 4.8, and 0.05x for Opus 5.5,
#: whose fast-mode cache-read rate is not published at all — $0.40 is derived,
#: not quoted.
FAST_MODE_PRICES: Dict[str, Dict[str, float]] = {
    "claude-opus-5-5": {"input": 8.0, "output": 40.0, "cache_read": 0.40},
    "claude-opus-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0},
    "claude-opus-4-8": {"input": 10.0, "output": 50.0, "cache_read": 1.0},
}

#: A 5-minute cache write costs 1.25x base input; a 1-hour write costs 2x.
#: These hold on every model, so unlike cache reads they are global.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

#: The cache-read rate for a price entry that does not state one: a tenth of
#: base input. Only overrides rely on it — every built-in entry states its own,
#: because three current models read the cache at a quarter or half of this.
CACHE_READ_MULTIPLIER = 0.1

#: Models that appear in logs but represent no billable API call.
NON_BILLABLE_MODELS = frozenset({"<synthetic>", "", "unknown"})

#: A dated snapshot id: ``claude-sonnet-4-5-20250929``.
_DATE_SUFFIX = re.compile(r"-\d{8}$")

#: Suffixes that name a deployment of a model rather than a different model:
#: the context-window marker (``claude-opus-4-6[1m]``) and the Vertex AI
#: snapshot form (``claude-opus-4-5@20251101``).
_VARIANT_SUFFIX = re.compile(r"(?:\[[^\]]*\]|@\d{8})$")


class PricingError(ValueError):
    """Raised for malformed price overrides."""


class Usage:
    """Token counts for one API response, split the way billing splits them."""

    __slots__ = (
        "cache_read_tokens",
        "cache_write_1h_tokens",
        "cache_write_5m_tokens",
        "input_tokens",
        "output_tokens",
    )

    def __init__(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
    ) -> None:
        self.input_tokens = int(input_tokens)
        self.output_tokens = int(output_tokens)
        self.cache_read_tokens = int(cache_read_tokens)
        self.cache_write_5m_tokens = int(cache_write_5m_tokens)
        self.cache_write_1h_tokens = int(cache_write_1h_tokens)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    @property
    def total_input_tokens(self) -> int:
        """Every token that entered the model, cached or not.

        Worth reporting separately: a session showing 40k uncached input and
        2M cache reads did not process 40k tokens of context, it processed
        2.04M — and the difference is what prompt caching bought.
        """
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_5m_tokens += other.cache_write_5m_tokens
        self.cache_write_1h_tokens += other.cache_write_1h_tokens

    def to_dict(self) -> Dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_5m_tokens": self.cache_write_5m_tokens,
            "cache_write_1h_tokens": self.cache_write_1h_tokens,
            "total_tokens": self.total_tokens,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Usage(in=%d out=%d read=%d)" % (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
        )


def resolve_model(
    model: Optional[str], prices: Optional[Mapping[str, Any]] = None
) -> Optional[str]:
    """Map a logged model string to a price-table key, or ``None``.

    Exact names win. Failing that, a date suffix (``claude-haiku-4-5-20251001``)
    or a deployment suffix (``[1m]``, ``@20251101``) is stripped and the bare
    name looked up again.

    There is deliberately no prefix matching. ``claude-opus-5-5`` starts with
    ``claude-opus-5``, and matching on that priced Opus 5.5 at Opus 5's rates —
    25% too high on input and output, 2.5x on cache reads. A point release this
    table has not heard of is a new model with a price nobody here has checked,
    so it comes back ``None`` and is reported as unpriced: a wrong price is
    worse than a stated gap.
    """
    if not model:
        return None
    name = model.strip()
    if name in NON_BILLABLE_MODELS:
        return None

    table = prices if prices is not None else PRICES
    if name in table:
        return name

    while True:
        stripped = _DATE_SUFFIX.sub("", _VARIANT_SUFFIX.sub("", name))
        if stripped == name:
            return None
        name = stripped
        if name in table:
            return name


def model_rates(
    model: Optional[str],
    prices: Optional[Mapping[str, Any]] = None,
    fast_mode: bool = False,
) -> Optional[Dict[str, float]]:
    """The input, output, and cache-read rates for a model, or ``None``.

    Overrides take precedence over the built-in table, in fast mode too: an
    override for ``claude-opus-5`` prices that model's fast turns as well,
    because a user who supplied a rate meant it.
    """
    table: Dict[str, Any] = dict(FAST_MODE_PRICES if fast_mode else PRICES)
    if prices:
        table.update(prices)

    key = resolve_model(model, table)
    if key is None:
        return None

    rate = table[key]
    try:
        input_rate = float(rate["input"])
        output_rate = float(rate["output"])
        cache_read = rate.get("cache_read")
        cache_read_rate = (
            input_rate * CACHE_READ_MULTIPLIER
            if cache_read is None
            else float(cache_read)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PricingError(
            "price entry for %r must have numeric 'input' and 'output' rates" % key
        )
    return {"input": input_rate, "output": output_rate, "cache_read": cache_read_rate}


def price_usage(
    usage: Usage,
    model: Optional[str],
    prices: Optional[Mapping[str, Any]] = None,
    fast_mode: bool = False,
) -> Optional[float]:
    """Cost in US dollars, or ``None`` when the model has no known price.

    Returning ``None`` rather than 0.0 is deliberate: an unpriced model that
    silently costs nothing produces a report that is quietly, confidently
    wrong. The caller is expected to surface the gap.
    """
    rates = model_rates(model, prices, fast_mode)
    if rates is None:
        return None

    per_token_input = rates["input"] / 1_000_000
    per_token_output = rates["output"] / 1_000_000
    per_token_cache_read = rates["cache_read"] / 1_000_000

    return (
        usage.input_tokens * per_token_input
        + usage.output_tokens * per_token_output
        + usage.cache_read_tokens * per_token_cache_read
        + usage.cache_write_5m_tokens * per_token_input * CACHE_WRITE_5M_MULTIPLIER
        + usage.cache_write_1h_tokens * per_token_input * CACHE_WRITE_1H_MULTIPLIER
    )


def uncached_equivalent(
    usage: Usage,
    model: Optional[str],
    prices: Optional[Mapping[str, Any]] = None,
    fast_mode: bool = False,
) -> Optional[float]:
    """What this usage would have cost with no prompt caching at all.

    Every cached token is repriced at full input rate. The gap between this and
    the real cost is what caching saved — the single most satisfying number in
    the whole report, and the one that makes people forward it.
    """
    rates = model_rates(model, prices, fast_mode)
    if rates is None:
        return None
    return (
        usage.total_input_tokens * rates["input"] / 1_000_000
        + usage.output_tokens * rates["output"] / 1_000_000
    )


def load_price_overrides(path: str) -> Dict[str, Dict[str, float]]:
    """Read a price override file.

    Accepts either ``{"prices": {...}}`` or a bare mapping of model to rates,
    because both shapes are the obvious thing to write.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise PricingError("no such price file: %s" % path)
    except json.JSONDecodeError as exc:
        raise PricingError("%s is not valid JSON: %s" % (path, exc)) from exc

    if not isinstance(raw, dict):
        raise PricingError("price file must contain a JSON object")

    table = raw.get("prices", raw)
    if not isinstance(table, dict):
        raise PricingError("'prices' must be an object mapping model to rates")

    out: Dict[str, Dict[str, float]] = {}
    for model, rate in table.items():
        if not isinstance(rate, dict) or "input" not in rate or "output" not in rate:
            raise PricingError(
                "price entry for %r needs 'input' and 'output' rates in "
                "dollars per million tokens" % model
            )
        cache_read = rate.get("cache_read", rate.get("cached_input"))
        try:
            entry = {"input": float(rate["input"]), "output": float(rate["output"])}
            if cache_read is not None:
                entry["cache_read"] = float(cache_read)
        except (TypeError, ValueError):
            raise PricingError("price entry for %r has non-numeric rates" % model)
        out[str(model)] = entry
    return out
