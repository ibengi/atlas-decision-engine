"""Run the candidate's exact create/except block against memory-only doubles."""
import ast
import copy
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace, ModuleType

ROOT=Path(__file__).resolve().parents[2]
clienttree=ast.parse((ROOT/'kalshi_client.py').read_text())
classes=[n for n in clienttree.body if isinstance(n,ast.ClassDef) and n.name in ('KalshiAPIError','CandleQualificationExpired')]
ns={}
exec(compile(ast.Module(body=classes,type_ignores=[]),'isolated-error-classes','exec'),ns)
stub=ModuleType('kalshi_client');stub.CandleQualificationExpired=ns['CandleQualificationExpired']
sys.modules['kalshi_client']=stub
tree=ast.parse((ROOT/'order_manager.py').read_text())
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='OrderManager')
method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='place_and_track')
block=next(n for n in method.body if isinstance(n,ast.Try) and any(isinstance(c,ast.Call) and isinstance(c.func,ast.Attribute) and c.func.attr=='create_order' for c in ast.walk(n)))
helper=ast.parse('def isolated(self, qualification_check):\n ticker="KXBTC15M-SYNTHETIC"\n side="yes"\n count=1\n limit_cents=20\n client_order_id="synthetic-only"\n').body[0]
helper.body.append(block)
ns.update(log_api=logging.getLogger('isolated-li06-manager'),ExecutionResult=lambda order_id,requested,filled,avg_price,status,state:SimpleNamespace(order_id=order_id,status=status,state=state))
exec(compile(ast.fix_missing_locations(ast.Module(body=[helper],type_ignores=[])),'isolated-exact-manager-block','exec'),ns)

def scenario(started):
    counts={'stub_create_calls':0,'flushes':0,'reconciliations':0}
    intent={'client_order_id':'synthetic-only','count':1,'price':20,'resolution':None}
    def create(*a,**kw):
        counts['stub_create_calls']+=1
        raise ns['CandleQualificationExpired'](started)
    def flush():counts['flushes']+=1;return True
    def reconcile(ticker,row):
        counts['reconciliations']+=1
        row['resolution']='UNAVAILABLE'
        return 'UNAVAILABLE'
    owner=SimpleNamespace(client=SimpleNamespace(create_order=create),
        pending_intents={'KXBTC15M-SYNTHETIC':intent},_flush_pending_intents=flush,resolve_intent=reconcile)
    result=ns['isolated'](owner,lambda:True)
    return dict(result_status=result.status,intent_retained=bool(owner.pending_intents),intent=copy.deepcopy(intent),**counts)
out={'never_started':scenario(False),'earlier_possible_send':scenario(True)}
assert out['never_started']['intent']['resolution']=='CLOSED_ABSENT'
assert out['never_started']['intent']['closure_source']=='local_transport_not_started'
assert out['never_started']['flushes']==1 and out['never_started']['reconciliations']==0
assert out['earlier_possible_send']['intent']['resolution']=='UNAVAILABLE'
assert 'closure_source' not in out['earlier_possible_send']['intent']
assert out['earlier_possible_send']['reconciliations']==1
assert all(row['intent_retained'] for row in out.values())
out['safety']={'real_network_requests':0,'broker_writes':0,'engine_constructions':0,'persistent_writes':0}
print(json.dumps(out,indent=2))
