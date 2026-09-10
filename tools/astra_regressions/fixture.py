"""Independent, credential-free safety probes against unmodified production classes.

FAIL means the stated safety assertion failed, not that the probe failed to run.
The original candidate's tests/helpers are deliberately not imported.
All broker responses are in-memory fixtures or requests adapters; socket access
is separately denied by offline/sitecustomize.py in this process and children.
"""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter
from cryptography.hazmat.primitives.asymmetric import rsa
import config
from config import CFG, _p
import equity_ledger as EL
from equity_ledger import EquityLedger
from execution_engine import ExecutionEngine
import execution_engine as EE
from kalshi_client import KalshiClient, KalshiAPIError, BrokerWriteForbidden
from order_manager import OrderManager
from persistence import JsonStore, PersistenceSentinel
from position_manager import PositionManager
from risk_manager import RiskManager
from trade_logger import TradeLogger
from authority_double import provider_for, FrozenSyntheticBroker, initialize_empty

logging.basicConfig(level=logging.ERROR)
OUT = Path(sys.argv[1])
OLD = '--old' in sys.argv
RESULTS = []
DETAIL = {}
os.environ['PROD_ACCESS_MODE'] = 'READ_ONLY'
os.environ['LIVE_BROKER_WRITES_AUTHORIZED'] = 'false'

class Broker:
    env = 'prod'
    def __init__(self):
        self.orders = []
        self.positions = []
        self.orders_error = None
        self.positions_error = None
        self.on_positions = None
        self.execution_freeze = FrozenSyntheticBroker(self)
    def list_orders(self, **kw):
        if self.orders_error: raise self.orders_error
        return copy.deepcopy(self.orders)
    def get_positions_proof(self):
        return {'rows': self.get_positions(), 'complete': True, 'pages': 1, 'cursors': []}
    def get_positions(self):
        if self.on_positions: self.on_positions()
        if self.positions_error: raise self.positions_error
        return copy.deepcopy(self.positions)

class Fixture:
    def __init__(self, seed=True):
        self.tmp = tempfile.TemporaryDirectory(prefix='atlas-adversary-')
        self.previous_data = CFG.DATA_DIR
        self.previous_identity = CFG.BROKER_ACCOUNT_ID
        CFG.BROKER_ACCOUNT_ID = "synthetic-account"
        self.previous_env_data = os.environ.get('DATA_DIR')
        CFG.DATA_DIR = self.tmp.name
        os.environ['DATA_DIR'] = self.tmp.name
        PersistenceSentinel.reset()
        self.broker = Broker()
        initialize_empty()
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        self.tlog = TradeLogger()
        self.pos = PositionManager(self.broker, self.tlog)
        self.orders = OrderManager(self.broker)
        self.pos.flush()
        self.orders.flush()
        self.risk = RiskManager(self.tlog, self.pos, 10)
        self.led = EquityLedger(self.tlog, self.pos, env='prod', authority=provider_for())
        self.risk.equity = self.led
        if seed:
            p = self.led.propose_seed(10, '2026-09-01T00:00:00Z', 'audit-fixture', 10)
            assert self.led.apply_seed(p, p['sha256'])
            p = self.led.propose_attestation('fixture-attest', 'a'*64)
            assert self.led.apply_attestation('fixture-attest', 'a'*64, p['token'])
    def close(self):
        CFG.DATA_DIR = self.previous_data
        CFG.BROKER_ACCOUNT_ID = self.previous_identity
        if self.previous_env_data is None: os.environ.pop('DATA_DIR', None)
        else: os.environ['DATA_DIR'] = self.previous_env_data
        self.tmp.cleanup()
        PersistenceSentinel.reset()
    def reload(self):
        self.tlog = TradeLogger()
        self.pos = PositionManager(self.broker, self.tlog)
        self.orders = OrderManager(self.broker)
        self.risk = RiskManager(self.tlog, self.pos, 10)
        self.led = EquityLedger(self.tlog, self.pos, env='prod', authority=provider_for())
        self.risk.equity = self.led
        return self.led
    def trade(self, pnl=None, observe=True, settled_at=None):
        if settled_at:
            with patch("trade_logger.now_iso", return_value=settled_at):
                return self.trade(pnl, observe=observe)
        n = len(self.tlog.trade_rows())
        t = self.tlog.open_trade(ticker='KXBTC15M-AUDIT-'+str(n),market_title='Offline fixture',
            side='yes',req_price=50,avg_price=50,req_count=10,filled_count=10,spread=1,
            fees=0,edge=.1,ev=.1,confidence=8,grade='A',reason='offline',analysis={},
            order_id='audit-'+str(n),order_status='executed')
        if pnl is not None:
            self.tlog.settle_trade(t['trade_id'], 'yes' if pnl>0 else 'no', pnl>0, pnl, pnl)
            t = next(row for row in self.tlog.trades if row['trade_id'] == t['trade_id'])
            if observe: self.led.observe(10+self.led.realized_pnl_cum())
        return t
    def ctx(self):
        return EE.equity_rebase_context(self.broker, self.orders, self.pos, self.risk, equity=self.led)
    def proposal(self): return self.led.propose_rebase('Offline audited rebase', 'fixture-rebase')
    def apply(self, p, ctx):
        return self.led.apply_rebase(p['reason'], p['operator_action_id'], p['token'], ctx)
    def snapshot(self):
        s = self.led.snapshot()
        s['conservative_equity'] = self.led.strategy_equity_conservative()
        s['tokens_consumed'] = len(self.led.state['consumed_tokens'])
        s['persistence_healthy'] = PersistenceSentinel.healthy()
        return s

def check(ok, message):
    if not ok: raise AssertionError(message)

def run(name, fn, seed=True):
    global DETAIL
    DETAIL = {}
    f = None
    try:
        f = Fixture(seed)
        fn(f)
        row = {'id':name, 'status':'PASS', 'detail':copy.deepcopy(DETAIL)}
    except AssertionError as e:
        row = {'id':name, 'status':'FAIL', 'reason':str(e), 'detail':copy.deepcopy(DETAIL)}
    except Exception as e:
        row = {'id':name, 'status':'ERROR', 'reason':repr(e), 'traceback':traceback.format_exc(),
               'detail':copy.deepcopy(DETAIL)}
    finally:
        if f: f.close()
    RESULTS.append(row)
    print(name, row['status'], row.get('reason',''), flush=True)

def image():
    return {p.name:p.read_bytes() for p in Path(CFG.DATA_DIR).iterdir() if p.is_file()}

def restore(files):
    for p in Path(CFG.DATA_DIR).iterdir():
        if p.is_file(): p.unlink()
    for name,data in files.items(): Path(_p(name)).write_bytes(data)

def remove_versions(name):
    for p in Path(CFG.DATA_DIR).glob(name+'*'):
        if p.is_file(): p.unlink()

def refusal(f, ctx):
    p = f.proposal()
    before = copy.deepcopy(f.led.state)
    disk = image()
    ok = f.apply(p, ctx)
    DETAIL.update(accepted=ok, context=ctx, snapshot=f.snapshot(),
                  memory_unchanged=(before==f.led.state),disk_unchanged=(disk==image()))
    check(not ok, 'Unsafe rebase was accepted')
    check(before==f.led.state and disk==image(), 'Refusal mutated state or consumed a token')
