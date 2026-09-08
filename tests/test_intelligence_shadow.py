# -*- coding: utf-8 -*-
"""Safety contract for the Atlas Intelligence Network phase-1 shadow layer."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligence.budget import DailyBudgetManager
from intelligence.cache import IntelligenceCache
from intelligence.evidence import IntelligenceEvidenceStore
from intelligence.router import IntelligenceRouter, ShadowOnlyPolicy
from intelligence.schemas import IntelligenceObservation, MarketCandidate


class _Clock:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t


class _Provider:
    def __init__(self, name="astra", probability=0.6, cost=0.02, fail=False):
        self.provider_name = name
        self.cache_ttl_seconds = 30.0
        self.probability = probability
        self.cost = cost
        self.fail = fail
        self.calls = 0

    def estimate_cost_usd(self, candidate):
        return self.cost

    def analyze(self, candidate):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return IntelligenceObservation(
            contract_id=candidate.contract_id,
            provider=self.provider_name,
            model="test-model",
            probability=self.probability,
            confidence=0.8,
            cost_usd=self.cost,
            latency_ms=12.0,
        )


class TestSchemas(unittest.TestCase):
    def test_observation_is_evidence_not_an_order(self):
        obs = IntelligenceObservation(
            contract_id="KXTEST",
            provider="astra",
            model="m",
            probability=0.61,
            confidence=0.82,
        )
        evidence = obs.as_evidence()
        self.assertTrue(evidence["shadow_only"])
        for forbidden in ("action", "quantity", "position_size", "order_payload"):
            self.assertNotIn(forbidden, evidence)

    def test_probability_must_be_calibratable_number(self):
        with self.assertRaises(ValueError):
            IntelligenceObservation("K", "astra", "m", 1.2, 0.8)

    def test_abstention_has_no_probability(self):
        with self.assertRaises(ValueError):
            IntelligenceObservation("K", "astra", "m", 0.5, 0.1, abstain=True)
        obs = IntelligenceObservation("K", "astra", "m", None, 0.1, abstain=True)
        self.assertTrue(obs.abstain)

    def test_execution_shaped_provider_payload_is_rejected_recursively(self):
        with self.assertRaisesRegex(ValueError, "executable field"):
            IntelligenceObservation.from_mapping({
                "contract_id": "K",
                "provider": "astra",
                "model": "m",
                "probability": 0.6,
                "confidence": 0.8,
                "metadata": {"proposal": {"quantity": 4}},
            })

    def test_candidate_context_cannot_smuggle_order_payload(self):
        with self.assertRaisesRegex(ValueError, "execution-shaped"):
            MarketCandidate("K", 0.5, context={"order_payload": {"x": 1}})


class TestShadowPolicy(unittest.TestCase):
    def test_default_policy_has_zero_trade_authority(self):
        with patch.dict(os.environ, {}, clear=True):
            policy = ShadowOnlyPolicy.from_environment()
        self.assertFalse(policy.can_influence_trade)
        self.assertFalse(policy.can_size_position)
        self.assertFalse(policy.can_submit_order)

    def test_attempt_to_arm_trade_influence_fails_closed(self):
        with patch.dict(os.environ, {"AI_CAN_INFLUENCE_TRADE": "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "shadow-only boundary"):
                ShadowOnlyPolicy.from_environment()

    def test_phase_one_rejects_advisory_or_capital_mode(self):
        with patch.dict(os.environ, {"AI_MODE": "ADVISORY"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "supports SHADOW only"):
                ShadowOnlyPolicy.from_environment()


class TestBudgetAndCache(unittest.TestCase):
    def test_provider_budget_is_atomic(self):
        b = DailyBudgetManager(10.0, {"astra": 0.05})
        self.assertTrue(b.reserve("astra", 0.03).allowed)
        denied = b.reserve("astra", 0.03)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "provider_daily_budget")

    def test_settle_replaces_estimate_with_actual(self):
        b = DailyBudgetManager(10.0)
        self.assertTrue(b.reserve("astra", 0.10).allowed)
        b.settle("astra", 0.10, 0.04)
        self.assertAlmostEqual(b.snapshot()["global_spend_usd"], 0.04)

    def test_cache_never_returns_stale_opinion_as_fresh(self):
        clock = _Clock()
        cache = IntelligenceCache(now_fn=clock.monotonic)
        obs = IntelligenceObservation("K", "astra", "m", 0.6, 0.8)
        cache.put(obs, 5.0)
        self.assertIs(cache.get("astra", "K"), obs)
        clock.t += 6.0
        self.assertIsNone(cache.get("astra", "K"))
        self.assertFalse(cache.peek("astra", "K")["fresh"])


class TestEvidenceStore(unittest.TestCase):
    def test_append_only_row_contains_market_and_provider_prediction(self):
        candidate = MarketCandidate("KXTEST", 0.42, category="btc")
        obs = IntelligenceObservation(
            "KXTEST", "astra", "m", 0.61, 0.82, cost_usd=0.02, latency_ms=15
        )
        with tempfile.TemporaryDirectory() as td:
            store = IntelligenceEvidenceStore(td, fsync=False)
            ids = store.append(candidate, [obs])
            lines = Path(td, "ai_shadow_observations.jsonl").read_text().splitlines()
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["contract_id"], "KXTEST")
        self.assertEqual(row["market_probability"], 0.42)
        self.assertEqual(row["observation"]["probability"], 0.61)
        self.assertEqual(row["observation"]["cost_usd"], 0.02)
        self.assertTrue(row["observation"]["shadow_only"])
        self.assertEqual(row["evidence_id"], ids[0])

    def test_multiple_appends_never_replace_previous_evidence(self):
        candidate = MarketCandidate("KXTEST", 0.42)
        with tempfile.TemporaryDirectory() as td:
            store = IntelligenceEvidenceStore(td, fsync=False)
            store.append(candidate, [IntelligenceObservation("KXTEST", "astra", "m", 0.6, 0.8)])
            store.append(candidate, [IntelligenceObservation("KXTEST", "gemini", "m", 0.55, 0.7)])
            lines = Path(td, "ai_shadow_observations.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["observation"]["provider"], "astra")
        self.assertEqual(json.loads(lines[1])["observation"]["provider"], "gemini")

    def test_contract_mismatch_is_refused_before_write(self):
        candidate = MarketCandidate("A", 0.5)
        obs = IntelligenceObservation("B", "astra", "m", 0.6, 0.8)
        with tempfile.TemporaryDirectory() as td:
            store = IntelligenceEvidenceStore(td, fsync=False)
            with self.assertRaises(ValueError):
                store.append(candidate, [obs])
            self.assertFalse(Path(td, "ai_shadow_observations.jsonl").exists())


class TestRouter(unittest.TestCase):
    def candidate(self):
        return MarketCandidate("KXTEST", 0.42, title="test")

    def router(self, provider, budget=1.0, cache=None):
        return IntelligenceRouter(
            [provider],
            DailyBudgetManager(budget, {provider.provider_name: budget}),
            cache=cache,
            policy=ShadowOnlyPolicy(),
        )

    def test_provider_failure_is_fail_soft(self):
        p = _Provider(fail=True)
        r = self.router(p)
        self.assertEqual(r.collect(self.candidate()), [])
        self.assertEqual(r.snapshot()["stats"]["provider_errors"], 1)
        self.assertEqual(r.snapshot()["budget"]["global_spend_usd"], 0.0)

    def test_cache_removes_provider_latency_from_second_decision(self):
        p = _Provider()
        r = self.router(p)
        first = r.collect(self.candidate())
        second = r.collect(self.candidate())
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(p.calls, 1)
        self.assertEqual(r.snapshot()["stats"]["cache_hits"], 1)

    def test_budget_exhaustion_skips_call_instead_of_harming_engine(self):
        p = _Provider(cost=0.20)
        r = self.router(p, budget=0.10)
        self.assertEqual(r.collect(self.candidate()), [])
        self.assertEqual(p.calls, 0)
        self.assertEqual(r.snapshot()["stats"]["budget_skips"], 1)

    def test_provider_identity_mismatch_is_rejected_and_reservation_released(self):
        class Bad(_Provider):
            def analyze(self, candidate):
                self.calls += 1
                return IntelligenceObservation(candidate.contract_id, "other", "m", 0.6, 0.8)

        p = Bad(name="astra", cost=0.03)
        r = self.router(p)
        self.assertEqual(r.collect(self.candidate()), [])
        self.assertEqual(r.snapshot()["budget"]["global_spend_usd"], 0.0)

    def test_duplicate_provider_names_are_rejected(self):
        with self.assertRaises(ValueError):
            IntelligenceRouter(
                [_Provider("astra"), _Provider("astra")],
                DailyBudgetManager(1.0),
                policy=ShadowOnlyPolicy(),
            )


if __name__ == "__main__":
    unittest.main()
