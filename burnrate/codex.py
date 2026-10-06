"""Reading OpenAI Codex CLI session logs.

Codex writes one JSONL *rollout* per thread under
``~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread-id>.jsonl`` and moves
archived ones to ``~/.codex/archived_sessions``. Each line is
``{timestamp, ordinal?, type, payload}``. A conversation is one *session*; each
subagent the model spawns runs in a thread of its own, written to its own file
and tagged with the conversation's ``session_id``. burnrate reports the session
— the root thread and its subagents together — because that is the unit
somebody started and is paying for. On the machine this was written against,
373 files held 48 sessions.

**Where the double counts hide.** Codex records usage in two places, and
neither obvious sum is right:

* ``event_msg``/``token_count`` carries ``total_token_usage``, cumulative for
  the thread, and ``last_token_usage`` for the latest response. It is
  re-emitted without a new response — 5,815 of 65,968 events repeated the
  previous total — and after a context compaction it reports the size of the
  compacted context as ``last`` with no model call behind it. Summing ``last``
  over every event overcounts. Reading the final total undercounts: the
  compaction calls never reach it, and a session resumed after a restart
  starts it again from zero.
* ``token_usage_record`` (CLI 0.15x) is one record per model response, keyed
  by ``response_id``, compaction calls included. Every ``token_count`` that
  advanced the total — 60,151 of them — was immediately preceded by the record
  for the same response with identical numbers, so counting both doubles the
  bill.

The rule: **one usage per ``response_id`` from ``token_usage_record``.** A
``token_count`` is counted only when no record arrived since the previous one —
which is every event on a CLI old enough not to write records — and only when
its cumulative total moved.

Forked subagent threads open with a copy of the parent's history (everything
below ``subagent_history_start_ordinal``). On CLI 0.153–0.160 that copy holds
messages only, and no usage record repeats across files. That is enforced
rather than assumed: a record carrying another thread's id is dropped, response
ids are deduplicated across a session's files, and in the record-less fallback
a fork's ``token_count`` events below the history start are not counted.

**What the counters mean**, measured on 65,966 events rather than assumed:
``cached_input_tokens`` never exceeds ``input_tokens``, and
``reasoning_output_tokens`` never exceeds ``output_tokens``, with
``total_tokens == input_tokens + output_tokens`` on every event that stands for
a model call. Cached input is part of input, and reasoning is part of output.
So uncached input is ``input - cached`` and reasoning is not added a second
time. ``cache_write_input_tokens`` was zero in every record; it is taken to be
part of ``input_tokens`` too and stays in uncached input, which understates
rather than inflates if it turns out to be billed on top.

**Prices.** No OpenAI rate in this package has been verified, so Codex models
are reported by name and token count as unpriced. ``--prices`` adds them.

**Plan usage.** For a subscription user the dollar figure is notional; the
number that matters is how much of the plan's window is gone. ``token_count``
events carry ``rate_limits``, and the latest snapshot is reported as it stands.
"""

from __future__ import annotations

import functools
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from .pricing import Usage
from .redact import redact
from .sessions import Candidate, Session, SessionError, Turn, collect

__all__ = [
    "DEFAULT_ROOTS",
    "candidates",
    "default_roots",
    "discover",
    "is_rollout",
    "parse_rollout",
    "parse_session",
]

#: Where Codex keeps rollouts. Archived sessions are still spend, so both are
#: read; a session split between them is reassembled by its ``session_id``.
DEFAULT_ROOTS = ("~/.codex/sessions", "~/.codex/archived_sessions")

#: The start of every compact rollout line:
#: ``{"timestamp":"…","ordinal":12,"type":"event_msg","payload":{"type":"…"``.
#:
#: A rollout is mostly bulk the receipt never reads — tool output, encrypted
#: reasoning, search results, compaction snapshots — and lines run to megabytes.
#: Matching this anchored prefix costs the same for a 40-byte line as for a
#: 40-megabyte one, and tells the parser what the line is without decoding it.
#: It is conservative in the same way as the Claude prefilter: a line that does
#: not match exactly — a different key order, a space after a colon — is
#: parsed in full, so a format change costs speed rather than correctness.
_HEAD = re.compile(
    r'\{"timestamp":"([^"]*)",(?:"ordinal":-?\d+,)?"type":"(\w+)"'
    r'(?:,"payload":\{"type":"(\w+)")?'
)

#: Of the event and item types, the only ones the parser reads.
_EVENTS_READ = frozenset({"token_count"})
_TOOL_ITEMS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})

#: Top-level types known to carry nothing a receipt uses. Anything else that
#: is not an event or item is parsed, including types this has never seen.
_IGNORED = frozenset({"world_state", "compacted", "inter_agent_communication_metadata"})

#: Function tools whose arguments are a shell command, across CLI versions.
_SHELL_FUNCTIONS = frozenset(
    {"shell", "shell_command", "container.exec", "exec_command", "local_shell"}
)

#: Recent CLIs run tools from generated JavaScript —
#: ``await tools.exec_command({cmd: "…"})`` — inside one ``exec`` call. The
#: command is a string literal in that source.
_CODE_COMMAND = re.compile(
    r"exec_command\(\s*\{[^{}]*?\bcmd\s*:\s*([\"'`])((?:\\.|(?!\1)[^\\])*)\1", re.S
)

#: The file headers of an ``apply_patch`` body, which name every file edited.
_PATCH_FILE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: ([^\n\\\"'`]+)")

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}

_COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class _Thread:
    """What one rollout file contributes to a session."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.forked_from: Optional[str] = None
        self.history_start: Optional[int] = None
        self.cwd: Optional[str] = None
        self.branch: Optional[str] = None
        self.meta_seen = False
        self.turns: List[Turn] = []
        self.tool_counts: Dict[str, int] = {}
        self.commands: List[str] = []
        self.files_touched: Dict[str, int] = {}
        self.first_timestamp: Optional[str] = None
        self.last_timestamp: Optional[str] = None
        self.plan_usage: Optional[Dict[str, Any]] = None
        self.duplicate_records = 0

    def saw(self, timestamp: object) -> None:
        if not isinstance(timestamp, str) or not timestamp:
            return
        if self.first_timestamp is None or timestamp < self.first_timestamp:
            self.first_timestamp = timestamp
        if self.last_timestamp is None or timestamp > self.last_timestamp:
            self.last_timestamp = timestamp


def default_roots() -> List[str]:
    return [
        os.path.expanduser(root)
        for root in DEFAULT_ROOTS
        if os.path.isdir(os.path.expanduser(root))
    ]


def is_rollout(path: str) -> bool:
    """Whether a file is a Codex rollout rather than a Claude transcript."""
    if os.path.basename(path).startswith("rollout-"):
        return True
    return bool(_first_meta(path))


def parse_rollout(path: str) -> Optional[_Thread]:
    """Read one rollout file. Malformed and truncated lines are skipped.

    A live rollout is appended to as the session runs, so its last line is
    routinely a partial write; refusing the file over it would make the tool
    useless exactly when someone wants it.
    """
    try:
        # Opened outside a `with` so an unreadable file is skipped rather than
        # aborting the report; the `with` below closes it.
        handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    except OSError:
        return None

    thread = _Thread(path)
    model: Optional[str] = None
    # A token_usage_record has arrived since the last token_count, so the next
    # token_count that moves the total describes a response already counted.
    backed = False
    previous_total: Optional[tuple] = None

    with handle:
        for line in handle:
            head = _HEAD.match(line)
            if head is not None:
                thread.saw(head.group(1))
                if not _wanted(head.group(2), head.group(3)):
                    continue
            elif not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            timestamp = record.get("timestamp")
            if head is None:
                thread.saw(timestamp)
            kind = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            if kind == "session_meta":
                # The first is this thread's own. A fork's file carries its
                # parent's next, as part of the replayed history.
                if not thread.meta_seen:
                    thread.meta_seen = True
                    model = _absorb_meta(thread, payload) or model
                continue

            if kind == "turn_context":
                model = _context_model(payload) or model
                continue

            if kind == "token_usage_record":
                owner = payload.get("thread_id")
                if thread.id and isinstance(owner, str) and owner != thread.id:
                    thread.duplicate_records += 1
                    continue
                usage = _usage(payload.get("usage"))
                if usage is None:
                    continue
                backed = True
                response = payload.get("response_id")
                thread.turns.append(
                    Turn(
                        message_id=response if isinstance(response, str) else None,
                        model=model,
                        usage=usage,
                        timestamp=timestamp,
                    )
                )
                continue

            if kind == "event_msg" and payload.get("type") == "token_count":
                snapshot = _plan_snapshot(payload.get("rate_limits"), timestamp)
                if snapshot is not None and (
                    thread.plan_usage is None
                    or snapshot["as_of"] >= thread.plan_usage["as_of"]
                ):
                    thread.plan_usage = snapshot

                info = payload.get("info")
                if not isinstance(info, dict):
                    continue  # null until the first response of a thread
                total = _counters(info.get("total_token_usage"))
                prior = previous_total
                if total is not None:
                    if total == previous_total:
                        # Re-emitted with no new response behind it, or the
                        # post-compaction context estimate.
                        thread.duplicate_records += 1
                        continue
                    previous_total = total
                if backed:
                    backed = False
                    thread.duplicate_records += 1
                    continue
                if _replayed(thread, record.get("ordinal")):
                    continue
                usage = _usage(info.get("last_token_usage"))
                if usage is None and total is not None:
                    usage = _usage_since(total, prior)
                if usage is not None:
                    thread.turns.append(Turn(None, model, usage, timestamp))
                continue

            if kind == "response_item":
                _absorb_tool(thread, payload)

    return thread


def parse_session(paths: Sequence[str]) -> Optional[Session]:
    """Assemble one session from the rollout files of its threads."""
    threads = [t for t in (parse_rollout(p) for p in paths) if t is not None]
    if not threads:
        return None
    threads.sort(key=lambda t: t.first_timestamp or "")

    root = next((t for t in threads if t.id and t.id == t.session_id), threads[0])
    session_id = (
        root.session_id or root.id or os.path.splitext(os.path.basename(root.path))[0]
    )
    session = Session(
        path=root.path,
        session_id=session_id,
        project=project_name(root.cwd),
        agent="codex",
    )
    session.threads = len(threads)
    session.cwd = root.cwd
    session.git_branch = root.branch

    seen = set()
    for thread in threads:
        for turn in thread.turns:
            if turn.message_id:
                if turn.message_id in seen:
                    session.duplicate_records += 1
                    continue
                seen.add(turn.message_id)
            session.turns.append(turn)
        for name, count in thread.tool_counts.items():
            session.tool_counts[name] = session.tool_counts.get(name, 0) + count
        for target, count in thread.files_touched.items():
            session.files_touched[target] = session.files_touched.get(target, 0) + count
        session.commands.extend(thread.commands)
        session.duplicate_records += thread.duplicate_records
        for timestamp in (thread.first_timestamp, thread.last_timestamp):
            if timestamp and (
                session.first_timestamp is None or timestamp < session.first_timestamp
            ):
                session.first_timestamp = timestamp
            if timestamp and (
                session.last_timestamp is None or timestamp > session.last_timestamp
            ):
                session.last_timestamp = timestamp
        snapshot = thread.plan_usage
        if snapshot is not None and (
            session.plan_usage is None
            or snapshot["as_of"] >= session.plan_usage["as_of"]
        ):
            session.plan_usage = snapshot

    if not session.turns and not session.tool_counts:
        return None
    return session


def candidates(roots: Sequence[str], project: Optional[str] = None) -> List[Candidate]:
    """Every Codex session under ``roots``, unread, grouped by ``session_id``.

    Grouping needs only the first line of each file — the thread's own
    ``session_meta`` — so the files themselves are not read until a session is
    actually wanted.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not (
                    filename.startswith("rollout-") and filename.endswith(".jsonl")
                ):
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                meta = _first_meta(full)
                key = _text(meta.get("session_id")) or _text(meta.get("id")) or full
                group = groups.setdefault(key, {"paths": [], "mtime": 0.0, "cwd": None})
                group["paths"].append(full)
                group["mtime"] = max(group["mtime"], mtime)
                cwd = _text(meta.get("cwd"))
                if cwd and (group["cwd"] is None or meta.get("id") == key):
                    group["cwd"] = cwd

    found: List[Candidate] = []
    for key, group in groups.items():
        if project and project.lower() not in project_name(group["cwd"]).lower():
            continue
        loader = functools.partial(parse_session, sorted(group["paths"]))
        found.append((group["mtime"], key, loader))
    return found


def discover(
    root: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Session]:
    """Find and parse Codex sessions, newest first."""
    if root:
        roots = [os.path.expanduser(root)]
        if not os.path.isdir(roots[0]):
            raise SessionError("not a directory: %s" % roots[0])
    else:
        roots = default_roots()
        if not roots:
            raise SessionError(
                "no Codex session directory found. Looked for: %s. Pass "
                "--codex-root to point at rollouts elsewhere."
                % ", ".join(DEFAULT_ROOTS)
            )
    return collect(candidates(roots, project), since, limit)


def project_name(cwd: Optional[str]) -> str:
    """The working directory's last component, as Codex recorded it."""
    if not cwd:
        return "(unknown)"
    parts = [part for part in re.split(r"[\\/]", cwd) if part]
    return parts[-1] if parts else cwd


# -- line handling ------------------------------------------------------------


def _wanted(kind: str, payload_kind: Optional[str]) -> bool:
    if kind == "event_msg":
        return payload_kind is None or payload_kind in _EVENTS_READ
    if kind == "response_item":
        return payload_kind is None or payload_kind in _TOOL_ITEMS
    return kind not in _IGNORED


def _first_meta(path: str) -> Dict[str, Any]:
    """The ``session_meta`` payload on a rollout's first line, or ``{}``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            line = handle.readline(1 << 20)
    except OSError:
        return {}
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return {}
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return {}
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _absorb_meta(thread: _Thread, payload: Dict[str, Any]) -> Optional[str]:
    """Record the thread's identity; return the model it was started with."""
    thread.id = _text(payload.get("id"))
    thread.session_id = _text(payload.get("session_id")) or thread.id
    thread.forked_from = _text(payload.get("forked_from_id"))
    start = payload.get("subagent_history_start_ordinal")
    if isinstance(start, int) and not isinstance(start, bool):
        thread.history_start = start
    thread.cwd = _text(payload.get("cwd"))
    git = payload.get("git")
    if isinstance(git, dict):
        thread.branch = _text(git.get("branch"))
    instructions = payload.get("base_instructions")
    if isinstance(instructions, dict):
        provenance = instructions.get("provenance")
        if isinstance(provenance, dict):
            return _text(provenance.get("model"))
    return None


def _context_model(payload: Dict[str, Any]) -> Optional[str]:
    model = _text(payload.get("model"))
    if model:
        return model
    mode = payload.get("collaboration_mode")
    settings = mode.get("settings") if isinstance(mode, dict) else None
    return _text(settings.get("model")) if isinstance(settings, dict) else None


def _replayed(thread: _Thread, ordinal: object) -> bool:
    """Whether a record is part of the parent history a fork starts with."""
    return (
        thread.forked_from is not None
        and thread.history_start is not None
        and isinstance(ordinal, int)
        and ordinal < thread.history_start
    )


def _count(raw: Dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _counters(raw: object) -> Optional[tuple]:
    if not isinstance(raw, dict):
        return None
    return tuple(_count(raw, key) for key in _COUNTERS)


def _usage(raw: object) -> Optional[Usage]:
    """Map one Codex usage object onto the billing categories.

    Cached input is a subset of input and reasoning a subset of output (see the
    module docstring), so neither is added on top.
    """
    if not isinstance(raw, dict):
        return None
    prompt = _count(raw, "input_tokens")
    cached = _count(raw, "cached_input_tokens")
    usage = Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=_count(raw, "output_tokens"),
        cache_read_tokens=cached,
    )
    return usage if usage.total_tokens else None


def _usage_since(total: tuple, prior: Optional[tuple]) -> Optional[Usage]:
    """Usage between two cumulative totals, for events that carry no ``last``.

    A total smaller than its predecessor means the counter restarted — a
    session resumed after a restart — so the new total is all new usage.
    """
    if prior is not None and all(now >= then for now, then in zip(total, prior)):
        delta = [now - then for now, then in zip(total, prior)]
    else:
        delta = list(total)
    return _usage(dict(zip(_COUNTERS, delta)))


def _plan_snapshot(limits: object, timestamp: object) -> Optional[Dict[str, Any]]:
    if not isinstance(limits, dict) or not isinstance(timestamp, str):
        return None
    primary = limits.get("primary")
    if not isinstance(primary, dict):
        return None
    used = primary.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    snapshot: Dict[str, Any] = {
        "as_of": timestamp,
        "plan": _text(limits.get("plan_type")),
        "used_percent": float(used),
        "window_minutes": _integer(primary.get("window_minutes")),
        "resets_at": _integer(primary.get("resets_at")),
    }
    secondary = limits.get("secondary")
    if isinstance(secondary, dict):
        other = secondary.get("used_percent")
        if not isinstance(other, bool) and isinstance(other, (int, float)):
            snapshot["secondary"] = {
                "used_percent": float(other),
                "window_minutes": _integer(secondary.get("window_minutes")),
            }
    return snapshot


def _absorb_tool(thread: _Thread, payload: Dict[str, Any]) -> None:
    kind = payload.get("type")
    if kind not in _TOOL_ITEMS:
        return
    name = _text(payload.get("name")) or str(kind)
    thread.tool_counts[name] = thread.tool_counts.get(name, 0) + 1

    commands: List[str] = []
    patch: object = None
    if kind == "local_shell_call":
        action = payload.get("action")
        if isinstance(action, dict):
            commands.append(_command_text(action.get("command")))
    elif kind == "function_call":
        arguments = payload.get("arguments")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            if name in _SHELL_FUNCTIONS:
                commands.append(
                    _command_text(parsed.get("command") or parsed.get("cmd"))
                )
            elif name == "apply_patch":
                patch = parsed.get("input")
    else:
        source = payload.get("input")
        if isinstance(source, str):
            commands.extend(
                _unescape(body) for _q, body in _CODE_COMMAND.findall(source)
            )
            patch = source

    for command in commands:
        if command.strip():
            # Redact at capture, not at render. A secret that never enters the
            # object graph cannot be leaked by an output path added later.
            thread.commands.append(redact(command.strip()))
    if isinstance(patch, str):
        for target in _PATCH_FILE.findall(patch):
            target = target.strip()
            if target:
                thread.files_touched[target] = thread.files_touched.get(target, 0) + 1


def _command_text(command: object) -> str:
    """A shell command from a string or an argv list.

    ``["bash", "-lc", "npm test"]`` is reported as ``npm test``: the wrapper is
    how Codex runs everything, not what it ran.
    """
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        if (
            len(command) >= 3
            and os.path.basename(command[0]) in ("bash", "sh", "zsh")
            and command[1] in ("-lc", "-c")
        ):
            return command[2]
        return " ".join(command)
    return ""


def _unescape(body: str) -> str:
    return re.sub(
        r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), body, flags=re.S
    )


def _text(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)
