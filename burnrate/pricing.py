"""Model prices, and the arithmetic that turns token counts into dollars.

Prices change. This table is dated, and every figure it produces carries that
date forward, because a cost report that does not say when its prices were
current is a number with a hidden expiry.

The part most tools get wrong is **cache write pricing**. A 5-minute cache
write costs 1.25x the base input rate; a 1-hour cache write costs 2x. Tools
that apply a single cache-write multiplier are wrong for whichever TTL they did
not pick, and on a long agentic session — where 1-hour writes dominate — that
error runs to real money. The session logs record the two separately
(``ephemeral_5m_input_tokens`` and ``ephemeral_1h_input_tokens``), so there is
no excuse for blending them.

Override any of this with a JSON file:

    {"prices": {"my-model": {"input": 3.0, "output": 15.0}}}

passed as ``--prices path.json``. Unknown models are never guessed at — they
are reported as unpriced, and their tokens are excluded from the total rather
than silently costed at zero.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_WRITE_5M_MULTIPLIER",
    "PRICES",
    "PRICES_AS_OF",
    "PricingError",
    "Usage",
    "load_price_overrides",
    "price_usage",
    "resolve_model",
]

#: The date these prices were verified. Printed on every report.
PRICES_AS_OF = "2026-08-14"

#: US dollars per million tokens, base rates.
PRICES: Dict[str, Dict[str, float]] = {
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

#: Fast mode is a different price for the same model, so it is keyed separately
#: rather than folded into the base entry.
FAST_MODE_PRICES: Dict[str, Dict[str, float]] = {
    "claude-opus-5": {"input": 10.0, "output": 50.0},
    "claude-opus-4-8": {"input": 10.0, "output": 50.0},
}

#: A 5-minute cache write costs 1.25x base input; a 1-hour write costs 2x.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

#: Cache reads cost roughly a tenth of base input. This is the number that
#: makes long sessions affordable, and the one worth showing people.
CACHE_READ_MULTIPLIER = 0.1

#: Models that appear in logs but represent no billable API call.
NON_BILLABLE_MODELS = frozenset({"<synthetic>", "", "unknown"})


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

    Handles the dated-snapshot form (``claude-haiku-4-5-20251001``) by falling
    back to the longest known prefix. Returns ``None`` for anything genuinely
    unknown rather than guessing — a wrong price is worse than a stated gap.
    """
    if not model:
        return None
    name = model.strip()
    if name in NON_BILLABLE_MODELS:
        return None

    table = prices if prices is not None else PRICES
    if name in table:
        return name

    matches = [key for key in table if name.startswith(key)]
    if matches:
        return max(matches, key=len)
    return None


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
    table = dict(PRICES)
    if fast_mode:
        table.update(FAST_MODE_PRICES)
    if prices:
        table.update(prices)

    key = resolve_model(model, table)
    if key is None:
        return None

    rate = table[key]
    try:
        input_rate = float(rate["input"])
        output_rate = float(rate["output"])
    except (KeyError, TypeError, ValueError):
        raise PricingError(
            "price entry for %r must have numeric 'input' and 'output' rates" % key
        )

    per_token_input = input_rate / 1_000_000
    per_token_output = output_rate / 1_000_000

    return (
        usage.input_tokens * per_token_input
        + usage.output_tokens * per_token_output
        + usage.cache_read_tokens * per_token_input * CACHE_READ_MULTIPLIER
        + usage.cache_write_5m_tokens * per_token_input * CACHE_WRITE_5M_MULTIPLIER
        + usage.cache_write_1h_tokens * per_token_input * CACHE_WRITE_1H_MULTIPLIER
    )


def uncached_equivalent(
    usage: Usage, model: Optional[str], prices: Optional[Mapping[str, Any]] = None
) -> Optional[float]:
    """What this usage would have cost with no prompt caching at all.

    Every cached token is repriced at full input rate. The gap between this and
    the real cost is what caching saved — the single most satisfying number in
    the whole report, and the one that makes people forward it.
    """
    table = dict(PRICES)
    if prices:
        table.update(prices)
    key = resolve_model(model, table)
    if key is None:
        return None
    rate = table[key]
    per_token_input = float(rate["input"]) / 1_000_000
    per_token_output = float(rate["output"]) / 1_000_000
    return (
        usage.total_input_tokens * per_token_input
        + usage.output_tokens * per_token_output
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
        try:
            out[str(model)] = {
                "input": float(rate["input"]),
                "output": float(rate["output"]),
            }
        except (TypeError, ValueError):
            raise PricingError("price entry for %r has non-numeric rates" % model)
    return out
