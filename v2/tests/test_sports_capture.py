import base64
import hashlib
from pathlib import Path
import tempfile
import unittest

from atlas_v2.domain import Refused, canonical
from atlas_v2.sports_capture import Capture, PublicSportsReader, book, native_body, SYNC_MS, AGE_MS
from atlas_v2.store import Store


def response(data,status=200):
    raw=canonical(data)
    return dict(method="GET",status=status,url="https://external-api.kalshi.com/trade-api/v2/markets",
                response_url="https://external-api.kalshi.com/trade-api/v2/markets",
                content_type="application/json",transport_complete=True,
                started_at="2026-09-26T00:00:00Z",received_at="2026-09-26T00:00:00Z",
                body_base64=base64.b64encode(raw).decode(),body_sha256=hashlib.sha256(raw).hexdigest())


class Reader:
    def __init__(self,responses):self.responses=iter(responses)
    def get(self,*args):return next(self.responses)


class SportsCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"sports.sqlite")
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def test_frozen_time_limits(self):
        self.assertEqual((SYNC_MS,AGE_MS),(250,1000))

    def test_financial_and_historical_paths_forbidden(self):
        reader=PublicSportsReader()
        for path in ["/portfolio/orders","/portfolio/positions","/historical/markets","https://example.com"]:
            with self.assertRaises(Refused):reader.get(path,{})
        self.assertEqual(reader.count,0)

    def test_http403_is_preserved_and_never_snapshot(self):
        Capture(self.store,Reader([response({},403)])).collect()
        self.assertEqual(len(self.store.events("SPORTS_RAW_HTTP")),1)
        self.assertEqual(len(self.store.events("SPORTS_CAPTURE_BLOCKED")),1)
        self.assertFalse(self.store.events("SPORTS_RAW_QUOTE"))
        self.assertFalse(self.store.events("SPORTS_SNAPSHOT"))
        self.store.verify()

    def test_complete_pagination_and_cursor_loop(self):
        c=Capture(self.store,Reader([response({"markets":[],"cursor":"x"}),response({"markets":[],"cursor":""})]))
        self.assertEqual(c.pages("/markets",{},"markets")[0],[])
        c=Capture(self.store,Reader([response({"markets":[],"cursor":"x"}),response({"markets":[],"cursor":"x"})]))
        with self.assertRaises(Refused):c.pages("/markets",{},"markets")

    def test_late_error_retains_pages(self):
        c=Capture(self.store,Reader([response({"markets":[],"cursor":"x"}),response({},500)]))
        with self.assertRaises(Refused):c.pages("/markets",{},"markets")
        self.assertEqual(len(self.store.events("SPORTS_RAW_HTTP")),2)

    def test_unknown_envelope_refused(self):
        c=Capture(self.store,Reader([response({"markets":[],"cursor":"","partial":True})]))
        with self.assertRaises(Refused):c.pages("/markets",{},"markets")

    def test_body_hash_and_truncation_refused(self):
        r=response({});r["body_sha256"]="0"*64
        with self.assertRaises(Refused):native_body(r)
        r=response({});r["transport_complete"]=False
        with self.assertRaises(Refused):native_body(r)

    def test_depth_preserved_no_midpoint_or_time_invention(self):
        b=book({"ticker":"KXTEST-A","orderbook_fp":{"yes_dollars":[["0.40","7"]],"no_dollars":[["0.55","3"]]}})
        self.assertEqual(b["yes_ask"],"0.45")
        self.assertEqual(b["yes_ask_depth"],[["0.45","3"]])
        self.assertEqual(b["yes_spread"],"0.05")
        self.assertIsNone(b["native_quote_timestamp"])

    def test_absent_side_is_not_zero_ask(self):
        b=book({"ticker":"KXTEST-A","orderbook_fp":{"yes_dollars":[["0.40","7"]],"no_dollars":[]}})
        self.assertIsNone(b["yes_ask"])
        self.assertEqual(b["yes_ask_depth"],[])

    def test_negative_duplicate_and_crossed_depth_refused(self):
        for yes,no in [([["0.4","-1"]],[]),([["0.4","1"],["0.4","2"]],[]),([["0.7","1"]],[["0.7","1"]])]:
            with self.assertRaises(Refused):book({"ticker":"KXTEST-A","orderbook_fp":{"yes_dollars":yes,"no_dollars":no}})


if __name__=="__main__":unittest.main()
