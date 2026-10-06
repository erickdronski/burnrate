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
    FAST_MODE_PRICES,
    PRICES,
    PRICES_AS_OF,
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
        self.assertEqual(resolve_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_dated_sonnet_snapshot(self):
        self.assertEqual(
            resolve_model("claude-sonnet-4-5-20250929"), "claude-sonnet-4-5"
        )

    def test_point_releases_resolve_to_themselves(self):
        """The bug this replaced: prefix matching priced Opus 5.5 as Opus 5.

        With no `claude-opus-5-5` entry, a longest-prefix lookup returned
        `claude-opus-5`, because the newer name starts with the older one, and
        every Opus 5.5 turn was billed at the older model's rates.
        """
        self.assertEqual(resolve_model("claude-opus-5-5"), "claude-opus-5-5")
        self.assertEqual(resolve_model("claude-fable-5-1"), "claude-fable-5-1")
        self.assertEqual(resolve_model("claude-sonnet-5-5"), "claude-sonnet-5-5")
        self.assertEqual(resolve_model("claude-opus-4-8"), "claude-opus-4-8")

    def test_unknown_point_release_is_not_priced_as_its_predecessor(self):
        self.assertIsNone(resolve_model("claude-opus-5-7"))
        self.assertIsNone(resolve_model("claude-fable-5-2-20270101"))

    def test_prefix_of_a_known_name_is_not_a_match(self):
        self.assertIsNone(resolve_model("claude-opus-5-5-preview"))

    def test_deployment_suffixes_are_stripped(self):
        self.assertEqual(resolve_model("claude-opus-4-6[1m]"), "claude-opus-4-6")
        self.assertEqual(resolve_model("claude-opus-4-5@20251101"), "claude-opus-4-5")
        self.assertEqual(
            resolve_model("claude-opus-5-5-20260401[1m]"), "claude-opus-5-5"
        )

    def test_override_names_resolve_exactly(self):
        table = {"gpt-6-astra": {"input": 1, "output": 2}}
        self.assertEqual(resolve_model("gpt-6-astra", table), "gpt-6-astra")
        self.assertIsNone(resolve_model("gpt-6", table))

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

    def test_cache_read_is_priced_per_model(self):
        """The rate that dominates a long session's bill, from the published
        table: 1M cache-read tokens cost exactly the per-model read rate."""
        usage = Usage(cache_read_tokens=1_000_000)
        expected = {
            "claude-fable-5-1": 0.25,  # 0.025x of $10
            "claude-mythos-5-1": 0.25,
            "claude-fable-5": 1.00,  # 0.1x of $10
            "claude-opus-5-5": 0.20,  # 0.05x of $4
            "claude-opus-5": 0.50,  # 0.1x of $5
            "claude-opus-4-8": 0.50,
            "claude-sonnet-5-5": 0.20,  # 0.1x of $2
            "claude-sonnet-5": 0.20,
            "claude-sonnet-4-6": 0.30,
            "claude-haiku-4-5": 0.10,
        }
        for model, dollars in expected.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(price_usage(usage, model), dollars, places=9)

    def test_opus_5_5_is_cheaper_than_opus_5_on_every_axis(self):
        # $4/$20 against $5/$25, and cache reads at $0.20 against $0.50.
        for usage, new, old in (
            (Usage(input_tokens=1_000_000), 4.0, 5.0),
            (Usage(output_tokens=1_000_000), 20.0, 25.0),
            (Usage(cache_read_tokens=1_000_000), 0.20, 0.50),
        ):
            self.assertAlmostEqual(price_usage(usage, "claude-opus-5-5"), new)
            self.assertAlmostEqual(price_usage(usage, "claude-opus-5"), old)

    def test_sonnet_5_is_two_and_ten(self):
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(price_usage(usage, "claude-sonnet-5"), 12.0, places=9)
        self.assertAlmostEqual(price_usage(usage, "claude-sonnet-5-5"), 12.0)
        self.assertAlmostEqual(price_usage(usage, "claude-sonnet-4-6"), 18.0)

    def test_a_realistic_turn_on_opus_5_5(self):
        # 1,000 uncached in at $4        = $0.004
        # 1M cache read at $0.20         = $0.200
        # 10k 5-minute writes at $5.00   = $0.050  (1.25 x $4)
        # 20k 1-hour writes at $8.00     = $0.160  (2 x $4)
        # 5k output at $20               = $0.100
        usage = Usage(
            input_tokens=1_000,
            output_tokens=5_000,
            cache_read_tokens=1_000_000,
            cache_write_5m_tokens=10_000,
            cache_write_1h_tokens=20_000,
        )
        self.assertAlmostEqual(price_usage(usage, "claude-opus-5-5"), 0.514, places=9)

    def test_a_realistic_turn_on_fable_5_1(self):
        # 2M cache read at $0.25 = $0.50; 100k 1-hour writes at $20 = $2.00;
        # 10k output at $50 = $0.50. Total $3.00.
        usage = Usage(
            output_tokens=10_000,
            cache_read_tokens=2_000_000,
            cache_write_1h_tokens=100_000,
        )
        self.assertAlmostEqual(price_usage(usage, "claude-fable-5-1"), 3.0, places=9)

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
        five_minute = price_usage(
            Usage(cache_write_5m_tokens=1_000_000), "claude-opus-5"
        )
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

    def test_opus_5_5_fast_mode(self):
        # $8 / $40. The cache-read rate is not published; it is the model's
        # 0.05x ratio applied to the fast input rate: $0.40.
        self.assertAlmostEqual(
            price_usage(Usage(input_tokens=1_000_000), "claude-opus-5-5", None, True),
            8.0,
        )
        self.assertAlmostEqual(
            price_usage(Usage(output_tokens=1_000_000), "claude-opus-5-5", None, True),
            40.0,
        )
        self.assertAlmostEqual(
            price_usage(
                Usage(cache_read_tokens=1_000_000), "claude-opus-5-5", None, True
            ),
            0.40,
        )
        # Cache writes scale from the fast input rate: 1M 1-hour = 2 x $8.
        self.assertAlmostEqual(
            price_usage(
                Usage(cache_write_1h_tokens=1_000_000), "claude-opus-5-5", None, True
            ),
            16.0,
        )

    def test_fast_turn_on_a_model_without_fast_pricing_is_unpriced(self):
        """Opus 4.7 fast mode was removed. A fast turn there is a gap, not a
        standard-rate turn."""
        usage = Usage(output_tokens=1_000)
        self.assertIsNone(price_usage(usage, "claude-opus-4-7", fast_mode=True))
        self.assertNotIn("claude-opus-4-7", FAST_MODE_PRICES)

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

    def test_override_without_cache_read_reads_at_a_tenth(self):
        """Backwards compatible: an entry without `cache_read` behaves as
        every entry did before cache reads were priced per model."""
        usage = Usage(cache_read_tokens=1_000_000)
        result = price_usage(
            usage, "my-model", prices={"my-model": {"input": 3, "output": 15}}
        )
        self.assertAlmostEqual(result, 3.0 * CACHE_READ_MULTIPLIER, places=9)

    def test_override_cache_read_is_used(self):
        usage = Usage(cache_read_tokens=1_000_000, input_tokens=1_000_000)
        result = price_usage(
            usage,
            "my-model",
            prices={"my-model": {"input": 3, "output": 15, "cache_read": 0.075}},
        )
        self.assertAlmostEqual(result, 3.075, places=9)

    def test_override_applies_to_fast_turns_too(self):
        usage = Usage(output_tokens=1_000_000)
        result = price_usage(
            usage,
            "claude-opus-5",
            prices={"claude-opus-5": {"input": 1, "output": 2}},
            fast_mode=True,
        )
        self.assertAlmostEqual(result, 2.0, places=6)

    def test_override_can_add_an_unknown_model(self):
        usage = Usage(output_tokens=1_000_000)
        result = price_usage(
            usage,
            "my-local-model",
            prices={"my-local-model": {"input": 0, "output": 1}},
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

    def test_fast_counterfactual_uses_the_fast_rate(self):
        # Otherwise a fast session's "without caching" figure can come out
        # below what it actually cost.
        usage = Usage(cache_read_tokens=1_000_000)
        self.assertAlmostEqual(
            uncached_equivalent(usage, "claude-opus-5-5", fast_mode=True), 8.0
        )

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
        # delete=False is required: the file is read back by path.
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_wrapped_form(self):
        path = self.write({"prices": {"m": {"input": 1, "output": 2}}})
        self.assertEqual(
            load_price_overrides(path), {"m": {"input": 1.0, "output": 2.0}}
        )

    def test_bare_form(self):
        path = self.write({"m": {"input": 1, "output": 2}})
        self.assertEqual(
            load_price_overrides(path), {"m": {"input": 1.0, "output": 2.0}}
        )

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

    def test_optional_cache_read(self):
        path = self.write({"m": {"input": 1, "output": 2, "cache_read": 0.05}})
        self.assertEqual(
            load_price_overrides(path),
            {"m": {"input": 1.0, "output": 2.0, "cache_read": 0.05}},
        )

    def test_cached_input_is_a_synonym_for_cache_read(self):
        """OpenAI's price sheets call it "cached input"."""
        path = self.write({"gpt-x": {"input": 1, "cached_input": 0.1, "output": 8}})
        self.assertEqual(load_price_overrides(path)["gpt-x"]["cache_read"], 0.1)

    def test_non_numeric_cache_read_is_rejected(self):
        path = self.write({"m": {"input": 1, "output": 2, "cache_read": "cheap"}})
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

    def test_every_entry_states_its_cache_read_rate(self):
        """A default would quietly apply 0.1x to a model that reads at 0.025x."""
        for table in (PRICES, FAST_MODE_PRICES):
            for model, rate in table.items():
                with self.subTest(model=model):
                    self.assertIn("cache_read", rate)
                    self.assertGreater(rate["cache_read"], 0)
                    self.assertLess(rate["cache_read"], rate["input"])

    def test_cache_read_ratios_match_the_published_ratios(self):
        ratios = {
            "claude-fable-5-1": 0.025,
            "claude-mythos-5-1": 0.025,
            "claude-opus-5-5": 0.05,
            "claude-fable-5": 0.1,
            "claude-opus-5": 0.1,
            "claude-sonnet-5-5": 0.1,
            "claude-sonnet-5": 0.1,
            "claude-haiku-4-5": 0.1,
        }
        for model, ratio in ratios.items():
            with self.subTest(model=model):
                rate = PRICES[model]
                self.assertAlmostEqual(rate["cache_read"] / rate["input"], ratio)

    def test_the_table_is_dated(self):
        self.assertEqual(PRICES_AS_OF, "2026-09-25")


if __name__ == "__main__":
    unittest.main()
