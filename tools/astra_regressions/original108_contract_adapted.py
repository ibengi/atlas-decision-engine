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
        self.previous_env_data = os.environ.get('DATA_DIR')
        CFG.DATA_DIR = self.tmp.name
        os.environ['DATA_DIR'] = self.tmp.name
        PersistenceSentinel.reset()
        self.broker = Broker()
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        self.tlog = TradeLogger()
        self.pos = PositionManager(self.broker, self.tlog)
        self.orders = OrderManager(self.broker)
        self.pos.flush()
        self.orders.flush()
        self.risk = RiskManager(self.tlog, self.pos, 10)
        self.led = EquityLedger(self.tlog, self.pos, env='prod')
        self.risk.equity = self.led
        if seed:
            p = self.led.propose_seed(10, '2026-09-01T00:00:00Z', 'audit-fixture', 10)
            assert self.led.apply_seed(p, p['sha256'])
            p = self.led.propose_attestation('fixture-attest', 'a'*64)
            assert self.led.apply_attestation('fixture-attest', 'a'*64, p['token'])
    def close(self):
        CFG.DATA_DIR = self.previous_data
        if self.previous_env_data is None: os.environ.pop('DATA_DIR', None)
        else: os.environ['DATA_DIR'] = self.previous_env_data
        self.tmp.cleanup()
        PersistenceSentinel.reset()
    def reload(self):
        self.tlog = TradeLogger()
        self.pos = PositionManager(self.broker, self.tlog)
        self.orders = OrderManager(self.broker)
        self.risk = RiskManager(self.tlog, self.pos, 10)
        self.led = EquityLedger(self.tlog, self.pos, env='prod')
        self.risk.equity = self.led
        return self.led
    def trade(self, pnl=None, observe=True, settled_at=None):
        n = len(self.tlog.trade_rows())
        t = self.tlog.open_trade(ticker='KXBTC15M-AUDIT-'+str(n),market_title='Offline fixture',
            side='yes',req_price=50,avg_price=50,req_count=10,filled_count=10,spread=1,
            fees=0,edge=.1,ev=.1,confidence=8,grade='A',reason='offline',analysis={},
            order_id='audit-'+str(n),order_status='executed')
        if pnl is not None:
            self.tlog.settle_trade(t['trade_id'], 'yes' if pnl>0 else 'no', pnl>0, pnl, pnl)
            if settled_at:
                t['timestamp']=t['settled_at']=settled_at
                self.tlog.flush()
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

def old_journal(f):
    f.trade(-3)
    JsonStore.save(_p(CFG.TRADES_FILE), [])
    f.reload()
    DETAIL.update(f.snapshot())
    check(not f.led.capital_eligible(), 'Restored pre-loss journal became eligible')
    check(f.led.drawdown_pct()>=30-1e-8, 'Previously observed loss disappeared')
run('D1_restore_pre_loss_journal_restart', old_journal)

def old_rebase(f):
    f.trade(-3)
    row={'order_id':'resting-old-repro','status':'resting','remaining_count':1}
    f.orders.open_orders[row['order_id']] = row
    f.orders.flush()
    f.broker.orders = [row]
    f.reload()
    p=f.proposal()
    e=ExecutionEngine.__new__(ExecutionEngine)
    e.client,e.orders,e.posmgr,e.risk,e.equity=f.broker,f.orders,f.pos,f.risk,f.led
    before=copy.deepcopy(f.led.state)
    with patch.dict(os.environ, {'EQUITY_LEDGER_REBASE_REASON':p['reason'],
          'EQUITY_LEDGER_REBASE_ACTION_ID':p['operator_action_id'],
          'EQUITY_LEDGER_REBASE_TOKEN':p['token']}):
        e._apply_equity_operator_actions()
    DETAIL.update(f.snapshot(),broker_open=len(f.broker.orders))
    check(f.led.state==before, 'Production boot accepted a rebase while an order rested at broker')
run('D2_production_boot_rebase_with_live_order', old_rebase)

if not OLD:
    def journal_case(f, kind):
        f.trade(-3)
        f.trade(1)
        valid=copy.deepcopy(f.tlog.trades)
        wm=copy.deepcopy(f.led.state['journal_watermark'])
        rows=copy.deepcopy(valid)
        if kind in ('truncate','delete','empty','invalid_json'):
            if kind=='truncate': JsonStore.save(_p(CFG.TRADES_FILE),rows[:1])
            else:
                remove_versions(CFG.TRADES_FILE)
                if kind=='empty': Path(_p(CFG.TRADES_FILE)).write_text('')
                if kind=='invalid_json': Path(_p(CFG.TRADES_FILE)).write_text('{"half":')
        elif kind=='same_count_old':
            rows[1]['trade_id']='other-history'; JsonStore.save(_p(CFG.TRADES_FILE),rows)
        elif kind=='pnl':
            rows[0]['net_pnl']=0; JsonStore.save(_p(CFG.TRADES_FILE),rows)
        elif kind=='reorder': JsonStore.save(_p(CFG.TRADES_FILE),list(reversed(rows)))
        elif kind=='same_aggregate':
            rows[0]['net_pnl']=-2; rows[1]['net_pnl']=0
            JsonStore.save(_p(CFG.TRADES_FILE),rows)
        elif kind=='digest':
            f.led.state['journal_watermark']['digest']='broken'
            JsonStore.save(f.led.path,f.led.state)
        elif kind in ('watermark_delete','watermark_zero'):
            if kind=='watermark_delete': f.led.state.pop('journal_watermark')
            else: f.led.state['journal_watermark']['settled_count']=0
            JsonStore.save(f.led.path,f.led.state)
            JsonStore.save(_p(CFG.TRADES_FILE),[])
        f.reload()
        if kind=='repeated_restart':
            JsonStore.save(_p(CFG.TRADES_FILE),[])
            for _ in range(5): f.reload()
        if kind=='return_valid':
            JsonStore.save(_p(CFG.TRADES_FILE),[]); f.reload()
            check(not f.led.capital_eligible(),'Mismatch not blocked')
            JsonStore.save(_p(CFG.TRADES_FILE),valid); f.reload()
            DETAIL.update(f.snapshot())
            check(f.led.capital_eligible() and not f.led.state['journal_mismatch'],'Valid evidence not restored')
            return
        DETAIL.update(f.snapshot(),watermark=f.led.state.get('journal_watermark'))
        check(not f.led.capital_eligible(),'Unproven journal history accepted')
        check(f.led.risk_equity_reference()>=10,'HWM lowered')
        check(f.led.strategy_equity_conservative()<=8+1e-8,'Loss bound improved')
    for kind in ('truncate','delete','empty','invalid_json','same_count_old','pnl','reorder',
                 'same_aggregate','digest','watermark_delete','watermark_zero','repeated_restart','return_valid'):
        run('J_'+kind,lambda f,k=kind:journal_case(f,k))

    def duplicate_win(f):
        win=f.trade(3); f.trade(-4)
        before=f.led.drawdown_pct()
        f.tlog.trades.append(copy.deepcopy(win)); f.tlog.flush(); f.reload()
        DETAIL.update(f.snapshot(),drawdown_before=before)
        check(not f.led.capital_eligible() or f.led.drawdown_pct()>=before,
              'Replayed profitable trade repaired drawdown without new economic evidence')
    run('J_appended_duplicate_profitable_trade',duplicate_win)

    def restored_pair(f):
        pre=image(); f.trade(-3); restore(pre); f.reload()
        DETAIL.update(f.snapshot())
        check(not f.led.capital_eligible(),'Coherent old restore silently erased observed loss')
    run('J_restore_both_journal_and_ledger',restored_pair)

    def restore_and_funding(f):
        pre=image()
        f.trade(-3,settled_at='2026-09-01T00:00:00Z')
        restore(pre); f.reload()
        # Actual economic scenario: a settled -3 loss then a +3 deposit;
        # broker cash is 10 again, with no open orders or positions.
        f.led.observe(10)
        e=ExecutionEngine.__new__(ExecutionEngine)
        e.client,e.orders,e.posmgr,e.risk,e.equity=f.broker,f.orders,f.pos,f.risk,f.led
        verdict=e._evaluate_global_guards()
        DETAIL.update(snapshot=f.snapshot(),global_guard=verdict,
                      hidden_settled_loss=-3,external_deposit=3)
        check(not verdict[0],'Restored history plus deposit passed actual global risk gates with erased loss')
    run('J_restored_pair_plus_deposit_passes_global_guards',restore_and_funding)

    def stale_writer(f):
        stale=EquityLedger(TradeLogger(),f.pos,env='prod')
        f.trade(-3)
        stale.save()
        JsonStore.save(_p(CFG.TRADES_FILE),[]); f.reload()
        DETAIL.update(f.snapshot())
        check(not f.led.capital_eligible(),'A stale second ledger writer rewound watermark and erased loss')
    run('CRASH_stale_second_writer_overwrites_watermark',stale_writer)

    def journal_ahead(f):
        old=image(); f.trade(-3,observe=False)
        f.reload()
        DETAIL.update(f.snapshot())
        check(abs(f.led.strategy_equity()-7)<1e-8,'New journal ignored on old-ledger recovery')
        f.led.observe(7)
        check(f.led.state['journal_watermark']['settled_count']==1,'Recovered watermark not advanced')
    run('CRASH_journal_ahead_ledger_valid_suffix',journal_ahead)

    def journal_window_restore(f):
        f.trade(-3,observe=False)
        JsonStore.save(_p(CFG.TRADES_FILE),[]); f.reload()
        DETAIL.update(f.snapshot())
        check(not f.led.capital_eligible(),'Journal commit before watermark permits loss erasure after restore')
    run('CRASH_after_journal_before_watermark_then_restore',journal_window_restore)

    def checksum_window(f):
        real_replace=os.replace
        def crash(src,dst):
            if str(dst)==f.led.path+'.sha256': raise OSError('simulated process failure before checksum replace')
            return real_replace(src,dst)
        f.trade(-3,observe=False)
        with patch('os.replace',side_effect=crash): f.led.observe(7)
        check(not PersistenceSentinel.healthy(),'Failure injection did not reach critical write')
        JsonStore.save(_p(CFG.TRADES_FILE),[])
        PersistenceSentinel.reset(); f.reload()
        DETAIL.update(f.snapshot())
        check(not f.led.capital_eligible(),'Checksum crash recovered old watermark and erased loss')
    run('CRASH_ledger_replace_before_checksum_replace',checksum_window)

    def watermark_ahead(f):
        f.trade(-3); JsonStore.save(_p(CFG.TRADES_FILE),[]); f.reload()
        DETAIL.update(f.snapshot())
        check(not f.led.capital_eligible(),'Ledger ahead of durable journal became eligible')
    run('CRASH_watermark_ahead_journal',watermark_ahead)

    for malformed in ('invalid_json','empty','future_schema','old_schema','missing'):
        def ledger_invalid(f,k=malformed):
            f.trade(-3); remove_versions(EL.LEDGER_FILE)
            if k=='invalid_json': Path(f.led.path).write_text('{')
            elif k=='empty': Path(f.led.path).write_text('')
            elif k in ('future_schema','old_schema'):
                JsonStore.save(f.led.path, {'version':999 if k=='future_schema' else 0})
            f.reload(); DETAIL.update(f.snapshot())
            check(not f.led.capital_eligible(),'Missing/corrupt ledger became eligible')
        run('J_ledger_'+malformed,ledger_invalid)

    def flows(f, direction):
        f.trade(-3); before=f.led.drawdown_pct()
        for i in range(4): f.led.observe(10 if direction=='deposit' else 6,cycle_n=i)
        DETAIL.update(f.snapshot(),drawdown_before=before)
        if direction=='withdrawal':
            check(not f.led.capital_eligible(),'Unknown withdrawal not held')
            for flow in list(f.led.unclassified_flows()):
                f.led.classify_flow(flow['id'],'withdrawal',action_id='offline-classification')
            DETAIL['after_classification']=f.snapshot()
            f.led.observe(6)
            DETAIL['after_fresh_observation']=f.snapshot()
        check(abs(f.led.drawdown_pct()-before)<1e-8,'External funding changed historical drawdown')
    run('J_deposit_after_loss',lambda f:flows(f,'deposit'))
    run('J_withdrawal_after_loss',lambda f:flows(f,'withdrawal'))
    for pnl_path in ((-1,-2,-3),(-3,1),(-3,3),(3,-3)):
        def history(f,path=pnl_path):
            eq=peak=10
            for pnl in path:
                f.trade(pnl); eq+=pnl; peak=max(peak,eq)
            f.reload(); DETAIL.update(f.snapshot(),path=path)
            check(abs(f.led.risk_equity_reference()-peak)<1e-8,'Historical HWM incorrect')
            check(abs(f.led.drawdown_pct()-100*(peak-eq)/peak)<1e-8,'Historical drawdown incorrect')
        run('J_history_'+str(pnl_path),history)

    def rebase_variant(f,k):
        f.trade(-3)
        row={'order_id':'O','status':'resting','remaining_count':1}
        if k in ('both_open','local_open','pending_cancel','partial_fill','local_open_broker_cancelled'):
            f.orders.open_orders['O']=copy.deepcopy(row)
        if k in ('both_open','broker_only','pending_cancel','partial_fill','local_cancelled_broker_open'):
            f.broker.orders=[copy.deepcopy(row)]
        if k=='local_open_broker_cancelled': f.broker.orders=[dict(row,status='canceled',remaining_count=0)]
        if k=='pending_cancel': f.orders.open_orders['O']['status']='cancel_pending'
        if k=='partial_fill':
            f.orders.open_orders['O']['filled_count']=1
            f.broker.orders[0]['fill_count']=1
        if k=='intent': f.orders._record_intent('KXBTC15M-AUDIT','cid',1,50)
        if k=='resolution_halt': f.orders.resolution_halt={'status':'UNKNOWN'}
        if k.startswith('http_'): f.broker.orders_error=KalshiAPIError(int(k[5:]),'offline error')
        if k=='timeout': f.broker.orders_error=requests.Timeout('offline timeout')
        if k=='exception': f.broker.orders_error=RuntimeError('offline broker failure')
        if k=='malformed': f.broker.orders=[7]
        if k=='empty_row': f.broker.orders=[{}]
        if k=='unknown_status': f.broker.orders=[dict(row,status='future_status')]
        if k=='none_listing': f.broker.orders=None
        if k=='broker_position': f.broker.positions=[{'ticker':'KXBTC15M-X','position':1}]
        if k in ('local_position','matched_position','fully_filled_unresolved'):
            t=f.trade(None); f.pos.open_position(t)
            if k=='matched_position': f.broker.positions=[{'ticker':t['ticker'],'position':10}]
        if k=='position_timeout': f.broker.positions_error=requests.Timeout('offline')
        if k=='unknown_position': f.broker.positions=[{'ticker':'KXBTC15M-X','position':'invalid'}]
        f.orders.flush()
        ctx=f.ctx()
        if k=='missing_order_block': ctx.pop('orders')
        if k=='missing_position_block': ctx.pop('open_positions')
        if k=='missing_reconcile': ctx.pop('reconcile_status')
        if k=='unknown_reconcile': ctx['reconcile_status']='UNKNOWN'
        if k=='mismatch_reconcile': ctx['reconcile_status']='MISMATCH'
        if k=='incomplete_order_block': ctx['orders']={'broker_open':0}
        if k=='stale_ctx':
            ctx['observed_at']='2000-01-01T00:00:00Z'
            f.broker.orders=[row]
        refusal(f,ctx)
    for k in ('both_open','local_open','broker_only','pending_cancel','partial_fill',
              'local_cancelled_broker_open','local_open_broker_cancelled','fully_filled_unresolved',
              'intent','resolution_halt','http_401','http_429','http_500','timeout','exception',
              'malformed','empty_row','unknown_status','none_listing','broker_position',
              'local_position','matched_position','position_timeout','unknown_position',
              'missing_order_block','missing_position_block','missing_reconcile','unknown_reconcile',
              'mismatch_reconcile','incomplete_order_block','stale_ctx'):
        run('R_'+k,lambda f,k=k:rebase_variant(f,k))

    class Adapter(BaseAdapter):
        def __init__(self, positions): self.positions=positions; self.calls=[]
        def send(self,request,**kw):
            self.calls.append({'method':request.method,'path':request.path_url})
            response=requests.Response(); response.status_code=200
            response._content=json.dumps(self.positions if '/positions' in request.path_url
                                         else {'orders':[]}).encode()
            response.headers['Content-Type']='application/json'; response.request=request
            return response
        def close(self): pass

    def real_client(payload):
        c=KalshiClient(env='prod',cache_enabled=False)
        c.base_url='https://atlas-audit.invalid/trade-api/v2'
        c.key_id='audit-only'; c._pk=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        c.session.trust_env=False
        a=Adapter(payload); c.session.mount('https://',a)
        return c,a

    def payload_test(f,payload):
        f.trade(-3)
        c,a=real_client(payload)
        f.broker=c; f.pos.client=c; f.orders.client=c
        ctx=f.ctx(); DETAIL['http']=a.calls
        refusal(f,ctx)
    for name,payload in (
        ('missing_envelope',{}),('null_positions',{'market_positions':None}),
        ('renamed_envelope',{'positions_v3':[{'ticker':'X','position':1}]}),
        ('unfollowed_cursor',{'market_positions':[],'cursor':'there-are-more'}),
        ('conflicting_envelopes',{'market_positions':[],'positions':[{'ticker':'X','position':1}]})):
        run('HTTP_positions_'+name,lambda f,p=payload:payload_test(f,p))

    def paginated_live_exposure(f):
        f.trade(-3)
        c,a=real_client({'market_positions':[{'ticker':'closed-'+str(i),'position':0}
                         for i in range(100)],'event_positions':[],'cursor':'open-position-next-page'})
        first_send=a.send
        def send(req,**kw):
            if 'cursor=' in req.path_url:
                a.positions={'market_positions':[{'ticker':'LIVE','position':1}],
                             'event_positions':[],'cursor':''}
            return first_send(req,**kw)
        a.send=send
        f.broker=c; f.pos.client=c; f.orders.client=c
        ctx=f.ctx(); DETAIL.update(http=a.calls,unread_second_page_position={'ticker':'LIVE','position':1})
        refusal(f,ctx)
    run('HTTP_real_pagination_100_flat_rows_hides_open_position_page_two',paginated_live_exposure)

    def race_broker(f):
        f.trade(-3)
        f.broker.on_positions=lambda:f.broker.orders.append(
            {'order_id':'raced','status':'resting','remaining_count':1})
        ctx=f.ctx(); p=f.proposal()
        before=copy.deepcopy(f.led.state); accepted=f.apply(p,ctx)
        DETAIL.update(accepted=accepted,ctx=ctx,broker_orders=f.broker.orders,snapshot=f.snapshot())
        check(not accepted and f.led.state==before,'Order appeared between broker queries; rebase committed')
    run('RACE_order_between_broker_order_and_position_queries',race_broker)

    def race_process(f):
        f.trade(-3); ctx=f.ctx(); p=f.proposal()
        child="""import json,sys
from config import CFG
from persistence import JsonStore
CFG.DATA_DIR=sys.argv[1]
JsonStore.save(CFG.DATA_DIR+'/orders_state.json',{'new-order':{'status':'resting','remaining_count':1}})
"""
        subprocess.run([sys.executable,'-c',child,CFG.DATA_DIR],check=True,capture_output=True,text=True)
        same=(f.proposal()['token']==p['token'])
        ok=f.apply(p,ctx)
        DETAIL.update(accepted=ok,token_still_matches=same,
                      disk_orders=JsonStore.load(_p(CFG.ORDERS_FILE),{}),snapshot=f.snapshot())
        check(not ok,'Second process changed persisted orders after validation; rebase committed')
    run('RACE_second_process_adds_order_after_validation',race_process)

    def race_settlement(f):
        f.trade(-3); ctx=f.ctx(); p=f.proposal()
        original=f.led.rebase_preconditions
        def interleave(ctx):
            failures=original(ctx)
            f.trade(-2,observe=False)
            return failures
        with patch.object(f.led,'rebase_preconditions',side_effect=interleave): ok=f.apply(p,ctx)
        DETAIL.update(accepted=ok,snapshot=f.snapshot(),proposal_new=p['new_baseline'])
        check(not ok,'Settlement changed evidence after validation; stale rebase committed')
    run('RACE_settlement_between_validation_and_commit',race_settlement)

    def fill_race(f):
        f.trade(-3)
        f.broker.on_positions=lambda:f.broker.positions.append({'ticker':'new-fill','position':1})
        refusal(f,f.ctx())
    run('RACE_fill_between_queries_visible_in_positions',fill_race)

    for missing in (CFG.ORDERS_FILE,CFG.POSITIONS_FILE,OrderManager.PENDING_FILE):
        def missing_local(f,name=missing):
            f.trade(-3); remove_versions(name); f.reload(); refusal(f,f.ctx())
        run('R_missing_local_file_'+missing,missing_local)

    def malformed_intent(f):
        f.trade(-3)
        JsonStore.save(_p(OrderManager.PENDING_FILE),{'pending':{'count':1,'price':50}})
        f.reload(); refusal(f,f.ctx())
    run('R_malformed_pending_intent_disappears_on_restart',malformed_intent)

    def unknown_local_position(f):
        f.trade(-3)
        f.pos.positions={'X':{'ticker':'X','state':'future_state','count':1,'avg_price':50,'side':'yes'}}
        f.pos.flush(); f.reload(); refusal(f,f.ctx())
    run('R_unknown_local_position_state',unknown_local_position)

    def normal_replay(f, restart=False):
        f.trade(-3); ctx=f.ctx(); p=f.proposal()
        check(f.apply(p,ctx),'Safe fixture rebase failed')
        check(not f.led.capital_eligible(),'Rebase cleared post-rebase hold')
        if restart: f.reload()
        before=copy.deepcopy(f.led.state); again=f.apply(p,ctx)
        DETAIL.update(accepted_second=again,snapshot=f.snapshot())
        check(not again and before==f.led.state,'Token replay mutated ledger')
    run('TOKEN_safe_rebase_hold_and_immediate_replay',normal_replay)
    run('TOKEN_replay_after_restart',lambda f:normal_replay(f,True))

    def restored_token(f):
        f.trade(-3); ctx=f.ctx(); p=f.proposal(); pre=image()
        check(f.apply(p,ctx),'First apply failed')
        restore(pre); f.reload(); again=f.apply(p,ctx)
        DETAIL.update(replay_accepted=again,snapshot=f.snapshot())
        check(not again,'A consumed authorization token was replayed after restore')
    run('TOKEN_replay_after_snapshot_restore',restored_token)

    def failed_save(f):
        f.trade(-3); ctx=f.ctx(); p=f.proposal()
        bad=Path(CFG.DATA_DIR)/'blocked'/EL.LEDGER_FILE; bad.mkdir(parents=True)
        f.led.path=str(bad)
        before=copy.deepcopy(f.led.state); ok=f.apply(p,ctx)
        DETAIL.update(return_value=ok,memory_unchanged=(before==f.led.state),snapshot=f.snapshot())
        check(not ok,'Failure injection did not cause refusal')
        check(f.led.state==before,'Failed save consumed token and mutated HWM/hold in memory')
    run('TOKEN_failed_actual_filesystem_save_mutates_risk',failed_save)

    def pending(f):
        f.led.observe(9.9)
        e=ExecutionEngine.__new__(ExecutionEngine)
        e.client,e.posmgr,e.orders,e.risk,e.equity=f.broker,f.pos,f.orders,f.risk,f.led
        verdict=e._evaluate_global_guards()
        DETAIL.update(f.snapshot(),global_guard_verdict=verdict)
        check(not f.led.capital_eligible() and not verdict[0],
              'Unidentified cash movement under observation still passes CAPITAL accounting/global guards')
    run('GATE_pending_negative_residual_must_block',pending)

    def attestation_mismatch(f):
        f.trade(-3); JsonStore.save(_p(CFG.TRADES_FILE),[]); f.reload()
        p=f.led.propose_attestation('retry','b'*64); before=copy.deepcopy(f.led.state)
        ok=f.led.apply_attestation('retry','b'*64,p['token'])
        DETAIL.update(accepted=ok,snapshot=f.snapshot())
        check(not ok and f.led.state==before,'Attestation bypassed evidence mismatch')
    run('TOKEN_attestation_cannot_bypass_detected_mismatch',attestation_mismatch)

    def rebase_mismatch(f):
        f.trade(-3); JsonStore.save(_p(CFG.TRADES_FILE),[]); f.reload(); refusal(f,f.ctx())
    run('TOKEN_rebase_cannot_bypass_detected_mismatch',rebase_mismatch)

    def migration(f,kind):
        p=f.led.propose_seed(.04,'2026-09-01T00:00:00Z','offline-evidence',9.84)
        before=image()
        if kind=='dry_run':
            DETAIL.update(proposal=p)
            check(image()==before and not f.led.seeded,'Seed proposal wrote state'); return
        if kind=='wrong_hash':
            ok=f.led.apply_seed(p,'wrong')
            DETAIL.update(accepted=ok)
            check(not ok and image()==before,'Wrong hash changed state'); return
        if kind=='settlement_after_proposal':
            f.trade(-3,observe=False)
            ok=f.led.apply_seed(p,p['sha256'])
            DETAIL.update(accepted=ok,snapshot=f.snapshot())
            check(not ok,'Seed committed after unbound new settlement'); return
        if kind=='modified_proposal':
            p['strategy_equity_0']=1000000
            ok=f.led.apply_seed(p,p['sha256'])
            DETAIL.update(accepted=ok,snapshot=f.snapshot())
            check(not ok,'Modified proposal accepted without recomputing hash'); return
        check(f.led.apply_seed(p,p['sha256']),'Seed control failed')
        if kind=='repeat':
            before=image(); ok=f.led.apply_seed(p,p['sha256'])
            check(not ok and image()==before,'Repeated migration changed state')
        if kind=='restart': f.reload()
        DETAIL.update(f.snapshot())
        check(not f.led.capital_eligible(),'Reconstructed migration silently certified historical provenance')
    for kind in ('dry_run','wrong_hash','repeat','restart','settlement_after_proposal','modified_proposal'):
        run('M_'+kind,lambda f,k=kind:migration(f,k),seed=False)

    def dryrun_mismatch(f):
        f.trade(-3); JsonStore.save(_p(CFG.TRADES_FILE),[])
        before=image()
        command=[sys.executable,'tools/equity_ledger_tool.py','status']
        p=subprocess.run(command,capture_output=True,text=True,timeout=20)
        after=image()
        DETAIL.update(returncode=p.returncode,changed_files=sorted(k for k in set(before)|set(after)
                      if before.get(k)!=after.get(k)))
        check(p.returncode==0,'Dry-run did not complete')
        check(before==after,'Read-only operator utility wrote ledger/backup files')
    run('M_dryrun_tool_on_journal_mismatch',dryrun_mismatch)

    def transport(f):
        c,a=real_client({'market_positions':[]})
        attempted=[]
        operations=[('submit',lambda:c.create_order('KXBTC15M-AUDIT','yes',1,20)),
                    ('cancel',lambda:c.cancel_order('offline'))]
        for method in ('POST','DELETE','PUT','PATCH',' post ',b'POST','UNKNOWN',None):
            for path in ('/portfolio/orders','/portfolio/orders/batched','/portfolio/orders/x/amend'):
                operations.append((repr(method)+' '+path,lambda m=method,p=path:c._req(m,p,retries=0,json={})))
        for name,fn in operations:
            try: fn()
            except BrokerWriteForbidden: attempted.append(name)
            else: raise AssertionError('READ_ONLY failed to refuse '+name)
        check(not a.calls,'READ_ONLY reached the HTTP send adapter')
        c._req('GET','/portfolio/orders',retries=0)
        check(len(a.calls)==1 and a.calls[0]['method']=='GET','Positive control did not reach real HTTP preparation')
        c.env='demo'
        c._req('POST','/portfolio/events/orders',retries=0,json={'offline_control':True})
        check(len(a.calls)==2 and a.calls[1]['method']=='POST','Simulated DEMO write control did not reach adapter')
        DETAIL.update(refusals=len(attempted),readonly_adapter_calls_before_controls=0,
                      positive_control_adapter_calls=a.calls,real_socket_writes=0)
    run('WRITE_real_signing_request_preparation_readonly_matrix',transport)

    def lost_intent_write(f):
        c,a=real_client({'market_positions':[]})
        c.env='demo'  # Synthetic adapter only; no production mode is enabled.
        om=OrderManager(c)
        target=Path(_p(OrderManager.PENDING_FILE))
        remove_versions(OrderManager.PENDING_FILE)
        target.mkdir()
        with patch.object(CFG,'ALLOW_ORDER_SUBMISSION',True), patch.object(CFG,'SHADOW_MODE',False):
            outcome=om.place_and_track('KXBTC15M-AUDIT','yes',1,20)
        posts=[x for x in a.calls if x['method']=='POST']
        DETAIL.update(simulated_transport_posts=posts,persistence_healthy=PersistenceSentinel.healthy(),
                      pending_intent_is_directory=target.is_dir(),outcome=str(outcome))
        check(not posts,'OrderManager submitted to simulated broker despite failed pending-intent persistence')
    run('PERSIST_failed_intent_write_reaches_simulated_order_transport',lost_intent_write)

    def repeated_withdrawal(f):
        f.trade(-3)
        for i in range(9): f.led.observe(6,cycle_n=i)
        DETAIL.update(f.snapshot(),flows=copy.deepcopy(f.led.state['flows']))
        amounts=[x['amount'] for x in f.led.unclassified_flows()]
        check(len(amounts)==1 and abs(sum(amounts)+1)<1e-8,
              'One unresolved withdrawal was recorded repeatedly as new economic events')
    run('FLOW_same_unclassified_withdrawal_repeated_nine_cycles',repeated_withdrawal)

    def historic_ledger(f):
        for value in (-.3,-.1839):
            row=f.trade(value,observe=False)
            row['net_pnl']=value
            row['settled_at']='2026-09-01T00:00:00Z'
        f.tlog.flush()

    def drawdown_9236(f):
        historic_ledger(f)
        p=f.led.propose_seed(.04,'2026-09-07T18:01:19Z','documented example; not broker proof',.04)
        check(f.led.apply_seed(p,p['sha256']),'Migration example failed')
        before=f.snapshot()
        for i in range(4): f.led.observe(9.84,cycle_n=i)
        DETAIL.update(before=before,after=f.snapshot(),expected=100*.4839/.5239)
        check(abs(f.led.drawdown_pct()-100*.4839/.5239)<1e-8,'92.36% algebra not reproduced')
        check(abs(f.led.strategy_equity()-.04)<1e-8,'Deposit inflated strategy equity')
        check(not f.led.capital_eligible(),'Migration claimed proven historical baseline')
    run('M_92_36_percent_drawdown_deposit_regression',drawdown_9236,seed=False)

    def rollback_gate(f,mode):
        # Historical fixture established before the ledger observes it, so
        # today's daily stop cannot mask the distinct drawdown decision.
        f.trade(-3,settled_at='2026-09-01T00:00:00Z')
        for i in range(4): f.led.observe(100,cycle_n=i)
        f.risk.capital=100
        e=ExecutionEngine.__new__(ExecutionEngine)
        e.client,e.orders,e.posmgr,e.risk,e.equity=f.broker,f.orders,f.pos,f.risk,f.led
        with patch.object(CFG,'RISK_EQUITY_MODE',mode):
            verdict=e._evaluate_global_guards()
            DETAIL.update(mode=mode,reported_drawdown=f.risk.rolling_drawdown_pct(),
                          strategy_drawdown=f.led.drawdown_pct(),global_guard=verdict)
        check(not verdict[0],'Cash/unknown accounting mode bypassed loss-derived CAPITAL guard')
    run('GATE_cash_rollback_after_deposit',lambda f:rollback_gate(f,'cash'))
    run('GATE_unknown_risk_accounting_mode',lambda f:rollback_gate(f,'stratgey'))

    def boot_migration(f,change):
        p=f.led.propose_seed(10,'2026-09-01T00:00:00Z','offline-evidence',10)
        env={'EQUITY_LEDGER_SEED_PRE_FLOW_CASH':'10',
             'EQUITY_LEDGER_SEED_PRE_FLOW_AT':'2026-09-01T00:00:00Z',
             'EQUITY_LEDGER_SEED_EVIDENCE':'offline-evidence','EQUITY_LEDGER_SEED_SHA256':p['sha256']}
        cash=10
        if change=='deposit': cash=11
        if change=='withdrawal': cash=9
        if change=='settlement': f.trade(-3,observe=False)
        if change=='settlement_race':
            orig=f.led.propose_seed
            def race(*a,**kw):
                proposal=orig(*a,**kw); f.trade(-3,observe=False); return proposal
            with patch.object(f.led,'propose_seed',side_effect=race):
                done=f.led.apply_operator_actions(env,cash,{})
        else: done=f.led.apply_operator_actions(env,cash,{})
        DETAIL.update(done=done,snapshot=f.snapshot())
        check(done.get('seed') is False and not f.led.seeded,
              'Boot migration accepted evidence changed since authorized proposal')
    for change in ('deposit','withdrawal','settlement','settlement_race'):
        run('M_boot_'+change,lambda f,c=change:boot_migration(f,c),seed=False)

    def finite_gate(f,kind):
        import time
        import model_gatekeeper as g
        tr={'generated_ts':time.time(),'failures':0,'errors':0,'ran':1}
        mv={'approved':True,'generated_ts':time.time()}
        if kind=='zero_tests': tr['ran']=0
        if kind=='nan_test_timestamp': tr['generated_ts']=float('nan')
        if kind=='nan_model_timestamp': mv['generated_ts']=float('nan')
        if kind=='future_test_timestamp': tr['generated_ts']=time.time()+864000
        Path(_p('test_report.json')).write_text(json.dumps(tr))
        Path(_p('model_validation.json')).write_text(json.dumps(mv))
        cwd=os.getcwd()
        try:
            os.chdir(CFG.DATA_DIR)
            with patch.dict(os.environ,{'NO_LIVE_PROMOTION':'0','MODEL_APPROVED_FOR_LIVE':'YES'}):
                ok,failed=g.check_live_allowed()
        finally: os.chdir(cwd)
        DETAIL.update(gate_returned=ok,failed_criteria=failed,kind=kind,
                      scope='Synthetic report files only; access mode stays READ_ONLY')
        check(not ok,'Gatekeeper accepted missing/invalid validation evidence')
    for kind in ('zero_tests','nan_test_timestamp','nan_model_timestamp','future_test_timestamp'):
        run('GATEKEEPER_'+kind,lambda f,k=kind:finite_gate(f,k))

summary={'candidate':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
         'results':RESULTS,'counts':{s:sum(r['status']==s for r in RESULTS) for s in ('PASS','FAIL','ERROR')},
         'capital_enabled_in_real_environment':False,'broker_writes_during_audit':0,
         'scope':'Offline assertions on real production classes; broker responses explicitly simulated'}
OUT.write_text(json.dumps(summary,indent=2,ensure_ascii=False,default=str))
print(json.dumps(summary['counts']))
sys.exit(2 if summary['counts']['ERROR'] else 1 if summary['counts']['FAIL'] else 0)
