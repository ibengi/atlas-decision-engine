import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import btc_context as bc
NOW=1_800_000_000.
def rows(end=NOW-60,count=30):
    return [{'ts':end-(count-1-i)*60,'open':65000.,'high':65040.,'low':64980.,
             'close':65025. if i%2 else 64985.,'volume':5.} for i in range(count)]
def wire(values,source):
    if source=='binance':
        return [[r['ts']*1000,r['open'],r['high'],r['low'],r['close'],r['volume'],r['ts']*1000+59999,0,1,0,0,0] for r in values]
    if source=='kraken':
        return {'error':[],'result':{'XXBTZUSD':[[r['ts'],r['open'],r['high'],r['low'],r['close'],0,r['volume'],1] for r in values],'last':int(NOW)}}
    return [[r['ts'],r['low'],r['high'],r['open'],r['close'],r['volume']] for r in reversed(values)]
def accepts(payload,source):
    with patch.object(bc,'_http_get_json_meta',return_value=(payload,{})):
        value,origin=bc.fetch_klines_with_fallback(providers=[(source,getattr(bc,'fetch_klines_'+source))],now=NOW)
    return value is not None,origin
out={}
for source in ('binance','kraken','coinbase'):
    out[source+'_positive']=accepts(wire(rows(),source),source)
payload=wire(rows(),'kraken')
payload['result']['XXBTZUSD'].insert(0,[NOW+3600,65000,65040,64980,65025,0,5,1])
out['future_prefix_kraken']=accepts(payload,'kraken')
payload=wire(rows(count=31),'coinbase');payload[-1][4]=True
out['malformed_prefix_coinbase']=accepts(payload,'coinbase')
payload=wire(rows(),'binance')
for row in payload:row[6]=(NOW+3600)*1000
out['contradictory_close_binance']=accepts(payload,'binance')
clock=[NOW]
spots=tuple(lambda name=name:{'source':name,'price':65000.,'ts':NOW} for name in ('a','b','c'))
def delay():clock[0]+=20;return rows(NOW-180)
with patch.object(bc.time,'time',side_effect=lambda:clock[0]):
    ctx=bc.get_btc_context(spot_sources=spots,klines_fn=delay,use_cache=False)
out['expired_during_io']={'valid':ctx.valid,'reason':ctx.reason,'flags':ctx.quality_flags}
with patch.object(bc,'fetch_klines_with_fallback',return_value=(rows(),'fresh:kraken')):
    ctx=bc.get_btc_context(spot_sources=spots,now=NOW,use_cache=False)
model={'valid':True,'features':{'sigma_1m':ctx.realized_vol_1m,'ret_5m':ctx.returns['5m'],'candle_provenance':ctx.klines_provenance}}
out['decision_positive']=bc.decision_candles_current(model,NOW)
out['decision_exact_expiry']=bc.decision_candles_current(model,NOW+90)
out['decision_after_expiry']=bc.decision_candles_current(model,NOW+90.01)
for field,value in [('schema','foreign'),('source','fresh:unknown'),('normalized_sha256','0'*64),('row_count',True),('last_close_ts',NOW+60),('valid_until',NOW+180),('validated_at',NOW+1),('degraded_cache_allowed',True)]:
    altered=copy.deepcopy(model);altered['features']['candle_provenance'][field]=value
    out['tamper_'+field]=bc.decision_candles_current(altered,NOW)
altered=copy.deepcopy(model);altered['features']['sigma_1m']+=.01
out['tamper_sigma']=bc.decision_candles_current(altered,NOW)
altered=copy.deepcopy(model);altered['features']['ret_5m']+=.01
out['tamper_return']=bc.decision_candles_current(altered,NOW)
assert all(out[source+'_positive'][0] for source in ('binance','kraken','coinbase'))
assert all(not out[key][0] for key in ('future_prefix_kraken','malformed_prefix_coinbase','contradictory_close_binance'))
assert out['expired_during_io']['valid'] is False
assert out['decision_positive'] is True and out['decision_exact_expiry'] is True
assert out['decision_after_expiry'] is False
assert all(value is False for key,value in out.items() if key.startswith('tamper_'))
print(json.dumps(out,indent=2))
