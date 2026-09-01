# -*- coding: utf-8 -*-
"""Mission E — full state5-style restore simulation.

Restore five golden-shaped files into a virgin directory via the real
env-driven restore (state_epoch created), boot the managers under
REQUIRE_PERSISTENT_STATE=true with ALLOW_ORDER_SUBMISSION=false, replay
the REAL broker payload shape (-6 vs the golden -5), and prove that the
startup reconciliation MISMATCH halts submissions WITHOUT touching one
byte of the restored state. A reconciliation mismatch must never destroy
what the migration just restored — that is the 2026-08-31 incident, and
it must be impossible now.
"""
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from persistence import PersistenceSentinel, verify_state_root  # noqa: E402
from state_restore import RESTORE_BASENAMES, restore_or_die  # noqa: E402
from test_broker_payload_fields import REAL_BROKER_PAYLOAD  # noqa: E402

# Golden-shaped fixture: 3 open positions, the journal that produced them.
# (Shapes mirror production; values are fixture data, not runtime state.)
POSITIONS = {
    "393ba1539d39": {"trade_id": "393ba1539d39",
                     "ticker": "KXBTCD-26AUG2817-T84999.99", "side": "yes",
                     "count_initial": 44, "count": 44, "avg_price": 3,
                     "fees": 0.09, "opened_at": "2026-08-28T01:43:19+00:00",
                     "order_ids": [], "fill_ids": ["f1"], "state": "open",
                     "strategy": "btc_daily_above_strike",
                     "category": "Crypto", "market_score": None,
                     "entry_edge": None, "entry_ev": None},
    "8f8cc89e2847": {"trade_id": "8f8cc89e2847",
                     "ticker": "KXBTCD-26AUG2808-T79799.99", "side": "no",
                     "count_initial": 1, "count": 1, "avg_price": 80,
                     "fees": 0.02, "opened_at": "2026-08-28T11:18:25+00:00",
                     "order_ids": [], "fill_ids": ["f2"], "state": "open",
                     "strategy": "btc_daily_above_strike",
                     "category": "Crypto", "market_score": None,
                     "entry_edge": None, "entry_ev": None},
    "c91a43f56c9c": {"trade_id": "c91a43f56c9c",
                     "ticker": "KXBTCD-26AUG2808-T79599.99", "side": "no",
                     "count_initial": 5, "count": 5, "avg_price": 19,
                     "fees": 0.06, "opened_at": "2026-08-28T11:32:11+00:00",
                     "order_ids": [], "fill_ids": ["f3"], "state": "open",
                     "strategy": "btc_daily_above_strike",
                     "category": "Crypto", "market_score": None,
                     "entry_edge": None, "entry_ev": None},
}
JOURNAL = [{"schema": "v11", "trade_id": tid, "timestamp": p["opened_at"],
            "ticker": p["ticker"], "side": p["side"],
            "requested_count": p["count"], "filled_count": p["count"],
            "avg_fill_price": p["avg_price"], "fees": p["fees"],
            "state": "open", "result": None, "won": None,
            "gross_pnl": None, "net_pnl": None, "settled_at": None}
           for tid, p in POSITIONS.items()]

FILES = {
    "submission_guard.json": json.dumps(
        {"KXBTCD-26AUG2808-T79799.99": 1787915905.081206}, indent=1).encode(),
    "orders_state.json": b"{}",
    "kalshi_trades.json": json.dumps(JOURNAL, indent=1,
                                     ensure_ascii=False).encode(),
    "risk_state.json": json.dumps({"date": "2026-08-16"}, indent=1).encode(),
    "positions_state.json": json.dumps(POSITIONS, indent=1,
                                       ensure_ascii=False).encode(),
}


def _arm_restore_env():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    os.environ["RESTORE_STATE_TGZ_B64"] = \
        base64.b64encode(buf.getvalue()).decode()
    os.environ["RESTORE_STATE_SHA256"] = json.dumps(
        {n: hashlib.sha256(p).hexdigest() for n, p in FILES.items()})


class State5RestoreSimulationTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_state5_")
        self._saved = {k: getattr(bot.CFG, k) for k in
                       ("DATA_DIR", "REQUIRE_PERSISTENT_STATE",
                        "ALLOW_FRESH_STATE", "ALLOW_ORDER_SUBMISSION",
                        "MAX_CONTRACTS_PER_ORDER")}
        bot.CFG.DATA_DIR = self.tmp
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        bot.CFG.ALLOW_FRESH_STATE = False
        bot.CFG.ALLOW_ORDER_SUBMISSION = False
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "1"
        PersistenceSentinel.reset()
        self._env = {k: os.environ.pop(k, None) for k in
                     ("RESTORE_STATE_TGZ_B64", "RESTORE_STATE_SHA256")}
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(bot.CFG, k, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hashes(self):
        return {n: hashlib.sha256(
            open(os.path.join(self.tmp, n), "rb").read()).hexdigest()
            for n in RESTORE_BASENAMES}

    def test_restored_state_survives_reconciliation_mismatch(self):
        # 1-2) Restore into the virgin directory; state_epoch created.
        _arm_restore_env()
        restore_or_die()                      # must not raise
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "state_epoch.json")))
        golden = self._hashes()
        for n, payload in FILES.items():
            self.assertEqual(golden[n], hashlib.sha256(payload).hexdigest())

        # 3) Boot: continuity check passes, managers load restored state.
        self.assertTrue(verify_state_root())
        self.assertTrue(PersistenceSentinel.healthy())
        cli = MagicMock()
        cli.env = "demo"
        cli.get_positions.return_value = [dict(e)
                                          for e in REAL_BROKER_PAYLOAD]
        tlog = bot.TradeLogger()
        pm = bot.PositionManager(cli, tlog)
        self.assertEqual(pm.open_count(), 3)
        self.assertEqual(len(tlog.trades), 3)

        # 4-5) Replay the real broker payload: -6 vs restored -5.
        report = pm.reconcile_with_broker()
        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual(report["mismatches"],
                         [{"ticker": "KXBTCD-26AUG2808-T79599.99",
                           "kind": "quantity_mismatch",
                           "broker": -6, "local": -5}])
        self.assertIsNotNone(pm.reconcile_halt)

        # 6) The five restored files are byte-identical after the mismatch.
        self.assertEqual(self._hashes(), golden,
                         "a reconciliation mismatch must not touch one "
                         "byte of the restored state")
        self.assertEqual(pm.open_count(), 3)

        # 7) Engine gate: alive but halted read-only.
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr = pm
        eng.risk = MagicMock()
        ok, guard = eng._post_balance_gates()
        self.assertFalse(ok)
        self.assertEqual(guard, "reconciliation_mismatch")

        # 8-10) Submissions disabled; zero broker writes end to end.
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXBTCD-26AUG2817-T84999.99", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:submission_disabled")
        self.assertEqual(cli.create_order.call_count, 0)
        self.assertEqual(cli.cancel_order.call_count, 0)
        self.assertEqual(self._hashes(), golden)


if __name__ == "__main__":
    unittest.main()
