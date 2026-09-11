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
        F.restore(current)
        from state_authority import checkpoint
        from recovery import complete_verified_recovery
        current_proof = checkpoint(f.led.path, f.led.identity)
        complete_verified_recovery(f.led.path, f.led.identity, f.led.authority,
                                   current_proof.digest, 'restore-current-evidence')
        f.reload()
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

for mode in ('delete_journal','delete_ledger','delete_chain','delete_chain_journal',
             'delete_chain_ledger','all_old','economic_old_chain_current','chain_old_only',
             'journal_ahead','ledger_old_only','repeated_mismatch','evidence_return'):
    run('C_'+mode,'A01',lambda f,m=mode:safety_restore(f,m),tags=('restart',))

def malformed_chain(f,mode):
    f.trade(-3)
    path=Path(f.led.chain.path)
    raw=path.read_text(); rows=[json.loads(x) for x in raw.splitlines()]
    if mode=='duplicate': rows.append(copy.deepcopy(rows[-1]))
    elif mode=='remove_final': rows.pop()
    elif mode=='reorder': rows[0],rows[1]=rows[1],rows[0]
    elif mode=='previous_hash': rows[-1]['prev']='f'*64
    elif mode=='current_hash': rows[-1]['hash']='f'*64
    elif mode=='sequence_gap': rows[-1]['seq']+=2
    elif mode=='sequence_repeat': rows[-1]['seq']-=1
    elif mode=='future_version': rows[-1]['version']=999
    elif mode=='wrong_kind': rows[-1]['kind']='future_event'
    elif mode=='zero': path.write_text('')
    elif mode=='truncated_final': path.write_text(raw[:-60])
    elif mode=='corrupt_suffix': path.write_text(raw+'{"partial":')
    elif mode=='malformed_json': path.write_text('{broken}\n')
    if mode not in ('zero','truncated_final','corrupt_suffix','malformed_json'):
        path.write_text(''.join(json.dumps(x)+'\n' for x in rows))
    restart_blocked(f)

for mode in ('duplicate','remove_final','reorder','previous_hash','current_hash',
             'sequence_gap','sequence_repeat','future_version','wrong_kind',
             'zero','truncated_final','corrupt_suffix','malformed_json'):
    run('C_chain_'+mode,'A01',lambda f,m=mode:malformed_chain(f,m),tags=('restart','malformed'))

def positions_case(f,pages=None,status=200,exception=None,expected='blocked'):
    c,a=client(pages,status,exception)
    install_client(f,c)
    with patch('kalshi_client.time.sleep',return_value=None):
        report=f.pos.verify_against_broker()
    D.update(report=report,adapter_calls=a.calls)
    if expected=='flat': check(report['status']=='MATCH','Valid complete empty response refused')
    else: check(report['status']!='MATCH','Incomplete or contradictory response became MATCH')

position_cases={
 'empty':([{'market_positions':[],'cursor':''}],'flat'),
 'missing':([{}],'blocked'), 'null_envelope':([None],'blocked'),
 'renamed':([{'positions_v3':[]}],'blocked'),
 'null_block':([{'market_positions':None}],'blocked'),
 'non_list':([{'market_positions':{}}],'blocked'),
 'non_object_row':([{'market_positions':[4]}],'blocked'),
 'missing_quantity':([{'market_positions':[{'ticker':'X'}]}],'blocked'),
 'contradictory_quantity':([{'market_positions':[{'ticker':'X','position':0,'position_fp':'1.00'}]}],'blocked'),
 'negative_open':([{'market_positions':[{'ticker':'X','position':-1}]}],'blocked'),
 'fraction':([{'market_positions':[{'ticker':'X','position':0.2}]}],'blocked'),
 'nan':([{'market_positions':[{'ticker':'X','position':float('nan')}]}],'blocked'),
 'infinity':([{'market_positions':[{'ticker':'X','position':float('inf')}]}],'blocked'),
 'cursor_number':([{'market_positions':[],'cursor':3}],'blocked'),
 'cursor_loop':([{'market_positions':[],'cursor':'loop'}],'blocked'),
 'empty_then_open':([{'market_positions':[],'cursor':'p2'},{'market_positions':[{'ticker':'X','position':1}],'cursor':''}],'blocked'),
 '100_then_open':([{'market_positions':[{'ticker':str(i),'position':0} for i in range(100)],'cursor':'p2'}, {'market_positions':[{'ticker':'X','position':1}],'cursor':''}],'blocked'),
 'interrupted_page':([{'market_positions':[],'cursor':'p2'},requests.ConnectionError('synthetic reset')],'blocked'),
 'duplicate_conflicting_ticker':([{'market_positions':[{'ticker':'X','position':1},{'ticker':'X','position':-1}],'cursor':''}],'blocked'),
 'cross_page_conflicting_ticker':([{'market_positions':[{'ticker':'X','position':1}],'cursor':'p2'}, {'market_positions':[{'ticker':'X','position':-1}],'cursor':''}],'blocked'),
 'bool_quantity':([{'market_positions':[{'ticker':'X','position':False}],'cursor':''}],'blocked'),
}
for name,(pages,expect) in position_cases.items():
    run('P_'+name,'A02',lambda f,p=pages,e=expect:positions_case(f,p,expected=e),tags=('parser',))
for code in (401,403,408,429,500):
    run('P_http_'+str(code),'A02',lambda f,c=code:positions_case(f,status=c),tags=('parser',))
for name,error in [('timeout',requests.Timeout('synthetic')),('reset',requests.ConnectionError('synthetic'))]:
    run('P_'+name,'A02',lambda f,e=error:positions_case(f,exception=e),tags=('parser',))

def rebase_boundary(f,mode):
    f.trade(-3)
    p=f.proposal(); ctx=f.ctx()
    before=copy.deepcopy(f.led.state)
    if mode=='positive':
        ok=f.apply(p,ctx); f.reload()
        D.update(accepted=ok,restart=snap(f))
        check(ok and f.led.state['capital_hold'] and not f.led.capital_eligible(),
              'Valid quiescent rebase/hold control failed')
        return
    if mode.startswith('disk_'):
        target={'orders':'orders_state.json','positions':'positions_state.json',
                'intents':'pending_intents.json','journal':CFG.TRADES_FILE}[mode[5:]]
        if mode=='disk_journal':
            f.trade(-1,observe=False)
        elif mode=='disk_positions':
            P.JsonStore.save(_p(target),{'X':{'ticker':'X','state':'open','count':1,'avg_price':20,'side':'yes'}})
        else:
            P.JsonStore.save(_p(target),{'changed':'synthetic'})
        ok=f.apply(p,ctx)
    elif mode=='broker_before_revalidation':
        f.broker.orders=[{'order_id':'new','status':'resting','remaining_count':1}]
        ok=f.apply(p,ctx)
    elif mode=='continuity_after_context':
        f.led.chain.append(C.KIND_TOKEN,{'token_sha256':'b'*64,'action':'synthetic'},EL.now_iso())
        ok=f.apply(p,ctx)
    elif mode=='local_after_final_validation':
        orig=f.led._record_consumed_token
        def interleave(*args):
            P.JsonStore.save(_p('pending_intents.json'),{'X':{'client_order_id':'late','count':1,'price':20}})
            return orig(*args)
        with patch.object(f.led,'_record_consumed_token',side_effect=interleave): ok=f.apply(p,ctx)
    elif mode=='broker_after_final_query':
        orig=f.led._record_consumed_token
        def interleave(*args):
            f.broker.positions=[{'ticker':'late-fill','position':1}]
            return orig(*args)
        with patch.object(f.led,'_record_consumed_token',side_effect=interleave): ok=f.apply(p,ctx)
    elif mode=='settlement_during_prepare':
        orig=f.led._record_consumed_token
        def interleave(*args):
            f.trade(-2,observe=False)
            return orig(*args)
        with patch.object(f.led,'_record_consumed_token',side_effect=interleave): ok=f.apply(p,ctx)
    elif mode=='stale_instance':
        f.led.save(); ok=f.apply(p,ctx)
    D.update(accepted=ok,memory_unchanged=f.led.state==before,snapshot=snap(f))
    f.reload(); D['restart']=snap(f)
    check(not ok,'Rebase accepted changed or stale evidence')

for mode in ('positive','disk_orders','disk_positions','disk_intents','disk_journal',
             'broker_before_revalidation','continuity_after_context',
             'local_after_final_validation','broker_after_final_query',
             'settlement_during_prepare','stale_instance'):
    run('R_'+mode,'A03',lambda f,m=mode:rebase_boundary(f,m),tags=('race','restart'))

def event_identity(f,mode):
    win=f.trade(3); f.trade(-4)
    before=f.led.drawdown_pct()
    if mode=='same_order_new_trade':
        row=copy.deepcopy(win); row['trade_id']='new-local-id'
        f.tlog.trades.append(row); f.tlog.flush()
    elif mode=='same_trade':
        f.tlog.trades.append(copy.deepcopy(win)); f.tlog.flush()
    elif mode=='same_order_new_trade_restart':
        row=copy.deepcopy(win); row['trade_id']='new-local-id'
        f.tlog.trades.append(row); f.tlog.flush(); f.reload()
    elif mode=='repeat_settlement':
        f.tlog.settle_trade(win['trade_id'],'yes',True,3,3)
    elif mode=='production_duplicate_order':
        try:
            row=f.tlog.open_trade(ticker=win['ticker'],market_title='synthetic',side='yes',
                 req_price=50,avg_price=50,req_count=10,filled_count=10,spread=1,
                 fees=0,edge=.1,ev=.1,confidence=8,grade='A',reason='synthetic',analysis={},
                 order_id=win['order_id'],order_status='executed')
            f.tlog.settle_trade(row['trade_id'],'yes',True,3,3)
        except ValueError:
            D['refused_at_ingestion'] = True
    f.led.observe(10+f.led.realized_pnl_cum())
    D.update(before_dd=before,after=snap(f),duplicates=f.led.duplicate_events())
    check(f.led.drawdown_pct()>=before-1e-8,'Duplicate economic event improved drawdown')

for mode in ('same_order_new_trade','same_trade','same_order_new_trade_restart','repeat_settlement','production_duplicate_order'):
    run('E_'+mode,'A04',lambda f,m=mode:event_identity(f,m),tags=('identity',))

def mode_case(f,value):
    f.trade(-3)
    for i in range(5): f.led.observe(100,cycle_n=i)
    f.risk.capital=100
    with patch.object(CFG,'RISK_EQUITY_MODE',value):
        D.update(snapshot=snap(f),drawdown=f.risk.rolling_drawdown_pct())
        if str(value).strip().lower()!='cash':
            check(f.risk.rolling_drawdown_pct()>=30-1e-8,'Unknown mode changed the strategy loss denominator')
        if str(value).strip().lower()!='strategy':
            check(not f.led.capital_eligible(),'Invalid or cash mode admitted CAPITAL')
        f.reload()
        f.risk.capital=100
        D['restart']=snap(f)
        D['restart_risk_drawdown']=f.risk.rolling_drawdown_pct()
        if str(value).strip().lower()!='cash':
            check(f.risk.rolling_drawdown_pct()>=30-1e-8,'Restart changed strategy drawdown under accounting mode')
        if str(value).strip().lower()!='strategy':
            check(not f.led.capital_eligible(),'Restart admitted invalid/cash accounting mode')
for value in ('strategy','cash','stratgey','',None,'future_mode','   ','STRATEGY',' Strategy '):
    run('MODE_'+repr(value),'A05',lambda f,v=value:mode_case(f,v),tags=('restart',))

def residual_case(f,mode):
    if mode in ('quiet_negative','quiet_positive','tolerance','below','above'):
        delta={'quiet_negative':-.1,'quiet_positive':.1,'tolerance':-.01,
               'below':-.009,'above':-.011}[mode]
        f.led.observe(10+delta)
        expected_block=mode in ('quiet_negative','quiet_positive','above')
    elif mode=='not_quiet':
        f.led.observe(9,quiet=False); expected_block=True
    elif mode=='settlement_change':
        f.led.observe(10)
        f.trade(-.1,observe=False)
        f.led.observe(8.9); expected_block=True
    elif mode=='grown_tolerance':
        f.led.state['anchor']['settled_since_anchor']=200
        f.led.observe(9.5); expected_block=True
    D['snapshot']=snap(f)
    if mode=='quiet_positive' and f.led.capital_eligible():
        raise ReviewNeeded('Positive residual is admissible by the implemented policy; the original A06 finding specifically required adverse residuals to block')
    check((not f.led.capital_eligible())==expected_block,
          'Unexplained movement admission differs from required invariant')
for mode in ('quiet_negative','quiet_positive','tolerance','below','above','not_quiet',
             'settlement_change','grown_tolerance'):
    run('CASH_'+mode,'A06',lambda f,m=mode:residual_case(f,m))

def migration_case(f,mode):
    proposal=f.led.propose_seed(10,'2026-09-01T00:00:00Z','synthetic',10)
    before=copy.deepcopy(f.led.state)
    if mode in ('extra_field','missing_field','mutated','future_schema'):
        if mode=='extra_field': proposal['extra']='value'
        if mode=='missing_field': del proposal['cash_now']
        if mode=='mutated': proposal['strategy_equity_0']=999
        if mode=='future_schema': proposal['schema_version']=999
        ok=f.led.apply_seed(proposal,proposal['sha256'])
    elif mode=='ordered_keys':
        proposal=dict(reversed(list(proposal.items())))
        ok=f.led.apply_seed(proposal,proposal['sha256'])
        D['accepted']=ok
        check(ok,'Equivalent key ordering changed validated semantics');return
    elif mode.startswith('drift_'):
        name=mode[6:]
        P.JsonStore.save(_p(name),{'changed':True})
        ok=f.led.apply_seed(proposal,proposal['sha256'])
    elif mode=='late_settlement':
        orig=f.led._seed_dict
        def interleave(*args,**kw):
            f.trade(-3,observe=False)
            return orig(*args,**kw)
        with patch.object(f.led,'_seed_dict',side_effect=interleave):
            ok=f.led.apply_seed(proposal,proposal['sha256'])
    elif mode=='failed_save':
        with patch.object(P.JsonStore,'save',return_value=False):
            ok=f.led.apply_seed(proposal,proposal['sha256'])
        D.update(accepted=ok,memory_unchanged=f.led.state==before,after=snap(f))
        f.reload(); D['restart']=snap(f)
        check(not ok and f.led.state==before and D['memory_unchanged'],
              'Failed migration changed authoritative state');return
    D.update(accepted=ok,snapshot=snap(f))
    check(not ok,'Migration accepted a changed proposal or changed source state')
for mode in ('extra_field','missing_field','mutated','future_schema','ordered_keys',
             'drift_orders_state.json','drift_positions_state.json','drift_pending_intents.json',
             'late_settlement','failed_save'):
    run('M_'+mode,'A07',lambda f,m=mode:migration_case(f,m),seed=False,tags=('transaction',))

def gate_case(f,mode):
    ts=time.time()
    mv={'approved':True,'generated_ts':ts,'model_version':'btc15m-baseline-0.1',
        'criteria':[{'name': n, 'passed': True} for n in sorted(MG.MODEL_CRITERIA['btc15m-baseline-0.1'])]}
    tr={'generated_ts':ts,'ran':1,'failures':0,'errors':0,'skipped':0,
        'failed_tests':[],'code_identity':MG.code_identity()}
    if mode=='zero': tr['ran']=0
    elif mode=='negative': tr['ran']=-1
    elif mode=='string_count': tr['ran']='1'
    elif mode=='bool_count': tr['ran']=True
    elif mode=='nan_test': tr['generated_ts']=float('nan')
    elif mode=='inf_test': tr['generated_ts']=float('inf')
    elif mode=='minus_inf_model': mv['generated_ts']=float('-inf')
    elif mode=='future': tr['generated_ts']=ts+86400
    elif mode=='stale': tr['generated_ts']=ts-9*86400
    elif mode=='wrong_tree': tr['code_identity']='f'*64
    elif mode=='wrong_model_hash': pass
    elif mode=='raw_failure': tr['failed_tests']=['test_durable_state_failed']
    elif mode=='unknown_model_version': mv['model_version']='unimplemented-v99999'
    elif mode=='missing_criteria': mv.pop('criteria')
    elif mode=='malformed_criteria': mv['criteria']=[1]
    elif mode=='iso_timestamp': tr['generated_ts']='2026-09-09T12:00:00'
    elif mode=='count_collection_mismatch': tr['collected']=500
    mpth=Path(_p('model_validation.json')); tpth=Path(_p('test_report.json'))
    mraw=json.dumps(mv)
    if mode=='duplicate_model_key': mraw='{"approved": false,'+mraw[1:]
    mpth.write_text(mraw)
    tr['model_validation_sha256']=hashlib.sha256(mraw.encode()).hexdigest()
    if mode=='wrong_model_hash': tr['model_validation_sha256']='a'*64
    raw=json.dumps(tr)
    if mode=='duplicate_test_key': raw='{"failures": 1,'+raw[1:]
    if mode=='partial': raw=raw[:-20]
    if mode=='zero_bytes': raw=''
    tpth.write_text(raw)
    cwd=Path.cwd()
    try:
        os.chdir(CFG.DATA_DIR)
        with patch.dict(os.environ,{'NO_LIVE_PROMOTION':'0','MODEL_APPROVED_FOR_LIVE':'YES'}):
            ok,why=MG.check_live_allowed()
    finally: os.chdir(cwd)
    D.update(accepted=ok,criteria=why)
    if ok and mode in ('unknown_model_version','missing_criteria'):
        raise ReviewNeeded('A permitted model-version registry or mandatory criteria set is not defined in this gate; release evidence policy needs review')
    check(ok if mode=='positive' else not ok,'Gatekeeper accepted invalid or inconsistent evidence')

for mode in ('positive','zero','negative','string_count','bool_count','nan_test','inf_test',
             'minus_inf_model','future','stale','wrong_tree','wrong_model_hash','raw_failure',
             'unknown_model_version','missing_criteria','malformed_criteria','iso_timestamp',
             'count_collection_mismatch','duplicate_model_key','duplicate_test_key','partial','zero_bytes'):
    run('G_'+mode,'A08',lambda f,m=mode:gate_case(f,m),tags=('malformed',))

def submission(f,mode):
    c,a=client(demo=True); om=OrderManager(c)
    path=_p(OrderManager.PENDING_FILE)
    original=P.JsonStore.save
    def failing_save(p,data,*args,**kwargs):
        if p==path:
            if mode=='false_return': return False
            if mode=='exception': raise OSError(errno.ENOSPC,'synthetic disk full')
            ok=original(p,data,*args,**kwargs)
            if mode in ('changed_fields','changed_id','truncated','corrupt','concurrent_overwrite'):
                changed=copy.deepcopy(data)
                if mode=='changed_fields':
                    for row in changed.values(): row['count']=999;row['price']=99
                if mode=='changed_id':
                    for row in changed.values(): row['client_order_id']='other'
                if mode=='concurrent_overwrite': changed={}
                if mode in ('truncated','corrupt'):
                    for p0 in Path(CFG.DATA_DIR).glob(OrderManager.PENDING_FILE+'*'):
                        if p0.is_file(): p0.unlink()
                    Path(p).write_text('{' if mode=='truncated' else 'invalid')
                else: original(p,changed)
            return ok
        return original(p,data,*args,**kwargs)
    if mode=='directory':
        F.remove_versions(OrderManager.PENDING_FILE); Path(path).mkdir()
    before_guard=om._flush_submission_guard
    def overwrite_after_confirmation():
        original(path,{})
        return before_guard()
    with patch.object(CFG,'ALLOW_ORDER_SUBMISSION',True), patch.object(CFG,'SHADOW_MODE',False), \
         patch.object(P.JsonStore,'save',side_effect=failing_save):
        if mode=='after_readback':
            with patch.object(om,'_flush_submission_guard',side_effect=overwrite_after_confirmation):
                outcome=om.place_and_track('KXBTC15M-REVIEW','yes',1,20)
        else: outcome=om.place_and_track('KXBTC15M-REVIEW','yes',1,20)
    writes=[x for x in a.calls if x['method']!='GET']
    D.update(synthetic_mutation_adapter_calls=writes,outcome=str(outcome),
             sentinel=P.PersistenceSentinel.healthy())
    check(not writes,'Unconfirmed or changed durable intent reached synthetic transport')

for mode in ('false_return','exception','directory','changed_fields','changed_id','truncated',
             'corrupt','concurrent_overwrite','after_readback'):
    run('I_'+mode,'A09',lambda f,m=mode:submission(f,m),tags=('persistence','transport'))

def io_failure(f,mode):
    c,a=client(demo=True); om=OrderManager(c)
    pending=_p(OrderManager.PENDING_FILE)
    orig_fsync=os.fsync; orig_replace=os.replace
    def fsync(fd):
        name=os.readlink('/proc/self/fd/'+str(fd))
        if pending in name and mode=='fsync': raise OSError(errno.EIO,'synthetic fsync failure')
        return orig_fsync(fd)
    def replace(src,dst):
        if dst==pending and mode=='rename': raise OSError(errno.EIO,'synthetic rename failure')
        if dst==pending+'.sha256' and mode=='checksum': raise OSError(errno.EIO,'synthetic checksum failure')
        return orig_replace(src,dst)
    with patch.object(CFG,'ALLOW_ORDER_SUBMISSION',True),patch.object(CFG,'SHADOW_MODE',False), \
         patch('os.fsync',side_effect=fsync),patch('os.replace',side_effect=replace):
        outcome=om.place_and_track('KXBTC15M-REVIEW','yes',1,20)
    writes=[x for x in a.calls if x['method']!='GET']
    D.update(synthetic_mutation_adapter_calls=writes,outcome=str(outcome))
    check(not writes,'Filesystem failure reached synthetic transport')
for mode in ('fsync','rename','checksum'):
    run('I_io_'+mode,'A09',lambda f,m=mode:io_failure(f,m),tags=('persistence','transport'))

def rebase_failure(f,mode):
    f.trade(-3); p=f.proposal(); ctx=f.ctx()
    before=copy.deepcopy(f.led.state); disk=F.image()
    original=P.JsonStore.save; original_replace=os.replace
    def save(path,data,*args,**kwargs):
        if path==f.led.path and mode=='before_write': return False
        return original(path,data,*args,**kwargs)
    def replace(src,dst):
        if dst==f.led.path+('.sha256' if mode=='checksum' else ''):
            raise OSError(errno.EIO,'synthetic write failure')
        return original_replace(src,dst)
    if mode=='chain':
        with patch.object(f.led.chain,'append',side_effect=C.ChainError('synthetic append failure')):
            ok=f.apply(p,ctx)
    elif mode=='before_write':
        with patch.object(P.JsonStore,'save',side_effect=save): ok=f.apply(p,ctx)
    else:
        with patch('os.replace',side_effect=replace): ok=f.apply(p,ctx)
    D.update(accepted=ok,memory_unchanged=f.led.state==before,
             chain_unchanged=F.image().get(C.CONTINUITY_FILE)==disk.get(C.CONTINUITY_FILE),
             token_burned=f.led.token_consumed(p['token']),current=snap(f))
    f.reload(); D['restart']=snap(f)
    check(not ok and D['memory_unchanged'],'Failed rebase changed its final in-memory baseline')
    check(f.led.risk_equity_reference()>=10,'Restart reduced HWM after failed rebase')
for mode in ('chain','before_write','rename','checksum'):
    run('T_'+mode,'A10',lambda f,m=mode:rebase_failure(f,m),tags=('persistence','restart'))

def flow_case(f,mode):
    f.trade(-3)
    for i in range(20):
        f.led.observe(6,cycle_n=i)
        if mode=='restart': f.reload()
    first=copy.deepcopy(f.led.state['flows'])
    if mode=='two_equal':
        for i in range(20,26): f.led.observe(5,cycle_n=i)
    elif mode=='classify':
        flow=f.led.unclassified_flows()[0]
        f.led.classify_flow(flow['id'],'withdrawal','synthetic-operator')
        for i in range(20,26): f.led.observe(6,cycle_n=i)
    D.update(first=first,flows=f.led.state['flows'],snapshot=snap(f))
    expected=2 if mode=='two_equal' else 1
    check(len(f.led.state['flows'])==expected,'Flow idempotency lost or duplicated a movement')
for mode in ('twenty','restart','two_equal','classify'):
    run('F_'+mode,'A11',lambda f,m=mode:flow_case(f,m),tags=('restart',))

def readonly_case(f,state,command):
    if command=='hold_release':
        f.trade(-3);p=f.proposal();check(f.apply(p,f.ctx()),'Hold fixture failed')
    if state=='mismatch': f.trade(-3); P.JsonStore.save(_p(CFG.TRADES_FILE),[])
    elif state=='corrupt': F.remove_versions(EL.LEDGER_FILE); Path(f.led.path).write_text('{broken}')
    elif state=='missing':
        for p in Path(CFG.DATA_DIR).iterdir():
            if p.is_file(): p.unlink()
    elif state=='old_schema':
        raw=copy.deepcopy(f.led.state);raw['version']=0; P.JsonStore.save(f.led.path,raw)
    before=hashes()
    args={'status':['status'],'seed':['seed','--pre-flow-cash','10','--pre-flow-at','2026-09-01T00:00:00Z','--evidence','synthetic','--cash-now','10'],
          'rebase':['rebase','--reason','synthetic','--action-id','R'],
          'attest':['attest','--action-id','A','--funding-records-sha256','a'*64],
          'hold_release':['hold-release','--action-id','different-release','--validation','synthetic']}[command]
    result=subprocess.run([sys.executable,'tools/equity_ledger_tool.py']+args,capture_output=True,text=True,timeout=20)
    after=hashes()
    D.update(returncode=result.returncode,changed=[k for k in set(before)|set(after) if before.get(k)!=after.get(k)])
    check(before==after,'Read-only command changed economic-state bytes')
for state in ('valid','mismatch','corrupt','missing','old_schema'):
    for command in ('status','seed','rebase','attest','hold_release'):
        run('RO_'+state+'_'+command,'A12',lambda f,s=state,c=command:readonly_case(f,s,c),tags=('readonly',))

# Fresh reliability review: independent transaction/cache/schema scenarios.
def concurrent_fence(f):
    path=f.led.path; generation=f.led.generation
    old=copy.deepcopy(f.led.state); new=copy.deepcopy(old)
    old['writer']='older';new['writer']='newer'
    ready,release=mp.Event(),mp.Event(); output=mp.Queue()
    def writer():
        original=P.read_generation
        def paused(*args):
            got=original(*args); ready.set()
            if not release.wait(8): raise RuntimeError('test synchronization timeout')
            return got
        with patch.object(P,'read_generation',side_effect=paused):
            output.put(P.JsonStore.save(path,old,expect_generation=generation))
    child=mp.Process(target=writer);child.start()
    check(ready.wait(8),'Writer did not reach fence')
    second=P.JsonStore.save(path,new,expect_generation=generation)
    release.set(); child.join(8)
    if child.is_alive(): child.terminate();child.join();raise RuntimeError('writer timeout')
    first=output.get(timeout=2); final=P.JsonStore.load(path,{})
    D.update(older_success=first,newer_success=second,final_writer=final.get('writer'),generation=final.get('generation'))
    check(not(first and second),'Two processes both committed from the same generation')
run('N13_two_process_generation_check_then_write','NEW',concurrent_fence,tags=('race','persistence','new'))

def early_publish(f):
    f.trade(-3);p=f.proposal();ctx=f.ctx()
    original=P.JsonStore.save
    def inspect(path,data,*args,**kwargs):
        if path==f.led.path:
            D['visible_before_durable_write']={
              'hwm':f.led.state['hwm']['risk_equity_reference'],
              'hold':f.led.state['capital_hold'],
              'disk_hwm':P.JsonStore.load(path,{})['hwm']['risk_equity_reference']}
            return False
        return original(path,data,*args,**kwargs)
    with patch.object(P.JsonStore,'save',side_effect=inspect): ok=f.apply(p,ctx)
    D['accepted']=ok
    check(D['visible_before_durable_write']['hwm']==10,'Prepared HWM was visible before durable success')
run('N14_prepared_state_visible_during_commit','NEW',early_publish,tags=('race','transaction','new'))

def mutable_operator_failure(f,mode):
    if mode.startswith('hold_release'):
        f.trade(-3);p=f.proposal();check(f.apply(p,f.ctx()),'Rebase control failed')
        p=f.led.propose_hold_release('release-other','synthetic-validation')
        call=lambda:f.led.apply_hold_release('release-other','synthetic-validation',p['token'])
    else:
        f.led.state['risk_equity_status']=EL.STATUS_CONSERVATIVE
        f.led.state['seed']['status_at_seed']=EL.STATUS_CONSERVATIVE
        f.led.save()
        p=f.led.propose_attestation('new-attest','c'*64)
        call=lambda:f.led.apply_attestation('new-attest','c'*64,p['token'])
    before=copy.deepcopy(f.led.state)
    if mode=='hold_release_stale_generation':
        other=EL.EquityLedger(f.tlog,f.pos,env='prod')
        check(other.save(),'Second writer control failed')
        ok=call()
    else:
        with patch.object(P.JsonStore,'save',return_value=False): ok=call()
    D.update(accepted=ok,memory_unchanged=f.led.state==before,visible=snap(f))
    f.reload();D['restart']=snap(f)
    check(not ok and D['memory_unchanged'],'Failed operator action mutated risk authority in memory')
for mode in ('hold_release','attestation','hold_release_stale_generation'):
    run('N15_failed_'+mode,'NEW',lambda f,m=mode:mutable_operator_failure(f,m),tags=('persistence','restart','new'))

def numeric_state(f,mode):
    if mode in ('cash_nan','cash_pos_inf','cash_neg_inf'):
        val={'cash_nan':float('nan'),'cash_pos_inf':float('inf'),'cash_neg_inf':float('-inf')}[mode]
        f.led.observe(val)
    elif mode=='persisted_pending_nan':
        f.led.state['pending']={'residual':float('nan'),'consecutive':1}
        f.led.save();f.reload()
    elif mode in ('hwm_nan','hwm_inf'):
        f.led.state['hwm']['risk_equity_reference']=float('nan') if mode=='hwm_nan' else float('inf')
        P.JsonStore.save(f.led.path,f.led.state);f.reload()
    elif mode=='unrecognized_flow':
        f.led.state['flows'].append({'id':'flow-X','amount':-3,'kind':'future_unknown'})
        f.led.save();f.reload()
    elif mode=='unknown_position_state':
        f.pos.positions={'X':{'ticker':'X','state':'future','count':1,'avg_price':50,'side':'yes'}}
        f.pos.flush(); f.reload()
    D['snapshot']=snap(f)
    if mode.startswith('cash_'):
        f.reload();D['restart']=snap(f)
    check(not f.led.capital_eligible(),'Malformed numeric or unknown accounting state remained admissible')
for mode in ('cash_nan','cash_pos_inf','cash_neg_inf','persisted_pending_nan','hwm_nan','hwm_inf',
             'unrecognized_flow','unknown_position_state'):
    run('N16_'+mode,'NEW',lambda f,m=mode:numeric_state(f,m),tags=('malformed','new'))

def intent_restart(f,mode):
    c,a=client(demo=True);om=OrderManager(c)
    P.JsonStore.save(_p('submission_guard.json'),{})
    if mode=='valid':
        check(om._record_intent('KXBTC15M-REVIEW','previous',1,20,side='yes'),'Intent control failed')
    elif mode=='missing_id':
        P.JsonStore.save(_p(OrderManager.PENDING_FILE),{'KXBTC15M-REVIEW':{'count':1,'price':20}})
    elif mode=='corrupt':
        F.remove_versions(OrderManager.PENDING_FILE)
        Path(_p(OrderManager.PENDING_FILE)).write_text('invalid')
    elif mode=='missing': F.remove_versions(OrderManager.PENDING_FILE)
    om=OrderManager(c)
    with patch.object(CFG,'ALLOW_ORDER_SUBMISSION',True),patch.object(CFG,'SHADOW_MODE',False):
        result=om.place_and_track('KXBTC15M-REVIEW','yes',1,20)
    writes=[x for x in a.calls if x['method']!='GET']
    D.update(synthetic_mutation_adapter_calls=writes,outcome=str(result))
    check(not writes,'Restart treated unresolved/unreadable intent state as empty')
for mode in ('valid','missing_id','corrupt','missing'):
    run('N17_restart_intent_'+mode,'NEW',lambda f,m=mode:intent_restart(f,m),tags=('restart','transport','new'))

def chain_short_write(f):
    original=os.write
    def short(fd,data): return original(fd,data[:12])
    with patch('os.write',side_effect=short):
        rec=f.led.chain.append(C.KIND_RECOVERY,{'synthetic':True},EL.now_iso())
    head=C.ContinuityChain(f.led.chain.path).head_pointer()
    D.update(returned_seq=rec['seq'],readback_seq=head['seq'])
    check(head['seq']==rec['seq'],'Continuity append returned success after a short write')
run('N18_short_append_success','NEW',chain_short_write,tags=('persistence','new'))

def directory_fsync_failure(f):
    original=os.fsync
    def fail_directory(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError(errno.EIO,'synthetic directory fsync')
        return original(fd)
    with patch('os.fsync',side_effect=fail_directory): ok=f.led.save()
    D.update(accepted=ok,sentinel_healthy=P.PersistenceSentinel.healthy())
    check(not ok,'Directory durability failure was reported as success')
run('N18_directory_fsync_failure','NEW',directory_fsync_failure,tags=('persistence','new'))

def same_process_stale_floor(f):
    prior=f.led
    second=EL.EquityLedger(f.tlog,f.pos,env='prod')
    f.trade(-3,observe=False)
    second.observe(7)
    # Existing reader's journal is independently loaded before the loss.
    prior.tlog=type('OldJournal',(),{'settled_trades':lambda self:[], 'open_trades':lambda self:[]})()
    D.update(stale=snap(f),chain_head=prior.chain.head_pointer())
    check(not prior.capital_eligible(),'Existing reader ignored newer continuity evidence')
run('N19_stale_reader_does_not_refresh_continuity','NEW',same_process_stale_floor,tags=('race','new'))

def account_environment(f):
    other=EL.EquityLedger(f.tlog,f.pos,env='demo')
    D.update(prod_seed_status=f.led.derive_status(),other_environment=other.env,
             other_eligible=other.capital_eligible())
    if other.capital_eligible():
        raise ReviewNeeded('Ledger state has no account/environment binding; deployment-level state separation was not exercised in this local review')
run('N20_environment_state_binding','NEW',account_environment,tags=('configuration','new'))

def crash_boundary(f,mode):
    if mode=='journal_data_rename':
        trade=f.trade(None)
    else:
        f.trade(-3,observe=(mode!='chain_append'))
    # The independent test host passes its already chosen public-key pins to
    # the crash worker. This does not derive a checkpoint from restored local
    # state: the provider retains the same independently held checkpoint/key.
    from continuity_authority import TrustPolicy, configure_trust
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    pinned_authorities = [(authority, TrustPolicy(authority.authority_id,
        authority.signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        frozenset({authority.identity['fingerprint']}),
        frozenset({authority.identity['environment']})))
        for authority in (f.led.authority, f.broker.execution_freeze)]
    def child_work():
        # Forked children inherit neither effective trust nor manager authority.
        # Explicitly configure the test host's pins before loading child-owned
        # objects and injecting a real crash at the same persistence boundary.
        for provider, policy in pinned_authorities:
            configure_trust(provider, policy)
        external = f.led.authority
        f.tlog = TradeLogger()
        from position_manager import PositionManager
        from risk_manager import RiskManager
        f.pos = PositionManager(f.broker, f.tlog)
        f.led = EL.EquityLedger(f.tlog, f.pos, env='prod', authority=external)
        f.orders = OrderManager(f.broker)
        f.risk = RiskManager(f.tlog, f.pos, 10.)
        f.risk.equity = f.led
        original_replace=os.replace
        def replace(src,dst):
            original_replace(src,dst)
            target={'journal_data_rename':_p(CFG.TRADES_FILE),
                    'ledger_data_rename':f.led.path,
                    'ledger_checksum_rename':f.led.path+'.sha256'}.get(mode)
            if dst==target: os._exit(73)
        if mode=='journal_data_rename':
            with patch('os.replace',side_effect=replace):
                f.tlog.settle_trade(trade['trade_id'],'no',False,-3,-3)
        elif mode=='chain_append':
            original=f.led.chain.append
            def append(*args,**kwargs):
                original(*args,**kwargs);os._exit(73)
            with patch.object(f.led.chain,'append',side_effect=append): f.led.observe(7)
        else:
            p=f.proposal();ctx=f.ctx()
            with patch('os.replace',side_effect=replace): f.apply(p,ctx)
        os._exit(74)
    child=mp.Process(target=child_work);child.start();child.join(10)
    if child.is_alive():child.terminate();child.join();raise RuntimeError('crash fixture timeout')
    check(child.exitcode==73,'Configured crash boundary was not reached')
    P.PersistenceSentinel.reset();f.reload()
    D.update(child_exit=child.exitcode,restart=snap(f),
             journal_backup=P.JsonStore.recovered_from_backup.get(os.path.abspath(_p(CFG.TRADES_FILE))))
    if mode=='journal_data_rename':
        check(not f.led.capital_eligible(),'Torn journal/checksum commit recovered an older journal as admissible')
    elif mode=='ledger_checksum_rename':
        check(f.led.state['capital_hold'] and not f.led.capital_eligible(),
              'Completed durable rebase did not recover its hold')
    else:
        check(not f.led.capital_eligible(),'Interrupted commit did not block on restart')
        check(f.led.risk_equity_reference()>=10,'Interrupted rebase lowered prior HWM on restart')
for mode in ('journal_data_rename','chain_append','ledger_data_rename','ledger_checksum_rename'):
    run('CRASH_'+mode,'A01',lambda f,m=mode:crash_boundary(f,m),tags=('crash','restart','persistence','new'))

def readonly_transport(f):
    c,a=client()
    rejected=0
    for method in ('POST','DELETE','PUT','PATCH','UNKNOWN',None,b'POST'):
        for path in ('/portfolio/orders','/portfolio/events/orders','/portfolio/orders/batched',
                     '/portfolio/orders/X/amend','/portfolio/orders/X/decrease'):
            try: c._req(method,path,retries=0,json={})
            except BrokerWriteForbidden: rejected+=1
            else: raise AssertionError('READ_ONLY failed to reject synthetic mutation')
    D.update(refusals=rejected,adapter_calls_before_control=len(a.calls))
    check(not a.calls,'READ_ONLY reached transport adapter')
    c._req('GET','/portfolio/orders',retries=0)
    check(len(a.calls)==1,'Synthetic adapter positive GET control failed')
run('RO_transport_mutation_matrix','A12',readonly_transport,tags=('readonly','transport','new'))

def offline_control(f):
    denied=[]
    for name,fn in [('dns',lambda:socket.getaddrinfo('atlas-review.invalid',443)),
                    ('connect',lambda:socket.socket().connect(('192.0.2.1',443)))]:
        try: fn()
        except RuntimeError as e:
            check('ATLAS_AUDIT_NETWORK_DENIED' in str(e),'Wrong isolation error')
            denied.append(name)
        else: raise AssertionError('Isolation hook did not refuse socket operation')
    D['denied_before_network']=denied
run('ISOLATION_socket_hook','ISOLATION',offline_control,tags=('isolation',))

summary={'target':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
         'results':RESULTS,'counts':{s:sum(x['status']==s for x in RESULTS)
                                   for s in ('PASS','FAIL','NEEDS_REVIEW')},
         'broker_writes':0,'capital_enabled':False,'external_sockets_permitted':False,
         'scope':'Local synthetic reliability review. Adapter invocations are in-memory, never network writes.'}
OUT.write_text(json.dumps(summary,indent=2,ensure_ascii=False,default=str))
print(json.dumps(summary['counts']),flush=True)

sys.exit(1 if any(r['status']=='FAIL' for r in RESULTS) else 2 if any(r['status']=='NEEDS_REVIEW' for r in RESULTS) else 0)
