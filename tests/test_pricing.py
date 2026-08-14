"""Tests for the pricing math.

Expected values are hand-computed from the published per-million rates rather
than snapshotted, because a snapshot would happily lock in a wrong cache
multiplier — the single most likely error in this file.
"""

import json
import os
import tempfile
import unittest

from burnrate.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    PRICES,
    PricingError,
    Usage,
    load_price_overrides,
    price_usage,
    resolve_model,
    uncached_equivalent,
)


class TestUsage(unittest.TestCase):
    def test_totals(self):
        usage = Usage(100, 200, 300, 400, 500)
        self.assertEqual(usage.total_tokens, 1500)

    def test_total_input_excludes_output(self):
        usage = Usage(100, 999, 300, 400, 500)
        self.assertEqual(usage.total_input_tokens, 1300)

    def test_add_accumulates_every_field(self):
        a = Usage(1, 2, 3, 4, 5)
        a.add(Usage(10, 20, 30, 40, 50))
        self.assertEqual(
            (
                a.input_tokens,
                a.output_tokens,
                a.cache_read_tokens,
                a.cache_write_5m_tokens,
                a.cache_write_1h_tokens,
            ),
            (11, 22, 33, 44, 55),
        )


class TestResolveModel(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(resolve_model("claude-opus-5"), "claude-opus-5")

    def test_dated_snapshot_resolves_to_base(self):
        self.assertEqual(
            resolve_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5"
        )

    def test_longest_prefix_wins(self):
        # "claude-opus-4-8" must not resolve via a shorter "claude-opus-4" key.
        self.assertEqual(resolve_model("claude-opus-4-8"), "claude-opus-4-8")

    def test_unknown_returns_none_rather_than_guessing(self):
        self.assertIsNone(resolve_model("some-other-vendor-model"))

    def test_synthetic_is_not_billable(self):
        self.assertIsNone(resolve_model("<synthetic>"))

    def test_empty_and_none(self):
        self.assertIsNone(resolve_model(None))
        self.assertIsNone(resolve_model(""))


class TestPriceUsage(unittest.TestCase):
    def test_plain_input_and_output(self):
        # Opus 5: $5/MTok in, $25/MTok out.
        # 1M in = $5.00; 100k out = $2.50. Total $7.50.
        usage = Usage(input_tokens=1_000_000, output_tokens=100_000)
        self.assertAlmostEqual(price_usage(usage, "claude-opus-5"), 7.50, places=6)

    def test_cache_read_is_a_tenth_of_input(self):
        usage = Usage(cache_read_tokens=1_000_000)
        self.assertAlmostEqual(
            price_usage(usage, "claude-opus-5"), 5.0 * CACHE_READ_MULTIPLIER, places=6
        )

    def test_five_minute_cache_write_is_1_25x(self):
        usage = Usage(cache_write_5m_tokens=1_000_000)
        self.assertAlmostEqual(
            price_usage(usage, "claude-opus-5"),
            5.0 * CACHE_WRITE_5M_MULTIPLIER,
            places=6,
        )

    def test_one_hour_cache_write_is_2x(self):
        usage = Usage(cache_write_1h_tokens=1_000_000)
        self.assertAlmostEqual(
            price_usage(usage, "claude-opus-5"),
            5.0 * CACHE_WRITE_1H_MULTIPLIER,
            places=6,
        )

    def test_the_two_cache_write_ttls_are_priced_differently(self):
        """The error this whole module exists to avoid.

        A tool that applies one cache-write multiplier is wrong for whichever
        TTL it did not pick. On long sessions 1-hour writes dominate, so
        blending them understates the bill.
        """
        five_minute = price_usage(Usage(cache_write_5m_tokens=1_000_000), "claude-opus-5")
        one_hour = price_usage(Usage(cache_write_1h_tokens=1_000_000), "claude-opus-5")
        self.assertLess(five_minute, one_hour)
        self.assertAlmostEqual(one_hour / five_minute, 1.6, places=6)

    def test_unknown_model_returns_none_not_zero(self):
        """Silently costing an unknown model at zero produces a wrong total."""
        self.assertIsNone(price_usage(Usage(output_tokens=1_000_000), "who-knows"))

    def test_fast_mode_is_priced_higher(self):
        usage = Usage(output_tokens=1_000_000)
        standard = price_usage(usage, "claude-opus-5")
        fast = price_usage(usage, "claude-opus-5", fast_mode=True)
        self.assertAlmostEqual(standard, 25.0, places=6)
        self.assertAlmostEqual(fast, 50.0, places=6)

    def test_model_tiers_are_ordered_as_published(self):
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        haiku = price_usage(usage, "claude-haiku-4-5")
        sonnet = price_usage(usage, "claude-sonnet-5")
        opus = price_usage(usage, "claude-opus-5")
        fable = price_usage(usage, "claude-fable-5")
        self.assertLess(haiku, sonnet)
        self.assertLess(sonnet, opus)
        self.assertLess(opus, fable)

    def test_overrides_take_precedence(self):
        usage = Usage(output_tokens=1_000_000)
        result = price_usage(
            usage, "claude-opus-5", prices={"claude-opus-5": {"input": 1, "output": 2}}
        )
        self.assertAlmostEqual(result, 2.0, places=6)

    def test_override_can_add_an_unknown_model(self):
        usage = Usage(output_tokens=1_000_000)
        result = price_usage(
            usage, "my-local-model", prices={"my-local-model": {"input": 0, "output": 1}}
        )
        self.assertAlmostEqual(result, 1.0, places=6)

    def test_zero_usage_costs_nothing(self):
        self.assertAlmostEqual(price_usage(Usage(), "claude-opus-5"), 0.0)


class TestUncachedEquivalent(unittest.TestCase):
    def test_reprices_every_cached_token_at_full_input_rate(self):
        usage = Usage(cache_read_tokens=1_000_000)
        real = price_usage(usage, "claude-opus-5")
        counterfactual = uncached_equivalent(usage, "claude-opus-5")
        self.assertAlmostEqual(counterfactual, 5.0, places=6)
        self.assertAlmostEqual(real, 0.5, places=6)

    def test_caching_never_appears_to_cost_more(self):
        usage = Usage(
            input_tokens=1000,
            output_tokens=5000,
            cache_read_tokens=2_000_000,
            cache_write_1h_tokens=50_000,
        )
        self.assertGreater(
            uncached_equivalent(usage, "claude-opus-5"),
            price_usage(usage, "claude-opus-5"),
        )

    def test_unknown_model_returns_none(self):
        self.assertIsNone(uncached_equivalent(Usage(output_tokens=1), "nope"))


class TestPriceOverrides(unittest.TestCase):
    def write(self, payload):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_wrapped_form(self):
        path = self.write({"prices": {"m": {"input": 1, "output": 2}}})
        self.assertEqual(load_price_overrides(path), {"m": {"input": 1.0, "output": 2.0}})

    def test_bare_form(self):
        path = self.write({"m": {"input": 1, "output": 2}})
        self.assertEqual(load_price_overrides(path), {"m": {"input": 1.0, "output": 2.0}})

    def test_missing_file(self):
        with self.assertRaises(PricingError):
            load_price_overrides("/nonexistent/prices.json")

    def test_missing_rate_key_is_rejected(self):
        path = self.write({"m": {"input": 1}})
        with self.assertRaises(PricingError):
            load_price_overrides(path)

    def test_non_numeric_rate_is_rejected(self):
        path = self.write({"m": {"input": "free", "output": 2}})
        with self.assertRaises(PricingError):
            load_price_overrides(path)


class TestPriceTableIntegrity(unittest.TestCase):
    def test_every_entry_has_both_rates(self):
        for model, rate in PRICES.items():
            with self.subTest(model=model):
                self.assertIn("input", rate)
                self.assertIn("output", rate)
                self.assertGreater(rate["input"], 0)
                self.assertGreater(rate["output"], 0)

    def test_output_always_costs_more_than_input(self):
        for model, rate in PRICES.items():
            with self.subTest(model=model):
                self.assertGreater(rate["output"], rate["input"])


if __name__ == "__main__":
    unittest.main()
