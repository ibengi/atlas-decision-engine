# -*- coding: utf-8 -*-
"""Tests for SignalValidator.check() ONE_TRADE_PER_MKT ticker guard — P2.3.

The bug: posmgr.positions is keyed by trade_id, not ticker, so
``ticker in posmgr.positions`` was always False — the ONE_TRADE_PER_MKT
guard was silently broken in SignalValidator.check().

Fix: ``ticker in posmgr.tickers_open()``.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402


class TestSignalValidatorOneTradePerMkt(unittest.TestCase):

    def setUp(self):
        """Create a PositionManager with a real client mock, then inject a position."""
        self.mock_client = MagicMock()
        self.mock_tlog = MagicMock()
        # has_open_on returns False so only posmgr matters
        self.mock_tlog.has_open_on.return_value = False

        self.pm = bot.PositionManager(self.mock_client, self.mock_tlog)
        self.pm.positions = {}

    def _add_open_position(self, ticker="KXBTC-01JAN99-T99999",
                           trade_id="t-sv-1", side="yes"):
        """Add a minimal open position to posmgr and verify it shows in tickers_open."""
        pos = {
            "trade_id": trade_id,
            "ticker": ticker,
            "side": side,
            "count": 10,
            "avg_price": 55,
            "fees": 0.07,
            "opened_at": "2026-07-01T00:00:00+00:00",
            "state": "open",
            "order_ids": [],
            "fill_ids": [],
            "count_initial": 10,
        }
        self.pm.positions[trade_id] = pos

    # ── 1. ONE_TRADE_PER_MKT=True blocks duplicate ticker ────────────────

    def test_one_trade_per_mkt_blocks_duplicate_ticker(self):
        """With ONE_TRADE_PER_MKT=True, a ticker already in posmgr is rejected."""
        self._add_open_position(ticker="KXBTCD-01JAN99-T99999", trade_id="existing")

        # Default is True; ensure env is clean
        original = bot.CFG.ONE_TRADE_PER_MKT
        bot.CFG.ONE_TRADE_PER_MKT = True
        try:
            ok, reason = bot.SignalValidator.check(
                "ACHETER YES", 55, "KXBTCD-01JAN99-T99999",
                self.mock_tlog, self.pm,
            )
            self.assertFalse(ok)
            self.assertIn("deja prise", reason)
        finally:
            bot.CFG.ONE_TRADE_PER_MKT = original

    # ── 2. different ticker passes ───────────────────────────────────────

    def test_one_trade_per_mkt_allows_different_ticker(self):
        """A ticker NOT in posmgr passes the guard."""
        self._add_open_position(ticker="KXBTCD-01JAN99-T99999", trade_id="existing")

        original = bot.CFG.ONE_TRADE_PER_MKT
        bot.CFG.ONE_TRADE_PER_MKT = True
        try:
            ok, reason = bot.SignalValidator.check(
                "ACHETER YES", 55, "KXBTCD-DIFFERENT-TICKER",
                self.mock_tlog, self.pm,
            )
            self.assertTrue(ok)
        finally:
            bot.CFG.ONE_TRADE_PER_MKT = original

    # ── 3. ONE_TRADE_PER_MKT=False allows duplicate ──────────────────────

    def test_one_trade_per_mkt_disabled_allows_duplicate(self):
        """When ONE_TRADE_PER_MKT=False, even same ticker is allowed."""
        self._add_open_position(ticker="KXBTCD-01JAN99-T99999", trade_id="existing")

        original = bot.CFG.ONE_TRADE_PER_MKT
        bot.CFG.ONE_TRADE_PER_MKT = False
        try:
            ok, reason = bot.SignalValidator.check(
                "ACHETER YES", 55, "KXBTCD-01JAN99-T99999",
                self.mock_tlog, self.pm,
            )
            self.assertTrue(ok)
        finally:
            bot.CFG.ONE_TRADE_PER_MKT = original

    # ── 4. Verify the bug — old code would silently pass duplicate ───────

    def test_old_code_would_silently_pass_duplicate(self):
        """Confirm that 'ticker in posmgr.positions' (keyed by trade_id)
        always returns False, proving the old code was broken."""
        self._add_open_position(ticker="KXBTCD-01JAN99-T99999", trade_id="existing")

        # Simulate the OLD (broken) logic
        old_result = "KXBTCD-01JAN99-T99999" in self.pm.positions
        self.assertFalse(old_result, "Old code: ticker-in-positions should be False (keyed by trade_id)")

        # New (fixed) logic
        new_result = "KXBTCD-01JAN99-T99999" in self.pm.tickers_open()
        self.assertTrue(new_result, "New code: ticker-in-tickers_open should be True")


if __name__ == "__main__":
    unittest.main()
