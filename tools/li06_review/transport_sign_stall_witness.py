"""Execute only extracted client transport function against in-memory doubles.

No broker module is imported, no engine/client is constructed, and no network
library is imported. Every request below means an in-memory stub invocation.
"""
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
tree=ast.parse((ROOT/'kalshi_client.py').read_text())
needed={'KalshiAPIError','CandleQualificationExpired','_normalized_http_method','_is_mutating_method'}
nodes=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in needed]
client=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='KalshiClient')
nodes.append(next(n for n in client.body if isinstance(n,ast.FunctionDef) and n.name=='_req'))
class Timeout(Exception):pass
class ConnectionError(Exception):pass
ns={'requests':SimpleNamespace(Timeout=Timeout,ConnectionError=ConnectionError),
    'READ_HTTP_METHODS':frozenset(('GET','HEAD','OPTIONS')),
    'RETRYABLE_STATUS':{429,500,502,503,504},'log_api':logging.getLogger('isolated-li06')}
exec(compile(ast.Module(body=nodes,type_ignores=[]),'extracted-local-transport','exec'),ns)

def scenario(kind):
    clock=[0.]
    counts={'stub_requests':0,'signs':0,'policy_checks':0,'qualification_checks':0}
    def sleep(duration):clock[0]+=duration
    ns['time']=SimpleNamespace(sleep=sleep)
    def policy(*a):counts['policy_checks']+=1
    def sign(*a):
        counts['signs']+=1
        if kind=='sign_stall':clock[0]=2.
        return {}
    def qualify():
        counts['qualification_checks']+=1
        if kind=='check_raises':raise ValueError('synthetic uncertainty')
        if kind=='truthy_nonboolean':return 'yes'
        if kind=='initial_expired':return False
        return clock[0] < 1.
    def request(*a,**kw):
        counts['stub_requests']+=1
        if kind=='timeout_then_expiry':raise Timeout('synthetic possible send')
        status=503 if kind=='503_then_expiry' else 200
        return SimpleNamespace(status_code=status,headers={},text='{}',json=lambda:{'synthetic':True})
    self=SimpleNamespace(_pk=object(),base_url='https://synthetic.invalid',
        _assert_broker_write_allowed=policy,_sign_headers=sign,session=SimpleNamespace(request=request))
    try:
        value=ns['_req'](self,'POST','/portfolio/synthetic',qualification_check=qualify)
        result={'outcome':'returned','value':value}
    except ns['CandleQualificationExpired'] as e:
        result={'outcome':'expired','request_started':e.request_started}
    result.update(counts)
    return result

result=scenario("sign_stall")
assert result["stub_requests"] == 0, "expired signal reached local session.request"
assert result["outcome"] == "expired" and result["request_started"] is False
print(json.dumps(result,indent=2))
