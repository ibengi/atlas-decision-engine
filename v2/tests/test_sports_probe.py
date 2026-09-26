"""Synthetic software tests; none are admissible market evidence."""
import asyncio
import base64
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from atlas_v2 import sports_probe as p
from atlas_v2.domain import Refused, utc
from atlas_v2.store import Store

AT='2026-09-26T14:00:00.100000Z'
TS=1790431200000
MEMBERS={'KXTEST-A':{'event_ticker':'KXTEST','status':'active','close_time':'2026-09-26T15:00:00Z'}}
CLOCK={'synchronized':True,'uncertainty_ms':'1','source_uncertainty_ms':'1'}

def ticker(**changes):
    return dict({'market_ticker':'KXTEST-A','market_id':'native-id','ts_ms':TS,
        'yes_bid_dollars':'0.40','yes_ask_dollars':'0.50','yes_bid_size_fp':'3','yes_ask_size_fp':'4'},**changes)

def credentials():
    key=ed25519.Ed25519PrivateKey.generate()
    pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    env={p.KEY_NAME:'dedicated-key-id',p.PRIVATE_NAME:pem}
    return p.Credentials(env),env


class SportsProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.store=Store(self.root/'test.sqlite');self.creds,self.env=credentials()
        self.ev=p.Evidence(self.store,self.creds.secrets,'test-run')
    def tearDown(self): self.store.close();self.tmp.cleanup()
    def session(self):
        s=p.Session(MEMBERS,self.ev,0)
        for i,ch in ((1,'ticker'),(2,'orderbook_delta')):
            s.process(json.dumps({'type':'subscribed','id':i,'msg':{'channel':ch,'sid':i}}),AT,CLOCK)
        return s
    def message(self,s,kind,msg,seq=None):
        value={'type':kind,'sid':1 if kind=='ticker' else 2,'msg':msg}
        if seq is not None:value['seq']=seq
        s.process(json.dumps(value),AT,CLOCK)
    def test_fixed_bounds_and_no_redirect(self):
        self.assertEqual((p.SYNC_MS,p.AGE_MS,p.CLOCK_MS),(250,1000,25))
        self.assertEqual(p.CONNECTIONS,2);self.assertLessEqual(p.MAX_SECONDS,180)
        exc=RuntimeError('redirect')
        self.assertIs(p.NoRedirectConnect(p.WS).process_redirect(exc),exc)
    def test_scope_requires_exact_matching_read_only_record(self):
        body={'api_keys':[{'api_key_id':self.creds.key_id,'scopes':['read']}]}
        self.assertEqual(p.verify_scope(body,self.creds.key_id)['scopes'],['read'])
        for scopes in ([],['read','write'],['write::trade'],['read','write::transfer'],None):
            body['api_keys'][0]['scopes']=scopes
            with self.assertRaises(Refused):p.verify_scope(body,self.creds.key_id)
        with self.assertRaises(Refused):p.verify_scope({'api_keys':[]},self.creds.key_id)
        with self.assertRaises(Refused):p.verify_scope({'api_keys':[{'api_key_id':'other','scopes':['read']}]},self.creds.key_id)
    def test_signature_binding_and_mutation_paths_refused(self):
        with patch.object(p.time,'time_ns',return_value=1234000000):h=self.creds.headers('/trade-api/ws/v2')
        self.creds.key.public_key().verify(base64.b64decode(h['KALSHI-ACCESS-SIGNATURE']),b'1234GET/trade-api/ws/v2')
        for path in ('/trade-api/v2/portfolio/orders','/trade-api/v2/transfers','/trade-api/v2/api_keys?x=1'):
            with self.assertRaises(Refused):self.creds.headers(path)
        reader=p.Reader(self.ev,time.monotonic()+2)
        for path in ('/portfolio/orders','/portfolio/balance','/transfers','https://evil.test'):
            with self.assertRaises(Refused):reader.get(path)
    def test_financial_guard(self):
        for name,value in [('CAPITAL','ON'),('BROKER_WRITES','1'),('REAL_ORDERS_SUBMITTED','1'),('LIVE_TRADING','true'),('PROD_ACCESS_MODE','LIVE')]:
            with self.assertRaises(Refused):p.financial_guard({name:value})
    def test_secrets_never_persist_even_provider_echo(self):
        signature=self.creds.headers('/trade-api/ws/v2')['KALSHI-ACCESS-SIGNATURE']
        for secret in [self.env[p.PRIVATE_NAME],self.creds.key_id,signature]:
            self.ev.raw(secret.encode(),AT,0)
            with self.assertRaises(Refused):self.ev.add('BAD',{'value':secret})
        escaped=json.dumps({'extra':self.creds.key_id}).replace('-',r'\u002d').encode()
        self.ev.raw(escaped,AT,0)
        raw=json.dumps(self.store.events())
        for secret in [self.env[p.PRIVATE_NAME],self.creds.key_id,signature]:self.assertNotIn(secret,raw)
        self.assertTrue(all(not e['payload']['raw_retained'] for e in self.store.events()))
    def test_scope_body_is_never_raw_evidence(self):
        raw=json.dumps({'api_keys':[{'api_key_id':self.creds.key_id,'scopes':['read']}]}).encode()
        self.ev.raw(raw,AT,'REST',public=False)
        self.assertFalse(self.store.events()[0]['payload']['raw_retained'])
    def test_quote_stale_future_clock_incomplete_and_close(self):
        good=p.qualify_ticker(ticker(),AT,CLOCK,MEMBERS)
        self.assertEqual(good['clock_delta_ms'],'100.0')
        for change in ({'ts_ms':TS-1001},{'ts_ms':TS+101},{'ts_ms':None},{'ts_ms':True},
                       {'yes_ask_size_fp':None},{'yes_bid_dollars':'NaN'},{'yes_bid_dollars':'0.6'},
                       {'market_ticker':'KXOTHER'},{'yes_ask_size_fp':'0'}):
            with self.assertRaises((Refused,ValueError,TypeError)):p.qualify_ticker(ticker(**change),AT,CLOCK,MEMBERS)
        for clock in ({'synchronized':False,'uncertainty_ms':'1','source_uncertainty_ms':'1'},{'synchronized':True,'uncertainty_ms':'26','source_uncertainty_ms':'1'},{'synchronized':True,'uncertainty_ms':'1','source_uncertainty_ms':None}):
            with self.assertRaises(Refused):p.qualify_ticker(ticker(),AT,clock,MEMBERS)
        with self.assertRaises(Refused):p.qualify_ticker(ticker(),AT,CLOCK,{'KXTEST-A':{'close_time':AT}})
    def test_snapshot_missing_skew_stale(self):
        q=p.qualify_ticker(ticker(),AT,CLOCK,MEMBERS)
        members=dict(MEMBERS,**{'KXTEST-B':MEMBERS['KXTEST-A']})
        with self.assertRaises(Refused):p.synchronized_snapshot({'KXTEST-A':q},members,AT)
        second=dict(q,ticker='KXTEST-B',native_ts_ms=TS-251)
        with self.assertRaises(Refused):p.synchronized_snapshot({'KXTEST-A':q,'KXTEST-B':second},members,AT)
        with self.assertRaises(Refused):p.synchronized_snapshot({'KXTEST-A':q},MEMBERS,'2026-09-26T14:00:02Z')
        second=dict(q,ticker='KXTEST-B',received_at='2026-09-26T14:00:00.400Z')
        with self.assertRaises(Refused):p.synchronized_snapshot({'KXTEST-A':q,'KXTEST-B':second},members,'2026-09-26T14:00:00.400Z')
        self.assertEqual(len(p.synchronized_snapshot({'KXTEST-A':q},MEMBERS,AT)['quotes']),1)
    def test_ack_and_reconnect_clear_quotes_and_books(self):
        s=p.Session(MEMBERS,self.ev,0)
        with self.assertRaises(Refused):self.message(s,'ticker',ticker())
        s=self.session();self.message(s,'ticker',ticker())
        self.assertEqual((s.quotes_count,s.snapshots_count),(1,1))
        with self.assertRaises(Refused):self.message(s,'ticker',ticker())
        new=p.Session(MEMBERS,self.ev,1)
        self.assertFalse(new.quotes);self.assertFalse(new.books);self.assertFalse(new.acks)
        with self.assertRaises(Refused):self.message(new,'ticker',ticker())
    def test_book_sequence_delta_baseline_and_unknown_messages(self):
        s=self.session()
        baseline={'market_ticker':'KXTEST-A','market_id':'native-id','yes_dollars_fp':[['0.4','3']], 'no_dollars_fp':[['0.5','4']]}
        self.message(s,'orderbook_snapshot',baseline,1)
        delta={'market_ticker':'KXTEST-A','market_id':'native-id','side':'yes','price_dollars':'0.4','delta_fp':'1','ts_ms':TS}
        self.message(s,'orderbook_delta',delta,2);self.assertEqual(s.deltas,1)
        with self.assertRaises(Refused):self.message(s,'orderbook_delta',delta,4)
        with self.assertRaises(Refused):self.message(s,'orderbook_delta',dict(delta,delta_fp='-100'),3)
        with self.assertRaises(Refused):self.message(s,'fill',delta)
        with self.assertRaises(Refused):s.process('{not json',AT,CLOCK)
    def test_native_ids_cannot_change_between_channels(self):
        s=self.session();self.message(s,'ticker',ticker())
        with self.assertRaises(Refused):self.message(s,'orderbook_snapshot',{'market_ticker':'KXTEST-A','market_id':'other'},1)
    def test_cursor_unknown_late_error_and_page_limit(self):
        reader=p.Reader(self.ev,time.monotonic()+10)
        with patch.object(reader,'get',side_effect=[{'markets':[],'cursor':'next'},{'markets':[],'cursor':'next'}]):
            with self.assertRaises(Refused):reader.pages('/markets','markets',{})
        with patch.object(reader,'get',return_value={'markets':[]}):
            with self.assertRaises(Refused):reader.pages('/markets','markets',{})
        with patch.object(reader,'get',side_effect=[{'markets':[],'cursor':'next'},Refused('LATE_ERROR')]):
            with self.assertRaises(Refused):reader.pages('/markets','markets',{})
        with patch.object(reader,'get',return_value={'markets':[],'cursor':''}):self.assertEqual(reader.pages('/markets','markets',{}),[])
    def test_probe_only_hook_precedes_other_stores(self):
        from atlas_v2 import service
        with patch.object(service,'release_identity',return_value={'sha':'a'*40}),patch.object(service,'authorized_mode',return_value='LIVE_MARKET_LEARNING'),patch.dict(service.os.environ,{'ATLAS_V2_SPORTS_PROBE_ONLY':'1'}),patch.object(p,'serve_probe') as serve,patch.object(service,'Store',side_effect=AssertionError('must not open BTC/learning')):
            service.run();serve.assert_called_once()
    def test_runtime_selfchecks_are_not_native_evidence(self):
        result=p.guard_selfchecks()
        self.assertTrue(result['synthetic_only']);self.assertEqual(result['native_quote_contribution'],0)
    def test_unproven_source_clock_retains_native_frame_without_admission(self):
        s=self.session()
        clock=dict(CLOCK,source_uncertainty_ms=None)
        s.process(json.dumps({'type':'ticker','sid':1,'msg':ticker()}),AT,clock)
        self.assertEqual(s.quotes_count,0)
        self.assertEqual(s.snapshots_count,0)
        self.assertIn('SOURCE_CLOCK_BOUND_UNPROVEN',s.rejection_reasons)
        self.assertEqual(len(self.store.events('SP_TICKER_OBSERVED')),1)

    def test_hard_deadline_terminates_child(self):
        from unittest.mock import MagicMock
        ctx=MagicMock();receiver=MagicMock();sender=MagicMock();process=MagicMock()
        ctx.Pipe.return_value=(receiver,sender);ctx.Process.return_value=process
        receiver.poll.return_value=False;process.is_alive.return_value=True
        with patch('multiprocessing.get_context',return_value=ctx):
            result=p.bounded_probe(self.root,'a'*40)
        self.assertEqual(result['reason'],'PROBE_HARD_DEADLINE')
        receiver.poll.assert_called_once_with(p.MAX_SECONDS+10)
        process.terminate.assert_called_once();process.kill.assert_called_once()
        self.assertEqual(result['qualified_snapshot_count'],0)

    def test_missing_key_fails_closed_without_secret_or_quote(self):
        result=p.run_probe(self.root/'probe','a'*40,{})
        self.assertEqual(result['state'],'SPORTS_TRANSPORT_BLOCKED')
        self.assertEqual(result['qualified_quote_count'],0)
        self.assertEqual(result['qualified_snapshot_count'],0)
        self.assertEqual(result['broker_writes'],0)
        self.assertTrue(Path(result['evidence_path']).exists())


class ConnectionTests(unittest.TestCase):
    setUp = SportsProbeTests.setUp
    tearDown = SportsProbeTests.tearDown
    def test_two_connection_orchestration_and_no_financial_commands(self):
        sent=[]
        def connect(*args,**kwargs):
            self.assertEqual(args[0],p.WS)
            class Fake:
                def __init__(self):
                    self.messages=iter([
                        {'type':'subscribed','id':1,'msg':{'channel':'ticker','sid':1}},
                        {'type':'subscribed','id':2,'msg':{'channel':'orderbook_delta','sid':2}},
                        {'type':'orderbook_snapshot','sid':2,'seq':1,'msg':{'market_ticker':'KXTEST-A','market_id':'native-id','yes_dollars_fp':[['0.4','3']],'no_dollars_fp':[['0.5','4']]}},
                        {'type':'orderbook_delta','sid':2,'seq':2,'msg':{'market_ticker':'KXTEST-A','market_id':'native-id','ts_ms':TS,'side':'yes','price_dollars':'0.4','delta_fp':'1'}},
                        {'type':'ticker','sid':1,'msg':ticker()}])
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def send(self,value):sent.append(json.loads(value))
                async def recv(self):return json.dumps(next(self.messages))
            return Fake()
        membership={'milestone_id':'native-event','events':['KXTEST'],'markets':MEMBERS}
        with patch.object(p,'NoRedirectConnect',side_effect=connect),patch.object(p,'clock_sample',return_value=CLOCK),patch.object(p,'now',return_value=AT),patch.object(p.Reader,'get',return_value={'api_keys':[{'api_key_id':self.creds.key_id,'scopes':['read']}]}),patch.object(p,'membership',return_value=membership):
            result=p.run_probe(self.root/'integrated','a'*40,self.env)
        self.assertEqual(result['state'],'SPORTS_TRANSPORT_READY')
        self.assertEqual(result['handshakes'],2);self.assertTrue(result['reconnect'])
        self.assertEqual(result['qualified_quote_count'],2);self.assertEqual(result['qualified_snapshot_count'],2)
        self.assertEqual(len(sent),4)
        self.assertTrue(all(v['cmd']=='subscribe' and v['params']['market_tickers']==['KXTEST-A'] for v in sent))
        self.assertEqual({v['params']['channels'][0] for v in sent},{'ticker','orderbook_delta'})
        evidence=Path(result['evidence_path']).read_text()
        self.assertNotIn(self.creds.key_id,evidence);self.assertNotIn(self.env[p.PRIVATE_NAME],evidence)

if __name__=='__main__':unittest.main()
