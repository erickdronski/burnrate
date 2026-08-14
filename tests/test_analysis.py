"""Tests for ranking and trend analysis.

A day-by-day total tells you *that* Tuesday was expensive. These answer the two
questions someone can actually act on: which session did it, and is the number
going up.
"""

import unittest

from burnrate.receipt import price_session, render_top, render_trend
from burnrate.sessions import parse_file

from .test_cli import run_cli
from .test_sessions import TranscriptFixture, assistant


def report(output_tokens, day="2026-08-14", end=None):
    start = "%sT10:00:00Z" % day
    finish = "%sT18:00:00Z" % (end or day)
    records = [
        assistant("m1", output_tokens=output_tokens, timestamp=start),
        assistant("m2", output_tokens=1, timestamp=finish),
    ]
    with TranscriptFixture(records) as fixture:
        return price_session(parse_file(fixture.path))


class TestTop(unittest.TestCase):
    def test_ranks_by_cost_descending(self):
        reports = [report(1_000_000), report(4_000_000), report(2_000_000)]
        text = render_top(reports)
        first = text.index("$100.00")  # 4M output on Opus 5
        second = text.index("$50.00")  # 2M
        third = text.index("$25.00")  # 1M
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_limit_is_respected(self):
        reports = [report(n * 1_000_000) for n in range(1, 8)]
        text = render_top(reports, limit=3)
        self.assertEqual(text.count("Projects") + text.count("demo"), 3)

    def test_reports_concentration(self):
        """The line that makes the output actionable."""
        reports = [report(10_000_000)] + [report(100_000) for _ in range(20)]
        text = render_top(reports, limit=1)
        self.assertIn("1 session(s) are", text)
        self.assertIn("%", text)

    def test_empty_input_does_not_crash(self):
        self.assertIn("MOST EXPENSIVE", render_top([]))


class TestTrend(unittest.TestCase):
    def test_buckets_by_day(self):
        reports = [
            report(1_000_000, day="2026-08-12"),
            report(2_000_000, day="2026-08-13"),
        ]
        text = render_trend(reports)
        self.assertIn("2026-08-12", text)
        self.assertIn("2026-08-13", text)

    def test_detects_a_rising_trend(self):
        reports = [
            report(1_000_000, day="2026-08-10"),
            report(8_000_000, day="2026-08-14"),
        ]
        self.assertIn("up", render_trend(reports))

    def test_detects_a_falling_trend(self):
        reports = [
            report(8_000_000, day="2026-08-10"),
            report(1_000_000, day="2026-08-14"),
        ]
        self.assertIn("down", render_trend(reports))

    def test_a_session_is_counted_on_the_day_it_last_ran(self):
        """Bucketing by start date dumps months of spend on one old bar."""
        spanning = report(4_000_000, day="2026-06-01", end="2026-08-14")
        text = render_trend([spanning])
        self.assertIn("2026-08-14", text)
        self.assertNotIn("2026-06-01", text)

    def test_spanning_sessions_are_disclosed(self):
        """Neither attribution is truly correct, so the count is reported."""
        text = render_trend([report(1_000_000, day="2026-06-01", end="2026-08-14")])
        self.assertIn("spanned more than one day", text)

    def test_single_day_sessions_are_not_flagged_as_spanning(self):
        text = render_trend([report(1_000_000, day="2026-08-14")])
        self.assertNotIn("spanned more than one day", text)

    def test_empty_input_does_not_crash(self):
        self.assertIn("No sessions", render_trend([]))


class TestAnalysisCLI(unittest.TestCase):
    def test_top_runs(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli("--root", fixture.root, "--top")
        self.assertEqual(code, 0)
        self.assertIn("MOST EXPENSIVE", out)

    def test_top_accepts_a_limit(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, _out, _ = run_cli("--root", fixture.root, "--top", "3")
        self.assertEqual(code, 0)

    def test_trend_runs(self):
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            code, out, _ = run_cli("--root", fixture.root, "--trend")
        self.assertEqual(code, 0)
        self.assertIn("DAILY BURN", out)

    def test_top_scans_widely_rather_than_only_the_last_session(self):
        """--top over one session would be useless."""
        records = [assistant("m", output_tokens=1_000_000)]
        with TranscriptFixture(records) as fixture:
            _code, out, _ = run_cli("--root", fixture.root, "--top")
        self.assertIn("across 1 session(s)", out)


if __name__ == "__main__":
    unittest.main()
