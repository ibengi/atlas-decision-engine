import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import btc_context as bc
NOW=1_800_000_000.
wire=[]
for i in range(30):
    ts=NOW-(29-i)*60
    wire.append([ts*1000,65000.,65040.,64980.,65025. if i%2 else 64985.,5.,ts*1000+59999,0,1,0,0,0])
clock=[NOW+59]
original=bc._wire_number
def delayed_parse(value):
    clock[0]=NOW+61
    return original(value)
with patch.object(bc.time,'time',side_effect=lambda:clock[0]), \
     patch.object(bc,'_http_get_json_meta',return_value=(wire,{'http_status':200})), \
     patch.object(bc,'_wire_number',side_effect=delayed_parse):
    qualified,origin=bc.fetch_klines_with_fallback(providers=[('binance',bc.fetch_klines_binance)])
assert qualified is not None and len(qualified)==29 and qualified[-1]['ts'] < NOW
print(json.dumps({'http_receipt_at':NOW+59,'qualification_at':clock[0],
                  'accepted':qualified is not None,'source':origin,
                  'accepted_count':len(qualified or []),
                  'last_open_at':(qualified or [{}])[-1].get('ts'),
                  'known_partial_bar_included':bool(qualified and qualified[-1]['ts']==NOW)},indent=2))
