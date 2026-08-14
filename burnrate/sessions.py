"""Reading agent session transcripts off disk.

Claude Code writes one JSONL file per session under
``~/.claude/projects/<slugged-cwd>/<session-id>.jsonl``. Each line is an event;
the ones that cost money are ``type: "assistant"`` records carrying
``message.usage``.

**The thing that makes this hard, and that a naive implementation gets wrong.**
The same assistant message is written to the log many times as it streams. On a
real machine, roughly 70% of assistant records share a ``message.id`` with
another record — so summing ``usage`` across all of them overcounts by around
3x. Every figure this tool reports would be wrong, in the expensive direction,
without deduplication.

The rule used here: **one usage record per ``message.id``, keeping the one with
the highest ``output_tokens``.** Streaming writes a growing count, so the
largest is the complete one. Where ids repeat with identical usage — the common
case — the rule is a no-op; where they differ, it picks the final state.

Everything here is read-only. Nothing is uploaded, and no network call is made.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

from .pricing import NON_BILLABLE_MODELS, Usage
from .redact import redact

__all__ = [
    "Session",
    "SessionError",
    "Turn",
    "default_root",
    "discover",
    "parse_file",
]


class SessionError(RuntimeError):
    """Raised when session logs cannot be read."""


#: Where Claude Code keeps transcripts. Overridable for other harnesses.
DEFAULT_ROOTS = ("~/.claude/projects",)


def default_root() -> Optional[str]:
    for candidate in DEFAULT_ROOTS:
        path = os.path.expanduser(candidate)
        if os.path.isdir(path):
            return path
    return None


class Turn:
    """One billable model response."""

    __slots__ = ("fast", "message_id", "model", "timestamp", "tools", "usage")

    def __init__(
        self,
        message_id: Optional[str],
        model: Optional[str],
        usage: Usage,
        timestamp: Optional[str] = None,
        tools: Optional[Sequence[str]] = None,
        fast: bool = False,
    ) -> None:
        self.message_id = message_id
        self.model = model
        self.usage = usage
        self.timestamp = timestamp
        self.tools = list(tools or [])
        self.fast = fast


class Session:
    """One transcript: its turns, the tools it called, and what it touched."""

    def __init__(self, path: str, session_id: str, project: str) -> None:
        self.path = path
        self.session_id = session_id
        self.project = project
        self.turns: List[Turn] = []
        self.tool_counts: Dict[str, int] = {}
        self.commands: List[str] = []
        self.files_touched: Dict[str, int] = {}
        self.first_timestamp: Optional[str] = None
        self.last_timestamp: Optional[str] = None
        self.git_branch: Optional[str] = None
        self.cwd: Optional[str] = None
        self.errors: int = 0
        self.duplicate_records: int = 0

    # -- aggregates ------------------------------------------------------

    def usage_by_model(self) -> Dict[str, Usage]:
        out: Dict[str, Usage] = {}
        for turn in self.turns:
            key = turn.model or "unknown"
            out.setdefault(key, Usage()).add(turn.usage)
        return out

    def total_usage(self) -> Usage:
        total = Usage()
        for turn in self.turns:
            total.add(turn.usage)
        return total

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def tool_calls(self) -> int:
        return sum(self.tool_counts.values())

    def top_tools(self, limit: int = 8) -> List[Tuple[str, int]]:
        return sorted(self.tool_counts.items(), key=lambda item: -item[1])[:limit]

    def top_files(self, limit: int = 8) -> List[Tuple[str, int]]:
        return sorted(self.files_touched.items(), key=lambda item: -item[1])[:limit]

    @property
    def date(self) -> Optional[str]:
        """The day this session started."""
        if not self.first_timestamp:
            return None
        return self.first_timestamp[:10]

    @property
    def end_date(self) -> Optional[str]:
        """The day this session last ran.

        Reporting by end date is what makes a spend chart reflect recent
        activity: a session begun in June and resumed through August did most
        of its spending in August, and bucketing it by start date hides that
        entirely.
        """
        if not self.last_timestamp:
            return None
        return self.last_timestamp[:10]


#: Tool names whose input carries a file path worth recording.
_FILE_TOOLS = frozenset({"Read", "Write", "Edit", "NotebookEdit", "MultiEdit"})

#: Record types this parser reads. Transcripts also carry queue operations,
#: attachments, titles, and mode changes, none of which affect a receipt.
#:
#: Deserializing every line dominated the runtime of a whole-history report —
#: 205k `json.loads` calls, most of them on records that are discarded a
#: microsecond later. A substring test on the raw line is roughly two orders of
#: magnitude cheaper than parsing, and the transcripts are written as compact
#: JSON, so the type token appears verbatim.
#:
#: The check is deliberately conservative: anything that does not clearly
#: announce an uninteresting type still gets parsed, so a format change
#: degrades speed rather than correctness.
_WANTED_TYPES = ('"type":"assistant"', '"type":"user"', '"type":"system"')


def parse_file(path: str) -> Optional[Session]:
    """Parse one JSONL transcript. Returns ``None`` if it holds no usage.

    Malformed lines are skipped rather than fatal — transcripts are appended to
    live, so the last line of an in-progress session is routinely a partial
    write, and refusing to read the file because of it would make the tool
    useless exactly when someone wants it.
    """
    session_id = os.path.splitext(os.path.basename(path))[0]
    project = _unslug(os.path.basename(os.path.dirname(path)))
    session = Session(path=path, session_id=session_id, project=project)

    # message.id -> the best usage record seen for it. See module docstring.
    best: Dict[str, Turn] = {}
    unkeyed: List[Turn] = []

    try:
        # Opened outside a `with` so an unreadable transcript is skipped rather
        # than aborting the whole report; the handle is closed by the `with`
        # immediately below.
        handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return None

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            # Cheap pre-filter: skip records this parser has no use for without
            # paying to deserialize them. See _WANTED_TYPES.
            if '"type":"' in line and not any(t in line for t in _WANTED_TYPES):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            _absorb_metadata(session, record)

            record_type = record.get("type")
            if record_type == "system" and record.get("level") == "error":
                session.errors += 1
                continue
            if record_type == "user":
                _absorb_tool_result(session, record)
                continue
            if record_type != "assistant":
                continue

            message = record.get("message")
            if not isinstance(message, dict):
                continue

            _absorb_tool_uses(session, message)

            usage = _extract_usage(message.get("usage"))
            if usage is None:
                continue

            model = message.get("model")
            if isinstance(model, str) and model in NON_BILLABLE_MODELS:
                continue

            turn = Turn(
                message_id=message.get("id"),
                model=model,
                usage=usage,
                timestamp=record.get("timestamp"),
                fast=_is_fast(message),
            )

            if not turn.message_id:
                unkeyed.append(turn)
                continue

            existing = best.get(turn.message_id)
            if existing is None:
                best[turn.message_id] = turn
            else:
                session.duplicate_records += 1
                # Streaming writes a growing output count; the largest record
                # is the complete one.
                if turn.usage.output_tokens > existing.usage.output_tokens:
                    best[turn.message_id] = turn

    session.turns = list(best.values()) + unkeyed
    if not session.turns and not session.tool_counts:
        return None
    return session


def _absorb_metadata(session: Session, record: dict) -> None:
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str) and timestamp:
        if session.first_timestamp is None or timestamp < session.first_timestamp:
            session.first_timestamp = timestamp
        if session.last_timestamp is None or timestamp > session.last_timestamp:
            session.last_timestamp = timestamp
    if session.git_branch is None and record.get("gitBranch"):
        session.git_branch = str(record["gitBranch"])
    if session.cwd is None and record.get("cwd"):
        session.cwd = str(record["cwd"])


def _absorb_tool_uses(session: Session, message: dict) -> None:
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "unknown")
        session.tool_counts[name] = session.tool_counts.get(name, 0) + 1

        payload = block.get("input")
        if not isinstance(payload, dict):
            continue

        if name == "Bash":
            command = payload.get("command")
            if isinstance(command, str) and command.strip():
                # Redact at capture, not at render. A secret that never enters
                # the object graph cannot be leaked by an output path someone
                # adds later.
                session.commands.append(redact(command.strip()))
        elif name in _FILE_TOOLS:
            target = payload.get("file_path") or payload.get("notebook_path")
            if isinstance(target, str) and target:
                session.files_touched[target] = session.files_touched.get(target, 0) + 1


def _absorb_tool_result(session: Session, record: dict) -> None:
    result = record.get("toolUseResult")
    if isinstance(result, dict) and result.get("is_error"):
        session.errors += 1


def _is_fast(message: dict) -> bool:
    usage = message.get("usage")
    if isinstance(usage, dict) and usage.get("speed") == "fast":
        return True
    return message.get("speed") == "fast"


def _extract_usage(raw: object) -> Optional[Usage]:
    """Pull the five billable counters out of a usage object.

    The two cache-write TTLs live in a nested ``cache_creation`` object. When
    that object is absent, the flat ``cache_creation_input_tokens`` total is
    attributed to the 5-minute bucket — the cheaper of the two, so an unknown
    split understates rather than inflates the bill.
    """
    if not isinstance(raw, dict):
        return None

    def count(key: str, source: Optional[dict] = None) -> int:
        target = source if source is not None else raw
        value = target.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    creation = raw.get("cache_creation")
    if isinstance(creation, dict):
        write_5m = count("ephemeral_5m_input_tokens", creation)
        write_1h = count("ephemeral_1h_input_tokens", creation)
    else:
        write_5m = count("cache_creation_input_tokens")
        write_1h = 0

    usage = Usage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_read_tokens=count("cache_read_input_tokens"),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )
    if usage.total_tokens == 0:
        return None
    return usage


def discover(
    root: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Session]:
    """Find and parse session transcripts, newest first.

    ``since`` is an ISO date (``2026-08-01``); sessions whose last activity
    predates it are skipped. ``project`` matches the project directory name as
    a case-insensitive substring.
    """
    base = root or default_root()
    if base is None:
        raise SessionError(
            "no session directory found. Looked for: %s. Pass --root to point "
            "at transcripts elsewhere." % ", ".join(DEFAULT_ROOTS)
        )
    base = os.path.expanduser(base)
    if not os.path.isdir(base):
        raise SessionError("not a directory: %s" % base)

    paths: List[Tuple[float, str]] = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for filename in filenames:
            if not filename.endswith(".jsonl"):
                continue
            full = os.path.join(dirpath, filename)
            if project:
                folder = os.path.basename(os.path.dirname(full))
                if project.lower() not in _unslug(folder).lower():
                    continue
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            paths.append((mtime, full))

    paths.sort(reverse=True)

    sessions: List[Session] = []
    for _mtime, full in paths:
        session = parse_file(full)
        if session is None:
            continue
        if since and session.last_timestamp and session.last_timestamp[:10] < since:
            continue
        sessions.append(session)
        if limit and len(sessions) >= limit:
            break
    return sessions


def _unslug(folder: str) -> str:
    """Turn ``-Users-dron-Projects-nalee`` back into something readable."""
    if not folder:
        return "(unknown)"
    text = folder.lstrip("-")
    parts = [part for part in text.split("-") if part]
    if not parts:
        return folder
    return parts[-1]
