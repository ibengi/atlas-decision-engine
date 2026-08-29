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

    def test_rebuilt_position_without_broker_price_is_flagged_estimated(self):
        """The production case: broker gives no avg_price, so 50c is filler."""
        self.client.get_positions.return_value = [
            {"ticker": "KXBTCD-26AUG2808-T79599.99", "position": -5}
        ]

        report = self.pm.reconcile_with_broker()

        self.assertEqual(report["rebuilt"], ["KXBTCD-26AUG2808-T79599.99"])
        pos = self.pm.positions["brk-KXBTCD-26AUG2808-T79599.99-no"]
        self.assertTrue(pos["avg_price_estimated"])
        self.assertTrue(pos["fees_estimated"])
        self.assertTrue(pos["opened_at_estimated"])

    def test_a_broker_supplied_price_is_not_flagged_estimated(self):
        """When the broker does report an average, it is a measurement."""
        self.client.get_positions.return_value = [
            {"ticker": "KXTEST", "position": 4, "avg_price": 19}
        ]

        self.pm.reconcile_with_broker()

        pos = self.pm.positions["brk-KXTEST-yes"]
        self.assertEqual(pos["avg_price"], 19)
        self.assertFalse(pos["avg_price_estimated"])

    def test_rebuild_is_idempotent_across_repeated_reconciliations(self):
        """CRITICAL: a restart must not duplicate positions. The brk- id is
        stable, so reconciling twice yields one position, not two."""
        self.client.get_positions.return_value = [
            {"ticker": "KXTEST", "position": 4}
        ]

        self.pm.reconcile_with_broker()
        self.pm.reconcile_with_broker()
        self.pm.reconcile_with_broker()

        self.assertEqual(self.pm.open_count(), 1)
        self.assertEqual(list(self.pm.positions), ["brk-KXTEST-yes"])

    def test_a_closed_broker_position_is_not_resurrected(self):
        """CRITICAL: broker says flat -> no local position survives."""
        self.pm.positions["brk-KXOLD-yes"] = {
            "trade_id": "brk-KXOLD-yes", "ticker": "KXOLD", "side": "yes",
            "count_initial": 2, "count": 2, "avg_price": 50, "fees": 0.0,
            "opened_at": "2026-08-28T00:00:00+00:00", "order_ids": [],
            "fill_ids": [], "state": "open", "strategy": "reconciled",
        }
        self.client.get_positions.return_value = []

        self.pm.reconcile_with_broker()

        self.assertEqual(self.pm.open_count(), 0)
        self.assertNotIn("brk-KXOLD-yes", self.pm.positions)

    def test_a_zero_quantity_broker_row_never_opens_a_position(self):
        self.client.get_positions.return_value = [
            {"ticker": "KXFLAT", "position": 0}
        ]

        report = self.pm.reconcile_with_broker()

        self.assertEqual(report["rebuilt"], [])
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
