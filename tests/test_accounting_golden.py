# -*- coding: utf-8 -*-
"""AIR-001 Wave 7 — golden tests for canonical Decimal settlement math.

Every number below is computed by hand from the contract definition
(binary contract pays $1 per contract on a win, premium is
count x avg_price cents, fees counted exactly once). These values are
frozen: any drift in accounting.py must fail here.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import accounting  # noqa: E402
import kalshi_alpha_bot as bot  # noqa: E402


class TestGoldenYesNo(unittest.TestCase):
    def test_yes_win(self):
        # 10 @ 48c, fees 0.07: cost 4.80, payout 10.00
        r = accounting.settle_yes_no(side="yes", result="yes", count=10,
                                     avg_price_cents=48,
                                     fees_dollars=0.07)
        self.assertEqual((r["won"], r["gross"], r["net"], r["cost"]),
                         (True, 5.20, 5.13, 4.80))

    def test_yes_loss(self):
        r = accounting.settle_yes_no(side="yes", result="no", count=10,
                                     avg_price_cents=48,
                                     fees_dollars=0.07)
        self.assertEqual((r["won"], r["gross"], r["net"]),
                         (False, -4.80, -4.87))

    def test_no_side_win(self):
        # NO position: avg_price is the NO premium paid. 5 @ 52c = 2.60
        r = accounting.settle_yes_no(side="no", result="no", count=5,
                                     avg_price_cents=52,
                                     fees_dollars=0.05)
        self.assertEqual((r["won"], r["gross"], r["net"]),
                         (True, 2.40, 2.35))

    def test_no_side_loss(self):
        r = accounting.settle_yes_no(side="no", result="yes", count=5,
                                     avg_price_cents=52,
                                     fees_dollars=0.05)
        self.assertEqual((r["won"], r["gross"], r["net"]),
                         (False, -2.60, -2.65))

    def test_partial_fill_counts_filled_only(self):
        # partial fill: 3 filled of 10 requested — accounting sees 3
        r = accounting.settle_yes_no(side="yes", result="yes", count=3,
                                     avg_price_cents=48,
                                     fees_dollars=0.02)
        self.assertEqual((r["gross"], r["net"]), (1.56, 1.54))

    def test_decimal_exactness_no_float_drift(self):
        # 3 @ 33c: cost is exactly 0.99; float arithmetic would give
        # 2.0100000000000002 for the win gross.
        r = accounting.settle_yes_no(side="yes", result="yes", count=3,
                                     avg_price_cents=33,
                                     fees_dollars=0.03)
        self.assertEqual((r["cost"], r["gross"], r["net"]),
                         (0.99, 2.01, 1.98))

    def test_unknown_result_refused(self):
        with self.assertRaises(ValueError):
            accounting.settle_yes_no(side="yes", result="", count=1,
                                     avg_price_cents=50,
                                     fees_dollars=0.0)
        with self.assertRaises(ValueError):
            accounting.settle_yes_no(side="yes", result="void", count=1,
                                     avg_price_cents=50,
                                     fees_dollars=0.0)

    def test_none_inputs_refused_never_zeroed(self):
        with self.assertRaises(ValueError):
            accounting.settle_yes_no(side="yes", result="yes",
                                     count=None, avg_price_cents=50,
                                     fees_dollars=0.0)


class TestGoldenVoidAndForced(unittest.TestCase):
    def test_void_fees_once(self):
        r = accounting.settle_void(fees_dollars=0.07)
        self.assertEqual((r["won"], r["gross"], r["net"]),
                         (False, 0.0, -0.07))

    def test_forced_conservative_full_cost_fees_once(self):
        """BEFORE (defect): gross=-0.07, net=-0.14 (fees only, counted
        twice). NOW: worst case, 10 @ 50c: gross=-5.00, net=-5.07."""
        r = accounting.settle_forced_conservative(count=10,
                                                  avg_price_cents=50,
                                                  fees_dollars=0.07)
        self.assertEqual((r["gross"], r["net"]), (-5.00, -5.07))

    def test_classification(self):
        self.assertEqual(accounting.classify_settlement(
            {"result": "yes"}), "yes")
        self.assertEqual(accounting.classify_settlement(
            {"result": "void"}), "void")
        self.assertEqual(accounting.classify_settlement(
            {"result": ""}), "UNKNOWN")
        self.assertEqual(accounting.classify_settlement(None), "UNKNOWN")


class TestSettlementIntegration(unittest.TestCase):
    """The engine's settlement path produces the golden numbers."""

    def _pm(self, market_row, position):
        client, tlog = MagicMock(), MagicMock()
        tlog.settle_trade.return_value = {"trade_id":
                                          position["trade_id"]}
        client.get_market.return_value = market_row
        pm = bot.PositionManager(client, tlog)
        pm.positions = {position["trade_id"]: position}
        return pm, tlog

    def _pos(self, **kw):
        base = {"trade_id": "g-1", "ticker": "KXG-1", "side": "yes",
                "count": 10, "avg_price": 48, "fees": 0.07,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "state": "open", "order_ids": [], "fill_ids": [],
                "count_initial": 10}
        base.update(kw)
        return base

    def test_win_numbers_through_engine_path(self):
        pm, tlog = self._pm({"status": "settled", "result": "yes"},
                            self._pos())
        pm.check_settlements()
        args = tlog.settle_trade.call_args[0]
        self.assertEqual((args[1], args[2], args[3], args[4]),
                         ("yes", True, 5.20, 5.13))

    def test_void_numbers_through_engine_path(self):
        pm, tlog = self._pm({"status": "settled", "result": "void"},
                            self._pos())
        pm.check_settlements()
        args = tlog.settle_trade.call_args[0]
        self.assertEqual((args[1], args[3], args[4]),
                         ("void", 0.0, -0.07))

    def test_expired_stale_no_double_fees(self):
        old = (datetime.now(timezone.utc)
               - timedelta(days=bot.CFG.MAX_POSITION_AGE_DAYS + 5)
               ).isoformat()
        pm, tlog = self._pm(None, self._pos(opened_at=old,
                                            avg_price=50))
        pm.check_settlements()
        args = tlog.settle_trade.call_args[0]
        self.assertEqual((args[1], args[3], args[4]),
                         ("expired_stale", -5.00, -5.07))


if __name__ == "__main__":
    unittest.main()
