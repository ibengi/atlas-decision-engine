# -*- coding: utf-8 -*-
"""Slot release for positions whose trade record is absent from the journal.

The production defect these tests pin down: ``TradeLogger.settle_trade``
returns ``None`` for exactly one reason — the trade_id is not in the journal —
and every settlement path used to respond by keeping the position "for retry".
An id absent from the journal never becomes present, so the retry could never
succeed: the slot stayed occupied until the 30-day ``MAX_POSITION_AGE_DAYS``
escape hatch, and ``MAX_OPEN_POSITIONS`` stayed blocked with it.

The concrete way a position gets an unknown trade_id in production is
``reconcile_with_broker`` after a container restart: positions are rebuilt
from the broker with ``brk-...`` ids, while the trade journal — which lived on
the previous container's ephemeral disk — starts empty.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from trade_logger import TradeLogger  # noqa: E402


def brk_position(ticker="KXBTCD-26AUG2808-T79599.99", side="no",
                 count=5, avg=19):
    """A position exactly as reconcile_with_broker rebuilds it."""
    return {
        "trade_id": f"brk-{ticker}-{side}", "ticker": ticker, "side": side,
        "count_initial": count, "count": count, "avg_price": avg,
        "fees": 0.0, "opened_at": "2026-08-28T13:59:00+00:00",
        "order_ids": [], "fill_ids": [], "state": "open",
        "strategy": "reconciled", "category": "Other",
        "market_score": None, "entry_edge": None, "entry_ev": None,
    }


def settled_market(result="no", status="finalized"):
    return {"ticker": "KXBTCD-26AUG2808-T79599.99",
            "result": result, "status": status}


class OrphanReleaseTest(unittest.TestCase):
    """check_settlements against a real TradeLogger with an EMPTY journal."""

    def setUp(self):
        self.client = MagicMock()
        self.tlog = TradeLogger()
        self.tlog.trades = []                       # the post-restart journal
        self.tlog.flush = MagicMock()               # no disk writes in tests
        self.pm = bot.PositionManager(self.client, self.tlog)
        self.pm.positions = {}
        self.pm.flush = MagicMock()

    def test_regression_orphan_position_is_released_when_market_settles(self):
        """The exact production scenario: brk- position, journal empty,
        broker finally publishes a result. The slot must be released, not
        kept 'for retry' forever."""
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="no")

        realized = self.pm.check_settlements()

        self.assertEqual(self.pm.open_count(), 0)
        self.assertEqual(len(realized), 1)
        self.assertTrue(realized[0].get("orphan"))
        self.assertEqual(realized[0]["result"], "no")
        self.assertEqual(realized[0]["state"], "settled")

    def test_orphan_settlement_is_written_to_the_journal_for_audit(self):
        """Releasing the slot must not erase the event: a settled, clearly
        marked orphan row lands in the journal."""
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="yes")

        self.pm.check_settlements()

        self.assertEqual(len(self.tlog.trades), 1)
        rec = self.tlog.trades[0]
        self.assertTrue(rec["orphan"])
        self.assertEqual(rec["reason"], "orphan_settlement")
        self.assertEqual(rec["ticker"], pos["ticker"])
        self.assertEqual(rec["filled_count"], 5)

    def test_orphan_win_and_loss_pnl_follow_the_position_side(self):
        """PnL math is unchanged by the orphan path: NO position, result NO
        -> won; result YES -> lost the cost."""
        pos = brk_position(count=5, avg=19)
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="no")
        won_row = self.pm.check_settlements()[0]
        self.assertTrue(won_row["won"])
        self.assertAlmostEqual(won_row["gross_pnl"], 5 * 1.0 - 5 * 19 / 100.0, places=2)

        pos2 = brk_position(ticker="KXBTCD-26AUG2817-T84999.99", side="yes",
                            count=44, avg=3)
        self.pm.positions[pos2["trade_id"]] = pos2
        self.client.get_market.return_value = {"result": "no",
                                               "status": "finalized"}
        lost_row = self.pm.check_settlements()[0]
        self.assertFalse(lost_row["won"])
        self.assertAlmostEqual(lost_row["gross_pnl"], -(44 * 3 / 100.0), places=2)

    def test_orphan_void_market_releases_the_slot_too(self):
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="void",
                                                             status="settled")

        realized = self.pm.check_settlements()

        self.assertEqual(self.pm.open_count(), 0)
        self.assertEqual(realized[0]["result"], "void")
        self.assertTrue(realized[0]["orphan"])

    def test_a_known_trade_still_settles_through_the_journal_not_as_orphan(self):
        """The normal path is untouched: when the trade exists, settle_trade
        handles it and no orphan row is written."""
        pos = brk_position()
        pos["trade_id"] = "real-trade-0001"
        self.pm.positions[pos["trade_id"]] = pos
        self.tlog.trades = [{
            "schema": TradeLogger.SCHEMA, "trade_id": "real-trade-0001",
            "ticker": pos["ticker"], "side": "no",
            "timestamp": "2026-08-28T11:32:11+00:00",
            "avg_fill_price": 19, "filled_count": 5, "state": "open",
        }]
        self.client.get_market.return_value = settled_market(result="no")

        realized = self.pm.check_settlements()

        self.assertEqual(self.pm.open_count(), 0)
        self.assertEqual(len(realized), 1)
        self.assertNotIn("orphan", realized[0])
        self.assertEqual(realized[0]["state"], "settled")

    def test_an_unsettled_market_still_keeps_the_position(self):
        """Fail-safe behaviour is unchanged: no broker result, no release.
        (The current production hold — broker has published nothing — must
        remain a hold.)"""
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = {"result": "",
                                               "status": "closed"}

        realized = self.pm.check_settlements()

        self.assertEqual(realized, [])
        self.assertEqual(self.pm.open_count(), 1)

    def test_slot_release_unblocks_the_open_position_count(self):
        """The guard reads open_count; releasing the orphan must lower it."""
        for i, res in enumerate(("no", "no", "no")):
            pos = brk_position(ticker=f"KXTEST-{i}")
            self.pm.positions[pos["trade_id"]] = pos
        self.assertEqual(self.pm.open_count(), 3)
        self.client.get_market.return_value = settled_market(result="no")

        self.pm.check_settlements()

        self.assertEqual(self.pm.open_count(), 0)


if __name__ == "__main__":
    unittest.main()
