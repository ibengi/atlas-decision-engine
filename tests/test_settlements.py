# -*- coding: utf-8 -*-
"""Tests for PositionManager.check_settlements() — P2.1 settlement handling.

Covers: yes/no/void/void_unreadable/expired_stale settlement paths,
        open-market skip, and API-failure resilience.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────

def _make_position(ticker="KXBTC-01JAN99-T99999", side="yes",
                   count=10, avg_price=50, fees=0.07,
                   trade_id="test-trade-1", opened_at=None,
                   state="open"):
    """Build a minimal position dict matching PositionManager schema."""
    if opened_at is None:
        opened_at = datetime.now(timezone.utc).isoformat()
    return {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "count": count,
        "avg_price": avg_price,
        "fees": fees,
        "opened_at": opened_at,
        "state": state,
        "order_ids": [],
        "fill_ids": [],
        "count_initial": count,
    }


def _iso_days_ago(days: float) -> str:
    """ISO timestamp N days in the past."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── test cases ─────────────────────────────────────────────────────────────

class TestSettlements(unittest.TestCase):

    def setUp(self):
        """Create a PositionManager with mocked client and trade logger."""
        self.mock_client = MagicMock()
        self.mock_tlog = MagicMock()
        self.mock_tlog.settle_trade.return_value = {"trade_id": "test-trade-1"}
        self.pm = bot.PositionManager(self.mock_client, self.mock_tlog)
        # Clear any pre-existing positions from file load
        self.pm.positions = {}

    # ── 1. test_settles_on_yes ──────────────────────────────────────────

    def test_settles_on_yes(self):
        """result 'yes' → position popped, won=True."""
        pos = _make_position(side="yes", trade_id="t-yes")
        self.pm.positions["t-yes"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "settled",
            "result": "yes",
        }
        realized = self.pm.check_settlements()

        self.assertEqual(len(realized), 1)
        self.assertNotIn("t-yes", self.pm.positions)  # popped
        self.mock_tlog.settle_trade.assert_called_once()
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertEqual(call_args[0][0], "t-yes")
        self.assertEqual(call_args[0][1], "yes")
        self.assertTrue(call_args[0][2])  # won=True

    # ── 2. test_settles_on_no ───────────────────────────────────────────

    def test_settles_on_no(self):
        """result 'no' → position popped, won=(side=='no')."""
        # side='yes' loses when result='no'
        pos = _make_position(side="yes", trade_id="t-no")
        self.pm.positions["t-no"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "settled",
            "result": "no",
        }
        realized = self.pm.check_settlements()

        self.assertEqual(len(realized), 1)
        self.assertNotIn("t-no", self.pm.positions)
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertEqual(call_args[0][0], "t-no")
        self.assertEqual(call_args[0][1], "no")
        self.assertFalse(call_args[0][2])  # won=False (side=yes, result=no)

    def test_settles_on_no_when_side_is_no(self):
        """result 'no' with side='no' → won=True."""
        pos = _make_position(side="no", trade_id="t-no-side")
        self.pm.positions["t-no-side"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "settled",
            "result": "no",
        }
        realized = self.pm.check_settlements()

        self.assertEqual(len(realized), 1)
        self.assertNotIn("t-no-side", self.pm.positions)
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertTrue(call_args[0][2])  # won=True

    # ── 3. test_settles_on_void ─────────────────────────────────────────

    def test_settles_on_void(self):
        """result 'void' → position popped, won=False, gross=0 (fees only loss)."""
        pos = _make_position(trade_id="t-void", fees=0.07)
        self.pm.positions["t-void"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "settled",
            "result": "void",
        }
        realized = self.pm.check_settlements()

        self.assertNotIn("t-void", self.pm.positions)
        self.mock_tlog.settle_trade.assert_called_once()
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertEqual(call_args[0][0], "t-void")
        self.assertEqual(call_args[0][1], "void")
        self.assertFalse(call_args[0][2])  # won=False
        self.assertEqual(call_args[0][3], 0.0)  # gross=0 (premium returned)

    # ── 4. test_settles_on_unreadable_settled ───────────────────────────

    def test_settles_on_unreadable_settled(self):
        """status 'settled', result empty → popped as void_unreadable."""
        pos = _make_position(trade_id="t-unread")
        self.pm.positions["t-unread"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "settled",
            "result": "",  # unreadable
        }
        realized = self.pm.check_settlements()

        self.assertNotIn("t-unread", self.pm.positions)
        self.mock_tlog.settle_trade.assert_called_once()
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertEqual(call_args[0][0], "t-unread")
        self.assertEqual(call_args[0][1], "void_unreadable")
        self.assertFalse(call_args[0][2])

    # ── 5. test_skips_open_market ───────────────────────────────────────

    def test_skips_open_market(self):
        """status 'open', result empty → position NOT popped."""
        pos = _make_position(trade_id="t-open")
        self.pm.positions["t-open"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "open",
            "result": "",
        }
        realized = self.pm.check_settlements()

        self.assertEqual(len(realized), 0)
        self.assertIn("t-open", self.pm.positions)  # still there
        self.mock_tlog.settle_trade.assert_not_called()

    # ── 6. test_cleans_stale_position ───────────────────────────────────

    def test_cleans_stale_position(self):
        """Position older than MAX_POSITION_AGE_DAYS, market not 'open' → popped as expired_stale."""
        old_opened = _iso_days_ago(bot.CFG.MAX_POSITION_AGE_DAYS + 5)
        pos = _make_position(trade_id="t-stale", opened_at=old_opened)
        self.pm.positions["t-stale"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "settled",
            "result": "",
        }
        realized = self.pm.check_settlements()

        self.assertNotIn("t-stale", self.pm.positions)
        self.mock_tlog.settle_trade.assert_called_once()
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertEqual(call_args[0][0], "t-stale")
        self.assertEqual(call_args[0][1], "expired_stale")
        self.assertFalse(call_args[0][2])

    def test_cleans_stale_position_api_failure(self):
        """Stale position + get_market returns None → still cleaned up."""
        old_opened = _iso_days_ago(bot.CFG.MAX_POSITION_AGE_DAYS + 5)
        pos = _make_position(trade_id="t-stale-nomkt", opened_at=old_opened)
        self.pm.positions["t-stale-nomkt"] = pos

        self.mock_client.get_market.return_value = None
        realized = self.pm.check_settlements()

        self.assertNotIn("t-stale-nomkt", self.pm.positions)
        self.mock_tlog.settle_trade.assert_called_once()
        call_args = self.mock_tlog.settle_trade.call_args
        self.assertEqual(call_args[0][1], "expired_stale")

    def test_keeps_stale_but_open_market(self):
        """Stale position but market status is 'open' → NOT popped."""
        old_opened = _iso_days_ago(bot.CFG.MAX_POSITION_AGE_DAYS + 5)
        pos = _make_position(trade_id="t-stale-open", opened_at=old_opened)
        self.pm.positions["t-stale-open"] = pos

        self.mock_client.get_market.return_value = {
            "ticker": "KXBTC-01JAN99-T99999",
            "status": "open",
            "result": "",
        }
        realized = self.pm.check_settlements()

        self.assertIn("t-stale-open", self.pm.positions)  # kept
        self.mock_tlog.settle_trade.assert_not_called()

    # ── 7. test_keeps_fresh_position_on_api_failure ─────────────────────

    def test_keeps_fresh_position_on_api_failure(self):
        """get_market returns None, position under age limit → NOT popped, warning logged."""
        fresh_opened = _iso_days_ago(1)  # 1 day old, well under limit
        pos = _make_position(trade_id="t-fresh-nomkt", opened_at=fresh_opened)
        self.pm.positions["t-fresh-nomkt"] = pos

        self.mock_client.get_market.return_value = None

        with self.assertLogs("POSITION", level="WARNING") as log_ctx:
            realized = self.pm.check_settlements()

        self.assertEqual(len(realized), 0)
        self.assertIn("t-fresh-nomkt", self.pm.positions)  # NOT popped
        self.mock_tlog.settle_trade.assert_not_called()
        # Warning was logged
        self.assertTrue(any("get_market" in msg for msg in log_ctx.output),
                        "Expected WARNING about get_market failure")


if __name__ == "__main__":
    unittest.main()
