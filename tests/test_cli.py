"""Tests for the receipt renderer and the CLI."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from burnrate.cli import main
from burnrate.receipt import fmt_money, fmt_tokens, price_session, render_summary
from burnrate.sessions import parse_file

from .test_sessions import TranscriptFixture, assistant, tool_use


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class TestFormatting(unittest.TestCase):
    def test_money_precision_scales_down(self):
        self.assertEqual(fmt_money(12.5), "$12.50")
        self.assertEqual(fmt_money(0.5), "$0.500")
        self.assertEqual(fmt_money(0.0003), "$0.0003")
        self.assertEqual(fmt_money(0), "$0.00")

    def test_money_uses_thousands_separators(self):
        self.assertEqual(fmt_money(1234.5), "$1,234.50")

    def test_money_none(self):
        self.assertEqual(fmt_money(None), "n/a")

    def test_tokens_compact(self):
        self.assertEqual(fmt_tokens(999), "999")
        self.assertEqual(fmt_tokens(1500), "1.5k")
        self.assertEqual(fmt_tokens(2_500_000), "2.5M")


class TestPriceSession(unittest.TestCase):
    def session(self):
        records = [
            assistant(
                "m1",
                output_tokens=1_000_000,
                cache_read=10_000_000,
                content=[tool_use("Bash", {"command": "pytest"})],
            )
        ]
        with TranscriptFixture(records) as fixture:
            return parse_file(fixture.path)

    def test_totals_and_savings(self):
        report = price_session(self.session())
        # 1M output on Opus 5 = $25; 10M cache read = 10 * $5 * 0.1 = $5.
        self.assertAlmostEqual(report["cost"], 30.0, places=6)
        # Uncached: 10M input at $5/M = $50, plus $25 output = $75.
        self.assertAlmostEqual(report["uncached_cost"], 75.0, places=6)
        self.assertAlmostEqual(report["saved_by_caching"], 45.0, places=6)

    def test_cache_hit_rate(self):
        report = price_session(self.session())
        self.assertAlmostEqual(report["cache_hit_rate"], 1.0, places=6)

    def test_unpriced_models_are_named_not_zeroed(self):
        records = [assistant("m", model="mystery-model", output_tokens=5_000_000)]
        with TranscriptFixture(records) as fixture:
            report = price_session(parse_file(fixture.path))
        self.assertIn("mystery-model", report["unpriced_models"])
        self.assertEqual(report["cost"], 0.0)

    def test_tools_are_surfaced(self):
        report = price_session(self.session())
        self.assertEqual(report["top_tools"], [("Bash", 1)])


class TestRenderSummary(unittest.TestCase):
    def test_groups_and_totals(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            report = price_session(parse_file(fixture.path))
        text = render_summary([report, report], group_by="day")
        self.assertIn("BURN BY DAY", text)
        self.assertIn("2026-08-14", text)
        self.assertIn("$50.00", text)  # two sessions at $25 each

    def test_group_by_model(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            report = price_session(parse_file(fixture.path))
        text = render_summary([report], group_by="model")
        self.assertIn("claude-opus-5", text)


class TestCLI(unittest.TestCase):
    def test_report_renders(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli("--root", fixture.root)
        self.assertEqual(code, 0)
        self.assertIn("TOTAL", out)
        self.assertIn("$25.00", out)

    def test_json_output_is_valid(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli("--root", fixture.root, "--format", "json")
        payload = json.loads(out)
        self.assertEqual(len(payload), 1)
        self.assertAlmostEqual(payload[0]["cost"], 25.0, places=6)
        self.assertIn("usage", payload[0])

    def test_summary_mode(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli("--root", fixture.root, "--summary", "day")
        self.assertEqual(code, 0)
        self.assertIn("BURN BY DAY", out)

    def test_project_filter_with_no_match_exits_one(self):
        records = [assistant("m", output_tokens=1000)]
        with TranscriptFixture(records) as fixture:
            code, _, err = run_cli("--root", fixture.root, "--project", "nomatch")
        self.assertEqual(code, 1)
        self.assertIn("No sessions found", err)

    def test_specific_session_file(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli("--session", fixture.path)
        self.assertEqual(code, 0)
        self.assertIn("$25.00", out)

    def test_guard_requires_a_cap(self):
        code, _, err = run_cli("guard")
        self.assertEqual(code, 2)
        self.assertIn("--cap", err)

    def test_guard_blocks(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, _, err = run_cli(
                "guard", "--cap", "1.00", "--transcript", fixture.path
            )
        self.assertEqual(code, 2)
        self.assertIn("cap", err)

    def test_guard_json(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli(
                "guard", "--cap", "1.00", "--transcript", fixture.path,
                "--format", "json",
            )
        payload = json.loads(out)
        self.assertEqual(payload["state"], "block")
        self.assertEqual(code, 2)

    def test_bad_price_file_exits_two(self):
        code, _, err = run_cli("--prices", "/nonexistent/p.json")
        self.assertEqual(code, 2)
        self.assertIn("price error", err)

    def test_bad_root_exits_two(self):
        code, _, err = run_cli("--root", "/nonexistent/root")
        self.assertEqual(code, 2)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
