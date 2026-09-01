# -*- coding: utf-8 -*-
"""State reconstruction honesty after an ephemeral-disk restart.

The engine service runs with no persistent volume, so DATA_DIR defaults to
the container filesystem. A Railway restart therefore destroys
positions_state.json, kalshi_trades.json, risk_state.json, orders_state.json
and submission_guard.json together.

reconcile_with_broker then rebuilds open positions from the broker, which is
correct as far as it goes — the broker knows existence, side and quantity.
It does NOT know the entry price actually paid, the fees, the strategy, or
when the position was opened. These tests pin the difference between
*measured* and *reconstructed* so a fabricated entry price can never again be
mistaken for an observed one.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402


class BrokerRebuildHonestyTest(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.tlog = MagicMock()
        self.tlog.trades = []
        self.pm = bot.PositionManager(self.client, self.tlog)
        self.pm.positions = {}
        self.pm.flush = MagicMock()

    def test_broker_only_position_is_never_adopted(self):
        """The old production behavior rebuilt broker-only positions with an
        invented 50c entry price. Since the 2026-08-31 hardening, a
        broker-only position is a MISMATCH halt: no fabricated financial
        history, the operator decides."""
        self.client.get_positions.return_value = [
            {"ticker": "KXBTCD-26AUG2808-T79599.99", "position_fp": "-5.00"}
        ]

        report = self.pm.reconcile_with_broker()

        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual(report["mismatches"][0]["kind"], "broker_only")
        self.assertEqual(self.pm.positions, {},
                         "no brk- reconstruction, no estimated entry price")
        self.assertIsNotNone(self.pm.reconcile_halt)

    def test_repeated_reconciliation_stays_halted_without_adoption(self):
        """CRITICAL: repeated startup passes must not accumulate state."""
        self.client.get_positions.return_value = [
            {"ticker": "KXTEST", "position": 4}
        ]

        self.pm.reconcile_with_broker()
        self.pm.reconcile_with_broker()
        self.pm.reconcile_with_broker()

        self.assertEqual(self.pm.open_count(), 0)
        self.assertEqual(self.pm.positions, {})
        self.assertEqual(self.pm.reconcile_halt["status"], "MISMATCH")

    def test_a_flat_broker_never_deletes_local_positions(self):
        """CRITICAL inversion of the pre-incident 'ghost cleanup': broker
        reads flat -> local position is PRESERVED and submissions halt.
        The 2026-08-31 incident deleted three real positions this way."""
        self.pm.positions["brk-KXOLD-yes"] = {
            "trade_id": "brk-KXOLD-yes", "ticker": "KXOLD", "side": "yes",
            "count_initial": 2, "count": 2, "avg_price": 50, "fees": 0.0,
            "opened_at": "2026-08-28T00:00:00+00:00", "order_ids": [],
            "fill_ids": [], "state": "open", "strategy": "reconciled",
        }
        self.client.get_positions.return_value = []

        report = self.pm.reconcile_with_broker()

        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual(report["mismatches"][0]["kind"], "local_only")
        self.assertEqual(self.pm.open_count(), 1)
        self.assertIn("brk-KXOLD-yes", self.pm.positions)
        self.assertIsNotNone(self.pm.reconcile_halt)

    def test_a_zero_quantity_broker_row_never_opens_a_position(self):
        self.client.get_positions.return_value = [
            {"ticker": "KXFLAT", "position": 0}
        ]

        report = self.pm.reconcile_with_broker()

        self.assertEqual(report["status"], "MATCH")
        self.assertEqual(self.pm.open_count(), 0)


class StateLossDetectionTest(unittest.TestCase):
    """The one moment the loss is visible: startup."""

    def setUp(self):
        self.client = MagicMock()
        self.tlog = MagicMock()
        self.pm = bot.PositionManager(self.client, self.tlog)
        self.pm.positions = {}

    def test_empty_ledger_and_empty_journal_warns_loudly(self):
        """No positions AND no trade history is how a wiped disk looks."""
        self.tlog.trades = []

        with self.assertLogs("POSITION", level="WARNING") as logs:
            self.pm.reconcile_startup()

        self.assertTrue(any("STATE_EMPTY" in m for m in logs.output))

    def test_a_populated_journal_with_no_open_positions_is_silent(self):
        """A clean flat engine that has traded before is not a loss event."""
        self.tlog.trades = [{"trade_id": "t1", "state": "settled"}]

        with self.assertNoLogs("POSITION", level="WARNING"):
            self.pm.reconcile_startup()

    def test_recovered_positions_are_reported_not_warned(self):
        self.tlog.trades = [{"trade_id": "t1", "state": "open"}]
        self.pm.positions["t1"] = {
            "trade_id": "t1", "ticker": "KXTEST", "side": "yes",
            "count": 1, "count_initial": 1, "avg_price": 10, "fees": 0.0,
            "opened_at": "2026-08-28T00:00:00+00:00", "state": "open",
        }

        with self.assertLogs("POSITION", level="INFO") as logs:
            self.pm.reconcile_startup()

        self.assertTrue(any("Recovery" in m for m in logs.output))
        self.assertFalse(any("STATE_EMPTY" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
