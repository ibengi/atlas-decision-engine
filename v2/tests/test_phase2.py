"""Synthetic software tests only; never production learning or economic evidence."""
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atlas_v2.data import capture_scan
from atlas_v2.domain import Refused, canonical, digest, utc
from atlas_v2.learning import LearningObserver
from atlas_v2.learning_phase2 import LearningPhase2, _observation
from atlas_v2.qualification import ORIGIN, settlement
from atlas_v2.store import Store
from atlas_v2.training_protocol import (START,FIT_AT,DEADLINE,HYPOTHESES,protocol,protocol_hash,
    train_family,evaluate_oos,predict,oos_window)
from atlas_v2.qualified_metrics import economic_reward


FAMILY = "time_structure_v1"


def sample_rows(start=START,days=17):
    rows=[]
    for day in range(days):
        for i in range(30):
            at=utc(start)+timedelta(days=day,minutes=15*i)
            rows.append({"decision_id":f"decision-{day}-{i}","decision_hash":digest([day,i]),
                "event_id":f"event-{day}-{i}","ticker":f"KXBTC15M-{day}-{i}","domain":"BTC_15M",
                "family":FAMILY,"candidate_hash":"b"*64,"features_hash":"c"*64,
                "protocol_hash":protocol_hash(),"observed_at":at.isoformat(),"decision_at":at.isoformat(),
                "close_at":(at+timedelta(minutes=5)).isoformat(),
                "label_available_at":(at+timedelta(minutes=10)).isoformat(),
                "probability":"0.8","market_probability":"0.8","ask_baseline":"0.82",
                "outcome":int(i%3==0),"settlement_hash":"d"*64,"source_kind":"NATIVE","provenance_verified":True})
    return rows


class ProtocolTests(unittest.TestCase):
    def test_fixed_splits_deterministic_fit_and_no_promotion(self):
        rows=sample_rows()
        result=train_family(FAMILY,rows,FIT_AT)
        self.assertEqual(result,train_family(FAMILY,rows,FIT_AT))
        self.assertEqual(result["counts"],[210,90,210])
        self.assertTrue(result["validation"]["predictive_pass"])
        self.assertFalse(result["promotion"])
        self.assertEqual(result["validation"]["comparisons"]["market_probability"]["day_sign_flip_p"],1/128)
        changed=deepcopy(rows)
        for row in changed[-210:]: row["outcome"]=1-row["outcome"]
        self.assertEqual(result["parameters"],train_family(FAMILY,changed,FIT_AT)["parameters"])

    def test_minimum_is_per_day_not_total_or_copies(self):
        rows=sample_rows()
        with self.assertRaises(Refused): train_family(FAMILY,rows[1:],FIT_AT)
        rows[1]["event_id"]=rows[0]["event_id"]
        with self.assertRaises(Refused): train_family(FAMILY,rows,FIT_AT)

    def test_late_labels_and_synthetic_are_not_training_evidence(self):
        for key,value in (("label_available_at","2026-10-14T02:00:00Z"),("source_kind","SYNTHETIC"),("protocol_hash","0"*64)):
            rows=sample_rows(); rows[0][key]=value
            with self.assertRaises(Refused): train_family(FAMILY,rows,FIT_AT)
        with self.assertRaises(Refused): train_family(FAMILY,sample_rows(),"2026-10-13T00:00:00Z")

    def test_oos_must_be_future_and_prospectively_recorded(self):
        c=train_family(FAMILY,sample_rows(),FIT_AT)
        c.update(candidate_hash="e"*64,locked_at=FIT_AT)
        start,end,cutoff=oos_window(c["locked_at"])
        rows=sample_rows(start,7)
        for r in rows:
            r.update(challenger_hash=c["candidate_hash"],prospective_probability=str(predict(c["parameters"],r["probability"])),prediction_recorded_at=r["decision_at"])
        self.assertTrue(evaluate_oos(c,rows,cutoff)["predictive_pass"])
        rows[0]["prediction_recorded_at"]=rows[0]["close_at"]
        with self.assertRaises(Refused): evaluate_oos(c,rows,cutoff)

    def test_reward_rejects_synthetic_and_never_grants_authority(self):
        with self.assertRaises(Refused): economic_reward([({"source_kind":"SYNTHETIC"},None)],as_of=FIT_AT)
        result=economic_reward([],as_of=FIT_AT)
        self.assertIsNone(result["reward_total"])
        self.assertFalse(result["runtime_admission"])

    def test_diagnostic_reward_costs_variance_and_open_cohort_block(self):
        from test_learning_reward import fixtures, AT
        first,win=fixtures("a"); second,loss=fixtures("b")
        loss.update(outcome=0,published_at="2026-09-25T11:08:00Z")
        for d in (first,second): d.update(source_kind="NATIVE",stale_quote_exposure=False,liquidity_failure=False)
        value=economic_reward([(first,win),(second,loss)],as_of=AT)
        self.assertEqual(value["net_pnl"],"0.28")
        self.assertAlmostEqual(value["normalized_score"],.14-.04+1-.2-.86/101.14-2-.04)
        self.assertFalse(value["runtime_admission"])
        self.assertFalse(value["independence_established"])
        self.assertIsNone(economic_reward([(first,win),(second,None)],as_of=AT)["reward_total"])
        first["control_violations"]=["price_cap"]
        self.assertIsNone(economic_reward([(first,win),(second,loss)],as_of=AT)["reward_total"])


class NativeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.clock="2026-09-26T14:10:00Z"
        self.patches=[patch(m+".now",lambda:self.clock) for m in ("atlas_v2.learning","atlas_v2.store","atlas_v2.learning_phase2")]
        for p in self.patches:p.start()
        self.l=Store(self.root/"learning.sqlite"); self.o=Store(self.root/"observations.sqlite"); self.q=Store(self.root/"qualification.sqlite")
        self.observer=LearningObserver(self.l,"a"*40)
        self.phase=LearningPhase2(self.observer,self.o,self.q,self.root/"reports")

    def tearDown(self):
        for s in (self.l,self.o,self.q):s.close()
        for p in self.patches:p.stop()
        self.tmp.cleanup()

    def scan(self, at):
        self.clock=at
        market={"ticker":"KXBTC15M-TEST-C","event_ticker":"KXBTC15M-TEST","yes_bid_dollars":"0.59","yes_ask_dollars":"0.61",
                "close_time":"2026-09-27T00:15:00Z","status":"active","updated_time":at,"yes_ask_size_fp":"10"}
        outer=self
        class Reader:
            def get_markets(self,series,cursor):
                url=ORIGIN+"/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=200"
                return {"url":url,"response_url":url,"method":"GET","status":200,"started_at":outer.clock,"received_at":outer.clock,
                        "raw":canonical({"markets":[market],"cursor":""}),"transport_complete":True,"content_type":"application/json","content_range":None,"request_cursor":""}
        capture_scan(self.o,Reader())

    def decide(self):
        self.scan("2026-09-27T00:09:02Z"); self.scan("2026-09-27T00:10:02Z")
        observations=[{"hash":e["hash"],**e["payload"]} for e in self.o.events("OBSERVATION")]
        self.observer.observe(observations,self.q); self.phase.tick()
        return observations

    def label(self,outcome=1):
        self.clock="2026-09-27T00:17:00Z"
        o=self.o.events("OBSERVATION")[-1]["payload"]
        data={"market":{"ticker":o["ticker"],"event_ticker":o["event_id"],"market_type":"binary","close_time":o["close_at"],
                        "status":"finalized","result":"yes" if outcome else "no","is_provisional":False,"settlement_ts":"2026-09-27T00:16:00Z","settlement_value_dollars":str(outcome)}}
        body=canonical(data); url=ORIGIN+"/trade-api/v2/markets/"+o["ticker"]
        value={"url":url,"response_url":url,"method":"GET","status":200,"started_at":self.clock,"received_at":self.clock,
               "content_type":"application/json","content_range":None,"transport_complete":True,"body_base64":base64.b64encode(body).decode(),"body_sha256":hashlib.sha256(body).hexdigest()}
        raw=self.q.append("raw:"+digest(value),"Q_RAW",value)
        label=settlement(raw,o)
        self.q.append("label:"+digest(label),"Q_LABEL",label)

    def test_native_pipeline_authoritative_metrics_and_no_economic_fabrication(self):
        observations=self.decide()
        self.assertEqual(self.phase.status()["qualified_predictive_decisions"],2)
        self.assertEqual(self.phase.status()["qualified_settlements"],0)
        self.label(); self.observer.settle(observations,self.q); self.phase.tick()
        self.assertEqual(self.phase.status()["qualified_settlements"],2)
        report=json.loads((self.root/"reports/latest.json").read_text())
        self.assertEqual(report["new_settlements"],2)
        self.assertEqual(report["by_family"][FAMILY]["daily"]["model"]["n"],1)
        self.assertIsNone(report["reward_total"])
        self.assertIsNone(report["net_hypothetical_pnl"])
        anchor=self.l.anchor(); self.phase.tick(); self.assertEqual(anchor,self.l.anchor())

    def test_conflicting_label_invalidates_qualified_dataset(self):
        obs=self.decide(); self.label(); self.observer.settle(obs,self.q); self.phase.tick()
        self.label(0); self.observer.settle(obs,self.q); self.phase.tick()
        self.assertEqual(len(self.phase.rows()),0)

    def test_protocol_rejects_late_initialization_and_changed_source(self):
        other=LearningObserver(self.l,"b"*40)
        with self.assertRaises(Refused): LearningPhase2(other,self.o,self.q,self.root/"reports")
        self.clock=START
        new=Store(self.root/"late.sqlite")
        try:
            with self.assertRaises(Refused): LearningPhase2(LearningObserver(new,"a"*40),self.o,self.q,self.root/"reports")
        finally:new.close()

    def test_fixed_batch_cannot_repeat_and_insufficient_is_not_no_edge(self):
        self.clock=FIT_AT; self.phase.tick()
        self.assertEqual(self.phase.status()["status"],"DATA_QUALIFICATION_FAILED")
        self.assertEqual(len(self.l.events("L_TRAINING_BATCH")),1)
        self.clock=DEADLINE
        restarted=LearningPhase2(LearningObserver(self.l,"a"*40),self.o,self.q,self.root/"reports")
        restarted.tick(); restarted.tick()
        self.assertEqual(len(self.l.events("L_TRAINING_BATCH")),1)
        self.assertFalse(self.l.get("phase2:terminal")["payload"]["further_retraining"])

    def test_raw_transport_cannot_be_replaced_by_normalized_quote(self):
        self.scan("2026-09-27T00:10:02Z")
        obs=self.o.events("OBSERVATION")[-1]
        self.assertEqual(_observation(self.o,obs["hash"])["ticker"],obs["payload"]["ticker"])
        forged=dict(obs["payload"],ask="0.1")
        bad=self.o.append("bad","OBSERVATION",forged)
        with self.assertRaises(Refused):_observation(self.o,bad["hash"])


if __name__=="__main__":unittest.main()
