"""Tests for the Codex rollout parser — above all, what it refuses to count.

Codex reports usage twice for most responses and not at all in its running
total for some, so every obvious sum is wrong. These fixtures reproduce the
sequences measured on real rollouts (CLI 0.153–0.160) and the shapes older
CLIs wrote, on disk, including truncated final lines. Every expected figure is
worked out by hand from the fixture.
"""

import json
import os
import shutil
import tempfile
import unittest

from burnrate import codex
from burnrate.receipt import fmt_plan_usage, price_session, render_session

from .test_cli import run_cli
from .test_sessions import TranscriptFixture, assistant

#: 2100-01-01T00:00:00Z — a reset that has not happened yet, whenever this runs.
FUTURE = 4102444800

PARENT = "01a0f951-7df3-73a0-aa48-7188925d7134"
CHILD = "01a0f952-1111-7222-8333-444455556666"


def usage(prompt, cached=0, output=0, reasoning=0, cache_write=None):
    """A Codex usage object. ``total_tokens`` is input + output, as logged:
    cached input is part of input, reasoning part of output."""
    out = {
        "input_tokens": prompt,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": prompt + output,
    }
    if cache_write is not None:
        out["cache_write_input_tokens"] = cache_write
    return out


def plus(*items):
    keys = items[0].keys()
    return {key: sum(item[key] for item in items) for key in keys}


class Rollout:
    """Builds one rollout file's records in order, numbering ordinals."""

    def __init__(self, thread_id, ordinals=True, start="2026-10-06T12:00:00.000Z"):
        self.thread_id = thread_id
        self.ordinals = ordinals
        self.records = []
        self.minute = 0
        self.start = start

    def add(self, kind, payload, timestamp=None):
        record = {"timestamp": timestamp or self.tick()}
        if self.ordinals:
            record["ordinal"] = len(self.records)
        record["type"] = kind
        record["payload"] = payload
        self.records.append(record)
        return self

    def tick(self):
        self.minute += 1
        return "%sT12:%02d:00.000Z" % (self.start[:10], self.minute % 60)

    def meta(self, session_id=None, forked_from=None, history_start=None, **extra):
        payload = {
            "id": self.thread_id,
            "session_id": session_id or self.thread_id,
            "cwd": "/home/someone/projects/demo",
            "cli_version": "0.160.0",
            "model_provider": "openai",
            "base_instructions": {"provenance": {"model": "gpt-6-astra"}},
        }
        if forked_from:
            payload["forked_from_id"] = forked_from
            payload["thread_source"] = "subagent"
        if history_start is not None:
            payload["subagent_history_start_ordinal"] = history_start
        payload.update(extra)
        return self.add("session_meta", payload)

    def context(self, model):
        return self.add(
            "turn_context",
            {"model": model, "collaboration_mode": {"settings": {"model": model}}},
        )

    def record(self, response_id, used, thread_id=None):
        return self.add(
            "token_usage_record",
            {
                "thread_id": thread_id or self.thread_id,
                "response_id": response_id,
                "usage": used,
                "turn_token_usage": used,
            },
        )

    def count(self, total, last, rate_limits=None):
        info = None if total is None else {"total_token_usage": total}
        if info is not None and last is not None:
            info["last_token_usage"] = last
        payload = {"type": "token_count", "info": info}
        if rate_limits is not None:
            payload["rate_limits"] = rate_limits
        return self.add("event_msg", payload)

    def item(self, kind, **fields):
        return self.add("response_item", dict({"type": kind}, **fields))

    def noise(self):
        """Record types the parser must skip without effect."""
        self.add("event_msg", {"type": "task_started", "turn_id": "t"})
        self.item("reasoning", encrypted_content="x" * 500)
        self.item("function_call_output", call_id="c", output="y" * 500)
        self.add("event_msg", {"type": "item_completed", "item": {"type": "x"}})
        self.add("world_state", {"full": True, "state": {}})
        return self


class CodexFixture:
    """A ``~/.codex/sessions``-shaped tree in a temp directory."""

    def __init__(self, *rollouts, compact=True):
        self.root = tempfile.mkdtemp(prefix="burnrate-codex-")
        self.paths = []
        for index, rollout in enumerate(rollouts):
            folder = os.path.join(self.root, "2026", "10", "06")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(
                folder,
                "rollout-2026-10-06T12-%02d-00-%s.jsonl" % (index, rollout.thread_id),
            )
            with open(path, "w", encoding="utf-8") as handle:
                for record in rollout.records:
                    if compact:
                        # Real rollouts are compact JSON; that is what the
                        # line-prefix fast path recognizes.
                        handle.write(json.dumps(record, separators=(",", ":")))
                    else:
                        handle.write(json.dumps(record))
                    handle.write("\n")
            self.paths.append(path)

    def append(self, index, text):
        with open(self.paths[index], "a", encoding="utf-8") as handle:
            handle.write(text)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)


# Four responses on one thread, as CLI 0.160 logs them.
A = usage(30_000, cached=20_000, output=500, reasoning=100)
B = usage(40_000, cached=35_000, output=800, reasoning=300)
C = usage(242_309, cached=233_472, output=5_611)  # a compaction call
D = usage(46_460, cached=18_944, output=200)
ESTIMATE = {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 30_576,
}


def current_thread(thread_id=PARENT, rate_limits=None):
    """The real sequence, including every trap:

    * an ``info: null`` count before the first response,
    * every response logged twice (record, then a count with the same numbers),
    * a count re-emitted with no new response behind it,
    * a compaction call that only the records see — the running total skips
      it, and the count after it reports the compacted context as ``last``.
    """
    return (
        Rollout(thread_id)
        .meta()
        .context("gpt-6-astra")
        .count(None, None)
        .noise()
        .record("resp_a", A)
        .count(A, A)
        .count(A, A)
        .record("resp_b", B)
        .count(plus(A, B), B)
        .record("resp_c", C)
        .add("compacted", {"message": "", "replacement_history": []})
        .count(plus(A, B), ESTIMATE)
        .record("resp_d", D)
        .count(plus(A, B, D), D, rate_limits=rate_limits)
    )


def tokens(session):
    total = session.total_usage()
    return (total.input_tokens, total.cache_read_tokens, total.output_tokens)


def expected(*items):
    """(uncached input, cached input, output) for the given usage objects."""
    return (
        sum(i["input_tokens"] - i["cached_input_tokens"] for i in items),
        sum(i["cached_input_tokens"] for i in items),
        sum(i["output_tokens"] for i in items),
    )


def parse(*rollouts, compact=True):
    with CodexFixture(*rollouts, compact=compact) as fixture:
        return codex.parse_session(fixture.paths)


class TestCounting(unittest.TestCase):
    """The rule the Codex figures depend on."""

    def test_each_response_is_counted_exactly_once(self):
        session = parse(current_thread())
        self.assertEqual(session.turn_count, 4)
        self.assertEqual(tokens(session), expected(A, B, C, D))

    def test_neither_naive_sum_is_what_was_used(self):
        """Pins the size of both errors being prevented."""
        events = [A, A, B, ESTIMATE, D]  # last_token_usage of every count
        naive_last = sum(e["total_tokens"] for e in events)
        final_total = plus(A, B, D)["total_tokens"]
        actual = sum(u["total_tokens"] for u in (A, B, C, D))
        self.assertGreater(naive_last, actual - C["total_tokens"])
        self.assertLess(final_total, actual)  # the compaction call is missing
        session = parse(current_thread())
        self.assertEqual(session.total_usage().total_tokens, actual)

    def test_cached_input_is_part_of_input_not_added_to_it(self):
        session = parse(Rollout(PARENT).meta().record("r", A))
        total = session.total_usage()
        self.assertEqual(total.input_tokens, 10_000)  # 30,000 - 20,000 cached
        self.assertEqual(total.cache_read_tokens, 20_000)
        self.assertEqual(total.total_input_tokens, 30_000)

    def test_reasoning_is_part_of_output_not_added_to_it(self):
        session = parse(Rollout(PARENT).meta().record("r", A))
        self.assertEqual(session.total_usage().output_tokens, 500)

    def test_compact_and_spaced_json_agree(self):
        """The line-prefix fast path must change speed, never the answer."""
        fast = parse(current_thread(), compact=True)
        slow = parse(current_thread(), compact=False)
        self.assertEqual(tokens(fast), tokens(slow))
        self.assertEqual(fast.turn_count, slow.turn_count)

    def test_a_count_with_its_payload_type_later_is_still_read(self):
        """If the payload stops leading with its type, the line is parsed in
        full rather than skipped."""
        rollout = Rollout(PARENT, ordinals=False).meta()
        rollout.records.append(
            {
                "timestamp": "2026-10-06T12:30:00.000Z",
                "type": "event_msg",
                "payload": {"info": {"total_token_usage": A, "last_token_usage": A}},
            }
        )
        rollout.records[-1]["payload"]["type"] = "token_count"
        self.assertEqual(tokens(parse(rollout)), expected(A))

    def test_noise_records_do_not_change_the_totals(self):
        clean = Rollout(PARENT).meta().record("r", A).count(A, A)
        noisy = Rollout(PARENT).meta().noise().record("r", A).noise().count(A, A)
        self.assertEqual(tokens(parse(clean)), tokens(parse(noisy)))

    def test_model_follows_the_turn_context(self):
        rollout = (
            Rollout(PARENT)
            .meta()
            .context("gpt-6-astra")
            .record("r1", A)
            .context("gpt-reserve")
            .record("r2", B)
        )
        by_model = parse(rollout).usage_by_model()
        self.assertEqual(by_model["gpt-6-astra"].output_tokens, 500)
        self.assertEqual(by_model["gpt-reserve"].output_tokens, 800)

    def test_model_falls_back_to_the_session_meta(self):
        session = parse(Rollout(PARENT).meta().record("r", A))
        self.assertEqual(list(session.usage_by_model()), ["gpt-6-astra"])


class TestOlderFormats(unittest.TestCase):
    """CLIs before token_usage_record: no ordinals, no records, no
    cache_write_input_tokens, and a null info before the first response."""

    def old_thread(self):
        return (
            Rollout(PARENT, ordinals=False)
            .meta(cli_version="0.40.0")
            .context("gpt-6-astra")
            .count(None, None)
            .count(A, A)
            .count(A, A)  # re-emitted
            .count(plus(A, B), B)
            # The CLI restarted and resumed the session: the running total
            # starts again from this response.
            .count(C, C)
            .count(plus(C, D), D)
        )

    def test_counts_are_used_when_there_are_no_records(self):
        session = parse(self.old_thread())
        self.assertEqual(session.turn_count, 4)
        self.assertEqual(tokens(session), expected(A, B, C, D))

    def test_a_truncated_final_line_is_skipped_not_fatal(self):
        with CodexFixture(self.old_thread()) as fixture:
            fixture.append(0, '{"timestamp":"2026-10-06T13:00:00.000Z","type":"ev')
            session = codex.parse_session(fixture.paths)
        self.assertEqual(tokens(session), expected(A, B, C, D))

    def test_a_count_without_last_usage_falls_back_to_the_total(self):
        rollout = (
            Rollout(PARENT, ordinals=False)
            .meta()
            .count(A, None)
            .count(plus(A, B), None)
            .count(C, None)  # a smaller total: the counter restarted
        )
        self.assertEqual(tokens(parse(rollout)), expected(A, B, C))

    def test_a_file_upgraded_mid_session_counts_both_halves(self):
        """Counts before the CLI started writing records are still usage."""
        rollout = (
            Rollout(PARENT).meta().count(A, A).record("resp_b", B).count(plus(A, B), B)
        )
        self.assertEqual(tokens(parse(rollout)), expected(A, B))

    def test_blank_and_garbage_lines_are_ignored(self):
        with CodexFixture(Rollout(PARENT).meta().record("r", A)) as fixture:
            fixture.append(0, "\n\nnot json\n[1, 2]\n")
            session = codex.parse_session(fixture.paths)
        self.assertEqual(tokens(session), expected(A))


class TestSubagents(unittest.TestCase):
    def forked_child(self, replay_usage=True):
        child = Rollout(CHILD).meta(
            session_id=PARENT, forked_from=PARENT, history_start=5
        )
        child.add("session_meta", {"id": PARENT, "session_id": PARENT})  # replayed
        child.item("message", role="user", content=[])
        if replay_usage:
            # Defensive: a fork that replayed the parent's usage verbatim.
            child.record("resp_a", A, thread_id=PARENT)
            child.count(A, A)
        while len(child.records) < 5:
            child.item("message", role="assistant", content=[])
        E = usage(12_000, cached=8_000, output=300)
        child.context("gpt-6-astra").record("resp_e", E).count(E, E)
        return child, E

    def test_a_session_is_its_root_thread_and_its_subagents(self):
        child, E = self.forked_child(replay_usage=False)
        session = parse(current_thread(), child)
        self.assertEqual(session.threads, 2)
        self.assertEqual(session.session_id, PARENT)
        self.assertEqual(session.project, "demo")
        self.assertEqual(tokens(session), expected(A, B, C, D, E))

    def test_replayed_parent_usage_is_not_counted_again(self):
        child, E = self.forked_child(replay_usage=True)
        session = parse(current_thread(), child)
        self.assertEqual(tokens(session), expected(A, B, C, D, E))

    def test_a_record_from_another_thread_is_dropped(self):
        """Even when its response id is new to this session."""
        child = (
            Rollout(CHILD)
            .meta(session_id=PARENT, forked_from=PARENT, history_start=2)
            .record("resp_parent_only", B, thread_id=PARENT)
            .record("resp_e", D)
        )
        self.assertEqual(tokens(parse(child)), expected(D))

    def test_a_response_id_is_counted_once_across_a_sessions_files(self):
        """Records without a thread id are deduplicated by response id."""
        first = Rollout(PARENT).meta()
        first.add("token_usage_record", {"response_id": "resp_a", "usage": A})
        second = Rollout(CHILD).meta(session_id=PARENT)
        second.add("token_usage_record", {"response_id": "resp_a", "usage": A})
        self.assertEqual(tokens(parse(first, second)), expected(A))

    def test_a_record_less_fork_skips_counts_from_the_replayed_history(self):
        child = (
            Rollout(CHILD)
            .meta(session_id=PARENT, forked_from=PARENT, history_start=3)
            .add("session_meta", {"id": PARENT})
            .count(plus(A, B), plus(A, B))  # the parent's, replayed
            .context("gpt-6-astra")
            .count(D, D)
        )
        self.assertEqual(tokens(parse(child)), expected(D))

    def test_discovery_groups_threads_by_session(self):
        child, _E = self.forked_child(replay_usage=False)
        other = Rollout("01a0f999-0000-7000-8000-000000000000").meta().record("x", A)
        with CodexFixture(current_thread(), child, other) as fixture:
            sessions = codex.discover(root=fixture.root)
        self.assertEqual(sorted(s.threads for s in sessions), [1, 2])

    def test_a_thread_id_prefix_is_not_a_label(self):
        """UUIDv7 ids start with a timestamp; the tail tells them apart."""
        session = parse(current_thread())
        self.assertEqual(session.short_id, "7188925d7134"[-8:])


class TestActivity(unittest.TestCase):
    def test_tool_calls_are_counted_by_name(self):
        rollout = (
            Rollout(PARENT)
            .meta()
            .record("r", A)
            .item("custom_tool_call", name="exec", input="text(1)")
            .item("custom_tool_call", name="exec", input="text(2)")
            .item("function_call", name="send_message", arguments="{}")
            .item("local_shell_call", action={"command": ["ls"]})
            .item("function_call_output", output="not a call")
        )
        session = parse(rollout)
        self.assertEqual(
            session.tool_counts, {"exec": 2, "send_message": 1, "local_shell_call": 1}
        )

    def test_commands_are_captured_from_every_shape(self):
        code = (
            'const r = await tools.exec_command({cmd: "npm test -- --watch=false",'
            ' workdir: "/x"});\ntext(r);'
        )
        rollout = (
            Rollout(PARENT)
            .meta()
            .record("r", A)
            .item("custom_tool_call", name="exec", input=code)
            .item(
                "function_call",
                name="shell",
                arguments=json.dumps({"command": ["bash", "-lc", "git status"]}),
            )
            .item("local_shell_call", action={"command": ["ls", "-la"]})
        )
        self.assertEqual(
            parse(rollout).commands,
            ["npm test -- --watch=false", "git status", "ls -la"],
        )

    def test_commands_are_redacted_at_capture(self):
        secret = "sk-" + "N0TAREALKEY" * 3
        code = "await tools.exec_command({cmd: 'export OPENAI_API_KEY=%s'})" % secret
        rollout = (
            Rollout(PARENT)
            .meta()
            .record("r", A)
            .item("custom_tool_call", name="exec", input=code)
        )
        session = parse(rollout)
        self.assertEqual(len(session.commands), 1)
        self.assertNotIn(secret, session.commands[0])
        self.assertIn("redacted", session.commands[0])

    def test_patched_files_are_recorded(self):
        patch = (
            "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n"
            "*** Add File: src/new.py\n+x\n*** End Patch"
        )
        rollout = (
            Rollout(PARENT)
            .meta()
            .record("r", A)
            .item("custom_tool_call", name="apply_patch", input=patch)
        )
        self.assertEqual(
            parse(rollout).files_touched, {"src/app.py": 1, "src/new.py": 1}
        )


class TestPlanUsage(unittest.TestCase):
    def limits(self, used, resets_at=FUTURE, plan="pro", window=10080):
        return {
            "limit_id": "codex",
            "primary": {
                "used_percent": used,
                "window_minutes": window,
                "resets_at": resets_at,
            },
            "secondary": None,
            "plan_type": plan,
        }

    def test_the_latest_snapshot_is_kept(self):
        rollout = (
            Rollout(PARENT)
            .meta()
            .record("r1", A)
            .count(A, A, rate_limits=self.limits(40.0))
            .record("r2", B)
            .count(plus(A, B), B, rate_limits=self.limits(56.0))
        )
        snapshot = parse(rollout).plan_usage
        self.assertEqual(snapshot["used_percent"], 56.0)
        self.assertEqual(snapshot["plan"], "pro")

    def test_wording(self):
        snapshot = {
            "as_of": "2026-10-06T21:07:32.376Z",
            "plan": "pro",
            "used_percent": 56.0,
            "window_minutes": 10080,
            "resets_at": FUTURE,
        }
        self.assertEqual(
            fmt_plan_usage(snapshot),
            "Codex plan usage (pro): 56% of the weekly limit, as of "
            "2026-10-06 21:07 UTC; resets 2100-01-01 00:00 UTC.",
        )

    def test_a_window_that_has_rolled_over_says_so(self):
        snapshot = {
            "as_of": "2026-09-01T00:00:00Z",
            "plan": None,
            "used_percent": 90.0,
            "window_minutes": 300,
            "resets_at": 1_000,
        }
        text = fmt_plan_usage(snapshot)
        self.assertIn("90% of the 5-hour limit", text)
        self.assertIn("that window has since reset", text)

    def test_the_receipt_prints_it(self):
        rollout = current_thread(rate_limits=self.limits(56.0))
        report = price_session(parse(rollout))
        text = render_session(report)
        self.assertIn("Codex plan usage (pro): 56% of the weekly limit", text)


class TestPricing(unittest.TestCase):
    def test_codex_models_are_named_as_unpriced_not_costed_at_zero(self):
        report = price_session(parse(current_thread()))
        self.assertEqual(report["unpriced_models"], ["gpt-6-astra"])
        self.assertFalse(report["priced"])
        text = render_session(report)
        self.assertIn("unpriced", text.split("TOTAL", 1)[1].splitlines()[0])

    def test_a_price_override_prices_them(self):
        # Arbitrary test rates, not a claim about anyone's price list.
        prices = {"gpt-6-astra": {"input": 1.0, "cache_read": 0.1, "output": 10.0}}
        report = price_session(parse(Rollout(PARENT).meta().record("r", A)), prices)
        # 10,000 uncached at $1/M = $0.010; 20,000 cached at $0.10/M = $0.002;
        # 500 output at $10/M = $0.005. Total $0.017.
        self.assertAlmostEqual(report["cost"], 0.017, places=9)


class TestCodexCLI(unittest.TestCase):
    def test_codex_receipt(self):
        with CodexFixture(current_thread()) as fixture:
            code, out, _ = run_cli("--codex-root", fixture.root)
        self.assertEqual(code, 0)
        self.assertIn("Codex", out)
        self.assertIn("gpt-6-astra", out)
        self.assertIn("unpriced", out)

    def both(self):
        claude = TranscriptFixture([assistant("m", output_tokens=1_000_000)])
        rollouts = CodexFixture(current_thread())
        roots = ("--root", claude.root, "--codex-root", rollouts.root)
        return claude, rollouts, roots

    def test_a_named_root_reads_only_that_agent(self):
        """Never the real home directory behind a fixture's back."""
        claude, rollouts, roots = self.both()
        with claude, rollouts:
            code, out, _ = run_cli(*roots, "--agent", "claude", "--all", "-v")
            _code, json_out, _ = run_cli(
                *roots, "--agent", "claude", "--all", "--format", "json"
            )
        self.assertEqual(code, 0)
        self.assertNotIn("Codex", out)
        self.assertEqual([r["agent"] for r in json.loads(json_out)], ["claude"])

    def test_top_and_trend_label_the_agent(self):
        claude, rollouts, roots = self.both()
        with claude, rollouts:
            _code, top, _ = run_cli(*roots, "--top")
            _code, trend, _ = run_cli(*roots, "--trend")
            _code, summary, _ = run_cli(*roots, "--summary", "agent")
        self.assertIn("AGENT", top)
        self.assertIn("codex", top)
        self.assertIn("claude", top)
        self.assertIn("no price on file for gpt-6-astra", top)
        self.assertIn("1 claude", trend)
        self.assertIn("1 codex", trend)
        self.assertIn("Claude Code", summary)
        self.assertIn("Codex", summary)

    def test_claude_only_top_has_no_agent_column(self):
        with TranscriptFixture([assistant("m", output_tokens=1_000_000)]) as claude:
            _code, top, _ = run_cli("--root", claude.root, "--top")
        self.assertNotIn("AGENT", top)

    def test_session_flag_reads_a_rollout(self):
        with CodexFixture(current_thread()) as fixture:
            code, out, _ = run_cli("--session", fixture.paths[0])
        self.assertEqual(code, 0)
        self.assertIn("Codex", out)

    def test_json_carries_agent_and_plan_usage(self):
        limits = TestPlanUsage().limits(56.0)
        with CodexFixture(current_thread(rate_limits=limits)) as fixture:
            _code, out, _ = run_cli("--codex-root", fixture.root, "--format", "json")
        report = json.loads(out)[0]
        self.assertEqual(report["agent"], "codex")
        self.assertEqual(report["threads"], 1)
        self.assertEqual(report["plan_usage"]["used_percent"], 56.0)

    def test_secrets_never_reach_json_output(self):
        secret = "sk-" + "N0TAREALKEY" * 3
        rollout = (
            Rollout(PARENT)
            .meta()
            .record("r", A)
            .item(
                "function_call",
                name="shell",
                arguments=json.dumps({"command": "curl -H 'x' --token %s" % secret}),
            )
        )
        with CodexFixture(rollout) as fixture:
            _code, out, _ = run_cli("--codex-root", fixture.root, "--format", "json")
        self.assertNotIn(secret, out)

    def test_missing_codex_root_exits_two(self):
        code, _, err = run_cli("--agent", "codex", "--codex-root", "/nonexistent/x")
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_guard_is_claude_only_and_fails_open(self):
        code, _, err = run_cli("guard", "--cap", "1.00", "--agent", "codex")
        self.assertEqual(code, 0)
        self.assertIn("Claude Code", err)

    def test_since_skips_files_untouched_since_without_reading_them(self):
        with CodexFixture(current_thread()) as fixture:
            os.utime(fixture.paths[0], (1_000_000_000, 1_000_000_000))  # 2001
            code, _, err = run_cli(
                "--codex-root", fixture.root, "--since", "2026-10-01"
            )
        self.assertEqual(code, 1)
        self.assertIn("No sessions found", err)


class TestDetection(unittest.TestCase):
    def test_rollouts_are_told_apart_from_claude_transcripts(self):
        with CodexFixture(current_thread()) as fixture:
            self.assertTrue(codex.is_rollout(fixture.paths[0]))
        with TranscriptFixture([assistant("m", output_tokens=1)]) as claude:
            self.assertFalse(codex.is_rollout(claude.path))

    def test_project_name_is_the_working_directory(self):
        self.assertEqual(codex.project_name("/home/a/projects/demo/"), "demo")
        self.assertEqual(codex.project_name("C:\\Users\\a\\demo"), "demo")
        self.assertEqual(codex.project_name(None), "(unknown)")


if __name__ == "__main__":
    unittest.main()
