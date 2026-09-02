# -*- coding: utf-8 -*-
"""T7-I invariants for the BTC daily evidence layer.

The patch is instrumentation. These tests exist to prove it is ONLY
instrumentation: that no trading decision, candidate, order, size, or risk
verdict moves, and that evidence failure can never increase trading activity.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from btc_daily_evidence import (BtcDailyEvidenceStore, build_prediction_record,
                                horizon_bucket, make_record_id,
                                EVIDENCE_SCHEMA_VERSION)
from opportunity_pipeline import MarketOpportunityPipeline
from strategy_router import GateConfig, ModelOutput, Strategy
from execution_engine import ExecutionEngine


def _iso(minutes_ahead):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes_ahead)).isoformat()


def mkt(ticker="KXBTCD-26AUG20-T70000", mins=600):
    # 600 min keeps the fixture INSIDE the production 24h scanner window, so a
    # decision actually reaches the observer. A 2000-minute market would be
    # dropped as outside_time_window — the very T7-H defect under audit — and
    # the equivalence tests would silently assert over an empty funnel.
    return {"ticker": ticker, "status": "open", "close_time": _iso(mins),
            "expiration_time": _iso(mins + 60), "yes_bid": 44, "yes_ask": 45,
            "no_bid": 55, "no_ask": 56, "volume_24h": 900,
            "open_interest": 120, "floor_strike": 70000}


FEATURES = {"model_version": "btc15m-v1.0-ref", "spot": 70500.0,
            "strike": 70000.0, "sigma_1m": 0.0008, "minutes_remaining": 2000,
            "ret_5m": 0.001, "data_quality": 80.0,
            "horizon_mode": "extended", "sigma_effective": 0.0008}


class _DailyStrategy(Strategy):
    name = "btc_daily_above_strike"
    market_types = ("btc_above_strike_daily",)

    def evaluate(self, market, book, minutes_remaining):
        return ModelOutput(True, "ok", probability_yes=0.62, confidence=6,
                           features=dict(FEATURES))


class _Router:
    def __init__(self):
        self._s = _DailyStrategy()

    def route(self, mt):
        return self._s if mt == "btc_above_strike_daily" else None

    def supported_market_types(self):
        return ("btc_above_strike_daily",)

    tradeable_market_types = supported_market_types


class _Client:
    env = "demo"

    def __init__(self, markets, result=None):
        self._m = markets
        self._result = result

    def get_markets(self, series, status="open", limit=200):
        return [dict(m) for m in self._m] if series == "KXBTCD" else []

    def get_market(self, ticker):
        for m in self._m:
            if m["ticker"] == ticker:
                out = dict(m)
                if self._result:
                    out["result"] = self._result
                return out
        return {}

    def _req(self, method, path, params=None, **kw):
        return {"markets": [], "cursor": None}


def _run_pipeline(tmp, observer):
    """One pipeline cycle. Returns (report, accepted-decision dicts)."""
    p = MarketOpportunityPipeline(
        _Client([mkt()]), _Router(), gates=GateConfig(),
        observer=observer, data_dir=tmp)
    res = p.run_cycle(max_accepted=3)
    return res["report"], [d.to_dict() for d in res["accepted"]]


class TestBehaviouralEquivalence(unittest.TestCase):
    """INVARIANTS 1-5: instrumentation must not move any trading number."""

    COUNTERS = ("scanned_raw", "after_status", "after_time_window",
                "after_liquidity", "after_classification", "scanner_kept",
                "supported", "model_evaluated", "positive_edge",
                "positive_net_ev", "risk_passed", "orders_submitted",
                "fills", "accepted")

    def _pair(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            base_rep, base_acc = _run_pipeline(a, None)
            store = BtcDailyEvidenceStore(b)
            seen = []

            def observer(snapshot, book, dec):
                seen.append(store.record(
                    decision=dec.to_dict(),
                    model_output=getattr(dec, "model_output", None),
                    market=snapshot.raw_market, book=book,
                    minutes_remaining=snapshot.minutes_remaining,
                    cycle_id="cyc"))

            patched_rep, patched_acc = _run_pipeline(b, observer)
            # Read the evidence INSIDE the context: leaving it deletes the
            # temp dir, and the store would then read an absent file as [].
            rows = store.predictions()
            return base_rep, base_acc, patched_rep, patched_acc, seen, rows

    def test_invariant_1_decisions_identical(self):
        base, bacc, patched, pacc, _s, _rows = self._pair()
        # Compare every funnel counter and the rejection ledger. The full
        # report dict is deliberately NOT compared: it embeds
        # minutes_remaining floats derived from wall-clock at each run, which
        # differ by microseconds between two sequential cycles and would make
        # this a clock test rather than an equivalence test.
        for k in self.COUNTERS:
            self.assertEqual(base.get(k), patched.get(k), k)
        self.assertEqual(base.get("rejections_by_reason"),
                         patched.get("rejections_by_reason"))
        self.assertEqual(base.get("model_rejections_detailed"),
                         patched.get("model_rejections_detailed"))
        # decision_id embeds a fresh uuid per run; compare the substance.
        self.assertEqual(len(bacc), len(pacc))
        for x, y in zip(bacc, pacc):
            x.pop("decision_id"), y.pop("decision_id")
            self.assertEqual(x, y)

    def test_invariant_2_candidate_count_identical(self):
        base, bacc, patched, pacc, _s, _rows = self._pair()
        self.assertEqual(len(bacc), len(pacc))
        self.assertEqual(base.get("accepted"), patched.get("accepted"))

    def test_invariant_3_order_submission_count_identical(self):
        base, _b, patched, _p, _s, _rows = self._pair()
        self.assertEqual(base.get("orders_submitted"),
                         patched.get("orders_submitted"))
        self.assertEqual(base.get("fills"), patched.get("fills"))

    def test_invariant_4_sizing_identical(self):
        _b, bacc, _p, pacc, _s, _rows = self._pair()
        self.assertEqual([d["taille"] for d in bacc],
                         [d["taille"] for d in pacc])

    def test_invariant_5_risk_decisions_identical(self):
        base, _b, patched, _p, _s, _rows = self._pair()
        self.assertEqual(base.get("risk_passed"), patched.get("risk_passed"))
        self.assertEqual(base.get("rejections_by_reason"),
                         patched.get("rejections_by_reason"))

    def test_invariant_6_daily_evidence_is_durable(self):
        _b, _ba, _p, _pa, seen, rows = self._pair()
        self.assertTrue(any(seen), "no evidence recorded for btc_daily")
        self.assertTrue(rows, "evidence did not survive to disk")
        r = rows[0]
        self.assertEqual(r["strategy_name"], "btc_daily_above_strike")
        self.assertEqual(r["schema_version"], EVIDENCE_SCHEMA_VERSION)
        self.assertEqual(r["predicted_probability"], 0.62)
        self.assertEqual(r["horizon_bucket"], "<=24h")
        self.assertIsNotNone(r["record_id"])
        self.assertIsNotNone(r["market_close_time"])
        self.assertEqual(r["model_version"], "btc15m-v1.0-ref")


class TestObserverRouting(unittest.TestCase):
    """INVARIANT 7: the btc15m path is untouched; daily never reaches it."""

    class _Dec:
        def __init__(self, strategy):
            self.strategy = strategy
            self.ticker = "KXBTCD-26AUG20-T70000"
            self.decision_id = "cyc12345-abcd1234"
            self.model_output = {"valid": True, "probability_yes": 0.62,
                                 "confidence": 6, "features": dict(FEATURES)}
            self.estimated_fees = 0.01
            self.expected_slippage = 0.01
            self.gross_edge = 0.1
            self.net_edge = 0.07
            self.net_ev = 0.05
            self.accepted = False
            self.rejection_reason = None
            self.market_type = "btc_above_strike_daily"

        def to_dict(self):
            return {"ticker": self.ticker, "strategy": self.strategy,
                    "decision_id": self.decision_id,
                    "market_type": self.market_type, "accepted": self.accepted,
                    "rejection_reason": self.rejection_reason,
                    "net_edge": self.net_edge, "gross_edge": self.gross_edge,
                    "net_ev": self.net_ev}

    class _Snap:
        # Called directly, bypassing the scanner, so a 2000-minute horizon is
        # fine here and exercises the 24-48h bucket.
        raw_market = mkt("KXBTCD-26AUG20-T70000", 2000)
        minutes_remaining = 2000
        quality = None

    class _FakeShadow:
        def __init__(self):
            self.calls = []

        def record(self, **kw):
            self.calls.append(kw)

    def _observe(self, strategy, tmp):
        """Drive the REAL _shadow_observer without building an engine."""
        class _Self:
            pass
        s = _Self()
        s.shadow_store = self._FakeShadow()
        s.btc_daily_evidence = BtcDailyEvidenceStore(tmp)
        ExecutionEngine._shadow_observer(s, self._Snap(), {
            "yes_bid": 44, "yes_ask": 45, "no_bid": 55, "no_ask": 56,
            "yes_mid": 45, "spread": 1}, self._Dec(strategy))
        return s

    def test_invariant_7_btc15m_still_goes_to_shadow_store_only(self):
        with tempfile.TemporaryDirectory() as t:
            s = self._observe("btc15m_above_strike", t)
            self.assertEqual(len(s.shadow_store.calls), 1)
            self.assertEqual(s.btc_daily_evidence.predictions(), [])

    def test_daily_goes_to_evidence_store_only(self):
        with tempfile.TemporaryDirectory() as t:
            s = self._observe("btc_daily_above_strike", t)
            self.assertEqual(s.shadow_store.calls, [])
            rows = s.btc_daily_evidence.predictions()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["cycle_id"], "cyc12345")
            self.assertEqual(rows[0]["market_implied_probability"], 0.45)

    def test_unrelated_strategy_records_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            s = self._observe("sports_moneyline_v1", t)
            self.assertEqual(s.shadow_store.calls, [])
            self.assertEqual(s.btc_daily_evidence.predictions(), [])


class TestJoinability(unittest.TestCase):
    """INVARIANT 8: prediction -> settlement joins deterministically."""

    def _store(self, tmp, mins=-10):
        st = BtcDailyEvidenceStore(tmp)
        st.record(decision={"ticker": "KXBTCD-A", "strategy": "btc_daily",
                            "decision_id": "c1-x", "market_type": "d"},
                  model_output={"valid": True, "probability_yes": 0.7,
                                "confidence": 6, "features": dict(FEATURES)},
                  market=mkt("KXBTCD-A", mins), book={"yes_mid": 45},
                  minutes_remaining=mins, cycle_id="c1")
        return st

    def test_record_id_is_deterministic(self):
        a = make_record_id(cycle_id="c", ticker="T", observed_at="t",
                           model_version="m")
        b = make_record_id(cycle_id="c", ticker="T", observed_at="t",
                           model_version="m")
        self.assertEqual(a, b)
        self.assertNotEqual(a, make_record_id(cycle_id="c", ticker="U",
                                              observed_at="t",
                                              model_version="m"))

    def test_invariant_8_settlement_joins_on_record_id(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t)
            n = st.settle(lambda tk: {"result": "yes", "status": "settled"})
            self.assertEqual(n, 1)
            rows = st.calibration_records()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["result"], "yes")
            self.assertEqual(rows[0]["probability_yes"], 0.7)
            self.assertEqual(rows[0]["record_id"],
                             st.predictions()[0]["record_id"])

    def test_duplicate_settlement_does_not_duplicate_samples(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t)
            st.settle(lambda tk: {"result": "yes", "status": "settled"})
            again = st.settle(lambda tk: {"result": "yes", "status": "settled"})
            self.assertEqual(again, 0, "settled record was polled twice")
            self.assertEqual(len(st.calibration_records()), 1)

    def test_two_tickers_do_not_collide(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t)
            st.record(decision={"ticker": "KXBTCD-B", "strategy": "btc_daily",
                                "decision_id": "c1-y", "market_type": "d"},
                      model_output={"valid": True, "probability_yes": 0.3,
                                    "confidence": 6,
                                    "features": dict(FEATURES)},
                      market=mkt("KXBTCD-B", -10), book={"yes_mid": 45},
                      minutes_remaining=-10, cycle_id="c1")
            ids = {r["record_id"] for r in st.predictions()}
            self.assertEqual(len(ids), 2)
            st.settle(lambda tk: {"result": "yes" if tk == "KXBTCD-A" else "no",
                                  "status": "settled"})
            got = {r["ticker"]: r["result"] for r in st.calibration_records()}
            self.assertEqual(got, {"KXBTCD-A": "yes", "KXBTCD-B": "no"})

    def test_immature_market_is_never_polled(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t, mins=5000)          # closes in the future
            calls = []

            def get_market(tk):
                calls.append(tk)
                return {"result": "yes", "status": "settled"}
            self.assertEqual(st.settle(get_market), 0)
            self.assertEqual(calls, [], "an unmatured market cost an API call")


class TestCalibrationHygiene(unittest.TestCase):
    """INVARIANT 9 plus segmentation."""

    def _seed(self, tmp):
        st = BtcDailyEvidenceStore(tmp)
        for i, (mins, p) in enumerate([(-10, 0.7), (-20, 0.4), (5000, 0.9)]):
            st.record(decision={"ticker": f"KXBTCD-{i}",
                                "strategy": "btc_daily_above_strike",
                                "decision_id": f"c-{i}", "market_type": "d"},
                      model_output={"valid": True, "probability_yes": p,
                                    "confidence": 6,
                                    "features": dict(FEATURES)},
                      market=mkt(f"KXBTCD-{i}", mins), book={"yes_mid": 50},
                      minutes_remaining=mins, cycle_id="c")
        return st

    def test_invariant_9_unsettled_never_enter_metrics(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._seed(t)
            st.settle(lambda tk: {"result": "yes", "status": "settled"})
            rows = st.calibration_records()
            self.assertEqual(len(rows), 2, "an unsettled record leaked in")
            self.assertNotIn("KXBTCD-2", [r["ticker"] for r in rows])
            rep = st.calibration_report()
            self.assertEqual(rep["count"], 2)
            self.assertTrue(rep["unsettled_excluded"])
            self.assertIsNotNone(rep["brier_score"])

    def test_segmentation_never_pools_silently(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._seed(t)
            st.settle(lambda tk: {"result": "yes", "status": "settled"})
            self.assertEqual(
                st.calibration_report(model_version="nope")["count"], 0)
            rep = st.calibration_report(model_version="btc15m-v1.0-ref",
                                        bucket="24-48h")
            self.assertEqual(rep["segment"]["horizon_bucket"], "24-48h")
            self.assertIn("btc15m-v1.0-ref", rep["label"])

    def test_horizon_buckets_cover_the_required_ranges(self):
        self.assertEqual(horizon_bucket(60), "<=24h")
        self.assertEqual(horizon_bucket(1440), "24-48h")
        self.assertEqual(horizon_bucket(2880), "2-3d")
        self.assertEqual(horizon_bucket(4320), "3-7d")
        self.assertEqual(horizon_bucket(10080), "7-14d")
        self.assertEqual(horizon_bucket(20160), ">14d")
        self.assertIsNone(horizon_bucket(None))
        self.assertIsNone(horizon_bucket("x"))


class TestFailureSemantics(unittest.TestCase):
    """INVARIANT 10: evidence failure cannot increase trading activity."""

    def test_unwritable_store_never_raises_and_records_nothing(self):
        st = BtcDailyEvidenceStore("/nonexistent-dir-t7i")
        rid = st.record(decision={"ticker": "KXBTCD-A", "strategy": "btc_daily",
                                  "decision_id": "c-1", "market_type": "d"},
                        model_output={"valid": True, "probability_yes": 0.5,
                                      "confidence": 6,
                                      "features": dict(FEATURES)},
                        market=mkt(), book={"yes_mid": 45},
                        minutes_remaining=2000, cycle_id="c")
        self.assertIsNone(rid)
        self.assertEqual(st.predictions(), [])
        self.assertEqual(st.settle(lambda tk: {"result": "yes", "status": "settled"}), 0)

    def test_invariant_10_failing_evidence_leaves_the_funnel_unchanged(self):
        with tempfile.TemporaryDirectory() as t:
            good, _ = _run_pipeline(t, None)
        with tempfile.TemporaryDirectory() as t:
            broken = BtcDailyEvidenceStore("/nonexistent-dir-t7i")

            def observer(snapshot, book, dec):
                broken.record(decision=dec.to_dict(),
                              model_output=getattr(dec, "model_output", None),
                              market=snapshot.raw_market, book=book,
                              minutes_remaining=snapshot.minutes_remaining,
                              cycle_id="c")
            bad, _ = _run_pipeline(t, observer)
        for k in ("accepted", "orders_submitted", "fills", "model_evaluated",
                  "positive_edge", "risk_passed"):
            self.assertEqual(good.get(k), bad.get(k), k)

    def test_observer_exception_cannot_reach_the_cycle(self):
        with tempfile.TemporaryDirectory() as t:
            def boom(snapshot, book, dec):
                raise RuntimeError("evidence exploded")
            rep, acc = _run_pipeline(t, boom)
            self.assertIsNotNone(rep)
            self.assertEqual(rep.get("orders_submitted", 0), 0)

    def test_torn_line_does_not_kill_the_read(self):
        with tempfile.TemporaryDirectory() as t:
            st = BtcDailyEvidenceStore(t)
            st.record(decision={"ticker": "KXBTCD-A", "strategy": "btc_daily",
                                "decision_id": "c-1", "market_type": "d"},
                      model_output={"valid": True, "probability_yes": 0.5,
                                    "confidence": 6,
                                    "features": dict(FEATURES)},
                      market=mkt(), book={"yes_mid": 45},
                      minutes_remaining=2000, cycle_id="c")
            with open(st.predictions_path, "a", encoding="utf-8") as f:
                f.write("{not json\n")
            self.assertEqual(len(st.predictions()), 1)


class TestEvidenceContract(unittest.TestCase):
    def test_absent_values_stay_null_and_never_become_zero(self):
        row = build_prediction_record(
            decision={"ticker": "KXBTCD-A", "strategy": "btc_daily"},
            model_output={"valid": False, "reason": "no_model_probability",
                          "features": {}},
            market={"ticker": "KXBTCD-A"}, book={},
            minutes_remaining=None, cycle_id="c")
        for field in ("underlying_price", "strike", "predicted_probability",
                      "data_quality", "minutes_remaining", "horizon_bucket",
                      "market_close_time", "market_implied_probability",
                      "model_version", "strategy_version", "model_hash"):
            self.assertIsNone(row[field], field)
        self.assertFalse(row["model_valid"])
        self.assertEqual(row["series"], "KXBTCD")

    def test_a_row_with_a_model_version_carries_the_model_hash(self):
        # A version string is a promise; the hash is what the code was.
        from btc_probability_model import model_hash
        row = build_prediction_record(
            decision={"ticker": "KXBTCD-A", "strategy": "btc_daily"},
            model_output={"valid": True, "probability_yes": 0.5,
                          "confidence": 6, "features": dict(FEATURES)},
            market=mkt(), book={}, minutes_remaining=600, cycle_id="c")
        self.assertEqual(row["model_hash"], model_hash())
        self.assertEqual(len(row["model_hash"]), 16)

    def test_record_is_json_serialisable(self):
        row = build_prediction_record(
            decision={"ticker": "KXBTCD-A", "strategy": "btc_daily",
                      "decision_id": "c-1"},
            model_output={"valid": True, "probability_yes": 0.6,
                          "confidence": 6, "features": dict(FEATURES)},
            market=mkt(), book={"yes_bid": 44, "yes_ask": 45, "yes_mid": 45},
            minutes_remaining=2000, cycle_id="c")
        self.assertEqual(json.loads(json.dumps(row))["record_id"],
                         row["record_id"])


if __name__ == "__main__":
    unittest.main()


class TestSettlementConfirmationProtocol(unittest.TestCase):
    """A first-poll answer is never a settlement on its own.

    Measured 2026-09-02: 62 KXBTCD events that closed with BTC above the
    strike were journaled "no" because the API was polled within minutes
    of close and its first answer was frozen. These tests pin the protocol
    that makes that impossible.
    """

    def _store(self, tmp, mins=-100):
        st = BtcDailyEvidenceStore(tmp)
        st.record(decision={"ticker": "KXBTCD-A", "strategy": "btc_daily",
                            "decision_id": "c1-x", "market_type": "d"},
                  model_output={"valid": True, "probability_yes": 0.7,
                                "confidence": 6, "features": dict(FEATURES)},
                  market=mkt("KXBTCD-A", mins), book={"yes_mid": 45},
                  minutes_remaining=mins, cycle_id="c1")
        return st

    def test_a_bare_result_without_finalized_status_is_only_an_observation(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t)
            self.assertEqual(st.settle(lambda tk: {"result": "no"}), 0)
            self.assertEqual(st.settlements(), {})
            self.assertEqual(st.calibration_records(), [])
            obs = st._observations()
            self.assertEqual(len(obs), 1)
            self.assertEqual(list(obs.values())[0][0]["kind"], "observation")

    def test_a_finalized_status_is_authoritative_at_once(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t)
            self.assertEqual(st.settle(lambda tk: {"result": "no",
                                                   "status": "finalized"}), 1)
            row = list(st.settlements().values())[0]
            self.assertEqual(row["confirmed_by"], "status")
            self.assertEqual(row["kind"], "settlement")

    def test_two_agreeing_observations_far_enough_apart_confirm(self):
        from btc_daily_evidence import SETTLE_MIN_LAG_S, SETTLE_CONFIRM_MIN_S
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t, mins=-1)          # closed a minute ago
            now = datetime.now(timezone.utc)
            close = now - timedelta(minutes=1)
            first = close + timedelta(seconds=SETTLE_MIN_LAG_S + 1)
            second = first + timedelta(seconds=SETTLE_CONFIRM_MIN_S + 1)
            self.assertEqual(st.settle(lambda tk: {"result": "yes"}, now=first), 0)
            self.assertEqual(st.settle(lambda tk: {"result": "yes"}, now=second), 1)
            row = list(st.settlements().values())[0]
            self.assertEqual(row["confirmed_by"], "repeat")
            self.assertEqual(row["prior_observations"], 1)

    def test_an_observation_taken_too_soon_after_close_cannot_confirm(self):
        # Exactly the failure mode: polled minutes after close, then again
        # later with the same answer. The first observation is too early to
        # count, so the second only becomes the first valid one.
        from btc_daily_evidence import SETTLE_CONFIRM_MIN_S
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t, mins=-1)
            now = datetime.now(timezone.utc)
            early = now + timedelta(minutes=5)
            later = early + timedelta(seconds=SETTLE_CONFIRM_MIN_S + 1)
            self.assertEqual(st.settle(lambda tk: {"result": "no"}, now=early), 0)
            self.assertEqual(st.settle(lambda tk: {"result": "no"}, now=later), 0)
            self.assertEqual(st.settlements(), {})

    def test_disagreeing_observations_never_confirm(self):
        from btc_daily_evidence import SETTLE_MIN_LAG_S, SETTLE_CONFIRM_MIN_S
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t, mins=-1)
            now = datetime.now(timezone.utc)
            first = now + timedelta(seconds=SETTLE_MIN_LAG_S + 1)
            second = first + timedelta(seconds=SETTLE_CONFIRM_MIN_S + 1)
            st.settle(lambda tk: {"result": "no"}, now=first)
            self.assertEqual(st.settle(lambda tk: {"result": "yes"}, now=second), 0)
            self.assertEqual(st.settlements(), {})

    def test_no_api_call_while_the_confirmation_window_is_open(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t, mins=-100)
            now = datetime.now(timezone.utc)
            calls = []

            def gm(tk):
                calls.append(tk)
                return {"result": "no"}
            st.settle(gm, now=now)
            st.settle(gm, now=now + timedelta(minutes=1))
            self.assertEqual(len(calls), 1, "re-polled inside the window")

    def test_legacy_first_poll_rows_are_excluded_from_calibration_by_default(self):
        with tempfile.TemporaryDirectory() as t:
            st = self._store(t)
            rid = st.predictions()[0]["record_id"]
            with open(st.settlements_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"schema_version": 1, "record_id": rid,
                                    "ticker": "KXBTCD-A", "result": "no",
                                    "settled_at": _iso(0)}) + "\n")
            self.assertEqual(len(st.settlements()), 1)         # inventory
            self.assertEqual(st.calibration_records(), [])       # not evidence
            self.assertEqual(len(st.calibration_records(
                require_confirmed=False)), 1)                    # only if asked
            cov = st.coverage()
            self.assertEqual(cov["settled_unconfirmed_legacy"], 1)
            self.assertEqual(cov["settled_confirmed"], 0)

    def test_a_spot_proxy_strike_never_enters_decisive_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            st = BtcDailyEvidenceStore(t)
            feats = dict(FEATURES, strike_source="spot_proxy")
            st.record(decision={"ticker": "KXBTCD-P", "strategy": "btc_daily",
                                "decision_id": "c-p", "market_type": "d"},
                      model_output={"valid": True, "probability_yes": 0.5,
                                    "confidence": 6, "features": feats},
                      market=mkt("KXBTCD-P", -100), book={"yes_mid": 45},
                      minutes_remaining=-100, cycle_id="c")
            st.settle(lambda tk: {"result": "yes", "status": "settled"})
            self.assertEqual(st.calibration_records(), [])
            self.assertEqual(len(st.calibration_records(
                decisive_strike_only=False)), 1)
