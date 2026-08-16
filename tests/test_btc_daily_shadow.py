# -*- coding: utf-8 -*-
"""T7-K invariants for pre-liquidity shadow evidence.

The claim under test is narrow and absolute: markets rejected for
no_liquidity may be OBSERVED, and may never become tradeable. Every test
here either proves an execution path stays shut, or proves the observation
policy does not manufacture correlated samples.
"""
import inspect
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import btc_daily_shadow as SH
from btc_daily_shadow import (BtcDailyShadowEvaluator, ShadowObservation,
                              checkpoint_for, observation_key,
                              shadow_record_id, CHECKPOINTS, SHADOW_ORIGIN)
from btc_daily_evidence import BtcDailyEvidenceStore
from market_scanner import MarketScanner, ScannerConfig
from opportunity_pipeline import MarketOpportunityPipeline
from strategy_router import GateConfig, ModelOutput, Strategy


def _iso(mins):
    return (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()


def illiquid(ticker="KXBTCD-26AUG20-T70000", mins=600):
    """In-window, open, classifiable, and provably empty of any book."""
    return {"ticker": ticker, "status": "open", "close_time": _iso(mins),
            "yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0,
            "volume_24h": 0, "volume": 0, "open_interest": 0, "liquidity": 0,
            "floor_strike": 70000, "_minutes_remaining": mins}


def liquid(ticker="KXBTCD-26AUG20-T71000", mins=600):
    m = illiquid(ticker, mins)
    m.update({"yes_bid": 44, "yes_ask": 45, "volume_24h": 900,
              "open_interest": 120})
    return m


FEATURES = {"model_version": "btc15m-v1.0-ref", "spot": 70500.0,
            "strike": 70000.0, "sigma_1m": 0.0008, "data_quality": 80.0}


class _DailyStrategy(Strategy):
    name = "btc_daily_above_strike"
    market_types = ("btc_above_strike_daily",)

    def __init__(self, valid=True):
        self._valid = valid
        self.books_seen = []

    def evaluate(self, market, book, minutes_remaining):
        self.books_seen.append(book)
        if not self._valid:
            return ModelOutput(False, "no_model_probability:contexte_invalide")
        return ModelOutput(True, "ok", probability_yes=0.62, confidence=6,
                           features=dict(FEATURES,
                                         minutes_remaining=minutes_remaining))


class _Router:
    def __init__(self, strat=None):
        self._s = strat or _DailyStrategy()

    def route(self, mt):
        return self._s if mt == "btc_above_strike_daily" else None

    def supported_market_types(self):
        return ("btc_above_strike_daily",)

    tradeable_market_types = supported_market_types


def _ev(tmp, router=None, enabled=True):
    store = BtcDailyEvidenceStore(tmp)
    return BtcDailyShadowEvaluator(router or _Router(), store,
                                   enabled=enabled), store


class TestCheckpointPolicy(unittest.TestCase):
    """PHASE 4/5: the observation policy must not resample per cycle."""

    def test_checkpoint_is_the_smallest_one_at_or_above_the_horizon(self):
        self.assertEqual(checkpoint_for(1400)[1], "24h")
        self.assertEqual(checkpoint_for(1440)[1], "24h")
        self.assertEqual(checkpoint_for(700)[1], "12h")
        self.assertEqual(checkpoint_for(360)[1], "6h")
        self.assertEqual(checkpoint_for(120)[1], "3h")
        self.assertEqual(checkpoint_for(30)[1], "1h")

    def test_no_checkpoint_outside_the_scanner_window_or_at_expiry(self):
        self.assertIsNone(checkpoint_for(1441))
        self.assertIsNone(checkpoint_for(0))
        self.assertIsNone(checkpoint_for(-5))
        self.assertIsNone(checkpoint_for(None))
        self.assertIsNone(checkpoint_for("x"))

    def test_every_checkpoint_lies_inside_the_model_non_extended_domain(self):
        # 1560 = BtcDailyStrategy.max_minutes; beyond it the model switches to
        # the extended branch and caps confidence. Every checkpoint must sit
        # below it so observations are all horizon_mode="normal".
        for mins, _label in CHECKPOINTS:
            self.assertLessEqual(mins, 1440, "outside the scanner window")
            self.assertLess(mins, 1560, "would trigger the extended branch")

    def test_a_ticker_crossing_checkpoints_yields_one_row_each(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            for mins in (1400, 1300, 700, 690, 300, 100, 30, 20):
                ev.run([illiquid(mins=mins)])
            rows = store.predictions()
            self.assertEqual(sorted(r["observation_checkpoint"] for r in rows),
                             ["12h", "1h", "24h", "3h", "6h"])
            self.assertEqual(len(rows), 5, "a cycle produced a duplicate row")

    def test_eighty_markets_one_cycle_do_not_become_eighty_per_cycle(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            pop = [illiquid(f"KXBTCD-A-{i}", 600) for i in range(80)]
            first = ev.run(pop)
            second = ev.run(pop)
            third = ev.run(pop)
            self.assertEqual(first["shadow_daily_predicted"], 80)
            self.assertEqual(second["shadow_daily_predicted"], 0)
            self.assertEqual(second["shadow_daily_deduplicated"], 80)
            self.assertEqual(third["shadow_daily_predicted"], 0)
            self.assertEqual(len(store.predictions()), 80)


class TestDedupAndRestart(unittest.TestCase):
    def test_observation_key_and_record_id_carry_no_timestamp(self):
        k = observation_key("KXBTCD-A", "m1", "6h")
        self.assertEqual(k, "KXBTCD-A|m1|6h")
        self.assertEqual(shadow_record_id(k), shadow_record_id(k))
        self.assertNotEqual(shadow_record_id(k),
                            shadow_record_id(observation_key("KXBTCD-A", "m1",
                                                             "3h")))

    def test_restart_rebuilds_the_dedup_index_from_disk(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            ev.run([illiquid()])
            self.assertEqual(len(store.predictions()), 1)
            # A brand-new evaluator, as after a redeploy or crash.
            ev2, store2 = _ev(t)
            out = ev2.run([illiquid()])
            self.assertEqual(out["shadow_daily_deduplicated"], 1)
            self.assertEqual(out["shadow_daily_predicted"], 0)
            self.assertEqual(len(store2.predictions()), 1)

    def test_a_new_model_version_is_a_new_observation(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            ev.run([illiquid()])
            FEATURES["model_version"] = "btc15m-v2.0"
            try:
                ev.run([illiquid()])
            finally:
                FEATURES["model_version"] = "btc15m-v1.0-ref"
            self.assertEqual(len(store.predictions()), 2)
            self.assertEqual({r["model_version"] for r in store.predictions()},
                             {"btc15m-v1.0-ref", "btc15m-v2.0"})


class TestPopulationScope(unittest.TestCase):
    """PHASE 2: only the seven-condition population may be observed."""

    def _one(self, market, router=None):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t, router=router)
            return ev.run([market]), store.predictions()

    def test_liquid_market_is_never_shadow_observed(self):
        S, rows = self._one(liquid())
        self.assertEqual(S["shadow_daily_predicted"], 0)
        self.assertEqual(S["shadow_daily_out_of_scope"], 1)
        self.assertEqual(rows, [])

    def test_closed_market_is_never_observed(self):
        m = illiquid(); m["status"] = "settled"
        S, rows = self._one(m)
        self.assertEqual(S["shadow_daily_predicted"], 0)
        self.assertEqual(rows, [])

    def test_outside_window_market_is_never_observed(self):
        S, rows = self._one(illiquid(mins=3000))
        self.assertEqual(S["shadow_daily_no_checkpoint"], 1)
        self.assertEqual(rows, [])

    def test_unsupported_market_type_is_never_observed(self):
        S, rows = self._one(illiquid("KXNFLSPREAD-26AUG20-X", 600))
        self.assertEqual(S["shadow_daily_out_of_scope"], 1)
        self.assertEqual(rows, [])

    def test_btc15m_market_is_never_observed_by_the_daily_path(self):
        S, rows = self._one(illiquid("KXBTC15M-26AUG20-T70000", 600))
        self.assertEqual(S["shadow_daily_out_of_scope"], 1)
        self.assertEqual(rows, [])

    def test_invalid_model_context_records_nothing(self):
        S, rows = self._one(illiquid(),
                            router=_Router(_DailyStrategy(valid=False)))
        self.assertEqual(S["shadow_daily_invalid"], 1)
        self.assertEqual(S["shadow_daily_predicted"], 0)
        self.assertEqual(rows, [])

    def test_strategy_without_probability_source_is_out_of_scope(self):
        class _NoSource(_DailyStrategy):
            def has_probability_source(self):
                return False
        S, rows = self._one(illiquid(), router=_Router(_NoSource()))
        self.assertEqual(S["shadow_daily_predicted"], 0)
        self.assertEqual(rows, [])

    def test_disabled_by_default_records_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t, enabled=False)
            S = ev.run([illiquid()])
            self.assertEqual(S["shadow_daily_considered"], 0)
            self.assertEqual(store.predictions(), [])

    def test_env_flag_defaults_to_off(self):
        os.environ.pop("BTC_DAILY_SHADOW_ENABLED", None)
        with tempfile.TemporaryDirectory() as t:
            ev = BtcDailyShadowEvaluator(_Router(),
                                         BtcDailyEvidenceStore(t))
            self.assertFalse(ev.enabled)


class TestExecutionIsolation(unittest.TestCase):
    """PHASE 3: structural, not conventional, separation."""

    def test_module_imports_no_execution_component(self):
        src = inspect.getsource(SH)
        for forbidden in ("order_manager", "risk_manager", "position_manager",
                          "OrderManager", "RiskManager", "PositionManager",
                          "create_order", "PositionSizer"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_result_type_has_no_accepted_field(self):
        fields = set(ShadowObservation.__dataclass_fields__)
        for forbidden in ("accepted", "side", "entry_ask", "taille",
                          "net_edge", "net_ev"):
            self.assertNotIn(forbidden, fields, forbidden)

    def test_shadow_never_constructs_or_imports_a_decision(self):
        # Prose may discuss Decision; code may not construct or import it.
        src = inspect.getsource(SH)
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("Decision(", code)
        self.assertNotIn("import Decision", code)
        self.assertFalse(hasattr(SH, "Decision"),
                         "Decision is reachable from the shadow module")

    def test_recorded_row_is_marked_ineligible_for_execution(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            ev.run([illiquid()])
            r = store.predictions()[0]
            self.assertEqual(r["origin"], SHADOW_ORIGIN)
            self.assertFalse(r["execution_eligible"])
            self.assertFalse(r["decision_accepted"])
            self.assertEqual(r["rejection_reason"], "no_liquidity")
            self.assertIsNone(r["edge"])
            self.assertIsNone(r["expected_value"])

    def test_model_is_given_no_synthetic_book(self):
        strat = _DailyStrategy()
        with tempfile.TemporaryDirectory() as t:
            ev, _s = _ev(t, router=_Router(strat))
            ev.run([illiquid()])
        self.assertEqual(strat.books_seen, [None],
                         "a fabricated book was passed to the model")

    def test_scanner_shadow_population_never_enters_kept_markets(self):
        class _C:
            def get_markets(self, series, status="open", limit=200):
                return ([illiquid("KXBTCD-ILQ"), liquid("KXBTCD-LIQ")]
                        if series == "KXBTCD" else [])

            def _req(self, *a, **k):
                return {"markets": [], "cursor": None}
        with tempfile.TemporaryDirectory() as t:
            sc = MarketScanner(_C(), router=_Router(), cfg=ScannerConfig(),
                               data_dir=t)
            sc._universe = None; sc._universe_ts = 0.0
            res = sc.scan_cycle()
            kept = {m["ticker"] for m in res["markets"]}
            shadow = {m["ticker"] for m in sc.shadow_population()}
            self.assertEqual(kept, {"KXBTCD-LIQ"})
            self.assertEqual(shadow, {"KXBTCD-ILQ"})
            self.assertEqual(kept & shadow, set(), "a market is in both")
            self.assertNotIn("shadow", json.dumps(res["report"]))


class TestBehaviouralEquivalence(unittest.TestCase):
    """PHASE 8: the ten trading invariants."""

    COUNTERS = ("scanned_raw", "after_status", "after_time_window",
                "after_liquidity", "after_classification", "scanner_kept",
                "supported", "model_evaluated", "positive_edge",
                "positive_net_ev", "risk_passed", "orders_submitted",
                "fills", "accepted")

    class _C:
        def get_markets(self, series, status="open", limit=200):
            if series != "KXBTCD":
                return []
            return ([illiquid(f"KXBTCD-ILQ-{i}", 600) for i in range(5)]
                    + [liquid("KXBTCD-LIQ", 600)])

        def get_market(self, tk):
            return {}

        def _req(self, *a, **k):
            return {"markets": [], "cursor": None}

    def _cycle(self, tmp, shadow_enabled):
        sc = MarketScanner(self._C(), router=_Router(), cfg=ScannerConfig(),
                           data_dir=tmp)
        sc._universe = None; sc._universe_ts = 0.0
        p = MarketOpportunityPipeline(self._C(), _Router(),
                                      gates=GateConfig(), scanner=sc,
                                      data_dir=tmp)
        res = p.run_cycle(max_accepted=3)
        store = BtcDailyEvidenceStore(tmp)
        ev = BtcDailyShadowEvaluator(_Router(), store,
                                     enabled=shadow_enabled)
        shadow = ev.run(sc.shadow_population(), res["report"].get("cycle_id"))
        return res["report"], [d.to_dict() for d in res["accepted"]], shadow, \
            store.predictions()

    def test_all_ten_trading_invariants_hold(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            off_rep, off_acc, off_sh, off_rows = self._cycle(a, False)
            on_rep, on_acc, on_sh, on_rows = self._cycle(b, True)
        for k in self.COUNTERS:                      # invariants 1-8, 10
            self.assertEqual(off_rep.get(k), on_rep.get(k), k)
        self.assertEqual(off_rep.get("rejections_by_reason"),
                         on_rep.get("rejections_by_reason"))
        self.assertEqual(len(off_acc), len(on_acc))  # invariant 3
        for x, y in zip(off_acc, on_acc):            # invariants 4, 9
            # decision_id carries a fresh uuid, and model_output.features
            # embeds a wall-clock-derived minutes_remaining that differs by
            # microseconds between two sequential runs. Comparing them would
            # test the clock, not equivalence; every field that decides a
            # trade is compared below.
            for d in (x, y):
                d.pop("decision_id")
                d.pop("model_output")
            self.assertEqual(x, y)
            self.assertEqual(x["taille"], y["taille"])        # sizing
            self.assertEqual(x["side"], y["side"])
            self.assertEqual(x["entry_ask"], y["entry_ask"])
            self.assertEqual(x["accepted"], y["accepted"])
        # and the shadow path did its own separate work
        self.assertEqual(off_sh["shadow_daily_predicted"], 0)
        self.assertEqual(on_sh["shadow_daily_predicted"], 5)
        self.assertEqual(off_rows, [])
        self.assertEqual(len(on_rows), 5)

    def test_shadow_counters_do_not_reuse_production_names(self):
        with tempfile.TemporaryDirectory() as t:
            _rep, _acc, shadow, _rows = self._cycle(t, True)
        for k in shadow:
            self.assertTrue(k.startswith("shadow_daily_"), k)
            self.assertNotIn(k, self.COUNTERS)

    def test_liquidity_counts_are_untouched_by_the_shadow_path(self):
        with tempfile.TemporaryDirectory() as t:
            rep, _acc, shadow, _rows = self._cycle(t, True)
        self.assertEqual(rep["after_liquidity"], 1)      # only the liquid one
        self.assertEqual(rep["rejections_by_reason"]["no_liquidity"], 5)
        self.assertEqual(shadow["shadow_daily_predicted"], 5)


class TestSettlementAndCounting(unittest.TestCase):
    """PHASES 6-7: outcomes come only from the API; N is not inflated."""

    def _seeded(self, tmp):
        ev, store = _ev(tmp)
        # two strikes on ONE expiry, one strike on another
        shared = _iso(-10)
        for tk in ("KXBTCD-E1-A", "KXBTCD-E1-B"):
            m = illiquid(tk, 600); m["close_time"] = shared
            ev.run([m])
        m = illiquid("KXBTCD-E2-A", 600); m["close_time"] = _iso(-20)
        ev.run([m])
        return ev, store

    def test_settlement_joins_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            _ev_, store = self._seeded(t)
            n = store.settle(lambda tk: {"result": "yes"})
            self.assertEqual(n, 3)
            self.assertEqual(store.settle(lambda tk: {"result": "yes"}), 0)
            self.assertEqual(len(store.calibration_records()), 3)

    def test_unmatured_observation_is_never_settled(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            ev.run([illiquid(mins=600)])       # closes in the future
            calls = []
            self.assertEqual(store.settle(lambda tk: calls.append(tk) or {}), 0)
            self.assertEqual(calls, [])

    def test_outcome_only_from_api_result(self):
        with tempfile.TemporaryDirectory() as t:
            _ev_, store = self._seeded(t)
            # a market that resolves to nothing must not become a sample
            self.assertEqual(store.settle(lambda tk: {"status": "closed"}), 0)
            self.assertEqual(store.calibration_records(), [])

    def test_independent_outcomes_counts_expiries_not_rows(self):
        with tempfile.TemporaryDirectory() as t:
            _ev_, store = self._seeded(t)
            store.settle(lambda tk: {"result": "yes"})
            st = store.statistics()
            self.assertEqual(st["settled_prediction_rows"], 3)
            self.assertEqual(st["settled_unique_tickers"], 3)
            self.assertEqual(st["independent_outcomes"], 2,
                             "two strikes on one expiry were counted twice")
            self.assertEqual(st["independent_outcome_basis"],
                             "distinct settled market_close_time")

    def test_statistics_reports_checkpoint_breakdown(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            for mins in (1400, 700, 300):
                ev.run([illiquid(mins=mins)])
            st = store.statistics()
            self.assertEqual(st["prediction_rows"], 3)
            self.assertEqual(set(st["by_checkpoint"]), {"24h", "12h", "6h"})


class TestFailureSemantics(unittest.TestCase):
    def test_unwritable_store_records_nothing_and_never_raises(self):
        store = BtcDailyEvidenceStore("/nonexistent-dir-t7k")
        ev = BtcDailyShadowEvaluator(_Router(), store, enabled=True)
        S = ev.run([illiquid()])
        self.assertEqual(S["shadow_daily_write_failed"], 1)
        self.assertEqual(S["shadow_daily_predicted"], 0)

    def test_exploding_strategy_cannot_escape(self):
        class _Boom(_DailyStrategy):
            def evaluate(self, *a, **k):
                raise RuntimeError("model exploded")
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t, router=_Router(_Boom()))
            S = ev.run([illiquid()])
            self.assertIsInstance(S, dict)
            self.assertEqual(store.predictions(), [])

    def test_per_cycle_cap_is_enforced(self):
        with tempfile.TemporaryDirectory() as t:
            ev, store = _ev(t)
            ev.max_per_cycle = 3
            S = ev.run([illiquid(f"KXBTCD-C-{i}", 600) for i in range(10)])
            self.assertEqual(S["shadow_daily_predicted"], 3)
            self.assertEqual(S["shadow_daily_capped"], 7)
            self.assertEqual(len(store.predictions()), 3)


if __name__ == "__main__":
    unittest.main()
