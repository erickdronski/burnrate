"""Command line interface.

burnrate                      # receipt for the most recent session
burnrate --last 5             # the last five sessions
burnrate --today              # everything from today
burnrate --summary day        # roll up by day, project, model, or agent
burnrate --project nalee      # filter to one project
burnrate --agent codex        # one agent's sessions (default: every agent found)
burnrate guard --cap 5.00     # hook: stop a session at a spend cap
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, codex
from .guard import DEFAULT_WARN_AT, run_guard
from .pricing import PricingError, load_price_overrides
from .receipt import (
    price_session,
    render_session,
    render_summary,
    render_top,
    render_trend,
)
from .sessions import (
    DEFAULT_ROOTS,
    Candidate,
    SessionError,
    candidates,
    collect,
    default_root,
    parse_file,
)

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
    selection.add_argument("--today", action="store_true", help="sessions active today")
    selection.add_argument(
        "--since", metavar="YYYY-MM-DD", help="sessions active on or after this date"
    )
    selection.add_argument("--project", help="filter by project name (substring)")
    selection.add_argument(
        "--session", metavar="PATH", help="report on one specific transcript file"
    )
    selection.add_argument(
        "--agent",
        choices=("claude", "codex", "all"),
        help="which agent's sessions to read (default: every agent whose logs "
        "are present; with --root or --codex-root, only those)",
    )
    selection.add_argument(
        "--root", help="Claude Code transcript directory (default: ~/.claude/projects)"
    )
    selection.add_argument(
        "--codex-root",
        metavar="DIR",
        help="Codex rollout directory (default: ~/.codex/sessions and "
        "~/.codex/archived_sessions)",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--summary",
        choices=("day", "project", "model", "agent"),
        help="roll up instead of printing individual receipts",
    )
    output.add_argument(
        "--top",
        nargs="?",
        type=int,
        const=10,
        metavar="N",
        help="rank the most expensive sessions (default: top 10)",
    )
    output.add_argument(
        "--trend",
        action="store_true",
        help="daily spend with a chart and whether it is rising",
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

    parser.add_argument(
        "--version", action="version", version="burnrate %s" % __version__
    )
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
        if args.agent == "codex":
            # The guard is a Claude Code hook. Failing open matches every other
            # condition it cannot evaluate: a misconfigured cap must not stop
            # anyone's agent.
            sys.stderr.write(
                "burnrate: guard supports Claude Code hooks only; not enforcing\n"
            )
            return 0
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
            "No sessions found%s. Point --root (Claude Code) or --codex-root "
            "(Codex) at your logs if they live somewhere unusual.\n"
            % (" matching that filter" if (args.project or since) else "")
        )
        return 1

    reports = [price_session(session, prices) for session in sessions]

    if args.format == "json":
        sys.stdout.write(json.dumps(_jsonable(reports), indent=2) + "\n")
        return 0

    if args.top:
        sys.stdout.write(render_top(reports, limit=args.top) + "\n")
        return 0

    if args.trend:
        sys.stdout.write(render_trend(reports) + "\n")
        return 0

    if args.summary:
        sys.stdout.write(render_summary(reports, args.summary) + "\n")
        return 0

    for report in reports:
        sys.stdout.write(render_session(report, verbose=args.verbose) + "\n")
    return 0


def _select_sessions(args, since: Optional[str]):
    if args.session:
        path = os.path.expanduser(args.session)
        if codex.is_rollout(path):
            session = codex.parse_session([path])
        else:
            session = parse_file(path)
        if session is None:
            raise SessionError("no billable usage found in %s" % args.session)
        return [session]

    limit = (
        None
        if (args.all or args.summary or args.top or args.trend or since)
        else args.last
    )
    found: List[Candidate] = []
    for agent, roots in _sources(args):
        if agent == "codex":
            found.extend(codex.candidates(roots, args.project))
        else:
            for root in roots:
                found.extend(candidates(root, args.project))
    sessions = collect(found, since=since, limit=limit)
    if (
        not args.all
        and not args.summary
        and not args.top
        and not args.trend
        and not since
    ):
        return sessions[: args.last]
    return sessions


def _sources(args) -> List[Any]:
    """Which agents to read, and the directories to read them from.

    With no ``--agent``, every agent whose logs exist is read — unless a root
    was named, in which case only the named roots are. Pointing at a
    directory means "read this", and quietly adding the real home directory
    to a report about a test fixture would be a surprise.
    """
    named = {"claude": args.root, "codex": args.codex_root}
    # `required`: the user asked for these agents specifically, so a missing
    # directory is an error rather than an agent that simply isn't installed.
    if args.agent in ("claude", "codex"):
        wanted, required = [args.agent], True
    elif args.agent is None and (args.root or args.codex_root):
        wanted, required = [agent for agent, root in named.items() if root], True
    else:
        wanted, required = ["claude", "codex"], False

    sources: List[Any] = []
    for agent in wanted:
        if named[agent]:
            root = os.path.expanduser(named[agent])
            if not os.path.isdir(root):
                raise SessionError("not a directory: %s" % root)
            sources.append((agent, [root]))
        elif agent == "codex":
            roots = codex.default_roots()
            if roots:
                sources.append((agent, roots))
            elif required:
                raise SessionError(
                    "no Codex session directory found. Looked for: %s. Pass "
                    "--codex-root to point at rollouts elsewhere."
                    % ", ".join(codex.DEFAULT_ROOTS)
                )
        else:
            root = default_root()
            if root:
                sources.append((agent, [root]))
            elif required:
                raise SessionError(
                    "no Claude Code session directory found. Looked for: %s. "
                    "Pass --root to point at transcripts elsewhere."
                    % ", ".join(DEFAULT_ROOTS)
                )
    if not sources:
        raise SessionError(
            "no session directory found. Looked for: %s. Pass --root (Claude "
            "Code) or --codex-root (Codex) to point at logs elsewhere."
            % ", ".join(DEFAULT_ROOTS + codex.DEFAULT_ROOTS)
        )
    return sources


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
