# -*- coding: utf-8 -*-
"""Historical orphan cases now require reconstruction of missing entry evidence.

The original cases and broker outcomes are retained. Unproven entry cost and
fees cannot become realized economics merely because the market has settled.
"""
import os
import sys
import unittest
import tempfile
from unittest.mock import patch
from authority_fixtures import initialize_empty
from persistence import PersistenceSentinel
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
        tmp = tempfile.TemporaryDirectory(prefix="atlas-orphan-evidence-")
        self.addCleanup(tmp.cleanup)
        root = patch.object(bot.CFG, "DATA_DIR", tmp.name)
        root.start()
        self.addCleanup(root.stop)
        PersistenceSentinel.reset()
        self.addCleanup(PersistenceSentinel.reset)
        initialize_empty()
        self.client = MagicMock()
        self.tlog = TradeLogger()
        self.pm = bot.PositionManager(self.client, self.tlog)

    def test_regression_orphan_position_is_released_when_market_settles(self):
        """The exact production scenario: brk- position, journal empty,
        broker finally publishes a result. The slot must be released, not
        kept 'for retry' forever."""
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="no")

        realized = self.pm.check_settlements()

        self.assertEqual(self.pm.open_count(), 1)
        self.assertEqual(realized, [])
        self.assertEqual(self.pm.reconcile_halt["status"], "UNKNOWN")
        self.assertEqual(self.tlog.trades, [])

    def test_orphan_settlement_is_written_to_the_journal_for_audit(self):
        """Releasing the slot must not erase the event: a settled, clearly
        marked orphan row lands in the journal."""
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="yes")

        self.pm.check_settlements()

        self.assertEqual(self.tlog.trades, [])
        self.assertEqual(self.pm.positions[pos["trade_id"]], pos)
        self.assertEqual(self.pm.reconcile_halt["status"], "UNKNOWN")

    def test_orphan_win_and_loss_pnl_follow_the_position_side(self):
        """PnL math is unchanged by the orphan path: NO position, result NO
        -> won; result YES -> lost the cost."""
        pos = brk_position(count=5, avg=19)
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="no")
        self.assertEqual(self.pm.check_settlements(), [])
        self.assertAlmostEqual(self.pm.open_risk(), 5 * 19 / 100.)
        pos2 = brk_position(ticker="KXBTCD-26AUG2817-T84999.99", side="yes", count=44, avg=3)
        self.pm.positions[pos2["trade_id"]] = pos2
        self.client.get_market.return_value = {"result": "no", "status": "finalized"}
        self.assertEqual(self.pm.check_settlements(), [])
        self.assertAlmostEqual(self.pm.open_risk(), 5 * 19 / 100. + 44 * 3 / 100.)
        self.assertEqual(self.tlog.trades, [])

    def test_orphan_void_market_releases_the_slot_too(self):
        pos = brk_position()
        self.pm.positions[pos["trade_id"]] = pos
        self.client.get_market.return_value = settled_market(result="void",
                                                             status="settled")

        realized = self.pm.check_settlements()

        self.assertEqual(self.pm.open_count(), 1)
        self.assertEqual(realized, [])
        self.assertEqual(self.pm.reconcile_halt["status"], "UNKNOWN")

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
        self.tlog.flush()
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

        self.assertEqual(self.pm.open_count(), 3)
        self.assertEqual(self.pm.reconcile_halt["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
