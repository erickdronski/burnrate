"""Rendering a session as a receipt somebody would actually read.

The design goal is the paper receipt: the total at the bottom, the line items
above it, and nothing you have to decode. A cost tool that requires a legend
does not get looked at twice.

Three things this prints that most token counters do not:

* **What caching saved.** Cache reads cost a tenth of base input, so a long
  session is usually an order of magnitude cheaper than its token count
  suggests. Showing the counterfactual is the most useful line in the report.
* **What it could not price.** Unknown models are named and excluded, never
  silently costed at zero.
* **What the agent actually did.** Tokens are the price; tools called, files
  touched, and commands run are the thing you are paying for.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .pricing import (
    PRICES_AS_OF,
    Usage,
    price_usage,
    uncached_equivalent,
)
from .sessions import Session

__all__ = ["fmt_money", "price_session", "render_session", "render_summary"]


def fmt_money(amount: Optional[float]) -> str:
    """Money at a precision that matches how small these numbers get."""
    if amount is None:
        return "n/a"
    if amount == 0:
        return "$0.00"
    if abs(amount) < 0.01:
        return "$%.4f" % amount
    if abs(amount) < 1:
        return "$%.3f" % amount
    return "${:,.2f}".format(amount)


def fmt_tokens(count: Optional[int]) -> str:
    if count is None:
        return "n/a"
    if count >= 1_000_000:
        return "{:.1f}M".format(count / 1_000_000)
    if count >= 1_000:
        return "{:.1f}k".format(count / 1_000)
    return "{:,}".format(count)


def price_session(
    session: Session, prices: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Compute every figure the report needs, once."""
    by_model: List[Dict[str, Any]] = []
    total_cost = 0.0
    uncached_total = 0.0
    unpriced: List[str] = []
    priced_usage = Usage()

    fast_models = {t.model for t in session.turns if t.fast and t.model}

    for model, usage in sorted(
        session.usage_by_model().items(), key=lambda item: -item[1].total_tokens
    ):
        cost = price_usage(usage, model, prices, fast_mode=model in fast_models)
        counterfactual = uncached_equivalent(usage, model, prices)
        if cost is None:
            unpriced.append(model)
        else:
            total_cost += cost
            priced_usage.add(usage)
            if counterfactual is not None:
                uncached_total += counterfactual
        by_model.append(
            {
                "model": model,
                "usage": usage,
                "cost": cost,
                "uncached_cost": counterfactual,
                "fast": model in fast_models,
            }
        )

    total_usage = session.total_usage()
    cache_hit_rate = (
        total_usage.cache_read_tokens / total_usage.total_input_tokens
        if total_usage.total_input_tokens
        else 0.0
    )

    return {
        "session_id": session.session_id,
        "project": session.project,
        "date": session.date,
        "end_date": session.end_date,
        "branch": session.git_branch,
        "turns": session.turn_count,
        "tool_calls": session.tool_calls,
        "errors": session.errors,
        "duplicate_records": session.duplicate_records,
        "usage": total_usage,
        "by_model": by_model,
        "cost": total_cost,
        "uncached_cost": uncached_total,
        "saved_by_caching": max(0.0, uncached_total - total_cost),
        "cache_hit_rate": cache_hit_rate,
        "unpriced_models": unpriced,
        "top_tools": session.top_tools(),
        "top_files": session.top_files(),
        "commands": session.commands,
    }


def render_session(
    report: Mapping[str, Any], verbose: bool = False, width: int = 64
) -> str:
    """Render one session's receipt."""
    lines: List[str] = []
    rule = "─" * width

    lines.append(rule)
    header = "  %s" % (report["project"] or "session")
    if report["date"]:
        header += "   %s" % report["date"]
    lines.append(header)
    subtitle = "  %s" % report["session_id"][:8]
    if report["branch"]:
        subtitle += " · %s" % report["branch"]
    lines.append(subtitle)
    lines.append(rule)

    usage: Usage = report["usage"]
    lines.append("")
    lines.append("  %-28s %12s %12s" % ("", "TOKENS", "COST"))

    for entry in report["by_model"]:
        model_usage: Usage = entry["usage"]
        label = entry["model"] or "unknown"
        if entry["fast"]:
            label += " (fast)"
        lines.append(
            "  %-28s %12s %12s"
            % (
                label[:28],
                fmt_tokens(model_usage.total_tokens),
                fmt_money(entry["cost"]) if entry["cost"] is not None else "unpriced",
            )
        )

    lines.append("")
    lines.append("  %-28s %12s" % ("input (uncached)", fmt_tokens(usage.input_tokens)))
    lines.append(
        "  %-28s %12s" % ("input (cache read)", fmt_tokens(usage.cache_read_tokens))
    )
    if usage.cache_write_5m_tokens:
        lines.append(
            "  %-28s %12s"
            % ("cache write (5m)", fmt_tokens(usage.cache_write_5m_tokens))
        )
    if usage.cache_write_1h_tokens:
        lines.append(
            "  %-28s %12s"
            % ("cache write (1h)", fmt_tokens(usage.cache_write_1h_tokens))
        )
    lines.append("  %-28s %12s" % ("output", fmt_tokens(usage.output_tokens)))

    lines.append("")
    lines.append(rule)
    lines.append("  %-28s %25s" % ("TOTAL", fmt_money(report["cost"])))
    lines.append(rule)

    if report["saved_by_caching"] > 0:
        lines.append("")
        lines.append(
            "  Prompt caching saved %s (%s without it, %s of input served"
            % (
                fmt_money(report["saved_by_caching"]),
                fmt_money(report["uncached_cost"]),
                "{:.0%}".format(report["cache_hit_rate"]),
            )
        )
        lines.append("  from cache).")

    if report["unpriced_models"]:
        lines.append("")
        lines.append(
            "  ! Not included in the total — no price on file for: %s"
            % ", ".join(report["unpriced_models"])
        )
        lines.append("    Supply one with --prices to include it.")

    lines.append("")
    lines.append(
        "  %d turns · %d tool calls%s"
        % (
            report["turns"],
            report["tool_calls"],
            " · %d errors" % report["errors"] if report["errors"] else "",
        )
    )

    if report["top_tools"]:
        lines.append("")
        lines.append("  Tools")
        for name, count in report["top_tools"]:
            lines.append("    %-30s %4d" % (name[:30], count))

    if verbose and report["top_files"]:
        lines.append("")
        lines.append("  Files touched")
        for path, count in report["top_files"]:
            lines.append("    %-46s %4d" % (_shorten(path, 46), count))

    if verbose and report["commands"]:
        lines.append("")
        lines.append("  Commands run (%d)" % len(report["commands"]))
        for command in report["commands"][:15]:
            lines.append("    %s" % _shorten(command.replace("\n", " "), 56))
        if len(report["commands"]) > 15:
            lines.append("    ... and %d more" % (len(report["commands"]) - 15))

    lines.append("")
    lines.append("  Prices as of %s. Estimate, not an invoice." % PRICES_AS_OF)
    lines.append("")
    return "\n".join(lines)


def render_top(reports, limit: int = 10, width: int = 64) -> str:
    """The sessions that actually cost you money, ranked.

    A day-by-day roll-up tells you *that* Tuesday was expensive. It does not
    tell you which session did it, and that is the only version of the question
    anyone can act on — you go look at that session and decide whether the work
    was worth it.
    """
    rule = "─" * width
    ranked = sorted(reports, key=lambda r: -r["cost"])[:limit]
    total = sum(r["cost"] for r in reports)

    lines = [rule, "  MOST EXPENSIVE SESSIONS", rule, ""]
    lines.append("  %-10s %-14s %8s %9s %8s" % ("DATE", "PROJECT", "TURNS", "TOKENS", "COST"))
    for report in ranked:
        share = (report["cost"] / total * 100) if total else 0
        lines.append(
            "  %-10s %-14s %8d %9s %8s%s"
            % (
                (report["date"] or "—")[:10],
                (report["project"] or "—")[:14],
                report["turns"],
                fmt_tokens(report["usage"].total_tokens),
                fmt_money(report["cost"]),
                "  %2.0f%%" % share if share >= 1 else "",
            )
        )
    lines.append("")
    if ranked and total:
        top_share = sum(r["cost"] for r in ranked) / total * 100
        lines.append(
            "  These %d session(s) are %.0f%% of %s across %d session(s)."
            % (len(ranked), top_share, fmt_money(total), len(reports))
        )
    lines.append("")
    lines.append("  Prices as of %s. Estimate, not an invoice." % PRICES_AS_OF)
    lines.append("")
    return "\n".join(lines)


def render_trend(reports, width: int = 64) -> str:
    """Daily spend with a bar, and whether the trend is up or down.

    The number people actually want is not "what did I spend" but "am I
    spending more than I was", and that is a comparison a table of totals makes
    you do in your head.
    """
    rule = "─" * width
    by_day = {}
    spanning = 0
    for report in reports:
        # Attribute a session to the day it *finished*, not the day it began.
        # Long sessions get resumed across weeks, and bucketing by start date
        # dumps months of spend onto a single old bar. Neither choice is truly
        # correct — a multi-day session's cost did not happen on one day — so
        # the count of spanning sessions is reported rather than hidden.
        day = report.get("end_date") or report["date"] or "(undated)"
        if report.get("end_date") and report.get("date") and report["end_date"] != report["date"]:
            spanning += 1
        entry = by_day.setdefault(day, {"cost": 0.0, "sessions": 0, "tokens": 0})
        entry["cost"] += report["cost"]
        entry["sessions"] += 1
        entry["tokens"] += report["usage"].total_tokens

    days = sorted(by_day)
    if not days:
        return "No sessions to chart."

    peak = max(by_day[d]["cost"] for d in days) or 1.0
    lines = [rule, "  DAILY BURN", rule, ""]
    for day in days:
        entry = by_day[day]
        lines.append(
            "  %-10s %9s %-22s %d session(s)"
            % (day, fmt_money(entry["cost"]), bar(entry["cost"] / peak, 20), entry["sessions"])
        )

    lines.append("")
    # Compare the two halves rather than first-vs-last day: a single quiet
    # Sunday would otherwise read as a collapse in spending.
    half = max(1, len(days) // 2)
    earlier = sum(by_day[d]["cost"] for d in days[:half]) / half
    later = sum(by_day[d]["cost"] for d in days[-half:]) / half
    if earlier > 0:
        change = (later - earlier) / earlier * 100
        direction = "up" if change > 0 else "down"
        lines.append(
            "  Daily average is %s %.0f%% across this window (%s -> %s)."
            % (direction, abs(change), fmt_money(earlier), fmt_money(later))
        )
    total = sum(by_day[d]["cost"] for d in days)
    lines.append(
        "  %s over %d day(s); %s per day on average."
        % (fmt_money(total), len(days), fmt_money(total / len(days)))
    )
    if spanning:
        lines.append(
            "  %d session(s) spanned more than one day and are counted on the "
            "day they last ran." % spanning
        )
    lines.append("")
    lines.append("  Prices as of %s. Estimate, not an invoice." % PRICES_AS_OF)
    lines.append("")
    return "\n".join(lines)


def bar(fraction: float, width: int = 20) -> str:
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def render_summary(
    reports: Sequence[Mapping[str, Any]],
    group_by: str = "day",
    width: int = 64,
) -> str:
    """Roll several sessions up by day, project, or model."""
    rule = "─" * width
    lines: List[str] = []

    buckets: Dict[str, Dict[str, Any]] = {}
    for report in reports:
        if group_by == "project":
            keys = [report["project"] or "(unknown)"]
        elif group_by == "model":
            keys = [entry["model"] or "unknown" for entry in report["by_model"]]
        else:
            keys = [report["date"] or "(undated)"]

        for key in keys:
            bucket = buckets.setdefault(
                key,
                {
                    "cost": 0.0,
                    "uncached": 0.0,
                    "tokens": 0,
                    "sessions": 0,
                    "tool_calls": 0,
                },
            )
            if group_by == "model":
                entry = next(
                    e for e in report["by_model"] if (e["model"] or "unknown") == key
                )
                bucket["cost"] += entry["cost"] or 0.0
                bucket["uncached"] += entry["uncached_cost"] or 0.0
                bucket["tokens"] += entry["usage"].total_tokens
            else:
                bucket["cost"] += report["cost"]
                bucket["uncached"] += report["uncached_cost"]
                bucket["tokens"] += report["usage"].total_tokens
                bucket["tool_calls"] += report["tool_calls"]
            bucket["sessions"] += 1

    total_cost = sum(b["cost"] for b in buckets.values())
    total_uncached = sum(b["uncached"] for b in buckets.values())

    lines.append(rule)
    lines.append("  BURN BY %s" % group_by.upper())
    lines.append(rule)
    lines.append("")
    lines.append("  %-22s %9s %10s %10s" % ("", "SESSIONS", "TOKENS", "COST"))

    reverse = group_by != "day"
    for key in sorted(buckets, reverse=reverse):
        bucket = buckets[key]
        lines.append(
            "  %-22s %9d %10s %10s"
            % (
                str(key)[:22],
                bucket["sessions"],
                fmt_tokens(bucket["tokens"]),
                fmt_money(bucket["cost"]),
            )
        )

    lines.append("")
    lines.append(rule)
    lines.append("  %-22s %30s" % ("TOTAL", fmt_money(total_cost)))
    lines.append(rule)

    saved = max(0.0, total_uncached - total_cost)
    if saved > 0:
        lines.append("")
        lines.append(
            "  Prompt caching saved %s across %d session(s)."
            % (fmt_money(saved), sum(1 for _ in reports))
        )

    lines.append("")
    lines.append("  Prices as of %s. Estimate, not an invoice." % PRICES_AS_OF)
    lines.append("")
    return "\n".join(lines)


def _shorten(text: str, width: int) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    return "..." + text[-(width - 3) :]
