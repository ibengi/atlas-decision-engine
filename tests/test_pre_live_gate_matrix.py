# -*- coding: utf-8 -*-
"""Pre-LIVE gate matrix: every refusal path, ZERO broker writes.

One consolidated proof, per gate, that the engine refuses to submit AND
that no create_order / cancel_order ever reaches the broker client. The
gates are checked in the order the code evaluates them, so this file also
pins the precedence: a structural failure (persistence, contract cap,
reconciliation) is decided BEFORE any risk or market consideration.

Nothing here touches a network, DEMO or LIVE.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from config import CFG, contract_cap_config  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402

TICKER = "KXTEST-GATE-T1"


class _GateBase(unittest.TestCase):
    """LIVE-capable configuration: REQUIRE_PERSISTENT_STATE=True, an
    explicit contract cap of 1, submissions ON — so each test proves its
    OWN gate is what blocks, never a leftover global switch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_gate_")
        self._saved = {k: getattr(CFG, k) for k in (
            "DATA_DIR", "REQUIRE_PERSISTENT_STATE", "ALLOW_ORDER_SUBMISSION",
            "MAX_CONTRACTS_PER_ORDER", "MAX_OPEN_POSITIONS", "KILL_SWITCH",
            "ALLOW_FRESH_STATE", "SUBMIT_DEDUP_TTL_S")}
        CFG.DATA_DIR = self.tmp
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True          # first boot on a fresh dir
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"
        CFG.KILL_SWITCH = False
        PersistenceSentinel.reset()
        self.client = self._client()
        self.tlog = bot.TradeLogger()
        self.pm = bot.PositionManager(self.client, self.tlog)
        self.om = bot.OrderManager(self.client)
        self.risk = bot.RiskManager(self.tlog, self.pm, capital=100.0)
        CFG.ALLOW_FRESH_STATE = False
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(CFG, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _client():
        c = MagicMock()
        c.env = "demo"
        c.last_http_status = 201
        c.get_positions.return_value = []
        c.create_order.return_value = {"order_id": "SHOULD-NEVER-HAPPEN",
                                       "status": "executed", "fill_count": 1,
                                       "remaining_count": 0}
        return c

    def assertNoBrokerWrites(self):
        self.assertEqual(self.client.create_order.call_count, 0,
                         "create_order reached the broker")
        self.assertEqual(self.client.cancel_order.call_count, 0,
                         "cancel_order reached the broker")

    def submit(self, count=1, price=40):
        return self.om.place_and_track(TICKER, "yes", count, price)

    def engine_gate(self):
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr, eng.risk = self.pm, self.risk
        return eng._post_balance_gates()

    def assertBlocked(self, status, guard=None):
        res = self.submit()
        self.assertEqual(res.state, "rejected")
        self.assertEqual(res.status, status)
        self.assertIsNone(res.order_id)
        self.assertEqual(res.filled, 0)
        self.assertNoBrokerWrites()
        if guard is not None:
            ok, got = self.engine_gate()
            self.assertFalse(ok)
            self.assertEqual(got, guard)
        return res


class SubmissionPathGateMatrix(_GateBase):

    def test_1_persistence_failure_blocks_before_broker_call(self):
        PersistenceSentinel.record_failure(
            "/data/state5/kalshi_trades.json", "disque en lecture seule")
        self.assertBlocked("blocked:persistence_failure",
                           guard="persistence_failure")

    def test_2_reconciliation_unknown_blocks(self):
        """A broker quantity that cannot be parsed is UNKNOWN, never 0."""
        self.client.get_positions.return_value = [
            {"ticker": TICKER, "position_fp": "not-a-number"}]
        rep = self.pm.reconcile_with_broker()

        self.assertEqual(rep["status"], "UNKNOWN")
        self.assertIsNotNone(self.pm.reconcile_halt)
        ok, guard = self.engine_gate()
        self.assertFalse(ok)
        self.assertEqual(guard, "reconciliation_unknown")
        self.assertNoBrokerWrites()

    def test_3_reconciliation_mismatch_blocks(self):
        self.pm.positions["t-local"] = {
            "trade_id": "t-local", "ticker": TICKER, "side": "yes",
            "count": 1, "count_initial": 1, "avg_price": 40, "fees": 0.0,
            "opened_at": "2026-08-28T00:00:00+00:00", "state": "open",
            "order_ids": [], "fill_ids": [], "strategy": "test"}
        self.client.get_positions.return_value = []   # broker says flat
        rep = self.pm.reconcile_with_broker()

        self.assertEqual(rep["status"], "MISMATCH")
        self.assertEqual(rep["mismatches"][0]["kind"], "local_only")
        self.assertEqual(self.pm.open_count(), 1, "nothing deleted")
        ok, guard = self.engine_gate()
        self.assertFalse(ok)
        self.assertEqual(guard, "reconciliation_mismatch")
        self.assertNoBrokerWrites()

    def test_4_broker_unavailable_blocks(self):
        self.client.get_positions.side_effect = bot.KalshiAPIError(
            503, "service unavailable")
        rep = self.pm.reconcile_with_broker()

        self.assertEqual(rep["status"], "BROKER_UNAVAILABLE")
        ok, guard = self.engine_gate()
        self.assertFalse(ok)
        self.assertEqual(guard, "reconciliation_broker_unavailable")
        self.assertNoBrokerWrites()

    def test_5_duplicate_submission_guard_blocks(self):
        self.om.session_submitted[TICKER] = __import__("time").time()
        self.assertBlocked("blocked:duplicate_submission_guard")

    def test_6_max_open_positions_blocks(self):
        for i in range(CFG.MAX_OPEN_POSITIONS):
            self.pm.positions[f"t{i}"] = {
                "trade_id": f"t{i}", "ticker": f"KXFULL-{i}", "side": "yes",
                "count": 1, "count_initial": 1, "avg_price": 40, "fees": 0.0,
                "opened_at": "2026-08-28T00:00:00+00:00", "state": "open",
                "order_ids": [], "fill_ids": [], "strategy": "test"}
        self.client.get_positions.return_value = [
            {"ticker": f"KXFULL-{i}", "position": 1}
            for i in range(CFG.MAX_OPEN_POSITIONS)]
        self.pm.reconcile_with_broker()      # MATCH: not a reconciliation halt

        ok, guard = self.engine_gate()
        self.assertFalse(ok)
        self.assertEqual(guard, "max_open_positions")
        self.assertNoBrokerWrites()

    def test_7_explicit_contract_cap_is_enforced(self):
        cap, err = contract_cap_config(live_capable=True)
        self.assertEqual((cap, err), (1, None))
        # a size above the cap is BLOCKED, never silently clamped
        res = self.submit(count=2)
        self.assertEqual(res.status, "blocked:contract_cap_exceeded")
        self.assertEqual(res.state, "rejected")
        self.assertNoBrokerWrites()

    def test_8_missing_or_invalid_live_cap_fails_closed(self):
        for raw in (None, "", "zero", "0", "-1", "1.5", "99999"):
            with self.subTest(cap=raw):
                CFG.MAX_CONTRACTS_PER_ORDER = raw
                cap, err = contract_cap_config(live_capable=True)
                self.assertIsNone(cap)
                self.assertTrue(err)
                res = self.submit()
                self.assertEqual(res.status, "blocked:contract_cap_invalid")
                ok, guard = self.engine_gate()
                self.assertFalse(ok)
                self.assertEqual(guard, "contract_cap_invalid")
                self.assertNoBrokerWrites()

    def test_9_kill_switch_blocks_the_cycle(self):
        """KILL_SWITCH is the cycle's first gate: it returns 0 trades
        before the balance call, the risk gates or the scan."""
        CFG.KILL_SWITCH = True
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr, eng.risk = self.pm, self.risk
        eng._record_cycle_evidence = MagicMock()
        eng.stats = MagicMock()
        eng._balance_gate = MagicMock(
            side_effect=AssertionError("balance must not be read"))
        eng.pipeline = MagicMock()
        eng.pipeline.run_cycle.side_effect = AssertionError("scan must not run")

        with self.assertLogs("RISK", level="WARNING") as logs:
            traded = eng._cycle_sequential(1)

        self.assertEqual(traded, 0)
        self.assertTrue(any("KILL_SWITCH actif" in m for m in logs.output))
        eng._record_cycle_evidence.assert_called_once_with(
            1, "sequential", "kill_switch")
        eng.pipeline.run_cycle.assert_not_called()
        eng._balance_gate.assert_not_called()
        self.assertNoBrokerWrites()

    def test_10_allow_order_submission_false_blocks(self):
        CFG.ALLOW_ORDER_SUBMISSION = False
        self.assertBlocked("blocked:submission_disabled")

    def test_gate_precedence_structural_before_risk(self):
        """Persistence outranks everything: even with every other gate
        satisfied, a sentinel failure is the reported blocker."""
        PersistenceSentinel.record_failure("x", "y")
        CFG.ALLOW_ORDER_SUBMISSION = False       # would also block
        res = self.submit()
        self.assertEqual(res.status, "blocked:persistence_failure")
        self.assertNoBrokerWrites()


if __name__ == "__main__":
    unittest.main()
