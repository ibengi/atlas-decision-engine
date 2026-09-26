"""Synthetic regression evidence only; no fixture is research/OOS evidence."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from atlas_v2.domain import Refused, canonical, digest
from atlas_v2.alpha_lab import plan, cohort, probability
from atlas_v2.data import capture_scan
from atlas_v2.research_export import export_database, observations_from_snapshot
from atlas_v2.store import Store
from atlas_v2.qualification import (ORIGIN, TICKER_URL, SERIES_URL, FEE_URL, Reader, Collector,
    permitted, body, settlement, reference, candles, candle_url, refreshed, ladder,
    fee_evidence, export_qualification, verified_events)
from atlas_v2.qualification_run import evaluate, costed_scenario


AT="2026-09-25T12:10:02+00:00"
CLOSE="2026-09-25T12:15:00+00:00"
TICKER="KXBTC15M-TEST-C"
EVENT="KXBTC15M-TEST"


def obs():
    return {"ticker":TICKER,"event_id":EVENT,"observed_at":AT,"close_at":CLOSE,"bid":"0.59","ask":"0.61","hash":"a"*64}


def market(**kwargs):
    d={"ticker":TICKER,"event_ticker":EVENT,"market_type":"binary","status":"active","close_time":CLOSE,
       "updated_time":AT,"yes_bid_dollars":"0.59","yes_ask_dollars":"0.61","yes_ask_size_fp":"2",
       "strike_type":"greater","floor_strike":"100","rules_primary":"Fixed common rule","rules_secondary":"Fixed common reference",
       "price_ranges":[{"start":"0","end":"1","step":"0.01"}]}
    d.update(kwargs)
    return d


def receipt(url,data,at=AT,**changes):
    raw=data if isinstance(data,bytes) else canonical(data)
    p={"url":url,"response_url":url,"method":"GET","status":200,"started_at":at,"received_at":at,
       "content_type":"application/pdf" if isinstance(data,bytes) else "application/json","content_range":None,
       "transport_complete":True,"body_base64":base64.b64encode(raw).decode(),"body_sha256":hashlib.sha256(raw).hexdigest()}
    p.update(changes)
    return {"kind":"Q_RAW","payload":p,"hash":digest(p)}


def label_receipt(**changes):
    d=market(status="finalized",result="yes",settlement_ts="2026-09-25T12:16:00Z",settlement_value_dollars="1",is_provisional=False)
    d.update(changes)
    return receipt(ORIGIN+"/trade-api/v2/markets/"+TICKER,{"market":d},"2026-09-25T12:17:00Z")


def bars():
    end=int(datetime.fromisoformat("2026-09-25T12:10:00+00:00").timestamp())
    return [[end-60*i,"99.123456789012345678","101","100","100.123456789012345678","1"] for i in range(31,0,-1)]


class QualificationTests(unittest.TestCase):
    def test_frozen_plan_and_public_only_urls(self):
        self.assertEqual(digest(plan()),"5f9b96e20314c1bc361f3f782a4d5ef28171ee7ecf126e87491ed10026eb96b1")
        for u in [TICKER_URL,SERIES_URL,FEE_URL,candle_url("2026-09-25T12:10:00Z"),ORIGIN+"/trade-api/v2/markets/"+TICKER]:self.assertTrue(permitted(u))
        for u in [ORIGIN+"/trade-api/v2/portfolio/orders",ORIGIN+"/trade-api/v2/markets/V1",TICKER_URL+"?secret=x",TICKER_URL+"#x","http://api.exchange.coinbase.com/products/BTC-USD/ticker"]:
            self.assertFalse(permitted(u))
            with self.assertRaises(Refused):Reader().get(u)

    def test_final_settlement_requires_status_scope_time_payout_and_no_provisional(self):
        self.assertEqual(settlement(label_receipt(),obs())["outcome"],1)
        cases=[{"status":"determined"},{"status":"closed"},{"is_provisional":True},{"is_provisional":None},
               {"result":"void"},{"settlement_value_dollars":"0"},{"settlement_ts":"2026-09-25T12:14:00Z"},
               {"settlement_ts":"2026-09-25T12:18:00Z"},{"ticker":"KXBTC15M-WRONG"},{"close_time":"2026-09-25T12:14:00Z"}]
        for c in cases:
            with self.subTest(c=c),self.assertRaises(Refused):settlement(label_receipt(**c),obs())

    def test_receipt_transport_hash_and_scope_are_mandatory(self):
        for c in [{"transport_complete":False},{"status":206},{"content_range":"bytes 1-2/5"},
                  {"content_type":"text/html"},{"response_url":"https://example.com"},{"method":"POST"},{"body_sha256":"f"*64}]:
            r=label_receipt();r["payload"].update(c)
            with self.subTest(c=c),self.assertRaises(Refused):settlement(r,obs())

    def test_reference_native_time_and_exact_sixty_seconds_are_not_manufactured(self):
        r=receipt(TICKER_URL,{"time":AT,"price":"100.123456789012345678","trade_id":12})
        value=reference(r)
        self.assertEqual(value["at"],AT)
        self.assertEqual(value["price"],"100.123456789012345678")
        self.assertFalse(value["interpolated"])
        row=cohort([obs()])[0][0]
        prior={**value,"price":"100","at":"2026-09-25T12:09:02+00:00"}
        self.assertGreater(probability("underlying_kalshi_lag_v1",row,{"reference_now":value,"reference_previous":prior}),0)
        prior["at"]="2026-09-25T12:09:01+00:00"
        with self.assertRaises(Refused):probability("underlying_kalshi_lag_v1",row,{"reference_now":value,"reference_previous":prior})
        for at in ["2026-09-25T12:09:56Z","2026-09-25T12:10:03Z"]:
            with self.assertRaises(Refused):reference(receipt(TICKER_URL,{"time":at,"price":"100","trade_id":1}))

    def test_candles_complete_closed_consecutive_precise(self):
        end="2026-09-25T12:10:00Z"; data=bars(); url=candle_url(end)
        value=candles(receipt(url,data),end)
        self.assertEqual(len(value["closed_candles"]),31)
        self.assertEqual(value["closed_candles"][-1]["close"],"100.123456789012345678")
        for bad in [data[:-1],data+[data[-1]],data[:15]+data[16:]+[[data[-1][0]+60,"99","101","100","100","1"]]]:
            with self.assertRaises(Refused):candles(receipt(url,bad),end)
        for index,change in [(0,1),(4,"0"),(2,"98")]:
            bad=deepcopy(data);bad[0][index]=change
            with self.assertRaises(Refused):candles(receipt(url,bad),end)
        with self.assertRaises(Refused):candles(receipt(url,data,at="2026-09-25T12:09:59Z"),end)

    def test_refresh_requires_fresh_after_decision_quotes_and_positive_slippage(self):
        url=ORIGIN+"/trade-api/v2/markets/"+TICKER
        r=receipt(url,{"market":market(yes_ask_dollars="0.63")},at="2026-09-25T12:10:03Z")
        value=refreshed(r,obs())
        self.assertEqual(value["slippage_assumption"],"0.03")
        self.assertFalse(value["slippage_qualified"])
        for r in [receipt(url,{"market":market()},at="2026-09-25T12:10:01Z"),
                  receipt(url,{"market":market(updated_time="2026-09-25T12:10:08Z")},at="2026-09-25T12:10:08Z"),
                  receipt(url,{"market":market(updated_time="2026-09-25T12:09:50Z")}),
                  receipt(url,{"market":market(yes_ask_size_fp="-1")})]:
            with self.assertRaises(Refused):refreshed(r,obs())

    def test_ladder_rejects_partial_stale_incomparable_or_wrong_center(self):
        url=ORIGIN+"/trade-api/v2/markets?"+urlencode({"event_ticker":EVENT,"limit":200})
        data=[market(ticker="KXBTC15M-TEST-L",floor_strike="90"),market(),market(ticker="KXBTC15M-TEST-H",floor_strike="110")]
        r=receipt(url,{"markets":data,"cursor":""})
        self.assertEqual(len(ladder([r],obs())["strike_triple"]),3)
        for bad in [receipt(url,{"markets":data,"cursor":"more"}),receipt(url,{"markets":data,"cursor":""},at="2026-09-25T12:09:55Z"),
                    receipt(url,{"markets":data,"cursor":""},at="2026-09-25T12:10:03Z"),receipt(url,{"markets":data[1:],"cursor":""})]:
            with self.assertRaises(Refused):ladder([bad],obs())
        for change in [{"strike_type":"greater_or_equal"},{"close_time":"2026-09-25T12:30:00Z"},{"rules_secondary":"other source"},{"yes_ask_dollars":"0.1"}]:
            bad=deepcopy(data);bad[0].update(change)
            with self.assertRaises(Refused):ladder([receipt(url,{"markets":bad,"cursor":""})],obs())

    def test_fee_is_nonzero_and_does_not_self_qualify(self):
        series=receipt(SERIES_URL,{"series":{"ticker":"KXBTC15M","fee_type":"quadratic","fee_multiplier":"1"}})
        schedule=receipt(FEE_URL,b"%PDF-synthetic-test-not-provider-evidence")
        value=fee_evidence(series,schedule,"0.50")
        self.assertEqual(value["fee_bound"],"0.03")
        self.assertFalse(value["fee_qualified"])
        with self.assertRaises(Refused):fee_evidence(series,receipt(FEE_URL,b"html failure"),"0.50")
        with self.assertRaises(Refused):fee_evidence(receipt(SERIES_URL,{"series":{"ticker":"KXBTC15M","fee_type":"quadratic","fee_multiplier":"0"}}),schedule,"0.50")

    def test_failures_and_raw_receipts_survive_no_historical_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/"qualification.sqlite")
            class Fake:
                def get(self,url):return label_receipt(status="closed")["payload"]
            c=Collector(s,Fake())
            self.assertIsNone(c.settlement(obs()))
            self.assertEqual([e["kind"] for e in s.events()],["Q_RAW","Q_FAILURE"])
            before=s.anchor()
            x=export_qualification(s.path,Path(d)/"exports")
            self.assertEqual(before,s.anchor())
            self.assertEqual(x,export_qualification(s.path,Path(d)/"exports"))
            with self.assertRaises(sqlite3.DatabaseError):s.db.execute("DELETE FROM events")
            snapshot=export_database(s.path)
            self.assertEqual(len(verified_events(snapshot,before)),2)
            with self.assertRaises(Refused):verified_events(snapshot,{**before,"hash":"f"*64})
            s.db.close()

    def test_costed_scenario_has_explicit_equity_and_uses_common_guards(self):
        o=obs();o.update(bid="0.29",ask="0.31")
        h=o["hash"];row={"observation":o,"mid":"0.30","ask":"0.31","prior_mid":"0.90","day":"2026-09-25"}
        m=market(yes_bid_dollars="0.29",yes_ask_dollars="0.31")
        q=refreshed(receipt(ORIGIN+"/trade-api/v2/markets/"+TICKER,{"market":m}),o)
        sr=receipt(SERIES_URL,{"series":{"ticker":"KXBTC15M","fee_type":"quadratic","fee_multiplier":"1"}},at="2026-09-25T12:09:59Z")
        fr=receipt(FEE_URL,b"%PDF-synthetic-only",at="2026-09-25T12:09:59Z")
        f=fee_evidence(sr,fr,o["ask"])
        args=([row],"microstructure_dislocation_v1",{h:{}},{TICKER:settlement(label_receipt(),o)},{h:q},{h:f},{sr["hash"]:sr,fr["hash"]:fr})
        result=costed_scenario(*args,"100")
        self.assertEqual(result["opportunity_count"],1)
        self.assertEqual(result["net_hypothetical_pnl"],"0.65")
        self.assertEqual(result["maximum_drawdown_dollars"],"0.35")
        self.assertEqual(result["maximum_drawdown_percent"],"0.3500")
        self.assertFalse(result["orders"][0]["would_submit"])
        with self.assertRaises(Refused):costed_scenario(*args,"0")
        q["ask"]="0.90"
        blocked=costed_scenario(*args,"100")
        self.assertEqual(blocked["opportunity_count"],0)
        self.assertIn("cap",blocked["orders"][0]["reason"])

    def test_replay_recomputes_labels_and_refuses_conflicting_or_forged_derivatives(self):
        with tempfile.TemporaryDirectory() as d:
            ostore=Store(Path(d)/"observations.sqlite");qstore=Store(Path(d)/"qualification.sqlite")
            class Markets:
                def get_markets(self,series,cursor):
                    url=ORIGIN+"/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=200"
                    raw=canonical({"markets":[market()],"cursor":""})
                    return {**receipt(url,{})["payload"],"raw":raw,"request_cursor":""}
            capture_scan(ostore,Markets())
            snap=export_database(ostore.path);anchor=ostore.anchor()
            o=observations_from_snapshot(snap,anchor)[0]
            raw=qstore.append("raw1","Q_RAW",label_receipt()["payload"])
            label=settlement(raw,o)
            qstore.append("label1","Q_LABEL",label)
            registration={"registered_at":"2026-09-25T00:00:00Z","plan":plan(),"plan_hash":digest(plan())}
            def run():return evaluate(snap,anchor,export_database(qstore.path),qstore.anchor(),registration,"2099-01-01T00:00:00Z")
            r=run()
            self.assertEqual(r["authoritative_labels"],1)
            self.assertIsNotNone(r["experiments"][-1]["paired_metrics"])
            self.assertEqual(r["experiments"][-1]["status"],"NOT_TESTABLE")
            raw2=qstore.append("raw2","Q_RAW",label_receipt(result="no",settlement_value_dollars="0")["payload"])
            qstore.append("label2","Q_LABEL",settlement(raw2,o))
            with self.assertRaises(Refused):run()
            ostore.db.close();qstore.db.close()


if __name__=="__main__":unittest.main()
