# -*- coding: utf-8 -*-
"""Tests for PositionManager broker reconciliation retry handling (P2.4)."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402


class TestReconciliationRetries(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.tlog = MagicMock()
        self.pm = bot.PositionManager(self.client, self.tlog)
        self.pm.positions = {}

    def test_retry_succeeds_on_second_attempt(self):
        """A transient None response is retried and reconciliation continues."""
        broker_positions = [{"ticker": "KXTEST-TICKER", "position": 2}]
        with patch.object(self.client, "get_positions", side_effect=[None, broker_positions]) as get_positions, \
             patch.object(bot.time, "sleep") as sleep:
            report = self.pm.reconcile_with_broker()

        self.assertEqual(get_positions.call_count, 2)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(report["rebuilt"], ["KXTEST-TICKER"])

    def test_retry_exhausted_skips(self):
        """Three failed calls leave state untouched and emit an error."""
        with patch.object(self.client, "get_positions", return_value=None) as get_positions, \
             patch.object(bot.time, "sleep") as sleep, \
             self.assertLogs("POSITION", level="ERROR") as logs:
            report = self.pm.reconcile_with_broker()

        self.assertEqual(get_positions.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(report, {"rebuilt": [], "ghost": [], "matched": []})
        self.assertTrue(any("failed after 3 attempts" in message for message in logs.output))
        self.assertEqual(self.pm.positions, {})

    def test_first_attempt_succeeds(self):
        """A successful initial response does not sleep or retry."""
        with patch.object(self.client, "get_positions", return_value=[]) as get_positions, \
             patch.object(bot.time, "sleep") as sleep:
            report = self.pm.reconcile_with_broker()

        get_positions.assert_called_once_with()
        sleep.assert_not_called()
        self.assertEqual(report, {"rebuilt": [], "ghost": [], "matched": []})


class TestGhostCleanup(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.tlog = MagicMock()
        self.pm = bot.PositionManager(self.client, self.tlog)

    def test_ghost_positions_removed(self):
        """Ghost-marked positions are popped from local state after reconciliation."""
        broker_positions = [{"ticker": "KXTEST-REAL", "position": 1}]
        self.pm.positions = {
            "local-X": {"trade_id": "local-X", "ticker": "KXTEST-GHOST", "side": "yes",
                         "count_initial": 1, "count": 1, "avg_price": 50, "fees": 0.0,
                         "opened_at": "2026-01-01T00:00:00Z", "order_ids": [], "fill_ids": [],
                         "state": "open", "strategy": "test", "market_score": None,
                         "entry_edge": None, "entry_ev": None},
        }
        with patch.object(self.client, "get_positions", return_value=broker_positions), \
             patch.object(self.pm, "flush"):
            report = self.pm.reconcile_with_broker()

        self.assertEqual(report["ghost"], ["KXTEST-GHOST"])
        self.assertEqual(report["rebuilt"], ["KXTEST-REAL"])
        # After cleanup, ghost position should be gone
        self.assertNotIn("local-X", self.pm.positions)
        self.assertEqual(len(self.pm.positions), 1)
        self.assertIn("brk-KXTEST-REAL-yes", self.pm.positions)

    def test_ghost_cleanup_logs_info(self):
        """Info log is emitted with removed count when ghost positions are cleaned up."""
        self.pm.positions = {
            "ghost-A": {"trade_id": "ghost-A", "ticker": "KXTEST-G1", "side": "yes",
                         "count_initial": 1, "count": 1, "avg_price": 50, "fees": 0.0,
                         "opened_at": "2026-01-01T00:00:00Z", "order_ids": [], "fill_ids": [],
                         "state": "open", "strategy": "test", "market_score": None,
                         "entry_edge": None, "entry_ev": None},
            "ghost-B": {"trade_id": "ghost-B", "ticker": "KXTEST-G2", "side": "no",
                         "count_initial": 2, "count": 2, "avg_price": 40, "fees": 0.0,
                         "opened_at": "2026-01-01T00:00:00Z", "order_ids": [], "fill_ids": [],
                         "state": "open", "strategy": "test", "market_score": None,
                         "entry_edge": None, "entry_ev": None},
        }
        with patch.object(self.client, "get_positions", return_value=[]), \
             patch.object(self.pm, "flush"), \
             self.assertLogs("POSITION", level="INFO") as logs:
            self.pm.reconcile_with_broker()

        self.assertTrue(any("Ghost cleanup: removed 2" in msg for msg in logs.output))
        self.assertTrue(any("ghost-A" in msg for msg in logs.output))


if __name__ == "__main__":
    unittest.main()
