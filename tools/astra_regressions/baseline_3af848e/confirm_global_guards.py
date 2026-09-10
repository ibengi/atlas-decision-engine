"""Defensive reliability tests; temporary data and synthetic adapters only.

No repository tests/helpers are imported. The fixture is from the first
independent review with explicit support for the new completeness contract.
No credentials, broker traffic, infrastructure changes, or production writes.
"""
import copy
import errno
import hashlib
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter
from cryptography.hazmat.primitives.asymmetric import rsa
import fixture as F
from config import CFG, _p
import continuity as C
import equity_ledger as EL
import execution_engine as EE
import model_gatekeeper as MG
import persistence as P
from kalshi_client import KalshiClient, KalshiAPIError, BrokerWriteForbidden
from order_manager import OrderManager
from trade_logger import TradeLogger

logging.disable(logging.CRITICAL)
OUT = Path(sys.argv[1])
RESULTS = []
D = {}
REPO = Path.cwd()
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

class ReviewNeeded(Exception):
    pass

def check(condition, message):
    if not condition:
        raise AssertionError(message)

def snap(f):
    return f.snapshot()

def hashes():
    return {k:hashlib.sha256(v).hexdigest() for k,v in F.image().items()}

def run(name, area, fn, seed=True, tags=()):
    global D
    D = {}
    f = None
    try:
        f = F.Fixture(seed)
        # An intentionally empty, valid pending file for the positive fixture.
        P.JsonStore.save(_p(OrderManager.PENDING_FILE), {})
        fn(f)
        row = dict(id=name, area=area, status='PASS', detail=copy.deepcopy(D))
    except ReviewNeeded as e:
        row = dict(id=name, area=area, status='NEEDS_REVIEW', reason=str(e), detail=copy.deepcopy(D))
    except AssertionError as e:
        row = dict(id=name, area=area, status='FAIL', reason=str(e), detail=copy.deepcopy(D))
    except Exception as e:
        import traceback
        row = dict(id=name, area=area, status='NEEDS_REVIEW', reason=repr(e),
                   traceback=traceback.format_exc(), detail=copy.deepcopy(D))
    finally:
        if f:
            f.close()
    row['tags'] = list(tags)
    RESULTS.append(row)
    print(name, row['status'], row.get('reason',''), flush=True)

class Adapter(BaseAdapter):
    def __init__(self, pages=None, status=200, exception=None):
        self.pages = pages or [{'market_positions':[], 'cursor':''}]
        self.status, self.exception = status, exception
        self.calls, self.pos_n = [], 0
    def send(self, request, **kwargs):
        self.calls.append({'method':request.method,'path':request.path_url})
        ispos = '/positions' in request.path_url
        if ispos and self.exception:
            raise self.exception
        if ispos:
            payload = self.pages[min(self.pos_n,len(self.pages)-1)]
            self.pos_n += 1
            if isinstance(payload, Exception):
                raise payload
        elif request.method == 'GET':
            payload = {'orders':[], 'cursor':''}
        else:
            payload = {'order':{'order_id':'synthetic-only','status':'resting',
                      'remaining_count':1,'fill_count':0}}
        response = requests.Response()
        # Stop simulated submissions at the measured adapter boundary;
        # no fill-polling or real-time order lifecycle is required here.
        response.status_code = self.status if ispos else (200 if request.method=='GET' else 400)
        response._content = json.dumps(payload).encode()
        response.headers['Content-Type'] = 'application/json'
        response.request = request
        return response
    def close(self):
        pass

def client(pages=None, status=200, exception=None, demo=False):
    c = KalshiClient(env='prod',cache_enabled=False)
    c.base_url = 'https://atlas-review.invalid/trade-api/v2'
    c.key_id = 'synthetic-review'
    c._pk = KEY
    c.session.trust_env = False
    if demo:
        c.env = 'demo'
    adapter = Adapter(pages,status,exception)
    c.session.mount('https://',adapter)
    return c,adapter

def install_client(f,c):
    f.broker = c
    f.pos.client = c
    f.orders.client = c

def restart_blocked(f):
    P.PersistenceSentinel.reset()
    f.reload()
    D['restart'] = snap(f)
    check(not f.led.capital_eligible(), 'Restart accepted uncertain economic state')

def safety_restore(f,mode):
    old=F.image()
    f.trade(-3)
    current=F.image()
    jp,lp,cp=CFG.TRADES_FILE,EL.LEDGER_FILE,C.CONTINUITY_FILE
    if mode.startswith('delete_'):
        selected={'journal':[jp], 'ledger':[lp], 'chain':[cp],
                  'chain_journal':[cp,jp], 'chain_ledger':[cp,lp]}[mode[7:]]
        for name in selected: F.remove_versions(name)
    elif mode=='all_old': F.restore(old)
    elif mode=='economic_old_chain_current':
        F.restore(old)
        Path(_p(cp)).write_bytes(current[cp])
    elif mode=='chain_old_only': Path(_p(cp)).write_bytes(old[cp])
    elif mode=='journal_ahead':
        f.trade(-2,observe=False)
    elif mode=='ledger_old_only':
        for name,data in old.items():
            if name.startswith(lp): Path(_p(name)).write_bytes(data)
    elif mode=='repeated_mismatch':
        P.JsonStore.save(_p(jp),[])
        for _ in range(4): f.reload()
    elif mode=='evidence_return':
        P.JsonStore.save(_p(jp),[]); f.reload()
        check(not f.led.capital_eligible(),'Initial mismatch not blocked')
        F.restore(current); f.reload()
        D['restart']=snap(f)
        check(f.led.capital_eligible() and f.led.drawdown_pct()>=30-1e-8,
              'Restored valid evidence failed recovery')
        return
    f.reload()
    D['restart']=snap(f)
    if mode=='journal_ahead':
        check(f.led.drawdown_pct()>=50-1e-8,'Journal-ahead loss was not recomputed')
    else:
        check(not f.led.capital_eligible(),'Rollback or missing evidence remained admissible')


def engine(f):
    e=EE.ExecutionEngine.__new__(EE.ExecutionEngine)
    e.client,e.orders,e.posmgr,e.risk,e.equity=f.broker,f.orders,f.pos,f.risk,f.led
    e.configured_capital=10
    return e

def nonfinite_global(f):
    e=engine(f)
    balance=e._balance_gate(float('nan'))
    verdict=e._evaluate_global_guards()
    D.update(balance_gate=balance,global_guards=verdict,snapshot=snap(f))
    check(not balance[0] or not verdict[0],'NaN balance passed balance and global accounting gates')
run('CONFIRM_nan_balance_global_gates','A16',nonfinite_global,tags=('malformed','new'))

def stale_release_global(f):
    f.trade(-3,settled_at='2026-09-01T00:00:00Z')
    p=f.proposal();check(f.apply(p,f.ctx()),'Rebase control failed')
    e=engine(f);before=e._evaluate_global_guards()
    check(not before[0],'Hold was not blocking before test')
    p=f.led.propose_hold_release('other-action','synthetic-validation')
    other=EL.EquityLedger(f.tlog,f.pos,env='prod')
    check(other.save(),'Concurrent writer control failed')
    accepted=f.led.apply_hold_release('other-action','synthetic-validation',p['token'])
    after=e._evaluate_global_guards()
    D.update(release_return=accepted,before=before,after=after,snapshot=snap(f))
    check(accepted or not after[0],'Failed fenced hold release passed actual global risk gates')
run('CONFIRM_stale_hold_release_global_gates','A15',stale_release_global,tags=('race','persistence','new'))

OUT.write_text(json.dumps({'results':RESULTS,'counts':{s:sum(r['status']==s for r in RESULTS) for s in ('PASS','FAIL','NEEDS_REVIEW')},'capital_enabled':False,'broker_writes':0},indent=2,default=str))
print(json.dumps({s:sum(r['status']==s for r in RESULTS) for s in ('PASS','FAIL','NEEDS_REVIEW')}))

sys.exit(1 if any(r['status']=='FAIL' for r in RESULTS) else 2 if any(r['status']=='NEEDS_REVIEW' for r in RESULTS) else 0)
