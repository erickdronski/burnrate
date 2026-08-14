"""The spend cap: a hook that stops a session before it runs away.

A receipt tells you what a session cost after it cost it. That is useful and
insufficient — the failure people actually want prevented is the loop that runs
for two hours while nobody is watching.

``burnrate guard`` is a Claude Code hook. It reads the current session's
transcript, prices it, and exits non-zero once the session crosses a cap you
set. The harness surfaces the message and stops.

Install it into ``~/.claude/settings.json``::

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "*",
            "hooks": [
              {"type": "command",
               "command": "burnrate guard --cap 5.00"}
            ]
          }
        ]
      }
    }

Design constraints worth stating, because a badly-built guard is worse than
none:

* **It fails open.** If the transcript cannot be found or parsed, the guard
  exits 0 and says so. A cost tool that bricks someone's agent because it could
  not read a log file deserves to be uninstalled, and would be.
* **It is cheap.** It reads one file, not the whole history directory, and it
  does so on every tool call. Parsing is capped so a very long session does not
  add latency to every step.
* **It warns before it blocks.** Crossing a warning threshold prints to stderr
  and exits 0. Only the hard cap stops the run.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Mapping, Optional

from .pricing import price_usage
from .receipt import fmt_money
from .sessions import Session, discover, parse_file

__all__ = ["evaluate", "run_guard", "GuardResult"]

#: Fraction of the cap at which to warn rather than block.
DEFAULT_WARN_AT = 0.75


class GuardResult:
    __slots__ = ("state", "cost", "cap", "message", "session_id")

    def __init__(
        self,
        state: str,
        cost: Optional[float],
        cap: float,
        message: str,
        session_id: Optional[str] = None,
    ) -> None:
        #: One of "ok", "warn", "block", "unknown".
        self.state = state
        self.cost = cost
        self.cap = cap
        self.message = message
        self.session_id = session_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "cost": self.cost,
            "cap": self.cap,
            "message": self.message,
            "session_id": self.session_id,
        }

    @property
    def exit_code(self) -> int:
        # Only a hard block is non-zero. Everything else — including a failure
        # to determine the cost — lets the agent continue.
        return 2 if self.state == "block" else 0


def session_cost(
    session: Session, prices: Optional[Mapping[str, Any]] = None
) -> Optional[float]:
    """Total priced cost of a session, or ``None`` if nothing could be priced."""
    total = 0.0
    priced_any = False
    fast_models = {t.model for t in session.turns if t.fast and t.model}
    for model, usage in session.usage_by_model().items():
        cost = price_usage(usage, model, prices, fast_mode=model in fast_models)
        if cost is not None:
            total += cost
            priced_any = True
    return total if priced_any else None


def evaluate(
    cap: float,
    transcript_path: Optional[str] = None,
    prices: Optional[Mapping[str, Any]] = None,
    warn_at: float = DEFAULT_WARN_AT,
) -> GuardResult:
    """Decide whether the current session may continue."""
    if cap <= 0:
        return GuardResult(
            "unknown", None, cap, "cap must be greater than zero; not enforcing"
        )

    path = transcript_path or _transcript_from_environment()
    if not path:
        return GuardResult(
            "unknown",
            None,
            cap,
            "burnrate: could not identify the current transcript; not enforcing",
        )

    if not os.path.isfile(path):
        return GuardResult(
            "unknown",
            None,
            cap,
            "burnrate: transcript not found at %s; not enforcing" % path,
        )

    try:
        session = parse_file(path)
    except Exception as exc:  # noqa: BLE001 - fail open, always
        return GuardResult(
            "unknown", None, cap, "burnrate: could not read transcript (%s); "
            "not enforcing" % type(exc).__name__
        )

    if session is None:
        return GuardResult(
            "ok", 0.0, cap, "burnrate: no billable usage yet"
        )

    cost = session_cost(session, prices)
    if cost is None:
        return GuardResult(
            "unknown",
            None,
            cap,
            "burnrate: no price on file for the models in this session; "
            "not enforcing",
            session.session_id,
        )

    if cost >= cap:
        return GuardResult(
            "block",
            cost,
            cap,
            (
                "burnrate: this session has reached %s against a %s cap.\n"
                "Stopping here. Raise the cap with --cap, or start a fresh "
                "session — context resets are usually cheaper than continuing "
                "a long one anyway."
                % (fmt_money(cost), fmt_money(cap))
            ),
            session.session_id,
        )

    if warn_at and cost >= cap * warn_at:
        return GuardResult(
            "warn",
            cost,
            cap,
            "burnrate: %s of %s cap used (%.0f%%)."
            % (fmt_money(cost), fmt_money(cap), 100 * cost / cap),
            session.session_id,
        )

    return GuardResult(
        "ok",
        cost,
        cap,
        "burnrate: %s of %s cap used" % (fmt_money(cost), fmt_money(cap)),
        session.session_id,
    )


def _transcript_from_environment() -> Optional[str]:
    """Find the transcript for the session invoking this hook.

    Claude Code passes hook context on stdin as JSON, which is the reliable
    path. Environment variables are checked as a fallback, and a most-recent
    lookup is the last resort — it is right in the overwhelmingly common case
    of one active session, and the guard fails open when it isn't.
    """
    payload = _read_stdin_json()
    if payload:
        for key in ("transcript_path", "transcriptPath"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return os.path.expanduser(value)

    for variable in ("CLAUDE_TRANSCRIPT_PATH", "BURNRATE_TRANSCRIPT"):
        value = os.environ.get(variable)
        if value:
            return os.path.expanduser(value)

    try:
        sessions = discover(limit=1)
    except Exception:  # noqa: BLE001 - fail open
        return None
    return sessions[0].path if sessions else None


def _read_stdin_json() -> Optional[Dict[str, Any]]:
    """Read hook context from stdin without blocking on an interactive TTY."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return None
    if not raw or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def run_guard(
    cap: float,
    transcript_path: Optional[str] = None,
    prices: Optional[Mapping[str, Any]] = None,
    warn_at: float = DEFAULT_WARN_AT,
    quiet: bool = False,
    as_json: bool = False,
) -> int:
    result = evaluate(cap, transcript_path, prices, warn_at)

    if as_json:
        sys.stdout.write(json.dumps(result.to_dict()) + "\n")
        return result.exit_code

    if result.state == "block":
        sys.stderr.write(result.message + "\n")
    elif result.state == "warn":
        sys.stderr.write(result.message + "\n")
    elif not quiet:
        sys.stderr.write(result.message + "\n")

    return result.exit_code
