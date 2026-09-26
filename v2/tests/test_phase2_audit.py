"""Independent lifecycle regressions using native replay and synthetic sources.

These tests never qualify market evidence. Statistical fit/pass stubs isolate
coordinator timing and revocation from the separate multiweek sample-size tests.
"""
import base64
from datetime import timedelta
import hashlib
import unittest
from unittest.mock import patch

import test_phase2 as support
from atlas_v2.data import capture_scan
from atlas_v2.domain import Refused, canonical, digest, utc
from atlas_v2.learning import LearningObserver
from atlas_v2.learning_phase2 import LearningPhase2
from atlas_v2.qualification import ORIGIN, settlement
from atlas_v2.training_protocol import FIT_AT, evaluate_oos, oos_window


class Phase2AuditTests(unittest.TestCase):
    setUp = support.NativeCoordinatorTests.setUp
    tearDown = support.NativeCoordinatorTests.tearDown
    scan = support.NativeCoordinatorTests.scan
    decide = support.NativeCoordinatorTests.decide
    label = support.NativeCoordinatorTests.label

    def _native_rows(self):
        return [{"hash": event["hash"], **event["payload"]}
                for event in self.o.events("OBSERVATION")]

    def _initial_settled(self):
        observations = self.decide()
        self.label()
        self.observer.settle(observations, self.q)
        self.phase.tick()
        return next(row for row in self.phase.rows()
                    if row["family"] == support.FAMILY)

    def _new_native_decision(self, at, name):
        self.clock = at
        ticker, event_id = "KXBTC15M-" + name + "-C", "KXBTC15M-" + name
        close = (utc(at) + timedelta(minutes=5)).isoformat()
        market = {"ticker": ticker, "event_ticker": event_id,
                  "yes_bid_dollars": "0.59", "yes_ask_dollars": "0.61",
                  "close_time": close, "status": "active", "updated_time": at,
                  "yes_ask_size_fp": "10"}
        outer = self

        class Reader:
            def get_markets(self, series, cursor):
                url = ORIGIN + "/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=200"
                return {"url": url, "response_url": url, "method": "GET", "status": 200,
                        "started_at": outer.clock, "received_at": outer.clock,
                        "raw": canonical({"markets": [market], "cursor": ""}),
                        "transport_complete": True, "content_type": "application/json",
                        "content_range": None, "request_cursor": ""}

        capture_scan(self.o, Reader())
        self.observer.observe(self._native_rows(), self.q)
        self.phase.tick()
        return next(row for row in self.phase.decisions()
                    if row["family"] == support.FAMILY and row["ticker"] == ticker)

    def _native_settlement(self, row, at, outcome):
        self.clock = at
        observation = self.observer.decisions[row["decision_id"]]["payload"]["observation"]
        settled_at = (utc(observation["close_at"]) + timedelta(minutes=1)).isoformat()
        market = {"ticker": observation["ticker"], "event_ticker": observation["event_id"],
                  "market_type": "binary", "close_time": observation["close_at"],
                  "status": "finalized", "result": "yes" if outcome else "no",
                  "is_provisional": False, "settlement_ts": settled_at,
                  "settlement_value_dollars": str(outcome)}
        body = canonical({"market": market})
        url = ORIGIN + "/trade-api/v2/markets/" + observation["ticker"]
        payload = {"url": url, "response_url": url, "method": "GET", "status": 200,
                   "started_at": at, "received_at": at, "content_type": "application/json",
                   "content_range": None, "transport_complete": True,
                   "body_base64": base64.b64encode(body).decode(),
                   "body_sha256": hashlib.sha256(body).hexdigest()}
        raw = self.q.append("audit:raw:" + digest(payload), "Q_RAW", payload)
        label = settlement(raw, observation)
        self.q.append("audit:label:" + digest(label), "Q_LABEL", label)
        self.observer.settle(self._native_rows(), self.q)
        self.phase.tick()

    @staticmethod
    def _passed_fit(family, rows, at):
        if family != support.FAMILY:
            raise Refused("test fixture has no qualified data for this family")
        return {"family": family, "parameters": {"a": 1, "b": 0, "c": 0},
                "validation": {"predictive_pass": True, "promotion": False},
                "dataset_hash": digest(rows), "promotion": False}

    def _fit_once(self, at=FIT_AT, crossing=None):
        self.clock = at
        original = self.l.append

        def append(*args, **kwargs):
            if crossing and args[1] == "L_CHALLENGER_LOCK":
                self.clock = crossing
            return original(*args, **kwargs)

        with patch("atlas_v2.learning_phase2.train_family", side_effect=self._passed_fit), \
                patch.object(self.l, "append", side_effect=append):
            self.phase.tick()
        self.assertEqual(len(self.phase.challengers), 1)
        return next(iter(self.phase.challengers.values()))

    def _complete_oos(self, lock):
        start, _, cutoff = oos_window(lock["recorded_at"])
        at = (utc(start) + timedelta(hours=12, minutes=10)).isoformat()
        row = self._new_native_decision(at, "AUDITOOS")
        self._native_settlement(row, (utc(at) + timedelta(minutes=7)).isoformat(), 1)
        self.clock = cutoff
        with patch("atlas_v2.learning_phase2.evaluate_oos", return_value={
                "predictive_pass": True, "qualification": "TEST_STATISTIC_STUB",
                "promotion": False}) as evaluator:
            self.phase.tick()
        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(evaluator.call_args.args[0]["locked_at"], lock["recorded_at"])
        candidate = lock["payload"]["candidate_hash"]
        recorded = self.l.get("phase2:oos:" + candidate)
        self.assertEqual(recorded["payload"]["oos_members"][0]["decision_id"], row["decision_id"])
        self.assertEqual(self.phase.status()["status"], "BLOCKED_BY_EXTERNAL_ECONOMIC_EVIDENCE")
        return row

    def _assert_revoked_without_repeat(self, candidate, prior_terminal, prior_oos):
        with patch("atlas_v2.learning_phase2.train_family", side_effect=AssertionError("repeat fit")), \
                patch("atlas_v2.learning_phase2.evaluate_oos", side_effect=AssertionError("repeat OOS")):
            self.phase.tick()
            restarted = LearningPhase2(LearningObserver(self.l, "a" * 40), self.o, self.q,
                                       self.root / "reports")
            restarted.tick()
        status = restarted.status()
        self.assertEqual(status["status"], "EVIDENCE_INVALIDATED_NO_RETRAIN")
        self.assertIn(candidate, status["invalidated_challengers"])
        self.assertEqual(self.l.get("phase2:terminal"), prior_terminal)
        self.assertEqual(self.l.get("phase2:oos:" + candidate), prior_oos)
        self.assertEqual(len(self.l.events("L_TRAINING_BATCH")), 1)
        self.assertEqual(len(self.l.events("L_OOS_EVALUATION")), 1)
        self.assertIsNone(status["reward_total"])
        self.assertIsNone(status["net_hypothetical_pnl"])
        self.assertFalse(status["auto_promotion"])

    def test_persisted_midnight_lock_controls_capture_and_oos_evaluation(self):
        self._initial_settled()
        lock = self._fit_once("2026-10-14T23:59:59.999999Z", "2026-10-15T00:00:00.000001Z")
        self.assertEqual(lock["payload"]["locked_at"], "2026-10-14T23:59:59.999999Z")
        self.assertEqual(lock["recorded_at"], "2026-10-15T00:00:00.000001Z")
        candidate = lock["payload"]["candidate_hash"]
        premature = self._new_native_decision("2026-10-15T12:10:00Z", "PARTIALDAY")
        self.assertIsNone(self.l.get("phase2:prediction:" + digest([candidate, premature["decision_id"]])))
        eligible = self._new_native_decision("2026-10-16T12:10:00Z", "FULLDAY")
        prediction = self.l.get("phase2:prediction:" + digest([candidate, eligible["decision_id"]]))
        self.assertIsNotNone(prediction)
        self.assertLess(utc(prediction["recorded_at"]), utc(eligible["close_at"]))
        self._native_settlement(eligible, "2026-10-16T12:17:00Z", 1)
        self.clock = "2026-10-22T01:00:00Z"
        self.phase.tick()
        self.assertIsNone(self.l.get("phase2:oos:" + candidate))
        self.clock = "2026-10-23T01:00:00Z"
        with patch("atlas_v2.learning_phase2.evaluate_oos", wraps=evaluate_oos) as evaluator:
            self.phase.tick()
        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(evaluator.call_args.args[0]["locked_at"], lock["recorded_at"])
        self.assertEqual(self.l.get("phase2:oos:" + candidate)["payload"]["status"], "DATA_QUALIFICATION_FAILED")

    def test_native_training_label_conflict_revokes_after_terminal_and_restart(self):
        training = self._initial_settled()
        lock = self._fit_once()
        self._complete_oos(lock)
        candidate = lock["payload"]["candidate_hash"]
        terminal, evaluated = self.l.get("phase2:terminal"), self.l.get("phase2:oos:" + candidate)
        self._native_settlement(training, "2026-10-23T02:00:00Z", 0)
        self.assertIn(training["decision_id"], self.observer.invalid)
        self._assert_revoked_without_repeat(candidate, terminal, evaluated)

    def test_native_oos_label_conflict_revokes_after_terminal_and_restart(self):
        self._initial_settled()
        lock = self._fit_once()
        oos = self._complete_oos(lock)
        candidate = lock["payload"]["candidate_hash"]
        terminal, evaluated = self.l.get("phase2:terminal"), self.l.get("phase2:oos:" + candidate)
        self._native_settlement(oos, "2026-10-23T02:00:00Z", 0)
        self.assertIn(oos["decision_id"], self.observer.invalid)
        self._assert_revoked_without_repeat(candidate, terminal, evaluated)

    def test_control_disqualification_revokes_historical_winner_after_terminal(self):
        training = self._initial_settled()
        lock = self._fit_once()
        self._complete_oos(lock)
        candidate = lock["payload"]["candidate_hash"]
        terminal, evaluated = self.l.get("phase2:terminal"), self.l.get("phase2:oos:" + candidate)
        self.observer.disqualify(training["candidate_hash"], "model_approval",
                                 "synthetic immutable audit finding: attempted approval bypass")
        self._assert_revoked_without_repeat(candidate, terminal, evaluated)

    def test_oos_conflict_before_evaluation_cannot_trim_registered_cohort(self):
        self._initial_settled()
        lock = self._fit_once()
        bad = self._new_native_decision("2026-10-15T12:10:00Z", "CONFLICTINGOOS")
        self._native_settlement(bad, "2026-10-15T12:17:00Z", 1)
        good = self._new_native_decision("2026-10-15T12:25:00Z", "SURVIVINGOOS")
        self._native_settlement(good, "2026-10-15T12:32:00Z", 1)
        self._native_settlement(bad, "2026-10-15T13:00:00Z", 0)
        self.assertIn(bad["decision_id"], self.observer.invalid)
        self.assertNotIn(good["decision_id"], self.observer.invalid)
        self.clock = oos_window(lock["recorded_at"])[2]
        # A statistically passing remaining subset must never reach admission.
        with patch("atlas_v2.learning_phase2.evaluate_oos", return_value={
                "predictive_pass": True, "qualification": "TEST_STATISTIC_STUB",
                "promotion": False}) as evaluator:
            self.phase.tick()
        evaluator.assert_not_called()
        candidate = lock["payload"]["candidate_hash"]
        self.assertEqual(self.l.get("phase2:oos:" + candidate)["payload"]["status"],
                         "DATA_QUALIFICATION_FAILED")
        self.assertNotEqual(self.phase.status()["status"], "BLOCKED_BY_EXTERNAL_ECONOMIC_EVIDENCE")
        self.assertEqual(len(self.l.events("L_TRAINING_BATCH")), 1)

    def test_validation_rejected_result_is_revoked_by_native_label_correction(self):
        training = self._initial_settled()
        self.clock = FIT_AT

        def rejected_fit(family, rows, at):
            result = self._passed_fit(family, rows, at)
            result["validation"]["predictive_pass"] = False
            return result

        with patch("atlas_v2.learning_phase2.train_family", side_effect=rejected_fit):
            self.phase.tick()
        self.assertEqual(self.phase.challengers, {})
        result = next(r for r in self.l.get("phase2:batch")["payload"]["results"]
                      if r["family"] == support.FAMILY)
        self.assertEqual(result["status"], "VALIDATION_REJECTED")
        self.assertEqual(result["result"]["training_members"][0]["decision_id"], training["decision_id"])
        terminal = self.l.get("phase2:terminal")
        self._native_settlement(training, "2026-10-23T02:00:00Z", 0)
        self.assertEqual(self.phase.status()["status"], "EVIDENCE_INVALIDATED_NO_RETRAIN")
        self.assertIsNotNone(self.l.latest("L_PHASE2_DATA_INVALIDATION"))
        self.assertEqual(self.l.get("phase2:terminal"), terminal)
        self.assertEqual(len(self.l.events("L_TRAINING_BATCH")), 1)


if __name__ == "__main__":
    unittest.main()
