# -*- coding: utf-8 -*-
"""Shared fixtures for the Astra A01-A12 regression families.

Every case below runs the PRODUCTION classes -- `TradeLogger`,
`PositionManager`, `OrderManager`, `EquityLedger`, `RiskManager`,
`KalshiClient`, `ExecutionEngine` -- on a throwaway DATA_DIR. Nothing here
opens a socket: the broker cases drive `KalshiClient` through a synthetic
transport that replaces only the send adapter, so request building and
signing stay real while no packet leaves the process.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

import equity_ledger as EL                                   # noqa: E402
from config import CFG, _p                                   # noqa: E402
from continuity import CONTINUITY_FILE, ContinuityChain      # noqa: E402
from equity_ledger import EquityLedger                       # noqa: E402
from persistence import JsonStore, PersistenceSentinel       # noqa: E402
from position_manager import PositionManager                 # noqa: E402
from trade_logger import TradeLogger                         # noqa: E402

PRE_AT = "2026-09-07T18:01:19Z"


class Client:
    """Read-only broker double honouring the collection-completeness
    contract (A02). `positions_complete=False` models a listing the broker
    never finished."""
    env = "prod"

    def __init__(self, orders=(), positions=(), orders_error=None,
                 positions_complete=True):
        self.orders = list(orders)
        self.positions = list(positions)
        self.orders_error = orders_error
        self.positions_complete = positions_complete
        self.order_calls = 0

    def list_orders(self, **kw):
        self.order_calls += 1
        if self.orders_error:
            raise self.orders_error
        return list(self.orders)

    def get_positions(self):
        return list(self.positions)

    def get_positions_proof(self, **_kw):
        return {"rows": list(self.positions),
                "complete": bool(self.positions_complete),
                "pages": 1, "cursors": [],
                "reason": None if self.positions_complete
                else "pagination truncated"}


def trade(tlog, ticker="KXBTC15M-X", count=6, price=50, order_id=None):
    return tlog.open_trade(
        ticker=ticker, market_title="m", side="yes", req_price=price,
        avg_price=price, req_count=count, filled_count=count, spread=1,
        fees=0.0, edge=0.1, ev=0.1, confidence=8, grade="A", reason="r",
        analysis={}, order_id=order_id or ("o-" + ticker),
        order_status="executed")


class AstraCase(unittest.TestCase):
    """Isolated DATA_DIR, clean promotion environment, real classes."""

    ENV_KEYS = ("PROD_ACCESS_MODE", "KALSHI_ENV_CONFIRM", "LIVE_TRADING",
                "LIVE_TRADING_CONFIRMED", "LIVE_BROKER_WRITES_AUTHORIZED",
                "RISK_EQUITY_MODE")

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self._tmp = tempfile.mkdtemp(prefix="astra-")
        self._data_dir = patch.object(CFG, "DATA_DIR", self._tmp)
        self._data_dir.start()
        self._mode = patch.object(CFG, "RISK_EQUITY_MODE", "strategy")
        self._mode.start()
        PersistenceSentinel.reset()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self._mode.stop()
        self._data_dir.stop()
        PersistenceSentinel.reset()
        shutil.rmtree(self._tmp, ignore_errors=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── state helpers ───────────────────────────────────────────────────
    def stack(self, client=None):
        client = client or Client()
        tlog = TradeLogger()
        pos = PositionManager(client, tlog)
        return client, tlog, pos

    def reconciled_ledger(self, tlog, pos, env="prod", cash=10.0):
        led = EquityLedger(tlog, pos, env=env)
        prop = led.propose_seed(cash, PRE_AT, "evidence-ref", cash)
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        att = led.propose_attestation("OPS-A", "a" * 64)
        self.assertTrue(led.apply_attestation("OPS-A", "a" * 64, att["token"]))
        self.assertEqual(led.derive_status(), EL.STATUS_RECONCILED)
        return led

    def lose(self, tlog, led, pnl=-3.0, cash=7.0, start=1, ticker="KXBTC15M-X"):
        t = trade(tlog, ticker=ticker)
        tlog.settle_trade(t["trade_id"], "no", False, pnl, pnl)
        for i in range(3):
            led.observe(cash, cycle_n=start + i, quiet=True)
        return t

    def reload(self, client=None):
        client, tlog, pos = self.stack(client)
        return EquityLedger(tlog, pos, env="prod"), tlog, pos

    # ── raw file access (the rollback surface) ──────────────────────────
    def ledger_file(self):
        return JsonStore.load(_p(EL.LEDGER_FILE), {})

    def snapshot_dir(self) -> dict:
        """Every byte under DATA_DIR, for a coherent restore or a
        byte-for-byte immutability assertion."""
        out = {}
        for name in sorted(os.listdir(self._tmp)):
            path = os.path.join(self._tmp, name)
            if os.path.isfile(path):
                out[name] = open(path, "rb").read()
        return out

    def restore_dir(self, snap: dict, only=None) -> None:
        for name, payload in snap.items():
            if only is not None and name not in only:
                continue
            with open(os.path.join(self._tmp, name), "wb") as fh:
                fh.write(payload)

    def chain(self) -> ContinuityChain:
        return ContinuityChain(os.path.join(self._tmp, CONTINUITY_FILE))

    def write_raw(self, name, payload) -> None:
        with open(os.path.join(self._tmp, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def assert_loss_preserved(self, led, floor_equity=7.0, hwm=10.0):
        """The evidenced loss survives: CAPITAL blocked, the HWM never
        lowered, and the reported drawdown still reflects the loss."""
        self.assertFalse(led.capital_eligible(), led.snapshot())
        self.assertEqual(led.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=6)
        self.assertLessEqual(led.strategy_equity_conservative(),
                             floor_equity + 1e-9)
        self.assertGreaterEqual(led.drawdown_pct(), 30.0 - 1e-6)
