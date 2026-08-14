"""Tests for transcript parsing — above all, the deduplication rule.

The dedup tests are the most important in the project. Without them, every
figure the tool reports is roughly 2–3x too high, and it would look completely
plausible while being wrong.
"""

import json
import os
import shutil
import tempfile
import unittest

from burnrate.guard import evaluate, session_cost
from burnrate.sessions import discover, parse_file


def assistant(
    message_id="msg_1",
    model="claude-opus-5",
    input_tokens=0,
    output_tokens=0,
    cache_read=0,
    cache_5m=0,
    cache_1h=0,
    timestamp="2026-08-14T10:00:00Z",
    content=None,
):
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "gitBranch": "main",
        "cwd": "/tmp/project",
        "message": {
            "id": message_id,
            "model": model,
            "content": content or [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cache_5m,
                    "ephemeral_1h_input_tokens": cache_1h,
                },
            },
        },
    }


def tool_use(name, payload=None):
    return {"type": "tool_use", "name": name, "input": payload or {}}


class TranscriptFixture:
    def __init__(self, records, project="-Users-someone-Projects-demo"):
        self.root = tempfile.mkdtemp(prefix="burnrate-test-")
        folder = os.path.join(self.root, project)
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "session-abc123.jsonl")
        with open(self.path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


class TestDeduplication(unittest.TestCase):
    """The rule the whole tool's accuracy depends on."""

    def test_repeated_message_id_is_counted_once(self):
        records = [
            assistant("msg_1", output_tokens=100, cache_read=1000),
            assistant("msg_1", output_tokens=100, cache_read=1000),
            assistant("msg_1", output_tokens=100, cache_read=1000),
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        usage = session.total_usage()
        self.assertEqual(session.turn_count, 1)
        self.assertEqual(usage.output_tokens, 100)
        self.assertEqual(usage.cache_read_tokens, 1000)
        self.assertEqual(session.duplicate_records, 2)

    def test_streaming_partials_keep_the_largest_record(self):
        """Streaming writes a growing output count; the last one is complete."""
        records = [
            assistant("msg_1", output_tokens=10),
            assistant("msg_1", output_tokens=250),
            assistant("msg_1", output_tokens=120),
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.total_usage().output_tokens, 250)

    def test_distinct_messages_all_count(self):
        records = [
            assistant("msg_1", output_tokens=100),
            assistant("msg_2", output_tokens=200),
            assistant("msg_3", output_tokens=300),
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.turn_count, 3)
        self.assertEqual(session.total_usage().output_tokens, 600)

    def test_records_without_an_id_are_all_kept(self):
        """No id means no way to prove duplication — keep them rather than
        silently dropping real usage."""
        records = [
            assistant(None, output_tokens=100),
            assistant(None, output_tokens=100),
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.total_usage().output_tokens, 200)

    def test_naive_sum_would_have_overcounted(self):
        """Pins the size of the error being prevented."""
        records = [assistant("msg_1", output_tokens=100)] * 3 + [
            assistant("msg_2", output_tokens=100)
        ] * 3
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        naive = sum(
            r["message"]["usage"]["output_tokens"] for r in records
        )
        self.assertEqual(naive, 600)
        self.assertEqual(session.total_usage().output_tokens, 200)


class TestUsageExtraction(unittest.TestCase):
    def test_cache_ttls_are_kept_separate(self):
        records = [assistant("m", cache_5m=100, cache_1h=200)]
        with TranscriptFixture(records) as fixture:
            usage = parse_file(fixture.path).total_usage()
        self.assertEqual(usage.cache_write_5m_tokens, 100)
        self.assertEqual(usage.cache_write_1h_tokens, 200)

    def test_flat_cache_field_is_attributed_to_the_cheaper_bucket(self):
        """An unknown TTL split must understate, never inflate, the bill."""
        record = {
            "type": "assistant",
            "message": {
                "id": "m",
                "model": "claude-opus-5",
                "usage": {"cache_creation_input_tokens": 500, "output_tokens": 1},
            },
        }
        with TranscriptFixture([record]) as fixture:
            usage = parse_file(fixture.path).total_usage()
        self.assertEqual(usage.cache_write_5m_tokens, 500)
        self.assertEqual(usage.cache_write_1h_tokens, 0)

    def test_synthetic_model_is_excluded(self):
        records = [
            assistant("m1", model="<synthetic>", output_tokens=999),
            assistant("m2", model="claude-opus-5", output_tokens=10),
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.total_usage().output_tokens, 10)

    def test_zero_usage_records_are_skipped(self):
        records = [assistant("m1"), assistant("m2", output_tokens=5)]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.turn_count, 1)

    def test_negative_counts_are_clamped(self):
        record = {
            "type": "assistant",
            "message": {
                "id": "m",
                "model": "claude-opus-5",
                "usage": {"output_tokens": -50, "input_tokens": 10},
            },
        }
        with TranscriptFixture([record]) as fixture:
            usage = parse_file(fixture.path).total_usage()
        self.assertEqual(usage.output_tokens, 0)
        self.assertEqual(usage.input_tokens, 10)


class TestRobustness(unittest.TestCase):
    def test_malformed_lines_are_skipped_not_fatal(self):
        """Live transcripts routinely end in a partial write."""
        with TranscriptFixture([assistant("m", output_tokens=5)]) as fixture:
            with open(fixture.path, "a", encoding="utf-8") as handle:
                handle.write('{"type": "assistant", "message": {trunca\n')
            session = parse_file(fixture.path)
        self.assertIsNotNone(session)
        self.assertEqual(session.total_usage().output_tokens, 5)

    def test_blank_lines_ignored(self):
        with TranscriptFixture([assistant("m", output_tokens=5)]) as fixture:
            with open(fixture.path, "a", encoding="utf-8") as handle:
                handle.write("\n\n\n")
            session = parse_file(fixture.path)
        self.assertEqual(session.turn_count, 1)

    def test_transcript_with_no_usage_returns_none(self):
        with TranscriptFixture([{"type": "user", "message": {}}]) as fixture:
            self.assertIsNone(parse_file(fixture.path))

    def test_unreadable_path_returns_none(self):
        self.assertIsNone(parse_file("/nonexistent/session.jsonl"))


class TestActivityCapture(unittest.TestCase):
    def test_tool_calls_are_counted(self):
        records = [
            assistant(
                "m",
                output_tokens=1,
                content=[tool_use("Bash", {"command": "ls -la"}), tool_use("Read")],
            )
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.tool_counts["Bash"], 1)
        self.assertEqual(session.tool_counts["Read"], 1)
        self.assertEqual(session.commands, ["ls -la"])

    def test_files_touched_are_recorded(self):
        records = [
            assistant(
                "m",
                output_tokens=1,
                content=[
                    tool_use("Edit", {"file_path": "/tmp/a.py"}),
                    tool_use("Edit", {"file_path": "/tmp/a.py"}),
                    tool_use("Write", {"file_path": "/tmp/b.py"}),
                ],
            )
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.files_touched["/tmp/a.py"], 2)
        self.assertEqual(session.files_touched["/tmp/b.py"], 1)

    def test_metadata_is_captured(self):
        with TranscriptFixture([assistant("m", output_tokens=1)]) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.git_branch, "main")
        self.assertEqual(session.date, "2026-08-14")
        self.assertEqual(session.project, "demo")

    def test_errors_are_counted(self):
        records = [
            assistant("m", output_tokens=1),
            {"type": "system", "level": "error", "error": "boom"},
            {"type": "user", "toolUseResult": {"is_error": True}},
        ]
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertEqual(session.errors, 2)


class TestDiscovery(unittest.TestCase):
    def test_finds_sessions_under_a_root(self):
        with TranscriptFixture([assistant("m", output_tokens=5)]) as fixture:
            sessions = discover(root=fixture.root)
        self.assertEqual(len(sessions), 1)

    def test_project_filter(self):
        with TranscriptFixture([assistant("m", output_tokens=5)]) as fixture:
            self.assertEqual(len(discover(root=fixture.root, project="demo")), 1)
            self.assertEqual(len(discover(root=fixture.root, project="other")), 0)

    def test_since_filter(self):
        with TranscriptFixture([assistant("m", output_tokens=5)]) as fixture:
            self.assertEqual(len(discover(root=fixture.root, since="2026-01-01")), 1)
            self.assertEqual(len(discover(root=fixture.root, since="2027-01-01")), 0)


class TestGuard(unittest.TestCase):
    def expensive(self):
        return [assistant("m", output_tokens=1_000_000)]  # $25 on Opus 5

    def test_blocks_above_the_cap(self):
        with TranscriptFixture(self.expensive()) as fixture:
            result = evaluate(cap=1.0, transcript_path=fixture.path)
        self.assertEqual(result.state, "block")
        self.assertEqual(result.exit_code, 2)

    def test_allows_below_the_cap(self):
        with TranscriptFixture(self.expensive()) as fixture:
            result = evaluate(cap=100.0, transcript_path=fixture.path)
        self.assertEqual(result.state, "ok")
        self.assertEqual(result.exit_code, 0)

    def test_warns_before_blocking(self):
        with TranscriptFixture(self.expensive()) as fixture:
            result = evaluate(cap=30.0, transcript_path=fixture.path, warn_at=0.75)
        self.assertEqual(result.state, "warn")
        self.assertEqual(result.exit_code, 0)

    def test_fails_open_on_a_missing_transcript(self):
        """A cost tool must never brick someone's agent."""
        result = evaluate(cap=1.0, transcript_path="/nonexistent/x.jsonl")
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.exit_code, 0)

    def test_fails_open_when_the_model_cannot_be_priced(self):
        records = [assistant("m", model="unknown-vendor-model", output_tokens=999999)]
        with TranscriptFixture(records) as fixture:
            result = evaluate(cap=0.01, transcript_path=fixture.path)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.exit_code, 0)

    def test_zero_cap_does_not_enforce(self):
        with TranscriptFixture(self.expensive()) as fixture:
            result = evaluate(cap=0, transcript_path=fixture.path)
        self.assertEqual(result.exit_code, 0)

    def test_session_cost_matches_the_price_table(self):
        with TranscriptFixture(self.expensive()) as fixture:
            session = parse_file(fixture.path)
        self.assertAlmostEqual(session_cost(session), 25.0, places=6)


if __name__ == "__main__":
    unittest.main()
