# -*- coding: utf-8 -*-
"""Blocker 3 — periodic, non-destructive broker reconciliation.

reconcile_with_broker() ran startup-only: 15 days of uptime meant 15 days
without a single broker/local verification. verify_against_broker() runs
periodically, keeps the broker authoritative, and on divergence halts NEW
submissions fail-closed WITHOUT auto-correcting uncertain financial state.
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402


def _pos(tid, ticker, side="yes", count=1):
    return {"trade_id": tid, "ticker": ticker, "side": side,
            "count": count, "count_initial": count, "avg_price": 40,
            "fees": 0.0, "opened_at": datetime.now(timezone.utc).isoformat(),
            "state": "open", "order_ids": [], "fill_ids": []}


class VerifyTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_reconcile_")
        self._old_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.cli = MagicMock()
        self.tlog = MagicMock()
        self.tlog.trades = []
        self.pm = bot.PositionManager(self.cli, self.tlog)
        self.pm.positions = {}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        bot.CFG.DATA_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestClassification(VerifyTestCase):

    def test_match_when_both_sides_agree(self):
        self.pm.positions["t1"] = _pos("t1", "KXA", "yes", 2)
        self.cli.get_positions.return_value = [{"ticker": "KXA", "position": 2}]
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "MATCH")
        self.assertIsNone(self.pm.reconcile_halt)

    def test_broker_position_missing_locally_is_mismatch(self):
        self.cli.get_positions.return_value = [{"ticker": "KXB", "position": 3}]
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "MISMATCH")
        self.assertEqual(rep["mismatches"][0]["kind"], "broker_only")
        self.assertIsNotNone(self.pm.reconcile_halt)
        # NON-destructive: nothing was rebuilt locally.
        self.assertEqual(self.pm.positions, {})

    def test_local_position_missing_at_broker_is_mismatch(self):
        self.pm.positions["t1"] = _pos("t1", "KXC")
        self.cli.get_positions.return_value = []
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "MISMATCH")
        self.assertEqual(rep["mismatches"][0]["kind"], "local_only")
        # NON-destructive: the local position is NOT removed or ghosted.
        self.assertIn("t1", self.pm.positions)
        self.assertEqual(self.pm.positions["t1"]["state"], "open")

    def test_quantity_mismatch(self):
        self.pm.positions["t1"] = _pos("t1", "KXD", "yes", 2)
        self.cli.get_positions.return_value = [{"ticker": "KXD", "position": 5}]
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "MISMATCH")
        self.assertEqual(rep["mismatches"][0]["kind"], "quantity_mismatch")
        self.assertEqual(self.pm.positions["t1"]["count"], 2)  # untouched

    def test_side_mismatch_is_a_mismatch(self):
        """Same ticker, opposite sign: never treated as equal."""
        self.pm.positions["t1"] = _pos("t1", "KXE", "no", 2)   # local net -2
        self.cli.get_positions.return_value = [{"ticker": "KXE", "position": 2}]
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "MISMATCH")

    def test_api_exception_is_broker_unavailable(self):
        self.cli.get_positions.side_effect = RuntimeError("timeout")
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "BROKER_UNAVAILABLE")
        # Absence of proof: no halt is set...
        self.assertIsNone(self.pm.reconcile_halt)

    def test_api_none_is_broker_unavailable(self):
        self.cli.get_positions.return_value = None
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "BROKER_UNAVAILABLE")

    def test_broker_unavailable_does_not_lift_an_existing_halt(self):
        """...and an existing halt is NOT cleared by an unreachable broker:
        recovery requires a positive MATCH, not a failed check."""
        self.pm.reconcile_halt = {"status": "MISMATCH", "detail": [],
                                  "at": "t0"}
        self.cli.get_positions.side_effect = RuntimeError("down")
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "BROKER_UNAVAILABLE")
        self.assertIsNotNone(self.pm.reconcile_halt)

    def test_unparsable_broker_rows_are_unknown_and_halt(self):
        self.cli.get_positions.return_value = ["not-a-dict"]
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "UNKNOWN")
        self.assertEqual(self.pm.reconcile_halt["status"], "UNKNOWN")

    def test_recovery_clears_halt_on_consistent_state(self):
        self.cli.get_positions.return_value = [{"ticker": "KXF", "position": 1}]
        self.pm.verify_against_broker()
        self.assertIsNotNone(self.pm.reconcile_halt)     # broker_only
        self.pm.positions["t1"] = _pos("t1", "KXF", "yes", 1)
        rep = self.pm.verify_against_broker()
        self.assertEqual(rep["status"], "MATCH")
        self.assertIsNone(self.pm.reconcile_halt)

    def test_verification_never_trades(self):
        self.cli.get_positions.return_value = [{"ticker": "KXG", "position": 4}]
        self.pm.verify_against_broker()
        self.cli.create_order.assert_not_called()
        self.cli.cancel_order.assert_not_called()


class TestEngineGate(VerifyTestCase):

    def test_reconcile_halt_blocks_submissions_fail_closed(self):
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr = self.pm
        eng.risk = MagicMock()
        self.pm.reconcile_halt = {"status": "MISMATCH", "detail": [],
                                  "at": "t0"}
        ok, guard = eng._post_balance_gates()
        self.assertFalse(ok)
        self.assertEqual(guard, "reconciliation_mismatch")
        eng.risk.can_trade.assert_not_called()

    def test_clear_halt_reaches_ordinary_risk_gates(self):
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr = self.pm
        eng.risk = MagicMock()
        eng.risk.can_trade.return_value = (False, "STOP JOURNALIER: x")
        self.pm.reconcile_halt = None
        ok, guard = eng._post_balance_gates()
        self.assertFalse(ok)
        self.assertNotEqual(guard, "reconciliation_mismatch")
        eng.risk.can_trade.assert_called_once()


if __name__ == "__main__":
    unittest.main()
