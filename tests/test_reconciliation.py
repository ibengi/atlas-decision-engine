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


if __name__ == "__main__":
    unittest.main()
