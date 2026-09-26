"""Synthetic regression evidence only, never admissible market outcomes."""
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from atlas_v2 import reconstruction as r, model_diagnosis as d
from atlas_v2 import qualification as q
from atlas_v2.domain import Refused, canonical, digest, utc
from atlas_v2.store import Store
from atlas_v2.execution import Quote, Limits

AT="2026-09-27T00:10:00Z"
SHA="a"*40


def legacy(i=0,**kw):
    row={"ts":f"2026-09-{i%2+1:02d}T00:00:00Z","settled_at":f"2026-09-{i%2+1:02d}T00:15:00Z",
        "ticker":"KXBTC15M-"+str(i),"spot":100,"strike":100,"sigma_1m":.001,
        "minutes_remaining":15,"ret_5m":.01,"yes_bid":45,"yes_ask":50,"no_ask":55,
        "result":"yes","estimated_fee":.02,"estimated_slippage":.01,"shadow_decision":"yes",
        "strike_source":"field"}
    row.update(kw);row.setdefault("probability_yes",d.probability(row)[0]);return row


class DiagnosisTests(unittest.TestCase):
    def analyse(self,rows):
        raw=canonical(rows);manifest={"dataset_sha256":hashlib.sha256(raw).hexdigest(),"row_count":len(rows)}
        return d.analyse(raw,manifest,"2026-09-01T00:00:00Z","2026-09-03T00:00:00Z")
    def test_shared_cohort_and_all_variants(self):
        result=self.analyse([legacy(i) for i in range(6)])
        self.assertEqual(len(result["variants"]),12)
        self.assertTrue(result["control_reproduces"])
        self.assertEqual(result["usable_count"],6)
        self.assertEqual(result["settlement_date_count"],2)
        self.assertEqual({v["model"]["count"] for v in result["variants"].values()},{6})
        self.assertEqual(result["pnl"]["trade_count"],6)
        self.assertEqual(result["momentum_saturation"]["0.5"],1)
        self.assertFalse(result["prospective_evidence"])
    def test_lineage_mismatch_and_exact_hash(self):
        result=self.analyse([legacy(probability_yes=.1)])
        self.assertEqual(result["verdict"],"LINEAGE_OR_DATA_BLOCKER")
        with self.assertRaises(Refused):d.analyse(b'[]',{"dataset_sha256":"0"*64,"row_count":0},AT,"2027-01-01T00:00:00Z")
    def test_missing_cost_is_not_zero_profit(self):
        rows=[legacy(),legacy(1,estimated_fee=None)]
        result=d.pnl(rows)
        self.assertIsNone(result["estimated_cost_pnl"])
        self.assertIsNone(result["net_hypothetical_pnl"])
        self.assertEqual(result["fee_coverage_pct"],50)
        self.assertEqual(result["slippage_coverage_pct"],100)
        self.assertEqual(d.pnl([legacy(estimated_slippage=0)])["slippage_coverage_pct"],0)
    def test_drawdown_explicit_high_water_and_fee_once(self):
        result=d.pnl([legacy(result="no"),legacy(1,result="yes")],100)
        self.assertAlmostEqual(result["absolute_drawdown"],.53)
        self.assertAlmostEqual(result["percentage_drawdown"],.53)
        self.assertAlmostEqual(result["estimated_cost_pnl"],-.06)
        self.assertIsNone(d.pnl([legacy(result="no")])["percentage_drawdown"])
    def test_duplicate_market_and_no_resplit(self):
        result=self.analyse([legacy(),legacy(1,ticker="KXBTC15M-0"),legacy(2,ts="2026-09-04T00:00:00Z")])
        self.assertEqual(result["outside_interval"],1)
        self.assertEqual(result["pnl"]["duplicate_market_decisions_excluded"],1)
        self.assertEqual(result["pnl"]["trade_count"],1)
    def test_cluster_uncertainty_counts_events_not_rows(self):
        result=d.block_ci([.1]*100+[.2]*100,["a"]*100+["b"]*100)
        self.assertEqual(result["blocks"],2)
        self.assertIsNotNone(result["ci95"])
        self.assertIsNone(d.block_ci([.1]*100,["a"]*100)["ci95"])


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/"native.sqlite")
        self.market={"ticker":"KXBTC15M-TEST","event_ticker":"KXBTC15M-EVENT","market_type":"binary",
            "status":"active","strike_type":"greater","floor_strike":100,"close_time":"2026-09-27T00:15:00Z",
            "updated_time":AT,"yes_bid_dollars":"0.45","yes_ask_dollars":"0.50",
            "rules_primary":"synthetic strict greater","rules_secondary":"synthetic authority"}
        self.add("market",q.ORIGIN+"/trade-api/v2/markets/"+self.market["ticker"],{"market":self.market})
        self.add("reference",q.TICKER_URL,{"time":AT,"price":"100.1","trade_id":1})
        end=int(utc(AT).timestamp())
        candles=[[end-60*i,90,110,100,100+math.sin(i)*.1,1] for i in range(31,0,-1)]
        self.add("candles",q.candle_url(AT),candles)
        self.features=r.native_features(self.store,self.store.anchor(),"market","reference","candles",AT)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def add(self,key,url,body):
        raw=canonical(body);p={"url":url,"response_url":url,"method":"GET","status":200,
            "started_at":AT,"received_at":AT,"content_type":"application/json","content_range":None,
            "transport_complete":True,"body_base64":base64.b64encode(raw).decode(),"body_sha256":hashlib.sha256(raw).hexdigest()}
        with patch("atlas_v2.store.now",return_value=AT):return self.store.append(key,"Q_RAW",p)
    def model(self):
        artifact={"protocol_id":r.authority.MR,"candidate_family":"MR-STRUCTURAL-1","family":"structural","candidate_identity":"MR-STRUCTURAL-1","scales":[1.0],"slope":1.,"intercept":0.,
            "protocol_hash":r.PROTOCOL_HASH,"source_git_sha":SHA,"implementation_sha256":hashlib.sha256(Path(r.__file__).read_bytes()).hexdigest(),
            "feature_schema":"MR-FEATURES-1","approved":False}
        return {"artifact":artifact,"model_artifact_sha256":digest(artifact)}
    def test_native_reconstruction_and_no_momentum(self):
        r.reconstruct_features(self.store,self.features)
        self.assertEqual(self.features["candle_count"],31)
        self.assertEqual(self.features["strike_source"],"field")
        model=self.model();p=r.predict(model,self.features,SHA)
        f=deepcopy(self.features);f["ret_5m"]=999;f["feature_hash"]=digest({k:v for k,v in f.items() if k!="feature_hash"})
        self.assertEqual(r.predict(model,f,SHA),p)
        with self.assertRaises(Refused):r.reconstruct_features(self.store,f)
    def test_proxy_missing_provenance_hash_and_stale_rejected(self):
        for k,v in (("strike_source","spot_proxy"),("provider",None),("feature_hash","0"*64),("candle_end","2026-09-27T00:08:00Z")):
            f=deepcopy(self.features);f[k]=v
            if k!="feature_hash":f["feature_hash"]=digest({k:v for k,v in f.items() if k!="feature_hash"})
            with self.assertRaises(Refused):r.predict(self.model(),f,SHA)
    def test_changed_model_release_and_validation_binding(self):
        model=self.model()
        with self.assertRaises(Refused):r.predict(model,self.features,"b"*40)
        tampered=deepcopy(model);tampered["artifact"]["slope"]=2
        with self.assertRaises(Refused):r.predict(tampered,self.features,SHA)
        result=r.validation_binding(model,"d"*64,"MR-STRUCTURAL-1","MR-FEATURES-1",SHA,{"brier":.2})
        self.assertEqual(result["sha256"],digest(result["payload"]))
        with self.assertRaises(Refused):r.validation_binding(model,"d"*64,"OTHER","MR-FEATURES-1",SHA,{})
    def test_prediction_recomputed_and_corruption_is_loud(self):
        with patch.object(r,"now",return_value=AT),patch("atlas_v2.store.now",return_value=AT):
            prediction=r.record_prediction(self.store,self.model(),self.features,SHA)
            self.assertEqual(float(prediction["payload"]["probability"]),r.predict(self.model(),self.features,SHA))
            with self.assertRaises(Refused):r.record_prediction(self.store,self.model(),self.features,SHA)
        with self.assertRaises(TypeError):r.record_prediction(self.store,self.model(),self.features,SHA,probability=.99)
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET payload='{}' WHERE event_id='reference'")
        with self.assertRaises(Refused):self.store.verify()
    def test_persistence_write_failure_not_swallowed(self):
        with patch.object(r,"now",return_value=AT),patch.object(self.store,"append",side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):r.record_prediction(self.store,self.model(),self.features,SHA)
        self.assertEqual(len(self.store.events("MR_PREDICTION")),0)
    def test_corrupt_load_is_not_empty_store(self):
        path=Path(self.tmp.name)/"bad.sqlite";path.write_bytes(b"corrupted")
        with self.assertRaises(sqlite3.DatabaseError):Store(path)
    def test_costs_recomputed_at_refresh_and_settlement_charged_once(self):
        quote=Quote("KXBTC15M-TEST","yes","0.48","0.50","2",AT,"2026-09-27T00:15:00Z","a"*64)
        base={"version":"MR-COST-1","ticker":quote.ticker,"receipt":"b"*64,"valid_from":"2026-09-27T00:00:00Z","valid_until":"2026-09-28T00:00:00Z"}
        fee={**base,"rate":"0.07","rounding":"CEIL_CENT_PER_ORDER"};slip={**base,"positive_bound":"0.01"}
        x=r.fresh_economics(quote,"0.49","0.9","100",fee,slip,Limits(),AT)
        self.assertEqual(x["fee_bound"],"0.02");self.assertEqual(x["slippage_bound"],"0.02")
        label={"authority":"Kalshi finalized market","outcome":1,"ticker":quote.ticker,"source_receipt":"c"*64}
        self.assertEqual(r.settled_pnl(x,label),r.decimal("0.46"))
        with self.assertRaises(Refused):r.settled_pnl(x,{**label,"outcome":None})
        expensive=Quote(quote.ticker,"yes","0.85","0.86","2",AT,quote.closes_at,quote.receipt_hash)
        with self.assertRaises(Refused):r.fresh_economics(expensive,"0.49","0.99","100",fee,slip,Limits(),AT)
        with self.assertRaises(Refused):r.fresh_economics(quote,"0.49","0.9","100",fee,{**slip,"positive_bound":"0"},Limits(),AT)
    def test_future_splits_refuse_small_consumed_or_early_data(self):
        with self.assertRaises(Refused):r.check_rows([],"TRAIN",AT)
        with self.assertRaises(Refused):r.check_rows([],"TRAIN","2026-11-01T00:00:00Z")
        row={**r.authority.binding(AT,"structural"),"source_protocol_id":r.authority.MR,"prior_candidate_uses":[],"features":self.features,"event_id":self.features["event_id"],"outcome":1,"settled_at":"2026-09-27T00:15:00Z",
             "label_received_at":"2026-09-27T00:16:00Z","settlement_receipt":"b"*64,"collector_source_sha":SHA,"consumed_v1":True}
        with self.assertRaisesRegex(Refused,"consumed"):r.check_rows([row],"TRAIN","2026-11-01T00:00:00Z")
    def test_registered_parameters_and_market_scope(self):
        for field,value in (("scales",[2.]),("slope",2.),("feature_schema","OTHER"),("candidate_identity","OTHER")):
            model=self.model();model["artifact"][field]=value;model["model_artifact_sha256"]=digest(model["artifact"])
            with self.assertRaises(Refused):r.predict(model,self.features,SHA)
        self.market["ticker"]="KXSPORTS-TEST"
        self.add("sports",q.ORIGIN+"/trade-api/v2/markets/KXSPORTS-TEST",{"market":self.market})
        with self.assertRaises(Refused):
            r.native_features(self.store,self.store.anchor(),"sports","reference","candles",AT)
    def test_model_hash_and_no_approval_gate(self):
        model=self.model();model["model_artifact_sha256"]="0"*64
        with self.assertRaises(Refused):r.predict(model,self.features,SHA)
        model=self.model();model["artifact"]["approved"]=True;model["model_artifact_sha256"]=digest(model["artifact"])
        with self.assertRaises(Refused):r.predict(model,self.features,SHA)
    def test_feature_receipts_cannot_be_replaced(self):
        f=deepcopy(self.features);f["spot"]="101";f["feature_hash"]=digest({k:v for k,v in f.items() if k!="feature_hash"})
        with self.assertRaises(Refused):r.reconstruct_features(self.store,f)
    def test_percentage_drawdown_is_not_dollars(self):
        result=d.pnl([legacy(result="no")],200)
        self.assertAlmostEqual(result["absolute_drawdown"],.53)
        self.assertAlmostEqual(result["percentage_drawdown"],.265)
    def test_training_calibration_fixed_windows_and_parameters(self):
        def cohort(stage):
            start,end=map(utc,r.protocol()["windows"][stage]);rows=[]
            for day in range((end-start).days):
                for j in range(30):
                    at=start+timedelta(days=day,minutes=10+30*j)
                    f=deepcopy(self.features);f.update(decision_at=at.isoformat(),close_at=(at+timedelta(minutes=5)).isoformat(),
                        candle_end=at.isoformat(),event_id=stage+str(day)+"-"+str(j))
                    f["feature_hash"]=digest({k:v for k,v in f.items() if k!="feature_hash"})
                    rows.append({**r.authority.binding(f["decision_at"],"structural"),"source_protocol_id":r.authority.MR,"prior_candidate_uses":[],"features":f,"event_id":f["event_id"],"consumed_v1":False,"outcome":j%2,
                        "settled_at":f["close_at"],"label_received_at":(at+timedelta(minutes=6)).isoformat(),
                        "settlement_receipt":"b"*64,"collector_source_sha":SHA})
            return rows
        train,cal=cohort("TRAIN"),cohort("CALIBRATION")
        # Fixed-grid behavior test only. Native reconstruction is independently
        # tested above; mocked synthetic rows are never research evidence.
        with patch.object(r,"reconstruct_row"):
            model=r.fit(train,cal,"structural",SHA,"2026-10-18T00:00:00Z",self.store)
        self.assertEqual(len(model["artifact"]["train_trials"]),3)
        self.assertEqual(len(model["artifact"]["calibration_trials"]),9)
        self.assertFalse(model["artifact"]["approved"])
        self.assertEqual(model["artifact"]["train_dataset_sha256"],digest(train))
        with self.assertRaises(Refused):r.check_rows(cal,"TRAIN","2026-10-18T00:00:00Z")
        with self.assertRaises(Refused):r.fit(train,cal,"unregistered",SHA,"2026-10-18T00:00:00Z",self.store)
    def test_protocol_frozen(self):
        self.assertEqual(digest(r.protocol()),r.PROTOCOL_HASH)
        self.assertEqual(len(r.protocol()["candidates"]),2)
        self.assertFalse(r.protocol()["approved"])
