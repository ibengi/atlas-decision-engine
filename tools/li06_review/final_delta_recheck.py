"""Final bounded independent witnesses: expiry cache and risk reservation."""
import ast
import json
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import btc_context as bc
NOW=1_800_000_000.
rows=[{'ts':NOW-(30-i)*60,'open':65000.,'high':65040.,'low':64980.,'close':65025. if i%2 else 64985.,'volume':5.} for i in range(30)]
spots=(lambda:{'source':'older','price':65000.,'ts':NOW-80},lambda:{'source':'a','price':65000.,'ts':NOW},lambda:{'source':'b','price':65000.,'ts':NOW})
bc.clear_cache()
with patch.dict(os.environ,{'BTC_CONTEXT_CYCLE_CACHE':'1'}),patch.object(bc.time,'time',return_value=NOW):
    first=bc.get_btc_context(strike=65000.,minutes_remaining=10.,spot_sources=spots,klines_fn=lambda:rows,now=NOW,use_cache=True)
    exact=bc.get_btc_context(strike=65000.,minutes_remaining=10.,spot_sources=spots,klines_fn=lambda:rows,now=NOW+10,use_cache=True)
    after=bc.get_btc_context(strike=65000.,minutes_remaining=10.,spot_sources=spots,klines_fn=lambda:rows,now=NOW+10.01,use_cache=True)
assert first.valid and exact is first and after.valid and after is not first
assert first.n_valid_sources==3 and after.n_valid_sources==2
assert after.data_quality_score<first.data_quality_score
out={'cache_earliest_expiry':{'before_source_count':first.n_valid_sources,'exact_expiry_reused':exact is first,'after_source_count':after.n_valid_sources,'before_quality':first.data_quality_score,'after_quality':after.data_quality_score}}

tree=ast.parse((ROOT/'execution_engine.py').read_text())
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ExecutionEngine')
method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_execute_decision')
block=next(n for n in method.body if isinstance(n,ast.If) and ast.unparse(n.test)=='exec_res.filled <= 0')
fn=ast.parse('def exact(self,exec_res,dec):\n ticker="synthetic"\n count=1\n entry=20\n').body[0]
fn.body.append(block)
ns={'log_trd':logging.getLogger('independent-final-delta')}
exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'exact-final-engine-release','exec'),ns)
for status,key,expected in [('ambiguous:candle_expired_after_send:unavailable','possible_send_holds_reservation',0),('blocked:candle_expired_not_sent','confirmed_not_sent_releases_reservation',1)]:
    releases=[]
    owner=SimpleNamespace(risk=SimpleNamespace(release_half_open_attempt=lambda *args:releases.append(args)))
    result=SimpleNamespace(filled=0,order_id=None,status=status,state='rejected')
    ns['exact'](owner,result,SimpleNamespace(side='yes'))
    assert len(releases)==expected
    out[key]={'status':status,'release_calls':len(releases)}
out['safety']={'actual_network_requests':0,'broker_writes':0,'engine_constructions':0,'credentials_read':0}
print(json.dumps(out,indent=2))
