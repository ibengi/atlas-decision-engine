"""Synthetic temporal/ledger regression tests, never evidence of economic edge."""
import base64
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atlas_v2.domain import Refused, canonical, digest, utc
from atlas_v2.learning import LearningObserver
from atlas_v2.qualification import ORIGIN, TICKER_URL, SERIES_URL, FEE_URL, reference, settlement, refreshed, fee_evidence
from atlas_v2.store import Store


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = "2026-09-26T12:10:01Z"
        self.patches = [patch("atlas_v2.learning.now", lambda: self.clock), patch("atlas_v2.store.now", lambda: self.clock)]
        for p in self.patches: p.start()
        self.store = Store(Path(self.tmp.name)/"learning.sqlite")
        self.q = Store(Path(self.tmp.name)/"qualification.sqlite")
        self.observer = LearningObserver(self.store, "a"*40)
        self.o = {"ticker": "KXBTC15M-TEST-C", "event_id": "KXBTC15M-TEST", "hash": "b"*64,
                  "observed_at": "2026-09-26T12:10:02Z", "close_at": "2026-09-26T12:15:00Z", "bid": "0.59", "ask": "0.61",
                  "receipt_hash": "c"*64, "scan_hash": "d"*64, "raw_market_hash": "e"*64}
        self.clock = "2026-09-26T12:10:03Z"

    def tearDown(self):
        self.store.close(); self.q.close()
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def raw(self, url, data, received=None):
        body = data if isinstance(data, bytes) else canonical(data)
        p = {"url": url, "response_url": url, "method": "GET", "status": 200,
             "started_at": received or self.clock, "received_at": received or self.clock,
             "content_type": "application/pdf" if isinstance(data, bytes) else "application/json", "content_range": None, "transport_complete": True,
             "body_base64": base64.b64encode(body).decode(), "body_sha256": hashlib.sha256(body).hexdigest()}
        return self.q.append("raw:"+digest(p), "Q_RAW", p)

    def label(self, outcome=1, settled="2026-09-26T12:16:00Z"):
        market = {"ticker": self.o["ticker"], "event_ticker": self.o["event_id"], "market_type": "binary",
                  "close_time": self.o["close_at"], "status": "finalized", "result": "yes" if outcome else "no",
                  "is_provisional": False, "settlement_ts": settled, "settlement_value_dollars": str(outcome)}
        r = self.raw(ORIGIN+"/trade-api/v2/markets/"+self.o["ticker"], {"market": market})
        value = settlement(r, self.o)
        return self.q.append("label:"+digest(value), "Q_LABEL", value)

    def decide(self):
        return self.observer.observe([self.o], self.q)

    def test_missing_evidence_retains_five_full_rejected_decisions(self):
        result = self.decide()
        self.assertEqual(result["decisions"], 5)
        self.assertEqual(result["accepted_decisions"], 0)
        self.assertEqual(result["active_models"], [])
        rows = self.store.events("L_DECISION")
        self.assertEqual(sum(e["payload"]["model_probability"] is not None for e in rows), 1)
        for e in rows:
            p = e["payload"]
            self.assertFalse(p["would_submit"])
            self.assertIn("MODEL_APPROVAL_MISSING", p["rejection_reasons"])
            self.assertEqual((p["intended_size"],p["accepted_size"]), (1,0))
            for key in ("economic_reward", "fees", "settlement_result", "hypothetical_realized_pnl", "maximum_adverse_excursion", "maximum_favorable_excursion"):
                self.assertIsNone(p[key])
        self.observer.settle([self.o], self.q)
        self.assertEqual(self.store.events("L_SETTLEMENT"), [])

    def test_restart_preserves_activation_and_deduplicates(self):
        self.decide(); anchor = self.store.anchor()
        self.clock = "2026-09-26T12:11:00Z"
        other = LearningObserver(self.store, "a"*40)
        other.observe([self.o], self.q)
        self.assertEqual(other.activation["activated_at"], "2026-09-26T12:10:01Z")
        self.assertEqual(self.store.anchor(), anchor)
        self.assertEqual(other.status()["decisions"], 5)

    def test_historical_and_stale_cohorts_are_explicit_exclusions(self):
        old = dict(self.o, observed_at="2026-09-25T12:10:02Z",close_at="2026-09-25T12:15:00Z")
        self.observer.observe([old], self.q)
        self.assertEqual(self.store.events("L_EXCLUSION")[0]["payload"]["reason"], "PRE_ACTIVATION_COHORT")
        fresh_event = dict(self.o, event_id="KXBTC15M-OTHER")
        self.clock = "2026-09-26T12:10:08Z"
        self.observer.observe([old,fresh_event], self.q)
        self.assertEqual(len(self.store.events("L_EXCLUSION")), 2)
        self.assertEqual(self.store.events("L_DECISION"), [])

    def test_late_label_adds_diagnostics_without_mutating_decision_or_reward(self):
        self.decide(); before = self.store.events("L_DECISION")
        self.clock = "2026-09-27T12:17:00Z"; self.label()
        result = self.observer.settle([self.o], self.q)
        self.assertEqual(result["settled_decisions"], 5)
        self.assertEqual(before, self.store.events("L_DECISION"))
        rows = self.store.events("L_SETTLEMENT")
        self.assertEqual(sum(e["payload"]["brier_improvement"] is not None for e in rows),1)
        for e in rows:
            self.assertEqual(e["payload"]["knowledge_at"],self.clock)
            self.assertIsNone(e["payload"]["economic_reward"])
            self.assertIsNone(e["payload"]["hypothetical_realized_pnl"])
        before = self.store.anchor(); self.observer.settle([self.o], self.q)
        self.assertEqual(before,self.store.anchor())

    def test_conflict_permanently_invalidates_even_if_later_label_reverts(self):
        self.decide(); self.clock="2026-09-26T12:17:00Z"; self.label(1)
        self.observer.settle([self.o],self.q)
        self.clock="2026-09-26T12:18:00Z"; self.label(0)
        self.observer.settle([self.o],self.q)
        self.clock="2026-09-26T12:19:00Z"; self.label(1)
        result=self.observer.settle([self.o],self.q)
        self.assertEqual(result["invalid_decisions"],5)
        self.assertEqual(result["valid_labelled_decisions"],0)
        self.assertEqual(len(self.store.events("L_SETTLEMENT")),5)
        self.assertEqual(LearningObserver(self.store,"a"*40).status()["invalid_decisions"],5)

    def test_untrusted_label_without_raw_is_not_accepted(self):
        self.decide(); self.clock="2026-09-26T12:17:00Z"
        self.q.append("fabrication","Q_LABEL",{"ticker":self.o["ticker"],"source_receipt":"f"*64,"outcome":1})
        self.observer.settle([self.o],self.q)
        self.assertEqual(len(self.store.events("L_LABEL_REJECTED")),5)
        self.assertEqual(self.store.events("L_SETTLEMENT"),[])

    def test_guard_rejection_is_not_bypass_and_bypass_survives_restart(self):
        self.decide()
        self.assertEqual(self.observer.status()["disqualified_candidates"],0)
        candidate = self.store.events("L_DECISION")[0]["payload"]["candidate_hash"]
        self.observer.disqualify(candidate,"price_cap","immutable audit receipt: attempted cap override")
        with self.assertRaises(Refused): self.observer.disqualify(candidate,"price_cap","")
        other=LearningObserver(self.store,"a"*40)
        new=dict(self.o,event_id="KXBTC15M-NEXT",ticker="KXBTC15M-NEXT-C",hash="f"*64)
        other.observe([self.o,new],self.q)
        chosen=[e for e in self.store.events("L_DECISION") if e["payload"]["event_id"]==new["event_id"] and e["payload"]["candidate_hash"]==candidate]
        self.assertEqual(len(chosen),1)
        self.assertIn("CANDIDATE_DISQUALIFIED",chosen[0]["payload"]["rejection_reasons"])

    def test_future_quote_never_changes_frozen_probability(self):
        prior=dict(self.o,observed_at="2026-09-26T12:09:02Z",bid="0.69",ask="0.71",hash="1"*64)
        future=dict(self.o,observed_at="2026-09-26T12:10:04Z",bid="0.01",ask="0.02",hash="2"*64)
        self.observer.observe([future,self.o,prior],self.q)
        micro=[e["payload"] for e in self.store.events("L_DECISION") if e["payload"]["model_version"]=="microstructure_dislocation_v1"][0]
        self.assertEqual(micro["model_probability"],"0.650")
        self.assertEqual(self.store.events("L_PATH"),[])
        self.clock="2026-09-26T12:10:05Z";self.observer.observe([future,self.o,prior],self.q)
        self.assertEqual(len(self.store.events("L_PATH")),1)
        self.assertIsNone(micro["maximum_adverse_excursion"])

    def test_reference_received_after_observation_cannot_become_decision_feature(self):
        prior = self.raw(TICKER_URL,{"time":"2026-09-26T12:09:02Z","price":"100","trade_id":1},"2026-09-26T12:09:02Z")
        p=reference(prior);self.q.append("ref:prior","Q_REFERENCE",p)
        current=self.raw(TICKER_URL,{"time":"2026-09-26T12:10:02Z","price":"101","trade_id":2})
        p=reference(current);self.q.append("ref:current","Q_REFERENCE",p)
        self.decide()
        row=[e["payload"] for e in self.store.events("L_DECISION") if e["payload"]["model_version"]=="underlying_kalshi_lag_v1"][0]
        self.assertIsNone(row["model_probability"])

    def test_append_deadline_crossing_rolls_back_all_family_decisions(self):
        original=self.store.append
        def append(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[1]=="L_DECISION":self.clock="2026-09-26T12:10:08Z"
            return result
        with patch.object(self.store,"append",append),self.assertRaises(Refused):self.decide()
        self.assertEqual(self.store.events("L_DECISION"),[])
        self.assertEqual(self.observer.status()["decisions"],0)

    def test_native_refresh_and_fee_join_reconstruct_cost_at_refreshed_price(self):
        self.clock="2026-09-26T12:10:01Z"
        series=self.raw(SERIES_URL,{"series":{"ticker":"KXBTC15M","fee_type":"quadratic","fee_multiplier":"1"}})
        schedule=self.raw(FEE_URL,b"%PDF-synthetic-test-not-provider-evidence")
        self.clock="2026-09-26T12:10:03Z"
        market={"ticker":self.o["ticker"],"event_ticker":self.o["event_id"],"market_type":"binary",
                "close_time":self.o["close_at"],"status":"active","updated_time":self.clock,
                "yes_bid_dollars":"0.85","yes_ask_dollars":"0.95","yes_ask_size_fp":"0",
                "price_ranges":[{"start":"0","end":"1","step":"0.01"}]}
        receipt=self.raw(ORIGIN+"/trade-api/v2/markets/"+self.o["ticker"],{"market":market})
        q=refreshed(receipt,self.o);self.q.append("refresh:fixture","Q_REFRESH",q)
        f={"decision_hash":self.o["hash"],"price":self.o["ask"],**fee_evidence(series,schedule,self.o["ask"])}
        self.q.append("fee:fixture","Q_FEE",f)
        self.decide()
        for event in self.store.events("L_DECISION"):
            row=event["payload"]
            self.assertEqual(row["refreshed_ask"],"0.95")
            self.assertEqual(row["refreshed_bid"],"0.85")
            self.assertEqual(row["fees"],"0.02")
            self.assertEqual(row["slippage_assumption"],"0.35")
            self.assertEqual(row["source_errors"],[])
            for guard in ("MODEL_APPROVAL_MISSING","PRICE_CAP","SPREAD_LIMIT","LIQUIDITY"):
                self.assertIn(guard,row["rejection_reasons"])
        self.assertEqual(self.observer.status()["disqualified_candidates"],0)

    def test_disqualification_removes_prior_prediction_training_eligibility(self):
        self.decide();self.clock="2026-09-26T12:17:00Z";self.label()
        self.observer.settle([self.o],self.q)
        self.assertEqual(self.observer.status()["eligible_predictive_predictions"],1)
        candidate=[e["payload"]["candidate_hash"] for e in self.store.events("L_DECISION")
                   if e["payload"]["model_version"]=="time_structure_v1"][0]
        self.observer.disqualify(candidate,"model_approval","audit receipt identifying bypass attempt")
        self.assertEqual(self.observer.status()["eligible_predictive_predictions"],0)
        self.assertEqual(len(self.store.events("L_SETTLEMENT")),5)
        self.clock="2026-09-28T00:00:01Z";self.observer.settle([self.o],self.q)
        checkpoint=self.store.events("L_DATASET_CHECKPOINT")[0]["payload"]
        self.assertEqual(checkpoint["disqualified_candidate_hashes"],[candidate])
        self.assertNotIn("MODEL_APPROVAL_MISSING",checkpoint["retraining_reasons"])
        self.assertNotIn("PROSPECTIVE_OOS_NOT_QUALIFIED",checkpoint["retraining_reasons"])

    def test_complete_day_checkpoint_is_immutable_and_never_starts_trainer(self):
        self.decide();self.clock="2026-09-28T00:00:01Z"
        self.observer.settle([self.o],self.q)
        rows=self.store.events("L_DATASET_CHECKPOINT")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["payload"]["day"],"2026-09-27")
        self.assertEqual(rows[0]["payload"]["retraining_status"],"BLOCKED")
        self.assertFalse(rows[0]["payload"]["trainer_started"])
        self.assertFalse(rows[0]["payload"]["promotion"])


if __name__ == "__main__": unittest.main()
