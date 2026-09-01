# -*- coding: utf-8 -*-
"""Blocker 4 — full orphan-position lifecycle across a restart.

Scenario pinned end-to-end with REAL state files (no mocked stores):

  1. a brk- position (reconstructed by the PRE-hardening engine after an
     earlier state loss; estimated entry data honestly labelled) is loaded
     from disk, and startup reconciliation MATCHes it against the broker
     without mutating anything (adoption of unknown broker positions is
     gone since the 2026-08-31 hardening)
  2. the market stays merely CLOSED -> closed != settled, slot stays held
  3. the broker publishes a final result -> orphan settlement releases
     EXACTLY one slot and writes exactly one journal row
  4. process restart -> the settlement survives, the position does not
     resurrect, and a repeat settlement pass settles nothing twice
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402

TICKER = "KXBTCD-ORPHAN-T1"


class OrphanLifecycleTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_orphan_")
        self._old_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        bot.CFG.DATA_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _client(position=1, status="active", result=""):
        c = MagicMock()
        c.env = "demo"
        c.get_positions.return_value = (
            [{"ticker": TICKER, "position": position}] if position else [])
        c.get_market.return_value = {"ticker": TICKER, "status": status,
                                     "result": result}
        return c

    def _managers(self, cli):
        tlog = bot.TradeLogger()
        pm = bot.PositionManager(cli, tlog)
        return tlog, pm

    def test_full_lifecycle(self):
        # 1) A brk- position from a pre-hardening rebuild lives on disk;
        #    the broker still holds it. Startup reconciliation must MATCH
        #    it and leave it untouched (no adoption, no deletion).
        cli = self._client(position=1)
        tlog, pm = self._managers(cli)
        self.assertEqual(len(tlog.trades), 0)
        tid = f"brk-{TICKER}-yes"
        pm.positions[tid] = {
            "trade_id": tid, "ticker": TICKER, "side": "yes",
            "count_initial": 1, "count": 1, "avg_price": 50, "fees": 0.0,
            "opened_at": "2026-08-28T00:00:00+00:00", "order_ids": [],
            "fill_ids": [], "state": "open", "strategy": "reconciled",
            "market_score": None, "entry_edge": None, "entry_ev": None,
            "avg_price_estimated": True, "fees_estimated": True,
            "opened_at_estimated": True,
        }
        pm.flush()
        rep = pm.reconcile_with_broker()
        self.assertEqual(rep["status"], "MATCH")
        self.assertEqual(pm.open_count(), 1)
        pos = pm.positions[tid]
        self.assertTrue(pos["avg_price_estimated"],
                        "estimated entry data stays labelled estimated")

        # 2) closed != settled: a merely closed market releases nothing.
        cli.get_market.return_value = {"ticker": TICKER, "status": "closed",
                                       "result": ""}
        self.assertEqual(pm.check_settlements(), [])
        self.assertEqual(pm.open_count(), 1)

        # 3) Broker-confirmed final result -> exactly one orphan release.
        cli.get_market.return_value = {"ticker": TICKER, "status": "settled",
                                       "result": "no"}
        realized = pm.check_settlements()
        self.assertEqual(len(realized), 1)
        self.assertTrue(realized[0].get("orphan"))
        self.assertEqual(pm.open_count(), 0)
        orphan_rows = [t for t in tlog.trades if t.get("orphan")]
        self.assertEqual(len(orphan_rows), 1)

        # 4) Restart on the SAME disk; broker is now flat.
        del tlog, pm
        cli2 = self._client(position=0, status="settled", result="no")
        tlog2, pm2 = self._managers(cli2)
        self.assertEqual(pm2.open_count(), 0, "no resurrection after restart")
        self.assertEqual(len([t for t in tlog2.trades if t.get("orphan")]), 1,
                         "the settlement row survived the restart")
        pm2.reconcile_with_broker()
        self.assertEqual(pm2.open_count(), 0)
        self.assertEqual(pm2.check_settlements(), [],
                         "nothing left to settle: no double settlement")
        self.assertEqual(len([t for t in tlog2.trades if t.get("orphan")]), 1)


if __name__ == "__main__":
    unittest.main()
