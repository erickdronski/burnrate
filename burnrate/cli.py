"""Command line interface.

    burnrate                      # receipt for the most recent session
    burnrate --last 5             # the last five sessions
    burnrate --today              # everything from today
    burnrate --summary day        # roll up by day, project, or model
    burnrate --project nalee      # filter to one project
    burnrate guard --cap 5.00     # hook: stop a session at a spend cap
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .guard import DEFAULT_WARN_AT, run_guard
from .pricing import PricingError, load_price_overrides
from .receipt import price_session, render_session, render_summary
from .sessions import SessionError, discover, parse_file

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="burnrate",
        description=(
            "What your coding agent actually cost, and a cap to stop it "
            "before it costs more. Reads local session logs; no API key, no "
            "network."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="report",
        choices=("report", "guard"),
        help="report (default) prints receipts; guard enforces a spend cap",
    )

    selection = parser.add_argument_group("selecting sessions")
    selection.add_argument(
        "--last",
        type=int,
        default=1,
        metavar="N",
        help="how many recent sessions to report (default: 1)",
    )
    selection.add_argument("--all", action="store_true", help="every session found")
    selection.add_argument(
        "--today", action="store_true", help="sessions active today"
    )
    selection.add_argument(
        "--since", metavar="YYYY-MM-DD", help="sessions active on or after this date"
    )
    selection.add_argument("--project", help="filter by project name (substring)")
    selection.add_argument(
        "--session", metavar="PATH", help="report on one specific transcript file"
    )
    selection.add_argument(
        "--root", help="transcript directory (default: ~/.claude/projects)"
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--summary",
        choices=("day", "project", "model"),
        help="roll up instead of printing individual receipts",
    )
    output.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="include files touched and commands run",
    )
    output.add_argument(
        "--format", choices=("text", "json"), default="text", help="output format"
    )
    output.add_argument(
        "--prices",
        metavar="FILE",
        help="JSON file overriding or adding model prices",
    )

    guard = parser.add_argument_group("guard")
    guard.add_argument(
        "--cap",
        type=float,
        metavar="USD",
        help="spend cap for the current session; exits 2 when reached",
    )
    guard.add_argument(
        "--warn-at",
        type=float,
        default=DEFAULT_WARN_AT,
        metavar="FRACTION",
        help="warn at this fraction of the cap (default: 0.75; 0 disables)",
    )
    guard.add_argument(
        "--transcript", help="transcript to check (default: the calling session)"
    )
    guard.add_argument(
        "--quiet", action="store_true", help="only print on warn or block"
    )

    parser.add_argument("--version", action="version", version="burnrate %s" % __version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    prices = None
    if args.prices:
        try:
            prices = load_price_overrides(args.prices)
        except PricingError as exc:
            sys.stderr.write("price error: %s\n" % exc)
            return 2

    if args.command == "guard":
        if args.cap is None:
            sys.stderr.write("guard requires --cap, e.g. --cap 5.00\n")
            return 2
        return run_guard(
            cap=args.cap,
            transcript_path=args.transcript,
            prices=prices,
            warn_at=args.warn_at,
            quiet=args.quiet,
            as_json=args.format == "json",
        )

    since = args.since
    if args.today:
        since = datetime.date.today().isoformat()

    try:
        sessions = _select_sessions(args, since)
    except SessionError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    if not sessions:
        sys.stderr.write(
            "No sessions found%s. Point --root at your transcript directory if "
            "it lives somewhere unusual.\n"
            % (" matching that filter" if (args.project or since) else "")
        )
        return 1

    reports = [price_session(session, prices) for session in sessions]

    if args.format == "json":
        sys.stdout.write(json.dumps(_jsonable(reports), indent=2) + "\n")
        return 0

    if args.summary:
        sys.stdout.write(render_summary(reports, args.summary) + "\n")
        return 0

    for report in reports:
        sys.stdout.write(render_session(report, verbose=args.verbose) + "\n")
    return 0


def _select_sessions(args, since: Optional[str]):
    if args.session:
        session = parse_file(os.path.expanduser(args.session))
        if session is None:
            raise SessionError(
                "no billable usage found in %s" % args.session
            )
        return [session]

    limit = None if (args.all or args.summary or since) else args.last
    sessions = discover(
        root=args.root, project=args.project, since=since, limit=limit
    )
    if not args.all and not args.summary and not since:
        return sessions[: args.last]
    return sessions


def _jsonable(reports: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for report in reports:
        entry = dict(report)
        entry["usage"] = report["usage"].to_dict()
        entry["by_model"] = [
            {
                "model": item["model"],
                "usage": item["usage"].to_dict(),
                "cost": item["cost"],
                "uncached_cost": item["uncached_cost"],
                "fast": item["fast"],
            }
            for item in report["by_model"]
        ]
        entry["top_tools"] = [
            {"tool": name, "calls": count} for name, count in report["top_tools"]
        ]
        entry["top_files"] = [
            {"path": path, "touches": count} for path, count in report["top_files"]
        ]
        entry["commands"] = report["commands"][:50]
        out.append(entry)
    return out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
