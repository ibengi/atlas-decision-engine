# -*- coding: utf-8 -*-
"""Startup broker reconciliation: shared parser + strictly NON-destructive.

Since the 2026-08-31 incident (position_fp mis-parse -> broker judged flat
-> the three freshly restored positions destroyed as "ghosts"), the startup
path follows the same law as the periodic verifier: no broker discrepancy
may delete, rebuild, or mutate local financial state. Every non-MATCH
outcome arms reconcile_halt (submissions fail closed) and leaves positions,
journal and broker untouched. Parse failure is never converted to qty 0.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from position_manager import PositionManager  # noqa: E402


def _pos(tid, ticker, side, count):
    return {"trade_id": tid, "ticker": ticker, "side": side,
            "count_initial": count, "count": count, "avg_price": 50,
            "fees": 0.0, "opened_at": "2026-01-01T00:00:00Z",
            "order_ids": [], "fill_ids": [], "state": "open",
            "strategy": "test", "market_score": None,
            "entry_edge": None, "entry_ev": None}


class ParseBrokerQtyTest(unittest.TestCase):
    """One shared parser for startup AND periodic reconciliation."""

    def _ok(self, bp, expected):
        qty, err = PositionManager.parse_broker_qty(bp)
        self.assertIsNone(err, f"{bp!r}: unexpected error {err}")
        self.assertEqual(qty, expected)

    def _err(self, bp):
        qty, err = PositionManager.parse_broker_qty(bp)
        self.assertIsNone(qty, f"{bp!r}: parse failure must never yield a "
                               f"quantity (got {qty})")
        self.assertTrue(err)

    def test_position_fp_signed_decimal_strings(self):
        self._ok({"position_fp": "-6.00"}, -6)
        self._ok({"position_fp": "+6.00"}, 6)
        self._ok({"position_fp": "6.00"}, 6)
        self._ok({"position_fp": "44.00"}, 44)

    def test_zero_is_a_measurement_not_a_failure(self):
        self._ok({"position_fp": "0.00"}, 0)
        self._ok({"position": 0}, 0)

    def test_legacy_fields_still_parse(self):
        self._ok({"position": -5}, -5)
        self._ok({"quantity": 3}, 3)
        self._ok({"count": "2"}, 2)

    def test_malformed_is_unknown_never_zero(self):
        for bp in ({"position_fp": "junk"}, {"position_fp": ""},
                   {"position_fp": None, "position": "abc"},
                   {"position_fp": float("nan")},
                   {"position_fp": "inf"}, {"position": float("inf")}):
            self._err(bp)

    def test_missing_all_quantity_fields_is_unknown(self):
        self._err({"ticker": "KX", "market_exposure_dollars": "1.14"})

    def test_fractional_contracts_are_refused(self):
        self._err({"position_fp": "6.50"})
        self._err({"position": 6.5})

    def test_conflicting_fields_are_unknown(self):
        self._err({"position": 5, "position_fp": "6.00"})

    def test_agreeing_fields_pass(self):
        self._ok({"position": -6, "position_fp": "-6.00"}, -6)


class _StartupBase(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.tlog = MagicMock()
        self.tlog.trades = []
        self.pm = bot.PositionManager(self.client, self.tlog)
        self.pm.positions = {}
        self.pm.flush = MagicMock()

    def _run(self, broker):
        if isinstance(broker, Exception):
            self.client.get_positions.side_effect = broker
        else:
            self.client.get_positions.return_value = broker
        with patch("position_manager.JsonStore.save"):
            return self.pm.reconcile_with_broker()

    def _assert_halted_untouched(self, report, status, before):
        self.assertEqual(report["status"], status)
        self.assertIsNotNone(self.pm.reconcile_halt,
                             "every non-MATCH outcome must arm the halt")
        self.assertEqual(self.pm.reconcile_halt["status"], status)
        self.assertEqual(self.pm.positions, before,
                         "no broker discrepancy may mutate local positions")
        self.pm.flush.assert_not_called()
        self.assertEqual(self.client.create_order.call_count, 0)
        self.assertEqual(self.client.cancel_order.call_count, 0)
        self.tlog.settle_trade.assert_not_called()


class StartupNonDestructiveTest(_StartupBase):

    def test_valid_match_clears(self):
        self.pm.positions = {"a": _pos("a", "KXA", "yes", 2)}
        report = self._run([{"ticker": "KXA", "position_fp": "2.00"}])
        self.assertEqual(report["status"], "MATCH")
        self.assertEqual(report["matched"], ["KXA"])
        self.assertIsNone(self.pm.reconcile_halt)
        self.assertEqual(len(self.pm.positions), 1)

    def test_broker_only_halts_without_adoption(self):
        before = dict(self.pm.positions)          # empty
        report = self._run([{"ticker": "KXNEW", "position_fp": "3.00"}])
        self._assert_halted_untouched(report, "MISMATCH", before)
        self.assertEqual(report["mismatches"],
                         [{"ticker": "KXNEW", "kind": "broker_only",
                           "broker": 3, "local": None}])
        self.assertEqual(self.pm.positions, {},
                         "no brk- reconstruction, no invented entry price")

    def test_local_only_halts_without_delete(self):
        self.pm.positions = {"x": _pos("x", "KXLOCAL", "yes", 1)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        report = self._run([])
        self._assert_halted_untouched(report, "MISMATCH", before)
        self.assertEqual(report["mismatches"][0]["kind"], "local_only")
        self.assertIn("x", self.pm.positions,
                      "the 2026-08-31 failure mode: ghost cleanup must be "
                      "gone -- local positions survive a flat broker read")

    def test_quantity_mismatch_preserves_both_values(self):
        self.pm.positions = {"c": _pos("c", "KXQ", "no", 5)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        report = self._run([{"ticker": "KXQ", "position_fp": "-6.00"}])
        self._assert_halted_untouched(report, "MISMATCH", before)
        self.assertEqual(report["mismatches"],
                         [{"ticker": "KXQ", "kind": "quantity_mismatch",
                           "broker": -6, "local": -5}])

    def test_side_mismatch_halts(self):
        self.pm.positions = {"s": _pos("s", "KXS", "yes", 2)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        report = self._run([{"ticker": "KXS", "position_fp": "-2.00"}])
        self._assert_halted_untouched(report, "MISMATCH", before)
        self.assertEqual(report["mismatches"][0]["kind"], "side_mismatch")

    def test_malformed_position_fp_is_unknown_never_flat(self):
        self.pm.positions = {"m": _pos("m", "KXM", "yes", 2)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        report = self._run([{"ticker": "KXM", "position_fp": "garbage"}])
        self._assert_halted_untouched(report, "UNKNOWN", before)

    def test_missing_every_quantity_field_is_unknown(self):
        self.pm.positions = {"m": _pos("m", "KXM", "yes", 2)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        report = self._run([{"ticker": "KXM",
                             "market_exposure_dollars": "1.00"}])
        self._assert_halted_untouched(report, "UNKNOWN", before)

    def test_conflicting_quantity_fields_are_unknown(self):
        before = dict(self.pm.positions)
        report = self._run([{"ticker": "KXC", "position": 5,
                             "position_fp": "6.00"}])
        self._assert_halted_untouched(report, "UNKNOWN", before)

    def test_partial_row_without_ticker_is_unknown(self):
        before = dict(self.pm.positions)
        report = self._run([{"position_fp": "2.00"}])
        self._assert_halted_untouched(report, "UNKNOWN", before)

    def test_broker_timeout_halts_unavailable(self):
        self.pm.positions = {"t": _pos("t", "KXT", "yes", 1)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        report = self._run(TimeoutError("broker timeout"))
        self._assert_halted_untouched(report, "BROKER_UNAVAILABLE", before)

    def test_empty_broker_and_empty_local_is_match(self):
        report = self._run([])
        self.assertEqual(report["status"], "MATCH")
        self.assertIsNone(self.pm.reconcile_halt)

    def test_zero_quantity_rows_are_flat_not_positions(self):
        report = self._run([{"ticker": "KXZERO", "position_fp": "0.00"}])
        self.assertEqual(report["status"], "MATCH")
        self.assertEqual(self.pm.positions, {})


class StartupRetriesTest(_StartupBase):

    def test_retry_succeeds_on_second_attempt(self):
        broker = [{"ticker": "KXA", "position_fp": "2.00"}]
        self.pm.positions = {"a": _pos("a", "KXA", "yes", 2)}
        with patch.object(self.client, "get_positions",
                          side_effect=[None, broker]) as gp, \
                patch.object(bot.time, "sleep") as sleep, \
                patch("position_manager.JsonStore.save"):
            report = self.pm.reconcile_with_broker()
        self.assertEqual(gp.call_count, 2)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(report["status"], "MATCH")

    def test_retry_exhausted_halts_unavailable(self):
        self.pm.positions = {"a": _pos("a", "KXA", "yes", 2)}
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        with patch.object(self.client, "get_positions",
                          return_value=None) as gp, \
                patch.object(bot.time, "sleep") as sleep, \
                patch("position_manager.JsonStore.save"):
            report = self.pm.reconcile_with_broker()
        self.assertEqual(gp.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(report["status"], "BROKER_UNAVAILABLE")
        self.assertIsNotNone(self.pm.reconcile_halt)
        self.assertEqual(self.pm.positions, before)

    def test_first_attempt_succeeds_no_sleep(self):
        with patch.object(self.client, "get_positions",
                          return_value=[]) as gp, \
                patch.object(bot.time, "sleep") as sleep, \
                patch("position_manager.JsonStore.save"):
            report = self.pm.reconcile_with_broker()
        gp.assert_called_once_with()
        sleep.assert_not_called()
        self.assertEqual(report["status"], "MATCH")


class EngineGateOnStartupHaltTest(unittest.TestCase):
    """A startup halt must block the cycle exactly like a periodic one."""

    def test_gate_reports_reconciliation_guard(self):
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr = MagicMock()
        eng.posmgr.reconcile_halt = {"status": "MISMATCH", "detail": "x",
                                     "at": "t"}
        eng.risk = MagicMock()
        saved = bot.CFG.MAX_CONTRACTS_PER_ORDER
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "1"
        try:
            ok, guard = eng._post_balance_gates()
        finally:
            bot.CFG.MAX_CONTRACTS_PER_ORDER = saved
        self.assertFalse(ok)
        self.assertEqual(guard, "reconciliation_mismatch")
        eng.risk.can_trade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
