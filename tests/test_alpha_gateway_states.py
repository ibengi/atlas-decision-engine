# -*- coding: utf-8 -*-
"""Alpha Gateway sections 1, 16 and 17 — end to end, every terminal state.

Each of the eight states in section 16 is produced by a real cycle through
`AlphaGateway.analyze`, and each is asserted to be an OBSERVATION: nothing
is executed, nothing is authorized, and the record says so.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider, valid_payload     # noqa: E402

from alpha_gateway import (STATE_DISAGREEMENT, STATE_INSUFFICIENT,  # noqa: E402
                           STATE_MARKET_MOVED, STATE_NO_EDGE,
                           STATE_POSITIVE_HIGH, STATE_POSITIVE_LOW,
                           STATE_STALE, STATE_TIMEOUT, SHADOW_STATES,
                           AlphaGateway)
from alpha_ledger import AlphaLedger                          # noqa: E402
from config import CFG                                        # noqa: E402


def answering(name, probability, *, width=0.03, confidence=0.85):
    return FakeProvider(name, behaviour=lambda s, p=probability: json.dumps(
        valid_payload(s, p_yes=p, low=max(0.0, p - width),
                      high=min(1.0, p + width), model=name,
                      confidence=confidence)))


class GatewayStates(AlphaCase):

    def run_cycle(self, providers, snapshot=None, quote_fn=None, **kw):
        gateway = AlphaGateway(providers=providers, ledger=AlphaLedger(), **kw)
        return gateway.analyze(snapshot or self.snapshot(), quote_fn=quote_fn)

    def assert_observation_only(self, opportunity):
        self.assertIn(opportunity["state"], SHADOW_STATES)
        self.assertIs(opportunity["executed"], False)
        self.assertIs(opportunity["execution_authorized"], False)
        self.assertTrue(opportunity["state_reason"],
                        "a state must carry the sentence that justifies it")

    # ── the eight states ────────────────────────────────────────────────
    def test_no_edge(self):
        """The ensemble agrees with the market, so there is nothing here."""
        snapshot = self.snapshot(yes_ask=0.60, yes_bid=0.58,
                                 no_ask=0.42, no_bid=0.40)
        opportunity = self.run_cycle(
            [answering("grok", 0.60), answering("gemini", 0.59),
             answering("openai", 0.61), answering("atlas_quant", 0.60)],
            snapshot)
        self.assertEqual(opportunity["state"], STATE_NO_EDGE)
        self.assertLessEqual(opportunity["shadow_net_edge"], 0)
        self.assert_observation_only(opportunity)

    def test_positive_edge_high_confidence(self):
        snapshot = self.snapshot(yes_ask=0.30, yes_bid=0.28,
                                 no_ask=0.72, no_bid=0.70)
        opportunity = self.run_cycle(
            [answering("grok", 0.80, confidence=0.95),
             answering("gemini", 0.81, confidence=0.95),
             answering("openai", 0.79, confidence=0.95),
             answering("atlas_quant", 0.80, confidence=0.95)], snapshot)
        self.assertEqual(opportunity["state"], STATE_POSITIVE_HIGH)
        self.assertGreater(opportunity["shadow_net_edge"], 0)
        self.assertGreaterEqual(opportunity["confidence"],
                                float(CFG.ALPHA_HIGH_CONFIDENCE))
        self.assert_observation_only(opportunity)

    def test_positive_edge_low_confidence(self):
        snapshot = self.snapshot(yes_ask=0.30, yes_bid=0.28,
                                 no_ask=0.72, no_bid=0.70)
        opportunity = self.run_cycle(
            [answering("grok", 0.75, width=0.10, confidence=0.30),
             answering("gemini", 0.74, width=0.10, confidence=0.30),
             answering("openai", 0.76, width=0.10, confidence=0.30)], snapshot)
        self.assertEqual(opportunity["state"], STATE_POSITIVE_LOW)
        self.assertGreater(opportunity["shadow_net_edge"], 0)
        self.assertLess(opportunity["confidence"],
                        float(CFG.ALPHA_HIGH_CONFIDENCE))
        self.assert_observation_only(opportunity)

    def test_insufficient_data_when_too_few_models_answer(self):
        """Section 18: missing is NO SIGNAL, and one opinion is not a
        consensus."""
        opportunity = self.run_cycle(
            [answering("grok", 0.80),
             FakeProvider("gemini", error="down"),
             FakeProvider("openai", error="down")])
        self.assertEqual(opportunity["state"], STATE_INSUFFICIENT)
        self.assertIn("valid model signal", opportunity["state_reason"])
        self.assert_observation_only(opportunity)

    def test_insufficient_data_when_no_model_answers(self):
        opportunity = self.run_cycle(
            [FakeProvider(n, error="down") for n in ("grok", "gemini")])
        self.assertEqual(opportunity["state"], STATE_INSUFFICIENT)
        self.assertIsNone(opportunity["p_meta"])
        self.assertNotEqual(opportunity["p_meta"], 0.5)
        self.assert_observation_only(opportunity)

    def test_model_disagreement(self):
        """Section 9: large disagreement is itself useful information, and
        it is its own terminal state rather than an averaged-away number."""
        snapshot = self.snapshot(yes_ask=0.30, yes_bid=0.28)
        opportunity = self.run_cycle(
            [answering("grok", 0.20), answering("gemini", 0.85),
             answering("openai", 0.35), answering("atlas_quant", 0.75)],
            snapshot)
        self.assertEqual(opportunity["state"], STATE_DISAGREEMENT)
        self.assertGreaterEqual(opportunity["disagreement"],
                                float(CFG.ALPHA_DISAGREEMENT_MAX))
        # the individual opinions survive into the record
        self.assertEqual(len(opportunity["per_model"]), 4)
        self.assert_observation_only(opportunity)

    def test_analysis_timeout(self):
        snapshot = self.snapshot(
            snapshot_time=datetime.now(timezone.utc) - timedelta(hours=2),
            minutes_to_resolution=240)
        opportunity = self.run_cycle([answering("grok", 0.80)], snapshot)
        self.assertEqual(opportunity["state"], STATE_TIMEOUT)
        self.assert_observation_only(opportunity)

    def test_stale_when_the_snapshot_expires_during_analysis(self):
        """A signal must never remain valid through a known catalyst."""
        snapshot = self.snapshot(minutes_to_resolution=240, catalyst_in=1800)
        after_catalyst = snapshot.catalyst_time + timedelta(seconds=1)
        opportunity = self.run_cycle(
            [answering("grok", 0.80), answering("gemini", 0.79)],
            snapshot, now_fn=lambda: after_catalyst)
        self.assertIn(opportunity["state"], (STATE_STALE, STATE_TIMEOUT))
        self.assert_observation_only(opportunity)

    def test_market_moved(self):
        """Section 17: the apparent alpha disappeared before the analysis
        completed. This is the direct measurement of latency decay."""
        snapshot = self.snapshot(yes_ask=0.30, yes_bid=0.28)
        quotes = [{"yes_bid": 0.28, "yes_ask": 0.30,
                   "no_bid": 0.70, "no_ask": 0.72},
                  {"yes_bid": 0.80, "yes_ask": 0.82,     # ran away from us
                   "no_bid": 0.18, "no_ask": 0.20}]
        calls = {"n": 0}

        def quote_fn():
            quote = quotes[min(calls["n"], 1)]
            calls["n"] += 1
            return quote

        opportunity = self.run_cycle(
            [answering("grok", 0.80), answering("gemini", 0.81),
             answering("openai", 0.79)], snapshot, quote_fn=quote_fn)
        self.assertEqual(opportunity["state"], STATE_MARKET_MOVED)
        self.assertTrue(opportunity["market_movement"]["measured"])
        self.assertAlmostEqual(
            opportunity["market_movement"]["delta_yes_ask"], 0.52, places=6)
        self.assert_observation_only(opportunity)

    def test_an_unmeasured_move_is_not_reported_as_market_moved(self):
        snapshot = self.snapshot(yes_ask=0.30, yes_bid=0.28)
        opportunity = self.run_cycle(
            [answering("grok", 0.80), answering("gemini", 0.81),
             answering("openai", 0.79)], snapshot, quote_fn=None)
        self.assertNotEqual(opportunity["state"], STATE_MARKET_MOVED)
        self.assertFalse(opportunity["market_movement"]["measured"])


class TheShadowRecordIsComplete(AlphaCase):
    """Section 13's field list, on a real record."""

    def test_every_required_field_is_present(self):
        opportunity = AlphaGateway(
            providers=self.agreeing_providers(),
            ledger=AlphaLedger()).analyze(self.snapshot())
        for field in ("market_snapshot_id", "contract_id", "prediction_time",
                      "p_meta", "market_yes_bid", "market_yes_ask",
                      "market_no_bid", "market_no_ask", "raw_edge",
                      "shadow_net_edge", "model_latency_ms", "model_cost_usd",
                      "actual_outcome", "market_class",
                      "time_to_resolution_s", "per_model", "weights",
                      "state", "state_reason"):
            self.assertIn(field, opportunity, field)
        self.assertIsNone(opportunity["actual_outcome"])
        self.assertEqual(set(opportunity["per_model"]),
                         {"grok", "gemini", "openai", "atlas_quant"})

    def test_the_snapshot_travels_with_the_prediction(self):
        """So the prediction can be audited against the exact market state it
        was made on, without trusting a later lookup."""
        from alpha_snapshot import snapshot_from_dict
        snapshot = self.snapshot()
        opportunity = AlphaGateway(
            providers=self.agreeing_providers(),
            ledger=AlphaLedger()).analyze(snapshot)
        restored = snapshot_from_dict(opportunity["snapshot"])
        self.assertEqual(restored.market_snapshot_id,
                         snapshot.market_snapshot_id)

    def test_excluded_models_are_named_with_their_reason(self):
        opportunity = AlphaGateway(
            providers=[FakeProvider("grok"),
                       FakeProvider("gemini", error="HTTP 503"),
                       FakeProvider("openai", behaviour="{{{")],
            ledger=AlphaLedger()).analyze(self.snapshot())
        excluded = {e["model"]: e["reason"]
                    for e in opportunity["dispatch"]["excluded"]}
        self.assertEqual(excluded["gemini"], "provider_failure")
        self.assertEqual(excluded["openai"], "malformed_json")

    def test_a_ledger_failure_is_loud_but_does_not_change_the_verdict(self):
        from alpha_ledger import LedgerError

        class _Broken(AlphaLedger):
            def record_prediction(self, opportunity):
                raise LedgerError("disk full")

        with self.assertLogs("ALPHA", level="ERROR") as captured:
            opportunity = AlphaGateway(
                providers=self.agreeing_providers(),
                ledger=_Broken()).analyze(self.snapshot())
        self.assertIn("could not persist", "\n".join(captured.output))
        self.assertIs(opportunity["persisted"], False)
        self.assertIn(opportunity["state"], SHADOW_STATES)

    def test_dry_run_writes_nothing(self):
        AlphaGateway(providers=self.agreeing_providers(),
                     ledger=AlphaLedger()).analyze(self.snapshot(),
                                                   record=False)
        self.assertEqual(AlphaLedger().predictions(), [])

    def test_a_tampered_snapshot_is_refused_before_a_token_is_spent(self):
        from alpha_snapshot import MarketSnapshot, SnapshotError
        snapshot = self.snapshot()
        forged = MarketSnapshot(**{**snapshot.as_dict(),
                                   "next_known_catalyst":
                                       snapshot.next_known_catalyst,
                                   "yes_ask": 0.01})
        providers = self.agreeing_providers()
        with self.assertRaises(SnapshotError):
            AlphaGateway(providers=providers,
                         ledger=AlphaLedger()).analyze(forged)
        self.assertTrue(all(p.calls == 0 for p in providers),
                        "providers were called on a tampered snapshot")


if __name__ == "__main__":
    import unittest
    unittest.main()
