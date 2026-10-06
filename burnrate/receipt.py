"""Rendering a session as a receipt somebody would actually read.

The design goal is the paper receipt: the total at the bottom, the line items
above it, and nothing you have to decode. A cost tool that requires a legend
does not get looked at twice.

Four things this prints that most token counters do not:

* **What caching saved.** Cache reads cost a tenth of base input or less, so a
  long session is usually an order of magnitude cheaper than its token count
  suggests. Showing the counterfactual is the most useful line in the report.
* **What it could not price.** Unknown models are named and excluded, never
  silently costed at zero.
* **What the agent actually did.** Tokens are the price; tools called, files
  touched, and commands run are the thing you are paying for.
* **How much of a subscription is gone.** On a flat-rate plan the dollar figure
  is notional; where the logs carry the plan's usage window, that is printed
  too.
"""

from __future__ import annotations

import datetime
import textwrap
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .pricing import (
    PRICES_AS_OF,
    Usage,
    price_usage,
    uncached_equivalent,
)
from .sessions import AGENT_LABELS, Session

__all__ = [
    "fmt_money",
    "fmt_plan_usage",
    "model_label",
    "price_session",
    "render_session",
    "render_summary",
    "render_top",
    "render_trend",
]


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


def model_label(model: Optional[str], fast: bool = False) -> str:
    """How a model is named in a report. Fast mode is its own line item."""
    label = model or "unknown"
    return label + " (fast)" if fast else label


def fmt_plan_usage(snapshot: Mapping[str, Any], now: Optional[float] = None) -> str:
    """One sentence on how much of a subscription window is used.

    The figure is the one the logs carried at ``as_of``, not a live reading,
    so the sentence says when it was taken and whether the window has rolled
    over since.
    """
    plan = snapshot.get("plan")
    text = "Codex plan usage%s: %.0f%% of the %s limit" % (
        " (%s)" % plan if plan else "",
        snapshot["used_percent"],
        _window(snapshot.get("window_minutes")),
    )
    secondary = snapshot.get("secondary")
    if secondary:
        text += " and %.0f%% of the %s limit" % (
            secondary["used_percent"],
            _window(secondary.get("window_minutes")),
        )
    text += ", as of %s" % _utc(snapshot["as_of"])
    resets = snapshot.get("resets_at")
    if resets:
        current = time.time() if now is None else now
        if resets <= current:
            text += "; that window has since reset."
        else:
            moment = datetime.datetime.fromtimestamp(resets, datetime.timezone.utc)
            text += "; resets %s." % moment.strftime("%Y-%m-%d %H:%M UTC")
    else:
        text += "."
    return text


def price_session(
    session: Session, prices: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Compute every figure the report needs, once."""
    by_model: List[Dict[str, Any]] = []
    total_cost = 0.0
    uncached_total = 0.0
    unpriced: List[str] = []
    priced_usage = Usage()

    for (model, fast), usage in sorted(
        session.usage_by_rate().items(), key=lambda item: -item[1].total_tokens
    ):
        cost = price_usage(usage, model, prices, fast_mode=fast)
        counterfactual = uncached_equivalent(usage, model, prices, fast_mode=fast)
        if cost is None:
            unpriced.append(model_label(model, fast))
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
                "fast": fast,
            }
        )

    total_usage = session.total_usage()
    cache_hit_rate = (
        total_usage.cache_read_tokens / total_usage.total_input_tokens
        if total_usage.total_input_tokens
        else 0.0
    )

    return {
        "agent": session.agent,
        "session_id": session.session_id,
        "short_id": session.short_id,
        "threads": session.threads,
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
        "priced": any(entry["cost"] is not None for entry in by_model),
        "plan_usage": session.plan_usage,
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
    span = _span(report["date"], report.get("end_date"))
    if span:
        header += "   %s" % span
    lines.append(header)
    subtitle = "  %s" % (report.get("short_id") or report["session_id"][:8])
    if report["branch"]:
        subtitle += " · %s" % report["branch"]
    subtitle += " · %s" % _agent_label(report)
    if report.get("threads", 1) > 1:
        subtitle += " · %d threads" % report["threads"]
    lines.append(subtitle)
    lines.append(rule)

    usage: Usage = report["usage"]
    lines.append("")
    lines.append("  %-28s %12s %12s" % ("", "TOKENS", "COST"))

    for entry in report["by_model"]:
        model_usage: Usage = entry["usage"]
        label = model_label(entry["model"], entry["fast"])
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
    lines.append(
        "  %-28s %25s" % ("TOTAL", _cost_cell(report["cost"], _priced(report)))
    )
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

    if report.get("plan_usage"):
        lines.append("")
        lines.extend(_wrap(fmt_plan_usage(report["plan_usage"]), width))

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
    # The agent column appears once anything but Claude Code is in the list,
    # so a Claude-only report reads exactly as it always has.
    with_agent = _several_agents(reports)
    if with_agent:
        lines.append(
            "  %-10s %-6s %-12s %6s %9s %9s"
            % ("DATE", "AGENT", "PROJECT", "TURNS", "TOKENS", "COST")
        )
    else:
        lines.append(
            "  %-10s %-14s %8s %9s %8s" % ("DATE", "PROJECT", "TURNS", "TOKENS", "COST")
        )
    for report in ranked:
        share = (report["cost"] / total * 100) if total else 0
        cells = (
            (report["date"] or "—")[:10],
            (report["project"] or "—")[: 12 if with_agent else 14],
            report["turns"],
            fmt_tokens(report["usage"].total_tokens),
            _cost_cell(report["cost"], _priced(report)),
            "  %2.0f%%" % share if share >= 1 else "",
        )
        if with_agent:
            lines.append(
                "  %-10s %-6s %-12s %6d %9s %9s%s"
                % (cells[0], report.get("agent", "claude")[:6], *cells[1:])
            )
        else:
            lines.append("  %-10s %-14s %8d %9s %8s%s" % cells)
    lines.append("")
    if ranked and total:
        top_share = sum(r["cost"] for r in ranked) / total * 100
        lines.append(
            "  These %d session(s) are %.0f%% of %s across %d session(s)."
            % (len(ranked), top_share, fmt_money(total), len(reports))
        )
    lines.extend(_footnotes(reports, width))
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
    by_day: Dict[str, Dict[str, Any]] = {}
    spanning = 0
    with_agent = _several_agents(reports)
    for report in reports:
        # Attribute a session to the day it *finished*, not the day it began.
        # Long sessions get resumed across weeks, and bucketing by start date
        # dumps months of spend onto a single old bar. Neither choice is truly
        # correct — a multi-day session's cost did not happen on one day — so
        # the count of spanning sessions is reported rather than hidden.
        day = report.get("end_date") or report["date"] or "(undated)"
        if (
            report.get("end_date")
            and report.get("date")
            and report["end_date"] != report["date"]
        ):
            spanning += 1
        entry = by_day.setdefault(
            day, {"cost": 0.0, "sessions": 0, "tokens": 0, "agents": {}}
        )
        entry["cost"] += report["cost"]
        entry["sessions"] += 1
        entry["tokens"] += report["usage"].total_tokens
        agent = report.get("agent", "claude")
        entry["agents"][agent] = entry["agents"].get(agent, 0) + 1

    days = sorted(by_day)
    if not days:
        return "No sessions to chart."

    peak = max(by_day[d]["cost"] for d in days) or 1.0
    lines = [rule, "  DAILY BURN", rule, ""]
    for day in days:
        entry = by_day[day]
        if with_agent:
            sessions = " · ".join(
                "%d %s" % (count, agent)
                for agent, count in sorted(entry["agents"].items())
            )
        else:
            sessions = "%d session(s)" % entry["sessions"]
        lines.append(
            "  %-10s %9s %-22s %s"
            % (
                day,
                fmt_money(entry["cost"]),
                bar(entry["cost"] / peak, 20),
                sessions,
            )
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
    lines.extend(_footnotes(reports, width))
    lines.append("")
    lines.append("  Prices as of %s. Estimate, not an invoice." % PRICES_AS_OF)
    lines.append("")
    return "\n".join(lines)


def bar(fraction: float, width: int = 20) -> str:
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def render_summary(
    reports: Sequence[Mapping[str, Any]],
    group_by: str = "day",
    width: int = 64,
) -> str:
    """Roll several sessions up by day, project, model, or agent."""
    rule = "─" * width
    lines: List[str] = []

    buckets: Dict[str, Dict[str, Any]] = {}
    for report in reports:
        if group_by == "project":
            keys = [report["project"] or "(unknown)"]
        elif group_by == "model":
            keys = [model_label(e["model"], e["fast"]) for e in report["by_model"]]
        elif group_by == "agent":
            keys = [_agent_label(report)]
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
                    "priced": False,
                },
            )
            if group_by == "model":
                entry = next(
                    e
                    for e in report["by_model"]
                    if model_label(e["model"], e["fast"]) == key
                )
                bucket["cost"] += entry["cost"] or 0.0
                bucket["uncached"] += entry["uncached_cost"] or 0.0
                bucket["tokens"] += entry["usage"].total_tokens
                bucket["priced"] = bucket["priced"] or entry["cost"] is not None
            else:
                bucket["priced"] = bucket["priced"] or _priced(report)
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
                _cost_cell(bucket["cost"], bucket["priced"]),
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

    lines.extend(_footnotes(reports, width))
    lines.append("")
    lines.append("  Prices as of %s. Estimate, not an invoice." % PRICES_AS_OF)
    lines.append("")
    return "\n".join(lines)


def _agent_label(report: Mapping[str, Any]) -> str:
    agent = report.get("agent", "claude")
    return AGENT_LABELS.get(agent, agent)


def _several_agents(reports: Sequence[Mapping[str, Any]]) -> bool:
    return any(report.get("agent", "claude") != "claude" for report in reports)


def _priced(report: Mapping[str, Any]) -> bool:
    """Whether any of a report's usage had a price. Reports built before the
    field existed were priced or listed their gaps, so default to True."""
    return report.get("priced", True) or not report.get("unpriced_models")


def _cost_cell(cost: float, priced: bool) -> str:
    """A cost, or "unpriced" where nothing could be priced.

    $0.00 beside a few billion tokens reads as free. It is not; it is unknown.
    """
    return fmt_money(cost) if priced else "unpriced"


def _footnotes(reports: Sequence[Mapping[str, Any]], width: int) -> List[str]:
    """What the totals above leave out, and the plan usage, if any.

    Per-session receipts name their unpriced models; a roll-up has to as well,
    or a gap of several billion tokens disappears into a total that looks
    complete.
    """
    lines: List[str] = []
    unpriced: Dict[str, int] = {}
    for report in reports:
        for entry in report["by_model"]:
            if entry["cost"] is None:
                label = model_label(entry["model"], entry["fast"])
                unpriced[label] = unpriced.get(label, 0) + entry["usage"].total_tokens
    if unpriced:
        names = ", ".join(
            "%s (%s tokens)" % (name, fmt_tokens(tokens))
            for name, tokens in sorted(unpriced.items(), key=lambda item: -item[1])
        )
        lines.append("")
        lines.extend(
            _wrap(
                "! Not in these totals — no price on file for %s. Supply prices "
                "with --prices to include them." % names,
                width,
                hang="    ",
            )
        )
    snapshots = [r["plan_usage"] for r in reports if r.get("plan_usage")]
    if snapshots:
        lines.append("")
        latest = max(snapshots, key=lambda snapshot: snapshot["as_of"])
        lines.extend(_wrap(fmt_plan_usage(latest), width))
    return lines


def _wrap(text: str, width: int, hang: str = "  ") -> List[str]:
    return textwrap.wrap(
        text,
        width=width - 2,
        initial_indent="  ",
        subsequent_indent=hang,
        break_on_hyphens=False,
    )


def _window(minutes: Optional[int]) -> str:
    if minutes == 10080:
        return "weekly"
    if minutes == 1440:
        return "daily"
    if minutes and minutes % 60 == 0:
        return "%d-hour" % (minutes // 60)
    if minutes:
        return "%d-minute" % minutes
    return "current"


def _utc(timestamp: str) -> str:
    """``2026-10-06T21:07:32.376Z`` as ``2026-10-06 21:07 UTC``."""
    if len(timestamp) >= 16 and timestamp[10] == "T":
        return "%s %s UTC" % (timestamp[:10], timestamp[11:16])
    return timestamp


def _span(start: Optional[str], end: Optional[str]) -> Optional[str]:
    """The days a session ran, as one date or ``start → end``.

    A session resumed across weeks printed only the day it began, so a receipt
    for work done this morning was headed with a date a month ago.
    """
    if start and end and end != start:
        return "%s → %s" % (start, end)
    return start or end


def _shorten(text: str, width: int) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    return "..." + text[-(width - 3) :]
