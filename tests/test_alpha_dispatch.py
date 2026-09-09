# -*- coding: utf-8 -*-
"""Alpha Gateway sections 4, 5, 17 and 18 — parallel dispatch and isolation.

THE INVARIANTS
    1. Providers are dispatched CONCURRENTLY. The wall clock of a cycle is
       the analysis budget, not the sum of the providers.
    2. No provider is mandatory. Any one of them failing, timing out,
       raising, or returning nonsense leaves the others intact.
    3. A missing model output is NO SIGNAL. It is never 0.5.
    4. The market is priced at dispatch (T0) and at completion (T1), so
       latency decay is measured rather than assumed.
    5. No model can modify the immutable snapshot.
"""
import json
import os
import sys
import threading
import time
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider, valid_payload     # noqa: E402

from alpha_dispatcher import dispatch, quote_movement         # noqa: E402
from alpha_providers import AlphaProvider                     # noqa: E402
from alpha_snapshot import SnapshotError                      # noqa: E402
from config import CFG                                        # noqa: E402


class _Hanging(AlphaProvider):
    """Ignores its budget entirely -- the case an adapter-level timeout
    alone would not catch."""
    env_key = None

    def __init__(self, name="hang", seconds=30.0):
        self.name, self.seconds = name, seconds
        super().__init__()

    def default_model(self):
        return self.name

    def configured(self):
        return True

    def analyze(self, snapshot, timeout):
        time.sleep(self.seconds)
        return None, {"latency_ms": 0, "cost": {}, "error": None}


class _Recording(AlphaProvider):
    """Records the thread it ran on, to prove concurrency rather than
    assume it."""
    env_key = None

    def __init__(self, name, delay=0.25):
        self.name, self.delay = name, delay
        self.thread = None
        self.started_at = None
        super().__init__()

    def default_model(self):
        return self.name

    def configured(self):
        return True

    def analyze(self, snapshot, timeout):
        self.thread = threading.current_thread().name
        self.started_at = time.monotonic()
        time.sleep(self.delay)
        return json.dumps(valid_payload(snapshot, model=self.name)), \
            {"latency_ms": int(self.delay * 1000), "cost": {}, "error": None}


class ProvidersRunInParallel(AlphaCase):

    def test_four_slow_providers_cost_one_delay_not_four(self):
        snapshot = self.snapshot()
        providers = [_Recording(f"p{i}", delay=0.30) for i in range(4)]
        started = time.monotonic()
        result = dispatch(snapshot, providers)
        elapsed = time.monotonic() - started
        self.assertEqual(len(result.valid), 4, result.as_dict())
        # Sequential would be >= 1.2s. Generous bound: the assertion is
        # "parallel", not "fast on a loaded CI box".
        self.assertLess(elapsed, 0.9,
                        f"providers ran sequentially ({elapsed:.2f}s)")

    def test_each_provider_runs_on_its_own_thread(self):
        snapshot = self.snapshot()
        providers = [_Recording(f"p{i}", delay=0.05) for i in range(4)]
        dispatch(snapshot, providers)
        threads = {p.thread for p in providers}
        self.assertEqual(len(threads), 4, f"shared threads: {threads}")


class OneProviderNeverBlocksAnother(AlphaCase):
    """Section 18, stated as four separate cases because "no single provider
    is mandatory" has to hold for EACH of them, not on average."""

    def setUp(self):
        super().setUp()
        # A short FAST budget so the four hang cases cost seconds, not half a
        # minute. The deadline VALUE is configuration (section 5); the
        # property under test -- that a hung provider is abandoned at the
        # deadline instead of awaited -- does not depend on its size, and
        # `test_market_class_selects_the_budget` pins the shipped ordering.
        # Two seconds, not a fraction: snapshot timestamps are
        # second-resolution, so a sub-second budget is not expressible.
        self._patches.append(patch.object(CFG, "ALPHA_DEADLINE_FAST_S", 2.0))
        self._patches[-1].start()

    def budgeted_snapshot(self):
        return self.snapshot(minutes_to_resolution=10)

    def run_with_one_hanging(self, hanging_name):
        names = ["grok", "gemini", "openai", "atlas_quant"]
        providers = []
        for name in names:
            if name == hanging_name:
                providers.append(_Hanging(name, seconds=5.0))
            else:
                providers.append(FakeProvider(name))
        started = time.monotonic()
        result = dispatch(self.budgeted_snapshot(), providers)
        return result, time.monotonic() - started

    def test_grok_timeout_does_not_block_gemini(self):
        result, elapsed = self.run_with_one_hanging("grok")
        self.assertEqual({s.model for s in result.valid},
                         {"gemini", "openai", "atlas_quant"})
        self.assertLess(elapsed, float(CFG.ALPHA_DEADLINE_FAST_S) + 3)

    def test_gemini_timeout_does_not_block_openai(self):
        result, _ = self.run_with_one_hanging("gemini")
        self.assertIn("openai", {s.model for s in result.valid})
        self.assertEqual(len(result.valid), 3)

    def test_openai_timeout_does_not_block_quant(self):
        result, _ = self.run_with_one_hanging("openai")
        self.assertIn("atlas_quant", {s.model for s in result.valid})
        self.assertEqual(len(result.valid), 3)

    def test_quant_timeout_does_not_block_the_llms(self):
        result, _ = self.run_with_one_hanging("atlas_quant")
        self.assertEqual({s.model for s in result.valid},
                         {"grok", "gemini", "openai"})

    def test_a_hung_provider_is_excluded_as_analysis_timeout(self):
        result, _ = self.run_with_one_hanging("grok")
        excluded = [s for s in result.excluded if s.model == "grok"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0].rejected_reason, "analysis_timeout")
        self.assertIsNone(excluded[0].p_yes)

    def test_a_provider_exception_does_not_crash_the_engine(self):
        def explode(snapshot):
            raise RuntimeError("vendor SDK blew up")
        providers = [FakeProvider("grok", behaviour=explode),
                     FakeProvider("gemini"), FakeProvider("openai")]
        result = dispatch(self.snapshot(), providers)
        self.assertEqual(len(result.valid), 2)
        failed = [s for s in result.excluded if s.model == "grok"][0]
        self.assertEqual(failed.rejected_reason, "provider_exception")
        self.assertIn("vendor SDK blew up", failed.rejected_detail)
        self.assertIsNone(failed.p_yes)

    def test_a_provider_error_never_becomes_a_probability(self):
        """Section 18's central rule, asserted directly."""
        providers = [FakeProvider("grok", error="HTTP 500: upstream"),
                     FakeProvider("gemini"),
                     FakeProvider("openai", behaviour="not json"),
                     FakeProvider("atlas_quant")]
        result = dispatch(self.snapshot(), providers)
        for signal in result.excluded:
            self.assertIsNone(signal.p_yes, signal.model)
            self.assertNotEqual(signal.p_yes, 0.5)
        self.assertEqual({s.model for s in result.valid},
                         {"gemini", "atlas_quant"})

    def test_a_missing_api_key_is_a_failure_not_a_neutral_estimate(self):
        from alpha_providers import GrokProvider
        provider = GrokProvider()
        self.assertFalse(provider.configured())
        result = dispatch(self.snapshot(), [provider, FakeProvider("gemini")])
        excluded = [s for s in result.excluded if s.provider == "grok"][0]
        self.assertIsNone(excluded.p_yes)
        self.assertIn("XAI_API_KEY", excluded.rejected_detail)
        self.assertEqual(len(result.valid), 1)

    def test_every_provider_failing_yields_no_signals_not_a_midpoint(self):
        providers = [FakeProvider(n, error="down")
                     for n in ("grok", "gemini", "openai", "atlas_quant")]
        result = dispatch(self.snapshot(), providers)
        self.assertEqual(result.valid, [])
        self.assertEqual(len(result.excluded), 4)


class DeadlinesAreEnforced(AlphaCase):

    def test_a_deadline_already_passed_calls_nobody(self):
        """No vendor is billed for an answer nobody could have used."""
        snapshot = self.snapshot(
            snapshot_time=datetime.now(timezone.utc) - timedelta(hours=2),
            minutes_to_resolution=240)
        providers = [FakeProvider("grok"), FakeProvider("gemini")]
        result = dispatch(snapshot, providers)
        self.assertEqual(result.valid, [])
        self.assertTrue(all(p.calls == 0 for p in providers))
        self.assertTrue(all(s.rejected_reason == "analysis_timeout"
                            for s in result.excluded))

    def test_market_class_selects_the_budget(self):
        from alpha_snapshot import analysis_budget_seconds
        fast = self.snapshot(minutes_to_resolution=10)
        medium = self.snapshot(minutes_to_resolution=120)
        deep = self.snapshot(minutes_to_resolution=60 * 48)
        self.assertEqual(fast.market_class, "FAST")
        self.assertEqual(medium.market_class, "MEDIUM")
        self.assertEqual(deep.market_class, "DEEP")
        self.assertLess(analysis_budget_seconds("FAST"),
                        analysis_budget_seconds("MEDIUM"))
        self.assertLess(analysis_budget_seconds("MEDIUM"),
                        analysis_budget_seconds("DEEP"))

    def test_the_budget_handed_to_a_provider_is_the_remaining_time(self):
        seen = {}

        class _Budget(AlphaProvider):
            env_key = None
            name = "budget"

            def default_model(self):
                return "budget"

            def configured(self):
                return True

            def analyze(self, snapshot, timeout):
                seen["timeout"] = timeout
                return None, {"latency_ms": 0, "cost": {}, "error": "noop"}

        snapshot = self.snapshot(minutes_to_resolution=10)
        dispatch(snapshot, [_Budget()])
        self.assertGreater(seen["timeout"], 0)
        self.assertLessEqual(seen["timeout"], float(CFG.ALPHA_DEADLINE_FAST_S))


class MarketMovementIsMeasured(AlphaCase):
    """Section 17: T0 and T1 prices, so latency decay is a measurement."""

    def test_dispatch_and_completion_quotes_are_both_recorded(self):
        quotes = [{"yes_bid": 0.44, "yes_ask": 0.45, "no_bid": 0.54, "no_ask": 0.55},
                  {"yes_bid": 0.53, "yes_ask": 0.54, "no_bid": 0.45, "no_ask": 0.46}]
        calls = {"n": 0}

        def quote_fn():
            quote = quotes[min(calls["n"], 1)]
            calls["n"] += 1
            return quote

        result = dispatch(self.snapshot(), [FakeProvider("grok")],
                          quote_fn=quote_fn)
        movement = quote_movement(result)
        self.assertTrue(movement["measured"])
        self.assertAlmostEqual(movement["dispatch_yes_ask"], 0.45, places=6)
        self.assertAlmostEqual(movement["completion_yes_ask"], 0.54, places=6)
        self.assertAlmostEqual(movement["delta_yes_ask"], 0.09, places=6)

    def test_an_unmeasured_move_is_never_reported_as_no_move(self):
        result = dispatch(self.snapshot(), [FakeProvider("grok")],
                          quote_fn=None)
        movement = quote_movement(result)
        self.assertFalse(movement["measured"])
        self.assertIsNone(movement["delta_yes_ask"])

    def test_a_failing_quote_does_not_end_the_cycle(self):
        def quote_fn():
            raise RuntimeError("book unavailable")
        result = dispatch(self.snapshot(), [FakeProvider("grok")],
                          quote_fn=quote_fn)
        self.assertEqual(len(result.valid), 1)
        self.assertFalse(quote_movement(result)["measured"])


class TheSnapshotIsImmutable(AlphaCase):
    """Four models must be answering the SAME question, or the ensemble is
    comparing incomparable things."""

    def test_a_provider_cannot_mutate_the_snapshot_object(self):
        snapshot = self.snapshot()
        with self.assertRaises(Exception):
            snapshot.yes_ask = 0.99
        with self.assertRaises(Exception):
            snapshot.contract_id = "OTHER"

    def test_a_provider_mutating_its_copy_cannot_reach_the_original(self):
        mutated = {}

        class _Vandal(AlphaProvider):
            env_key = None
            name = "vandal"

            def default_model(self):
                return "vandal"

            def configured(self):
                return True

            def analyze(self, snap, timeout):
                view = snap.for_provider()
                view["yes_ask"] = 0.01
                view["contract_id"] = "HIJACKED"
                view["next_known_catalyst"]["time_utc"] = None
                mutated.update(view)
                return json.dumps(valid_payload(snap, model="vandal")), \
                    {"latency_ms": 1, "cost": {}, "error": None}

        snapshot = self.snapshot(catalyst_in=3600)
        original_ask = snapshot.yes_ask
        original_catalyst = snapshot.next_known_catalyst.time_utc
        result = dispatch(snapshot, [_Vandal()])
        self.assertEqual(mutated["contract_id"], "HIJACKED")     # its copy
        self.assertAlmostEqual(snapshot.yes_ask, original_ask)   # not ours
        self.assertEqual(snapshot.contract_id, self.snapshot().contract_id)
        self.assertEqual(snapshot.next_known_catalyst.time_utc,
                         original_catalyst)
        snapshot.verify()
        self.assertEqual(len(result.valid), 1)

    def test_a_tampered_snapshot_fails_its_own_identity_check(self):
        """Identity is DERIVED from content, so tampering is detectable even
        across a serialization boundary."""
        from alpha_snapshot import snapshot_from_dict
        snapshot = self.snapshot()
        payload = snapshot.as_dict()
        payload["yes_ask"] = 0.01
        with self.assertRaises(SnapshotError) as ctx:
            snapshot_from_dict(payload)
        self.assertIn("altered", str(ctx.exception))

    def test_an_untampered_snapshot_round_trips(self):
        from alpha_snapshot import snapshot_from_dict
        snapshot = self.snapshot(catalyst_in=1800)
        restored = snapshot_from_dict(snapshot.as_dict())
        self.assertEqual(restored.market_snapshot_id,
                         snapshot.market_snapshot_id)
        self.assertEqual(restored.next_known_catalyst.time_utc,
                         snapshot.next_known_catalyst.time_utc)


if __name__ == "__main__":
    import unittest
    unittest.main()
