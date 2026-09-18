"""A cycle authorized to observe must never acquire write authority later.

All HTTP is a local recorder and the session raises on real transport. These
tests run the actual cycle, finalization, decision, OrderManager and client.
The capital control demonstrates that the same recorders observe a mutation.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401
import config
from config import CFG
import execution_engine
import test_readonly_observation_scan as observation
import test_shadow_write_layer_isolation as isolation


class ReadOnlyCycleAuthorityIsAnImmutableRestriction(
        isolation._IsolatedState, unittest.TestCase):

    def _run_cycle(self, *, drift=None, path="sequential", capital=False):
        os.environ["PROD_ACCESS_MODE"] = (
            config.PROD_CAPITAL if capital else config.PROD_READ_ONLY)
        client = isolation.ReadOnlyShadowNeverEntersTheWriteLayer._client(self)
        eng = isolation.ReadOnlyShadowNeverEntersTheWriteLayer._engine(
            self, client)
        auxiliary = observation._FullCycleEngine()
        for key, value in vars(auxiliary).items():
            if not hasattr(eng, key):
                setattr(eng, key, value)
        eng._balance_gate = lambda *a: (True, "synthetic balance")
        eng._post_balance_gates = lambda: (
            (True, None) if capital else (False, "equity_drawdown"))
        eng.posmgr.tickers_open = lambda: set()
        decisions = [isolation._Decision()]
        if drift == "second_decision":
            decisions.append(isolation._Decision())
        for decision in decisions:
            decision.reason = "synthetic authority regression"
        report = {"cycle_id": "immutable-readonly", "scanned_raw": 2,
                  "scanned": 2, "ranker_eligible": len(decisions),
                  "accepted": len(decisions), "rejections": {}}
        eng.pipeline.run_cycle = lambda **kw: {
            "report": report, "accepted": decisions}
        original_book = eng.fresh_book
        book_calls = []

        def book(ticker):
            book_calls.append(ticker)
            if drift == "book" or (
                    drift == "second_decision" and len(book_calls) == 2):
                os.environ["PROD_ACCESS_MODE"] = config.PROD_CAPITAL
            elif drift == "client_environment":
                client.env = "demo"
            return original_book(ticker)

        eng.fresh_book = book
        portfolio_calls = []

        def portfolio_check(*a, **kw):
            portfolio_calls.append(a)
            if drift == "sizing" and len(portfolio_calls) == 2:
                os.environ["PROD_ACCESS_MODE"] = config.PROD_CAPITAL
            return True, ""

        eng.risk.portfolio_check = portfolio_check
        if path == "parallel":
            class Future:
                def result(self):
                    return 500.0, None
            eng._executor = type(
                "Executor", (), {"submit": lambda *a: Future()})()
            eng._background_balance_health = lambda: (500.0, None)
        # Deliberately arm the other test-only gates. The restriction being
        # proved is the captured READ_ONLY cycle, independently of SHADOW.
        with patch.object(CFG, "SHADOW_MODE", False), \
                patch.object(CFG, "ALLOW_ORDER_SUBMISSION", True), \
                patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True), \
                patch.object(CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
                patch.object(CFG, "ORDER_TTL_SECONDS", 0), \
                patch.object(CFG, "KILL_SWITCH", False), \
                patch.object(execution_engine.JsonStore, "save", lambda *a: True):
            placed = getattr(eng, "_cycle_" + path)(42)
        return placed, report, eng.cycles_jsonl.rows, book_calls

    def _assert_observation_only(self, **kw):
        placed, report, rows, book_calls = self._run_cycle(**kw)
        self.assertGreater(len(book_calls), 0,
                           "the decision did not run; zero writes is vacuous")
        if kw.get("drift") == "second_decision":
            self.assertEqual(len(book_calls), 2,
                             "the second accepted decision was never reached")
        self.assertEqual(placed, 0)
        self.assertEqual(report.get("orders_submitted", 0), 0)
        self.assertEqual(self.boundary.place_and_track, [])
        self.assertEqual(self.boundary.create_order, [])
        self.assertEqual(self.boundary.cancel_order, [])
        self.assertEqual(self.boundary.mutating_http, [])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["scan_executed"])
        self.assertEqual(rows[0]["cycle"], 42)
        self.assertEqual(rows[0]["would_block_capital"], "equity_drawdown")
        return report

    def test_stable_sequential_cycle_observes(self):
        report = self._assert_observation_only()
        self.assertEqual(report["would_submit"], 1)

    def test_stable_parallel_cycle_observes(self):
        report = self._assert_observation_only(path="parallel")
        self.assertEqual(report["would_submit"], 1)

    def test_late_mode_drift_during_book_cannot_upgrade_sequential_cycle(self):
        self._assert_observation_only(drift="book")

    def test_late_mode_drift_during_book_cannot_upgrade_parallel_cycle(self):
        self._assert_observation_only(drift="book", path="parallel")

    def test_late_mode_drift_during_sizing_cannot_upgrade_cycle(self):
        self._assert_observation_only(drift="sizing")

    def test_drift_between_decisions_cannot_upgrade_second_decision(self):
        report = self._assert_observation_only(drift="second_decision")
        self.assertEqual(report.get("would_submit"), 2)

    def test_client_environment_drift_cannot_upgrade_readonly_cycle(self):
        self._assert_observation_only(drift="client_environment")

    def test_control_capital_cycle_reaches_synthetic_write_recorders(self):
        _, report, rows, _ = self._run_cycle(capital=True)
        self.assertEqual(report.get("orders_submitted"), 1)
        self.assertGreater(len(self.boundary.place_and_track), 0)
        self.assertGreater(len(self.boundary.create_order), 0)
        self.assertGreater(len(self.boundary.mutating_http), 0)
        self.assertIsNone(rows[0]["would_block_capital"])
