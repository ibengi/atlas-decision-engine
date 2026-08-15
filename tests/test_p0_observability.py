# -*- coding: utf-8 -*-
"""P0 observability: prove a zero-trade cycle is explainable from evidence.

Before P0 a cycle that returned early at a global guard wrote nothing at all,
and a cycle whose universe was eliminated inside the scanner wrote only
aggregate counters. Both looked identical from the outside to an engine that
was not running. These tests pin the distinction down.

The load-bearing invariant is the LAST class in this file: P0 changes what is
recorded and nothing about what is traded.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import research_export as rx
from market_scanner import MarketScanner, ScannerConfig
from opportunity_pipeline import MarketOpportunityPipeline
from strategy_router import GateConfig


def _iso(minutes_ahead):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes_ahead)).isoformat()


def mk(ticker, mins=120, yes_ask=45, yes_bid=44, status="open", **kw):
    m = {"ticker": ticker, "status": status, "close_time": _iso(mins),
         "yes_ask": yes_ask, "yes_bid": yes_bid, "volume_24h": 100}
    m.update(kw)
    return m


def dead(ticker, mins=120):
    """A market with no executable side and no activity of any kind."""
    return {"ticker": ticker, "status": "open", "close_time": _iso(mins),
            "yes_ask": None, "yes_bid": None, "no_ask": None, "no_bid": None,
            "volume_24h": 0, "volume": 0, "open_interest": 0, "liquidity": 0}


class FakeClient:
    def __init__(self, by_series=None):
        self.by_series = by_series or {}

    def _req(self, method, path, params=None):
        return {"markets": [], "cursor": None}

    def get_markets(self, series, status="open", limit=200):
        return self.by_series.get(series, [])


class FakeRouter:
    """Serves one market type. Anything else is unsupported."""

    def __init__(self, supported=("btc_above_strike_daily",)):
        self._supported = tuple(supported)

    def supported_market_types(self):
        return self._supported

    def tradeable_market_types(self):
        return self._supported

    def route(self, market_type):
        return None


def _cfg(**kw):
    c = ScannerConfig()
    c.general_crawl = False
    c.min_minutes = kw.get("min_minutes", 5)
    c.lookahead_hours = kw.get("lookahead_hours", 24)
    c.max_markets_per_cycle = kw.get("cap", 300)
    return c


def _scan(markets, series="KXBTCD", router=None, **cfgkw):
    cli = FakeClient(by_series={series: markets})
    with tempfile.TemporaryDirectory() as d:
        sc = MarketScanner(cli, router=router or FakeRouter(),
                           cfg=_cfg(**cfgkw), data_dir=d)
        sc.target_series = lambda: [series]
        return sc.scan_cycle()


def _detail_reasons(rep):
    return [r["rejection_reason"] for r in rep["rejections_detail"]]


# ── 1-5, 8: the scanner-stage causes, each provable per market ─────────────

class TestScannerStageEvidence(unittest.TestCase):

    def test_1_zero_market_universe_is_distinguishable(self):
        """An empty universe reports zero scanned and rejects nothing.

        This is the case that must never again look like "engine down": the
        report exists, it just describes an empty population.
        """
        rep = _scan([])["report"]
        self.assertEqual(rep["funnel"]["scanned_raw"], 0)
        self.assertEqual(rep["rejections_detail"], [])
        self.assertEqual(rep["excluded_by_reason"], {})

    def test_2_all_outside_lookahead_named_per_market(self):
        markets = [mk(f"KXBTCD-{i}", mins=48 * 60) for i in range(4)]
        rep = _scan(markets)["report"]
        self.assertEqual(rep["funnel"]["after_time_window"], 0)
        self.assertEqual(rep["excluded_by_reason"]["outside_time_window"], 4)
        self.assertEqual(_detail_reasons(rep), ["outside_time_window"] * 4)
        row = rep["rejections_detail"][0]
        self.assertEqual(row["rejection_stage"], "time_window")
        self.assertTrue(row["ticker"].startswith("KXBTCD-"))
        # minutes_remaining is the fact that makes the rejection checkable
        self.assertGreater(row["minutes_remaining"], 24 * 60)

    def test_2b_closes_too_soon_is_a_different_reason(self):
        """`outside_time_window` must not absorb the near-expiry cohort.

        These are opposite ends of the horizon and conflating them is what
        made the 201-to-0 funnel unreadable in the first place.
        """
        rep = _scan([mk("KXBTCD-SOON", mins=1)])["report"]
        self.assertEqual(_detail_reasons(rep), ["closes_too_soon"])
        self.assertNotIn("outside_time_window", rep["excluded_by_reason"])

    def test_3_all_no_liquidity_carries_the_diagnostic(self):
        rep = _scan([dead(f"KXBTCD-{i}") for i in range(3)])["report"]
        self.assertEqual(rep["funnel"]["after_liquidity"], 0)
        self.assertEqual(_detail_reasons(rep), ["no_liquidity"] * 3)
        diag = rep["rejections_detail"][0]["liquidity_diagnostic"]
        # the evidence must show WHY it was judged illiquid, not just that it was
        self.assertIsNone(diag["best_bid"])
        self.assertIsNone(diag["best_ask"])
        self.assertEqual(diag["volume"], 0)
        self.assertEqual(diag["open_interest"], 0)

    def test_4_unsupported_markets_named_at_classification(self):
        router = FakeRouter(supported=("something_else",))
        rep = _scan([mk("KXBTCD-26AUG20-T65000")], router=router)["report"]
        self.assertEqual(rep["funnel"]["after_classification"], 0)
        reasons = _detail_reasons(rep)
        self.assertEqual(len(reasons), 1)
        self.assertIn(reasons[0],
                      ("no_compatible_strategy", "unknown_market_type"))
        self.assertEqual(rep["rejections_detail"][0]["rejection_stage"],
                         "classification")

    def test_5_closed_markets_named_at_status(self):
        rep = _scan([mk("KXBTCD-X", status="settled")])["report"]
        self.assertEqual(_detail_reasons(rep), ["closed_or_settled"])
        self.assertEqual(rep["rejections_detail"][0]["rejection_stage"],
                         "status")

    def test_detail_is_capped_and_the_cap_is_reported(self):
        """Truncation must be visible. A silent cap reads as full coverage."""
        os.environ["SCANNER_REJECT_DETAIL_MAX"] = "5"
        try:
            rep = _scan([dead(f"KXBTCD-{i}") for i in range(12)])["report"]
        finally:
            del os.environ["SCANNER_REJECT_DETAIL_MAX"]
        self.assertEqual(len(rep["rejections_detail"]), 5)
        self.assertEqual(rep["rejections_detail_truncated"], 7)
        # aggregate counters stay exact regardless of the cap
        self.assertEqual(rep["excluded_by_reason"]["no_liquidity"], 12)


# ── the pipeline carries the stages and persists the rows ─────────────────

class TestPipelineFunnelEvidence(unittest.TestCase):

    def _pipeline(self, markets, directory, router=None):
        cli = FakeClient(by_series={"KXBTCD": markets})
        sc = MarketScanner(cli, router=router or FakeRouter(), cfg=_cfg(),
                           data_dir=directory)
        sc.target_series = lambda: ["KXBTCD"]
        return MarketOpportunityPipeline(
            cli, router or FakeRouter(), gates=GateConfig(),
            scanner=sc, data_dir=directory)

    def test_all_scanner_stages_reach_the_cycle_report(self):
        with tempfile.TemporaryDirectory() as d:
            pipe = self._pipeline([dead("KXBTCD-A"), dead("KXBTCD-B")], d)
            rep = pipe.run_cycle()["report"]
        for key in ("after_status", "after_time_window", "after_liquidity",
                    "after_classification", "scanner_kept", "occurred_at"):
            self.assertIn(key, rep)
        self.assertEqual(rep["after_status"], 2)
        self.assertEqual(rep["after_liquidity"], 0)

    def test_scanner_rejections_are_written_to_their_own_sink(self):
        with tempfile.TemporaryDirectory() as d:
            pipe = self._pipeline([dead("KXBTCD-A"), dead("KXBTCD-B")], d)
            rep = pipe.run_cycle()["report"]
            path = os.path.join(d, "funnel_rejections.jsonl")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["rejection"]["ticker"] for r in rows},
                         {"KXBTCD-A", "KXBTCD-B"})
        self.assertTrue(all(r["cycle_id"] == rep["cycle_id"] for r in rows))
        self.assertTrue(all(r["observed_at"] for r in rows))

    def test_funnel_rows_never_pollute_the_decision_dataset(self):
        """A scanner drop is not a decision. Merging them would make the
        decision dataset mean two different things."""
        with tempfile.TemporaryDirectory() as d:
            pipe = self._pipeline([dead("KXBTCD-A")], d)
            pipe.run_cycle()
            self.assertFalse(os.path.exists(os.path.join(d, "decisions.jsonl")))
            self.assertTrue(os.path.exists(
                os.path.join(d, "funnel_rejections.jsonl")))


# ── 6, 7, 8: global guards become nameable ────────────────────────────────

class _StubEngine:
    """The evidence recorder, exercised without building a full engine.

    `_record_cycle_evidence` is deliberately free of engine state beyond the
    jsonl sink, so it can be bound to a stub. That is a property worth
    keeping: evidence writing must not depend on a healthy trading engine.
    """

    def __init__(self, directory):
        from opportunity_pipeline import RotatingJsonl
        from execution_engine import ExecutionEngine
        self.cycles_jsonl = RotatingJsonl(
            os.path.join(directory, "cycles.jsonl"))
        # borrowed rather than restated: a copy here would drift silently
        self._FUNNEL_KEYS = ExecutionEngine._FUNNEL_KEYS

    def record(self, *a, **kw):
        from execution_engine import ExecutionEngine
        return ExecutionEngine._record_cycle_evidence(self, *a, **kw)


class TestGlobalGuardEvidence(unittest.TestCase):

    def test_7_kill_switch_is_named_and_scan_absent(self):
        with tempfile.TemporaryDirectory() as d:
            row = _StubEngine(d).record(4, "sequential", "kill_switch")
        self.assertEqual(row["blocking_global_guard"], "kill_switch")
        self.assertFalse(row["scan_executed"])
        # the whole point: no fabricated zeros for a scan that never happened
        self.assertNotIn("funnel", row)

    def test_8_max_open_positions_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            row = _StubEngine(d).record(9, "sequential", "max_open_positions")
        self.assertEqual(row["blocking_global_guard"], "max_open_positions")
        self.assertFalse(row["scan_executed"])

    def test_6_risk_blocked_after_a_scan_keeps_the_funnel(self):
        """Opportunity existed and risk stopped it: both facts survive."""
        pipeline = {"report": {"cycle_id": "abc123", "scanned_raw": 40,
                               "after_liquidity": 12, "supported": 9,
                               "model_evaluated": 9, "positive_edge": 2,
                               "accepted": 1, "rejections_by_reason": {}}}
        with tempfile.TemporaryDirectory() as d:
            row = _StubEngine(d).record(11, "parallel", "max_open_positions",
                                        pipeline=pipeline)
        self.assertEqual(row["blocking_global_guard"], "max_open_positions")
        self.assertTrue(row["scan_executed"])
        self.assertEqual(row["funnel"]["accepted"], 1)
        self.assertEqual(row["cycle_id"], "abc123")

    def test_9_normal_cycle_records_no_blocking_guard(self):
        pipeline = {"report": {"cycle_id": "ok1", "scanned_raw": 20,
                               "accepted": 0, "rejections_by_reason": {}}}
        with tempfile.TemporaryDirectory() as d:
            row = _StubEngine(d).record(1, "sequential", None,
                                        pipeline=pipeline)
        self.assertIsNone(row["blocking_global_guard"])
        self.assertTrue(row["scan_executed"])

    def test_balance_gate_carries_its_detail(self):
        with tempfile.TemporaryDirectory() as d:
            row = _StubEngine(d).record(2, "sequential", "balance_gate",
                                        detail="solde indisponible")
        self.assertEqual(row["blocking_global_guard"], "balance_gate")
        self.assertIn("solde", row["blocking_detail"])

    def test_absent_funnel_counter_is_absent_not_zero(self):
        """A missing key and a zero are different facts and must stay so."""
        pipeline = {"report": {"cycle_id": "z", "scanned_raw": 0,
                               "rejections_by_reason": {}}}
        with tempfile.TemporaryDirectory() as d:
            row = _StubEngine(d).record(3, "sequential", None,
                                        pipeline=pipeline)
        self.assertEqual(row["funnel"]["scanned_raw"], 0)
        self.assertNotIn("model_evaluated", row["funnel"])

    def test_can_trade_prose_maps_to_stable_guard_names(self):
        from execution_engine import _classify_can_trade_guard as clf
        self.assertEqual(clf("STOP JOURNALIER: PnL realise -5.00$"),
                         "daily_loss_stop")
        self.assertEqual(clf("ARRET: 3 pertes consecutives >= 3"),
                         "consecutive_loss_breaker")
        self.assertEqual(clf("max 3 trades/cycle atteint"), "max_trades_cycle")
        self.assertEqual(clf("budget de risque ouvert atteint"),
                         "open_risk_budget")
        # an unrecognised wording admits it rather than guessing
        self.assertEqual(clf("something nobody mapped"),
                         "risk_can_trade_unclassified")


# ── 11, 12: persistence and contract compatibility ────────────────────────

class TestResearchSurface(unittest.TestCase):

    def _write(self, directory, name, rows):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_11_evidence_survives_restart_and_cursors_resume(self):
        """Rows are read back from disk by a fresh reader, and a cursor
        handed back resumes after the row it names."""
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "cycles.jsonl",
                        [{"cycle": i, "blocking_global_guard": None}
                         for i in range(5)])
            first = rx.cycles(d, "", 2)
            self.assertEqual(len(first["rows"]), 2)
            self.assertTrue(first["has_more"])
            second = rx.cycles(d, first["next_cursor"], 2)
            self.assertEqual(second["cursor_state"], "resumed")
            self.assertEqual([r["cycle"] for r in second["rows"]], [2, 3])

    def test_rotated_generations_are_read_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "cycles.jsonl.1", [{"cycle": 1}])
            self._write(d, "cycles.jsonl", [{"cycle": 2}])
            rows = rx.cycles(d, "", 100)["rows"]
        self.assertEqual([r["cycle"] for r in rows], [1, 2])

    def test_unparseable_lines_are_counted_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "cycles.jsonl"), "w") as fh:
                fh.write('{"cycle": 1}\n')
                fh.write("{ this is not json\n")
            out = rx.cycles(d, "", 100)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["unreadable_lines"], 1)

    def test_missing_files_serve_an_empty_dataset_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            for fn in (rx.cycles, rx.funnel_rejections):
                out = fn(d, "", 10)
                self.assertEqual(out["rows"], [])
                self.assertEqual(out["available_rows"], 0)

    def test_12_existing_v1_datasets_are_untouched(self):
        """Backward compatibility: contract version and the schema versions
        of the pre-existing datasets do not move because P0 added rows
        elsewhere."""
        self.assertEqual(rx.RESEARCH_CONTRACT_VERSION, 1)
        self.assertEqual(rx.SCHEMA_VERSIONS["decisions"], 1)
        self.assertEqual(rx.SCHEMA_VERSIONS["settlements"], 2)
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "decisions.jsonl", [{"cycle_id": "c", "decision": {}}])
            before = rx.decisions(d, "", 10)
            self._write(d, "cycles.jsonl", [{"cycle": 1}])
            self._write(d, "funnel_rejections.jsonl", [{"cycle_id": "c"}])
            after = rx.decisions(d, "", 10)
        self.assertEqual(before["rows"], after["rows"])
        self.assertEqual(before["schema_version"], after["schema_version"])

    def test_new_datasets_declare_themselves_in_capabilities(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "cycles.jsonl", [{"cycle": 1}])
            manifest = rx.capabilities(d)
        by_name = {x["name"]: x for x in manifest["datasets"]}
        self.assertIn("cycles", by_name)
        self.assertIn("funnel_rejections", by_name)
        self.assertEqual(by_name["cycles"]["row_count"], 1)
        self.assertFalse(by_name["cycles"]["permanent"])
        self.assertIn("scan_executed", by_name["cycles"]["known_gaps"])


# ── 6, 10, and the invariant that matters most ────────────────────────────

class TestBehaviouralEquivalence(unittest.TestCase):
    """P0 must change what is recorded and nothing about what is traded."""

    def _run(self, markets, directory):
        cli = FakeClient(by_series={"KXBTCD": markets})
        router = FakeRouter()
        sc = MarketScanner(cli, router=router, cfg=_cfg(), data_dir=directory)
        sc.target_series = lambda: ["KXBTCD"]
        pipe = MarketOpportunityPipeline(cli, router, gates=GateConfig(),
                                         scanner=sc, data_dir=directory)
        return pipe.run_cycle()

    #: Every field of the cycle report that the ExecutionEngine reads to
    #: decide what to trade. If any of these moved, P0 changed behaviour.
    TRADING_KEYS = ("accepted", "supported", "model_evaluated",
                    "positive_edge", "positive_net_ev", "risk_passed",
                    "orders_submitted", "fills", "scanned_raw",
                    "rejections_by_reason", "scanner_included", "eligible")

    def test_scanner_survivors_are_identical_with_detail_on_or_off(self):
        """The recorder must not filter. Same markets in, same markets out,
        whether or not per-market detail is being collected."""
        markets = [mk("KXBTCD-A"), dead("KXBTCD-B"),
                   mk("KXBTCD-C", mins=48 * 60), mk("KXBTCD-D", status="closed")]
        full = _scan(markets)
        os.environ["SCANNER_REJECT_DETAIL_MAX"] = "0"
        try:
            capped = _scan(markets)
        finally:
            del os.environ["SCANNER_REJECT_DETAIL_MAX"]
        self.assertEqual([m["ticker"] for m in full["markets"]],
                         [m["ticker"] for m in capped["markets"]])
        self.assertEqual(full["report"]["funnel"], capped["report"]["funnel"])
        self.assertEqual(full["report"]["excluded_by_reason"],
                         capped["report"]["excluded_by_reason"])
        # only the evidence differs
        self.assertEqual(capped["report"]["rejections_detail"], [])
        self.assertEqual(len(full["report"]["rejections_detail"]), 3)

    def test_trading_outputs_are_deterministic_across_runs(self):
        markets = [mk("KXBTCD-A"), dead("KXBTCD-B"),
                   mk("KXBTCD-C", mins=48 * 60)]
        with tempfile.TemporaryDirectory() as d1:
            a = self._run(markets, d1)["report"]
        with tempfile.TemporaryDirectory() as d2:
            b = self._run(markets, d2)["report"]
        for key in self.TRADING_KEYS:
            self.assertEqual(a.get(key), b.get(key), f"{key} diverged")

    def test_10_evidence_write_failure_cannot_break_a_cycle(self):
        """An unwritable sink degrades evidence, never trading."""
        class Exploding:
            def write(self, obj):
                raise OSError("disk full")

        with tempfile.TemporaryDirectory() as d:
            stub = _StubEngine(d)
            stub.cycles_jsonl = Exploding()
            row = stub.record(1, "sequential", None)   # must not raise
        self.assertEqual(row["cycle"], 1)

    def test_funnel_rejection_write_failure_is_contained(self):
        class Exploding:
            def write(self, obj):
                raise OSError("disk full")

        with tempfile.TemporaryDirectory() as d:
            cli = FakeClient(by_series={"KXBTCD": [dead("KXBTCD-A")]})
            router = FakeRouter()
            sc = MarketScanner(cli, router=router, cfg=_cfg(), data_dir=d)
            sc.target_series = lambda: ["KXBTCD"]
            pipe = MarketOpportunityPipeline(cli, router, gates=GateConfig(),
                                             scanner=sc, data_dir=d)
            pipe.funnel_jsonl = Exploding()
            report = pipe.run_cycle()["report"]     # must not raise
        self.assertEqual(report["accepted"], 0)
        self.assertEqual(report["after_liquidity"], 0)


if __name__ == "__main__":
    unittest.main()
