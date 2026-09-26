"""Native append-only Phase 2 coordinator; no imported rows or promotion API.

Predictive admission and economic admission are deliberately separate. No accepted
fill writer exists while approval, reconciliation and costs remain unqualified.
Only the local GET collectors supply the three ledgers passed by service.run.
"""
import base64
import hashlib
import json
from pathlib import Path
from datetime import timedelta

from .alpha_lab import HYPOTHESES, cohort, probability, plan
from .data import observation as reconstruct_observation
from .domain import Refused, decimal, digest, now, strict_json, utc
from .learning import _raw, BLOCKERS
from .qualification import settlement
from .qualified_metrics import daily_report
from .training_protocol import (START, VALID_END, FIT_AT, DEADLINE, protocol, protocol_hash,
                                train_family, predict, oos_window, evaluate_oos)


def _find(store, value, kind):
    with store.mutex:
        event = store._decode(store.db.execute("SELECT * FROM events WHERE hash=?",(value,)).fetchone())
    if not event or event["kind"] != kind or digest({k:v for k,v in event.items() if k!="hash"}) != value:
        raise Refused("native evidence hash/kind mismatch")
    return event


def _observation(store, value):
    event = _find(store,value,"OBSERVATION")
    p = event["payload"]
    scan = _find(store,p["scan_hash"],"SCAN")
    receipts = [_find(store,h,"RAW_HTTP") for h in scan["payload"]["pages"]]
    if not receipts or scan["payload"].get("complete") is not True or scan["payload"].get("terminal_cursor") != "":
        raise Refused("complete native scan required")
    cursor, matches, tickers, count = "", [], set(), 0
    for receipt in receipts:
        r = receipt["payload"]
        if (not receipt["seq"] < scan["seq"] < event["seq"] or r["transport_complete"] is not True
                or r["status"] != 200 or r["method"] != "GET" or r["content_type"] != "application/json"
                or r.get("content_range") is not None or r["response_url"] != r["url"]
                or not r["url"].startswith("https://external-api.kalshi.com/trade-api/v2/markets?")
                or r["request_cursor"] != cursor or utc(r["received_at"]) < utc(r["started_at"])):
            raise Refused("native scan transport/pagination")
        raw = base64.b64decode(r["body_base64"],validate=True)
        if hashlib.sha256(raw).hexdigest() != r["body_sha256"]: raise Refused("native body changed")
        body = strict_json(raw)
        if set(body) != {"markets","cursor"}: raise Refused("unknown native envelope")
        for market in body["markets"]:
            if market["ticker"] in tickers: raise Refused("duplicate scan market")
            tickers.add(market["ticker"]); count += 1
            if digest(market) == p["raw_market_hash"] and receipt["hash"] == p["receipt_hash"]:
                matches.append(reconstruct_observation(market,receipt,scan))
        cursor = body["cursor"]
    if cursor or count != scan["payload"]["market_count"] or matches != [p]:
        raise Refused("observation differs from complete raw scan")
    return {"hash":event["hash"],**p}


class LearningPhase2:
    def __init__(self, observer, observations, qualification, directory):
        from .protocol_authority import authority
        authority()
        self.observer, self.store = observer, observer.store
        self.observations, self.qualification = observations, qualification
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.protocol_hash = protocol_hash()
        freeze = self.store.get("phase2:protocol")
        if freeze is None:
            if utc(now()) >= utc(START): raise Refused("protocol was not frozen before eligible observations")
            freeze = self.store.append("phase2:protocol","L_TRAINING_PROTOCOL",{
                "protocol": protocol(), "protocol_hash": self.protocol_hash,
                "source_sha": observer.source_sha, "frozen_at": now(),
                "champion": observer.activation.get("champion"), "self_promotion": False})
        if (freeze["payload"]["protocol_hash"] != self.protocol_hash or freeze["payload"]["protocol"] != protocol()
                or freeze["payload"]["source_sha"] != observer.source_sha or utc(freeze["recorded_at"]) >= utc(START)):
            raise Refused("frozen protocol/source changed or late")
        self.freeze = freeze
        self.admitted = {e["payload"]["decision_id"]:e for e in self.store.events("L_QUALIFIED_DECISION")}
        self.outcomes = {e["payload"]["decision_id"]:e for e in self.store.events("L_QUALIFIED_OUTCOME")}
        self.checked = {e["payload"]["decision_id"] for e in self.store.events("L_PHASE2_EXCLUSION")}|set(self.admitted)
        self.challengers = {e["payload"]["candidate_hash"]:e for e in self.store.events("L_CHALLENGER_LOCK")}
        self.report_hash = None
        observer.phase2 = self

    def _admit(self, decision, selected, native_observations):
        d = decision["payload"]
        from .protocol_authority import reject_mr_rows
        reject_mr_rows([d])
        o = _observation(self.observations,d["observation_hash"])
        row = selected.get(o["hash"])
        if row is None or o != d["observation"]: raise Refused("not canonical native cohort")
        if row["prior_mid"] is not None:
            earlier = [r for r in native_observations if r["ticker"]==o["ticker"]
                       and (utc(r["observed_at"]),r["hash"])<(utc(o["observed_at"]),o["hash"])]
            prior = max(earlier,key=lambda r:(utc(r["observed_at"]),r["hash"]))
            verified = _observation(self.observations,prior["hash"])
            if (not 30<=(utc(o["observed_at"])-utc(verified["observed_at"])).total_seconds()<=90
                    or str((decimal(verified["bid"])+decimal(verified["ask"]))/2)!=row["prior_mid"]):
                raise Refused("prior feature differs from raw observation")
        if (d.get("training_protocol_hash") != self.protocol_hash or d["source_sha"] != self.freeze["payload"]["source_sha"]
                or not utc(START) <= utc(o["observed_at"]) <= utc(d["decision_timestamp"]) <= utc(decision["recorded_at"]) < utc(o["close_at"])
                or (utc(decision["recorded_at"])-utc(o["observed_at"])).total_seconds() > 5):
            raise Refused("protocol/decision chronology")
        features,quote,fee,errors,provenance = self.observer._sources(o,self.qualification,d["decision_timestamp"],d["source_evidence"])
        snapshot = {k:v for k,v in row.items() if k!="observation"}
        full_features = {"cohort":snapshot,"sources":features}
        if (d["feature_snapshot"] != features or d["cohort_snapshot"] != snapshot or d["features_hash"] != digest(full_features)
                or str(probability(d["model_version"],row,features)) != d["model_probability"]
                or d["market_probability"] != row["mid"] or d["market_ask_baseline"] != row["ask"]
                or d["candidate_hash"] != digest({"source_sha":d["source_sha"],"family":d["model_version"],"plan_hash":digest(plan())})):
            raise Refused("frozen prediction/features mismatch")
        return {"decision_id": decision["event_id"], "decision_hash": decision["hash"],
                "event_id":o["event_id"], "ticker":o["ticker"], "domain":d["domain"], "family":d["model_version"],
                "candidate_hash":d["candidate_hash"], "observed_at":o["observed_at"],
                "decision_at":d["decision_timestamp"], "close_at":o["close_at"],
                "features_hash":d["features_hash"], "feature_snapshot":full_features,
                "probability":d["model_probability"], "market_probability":d["market_probability"],
                "ask_baseline":d["market_ask_baseline"], "protocol_hash":self.protocol_hash,
                "source_kind":"NATIVE", "provenance_verified":True,
                "admission":"PREDICTIVE_ONLY", "economic_admission":False,
                "acceptance":d["acceptance"], "hypothetical_size":d["hypothetical_size"],
                "observation_anchor":self.observations.anchor(), "qualification_anchor":self.qualification.anchor()}

    def capture_challengers(self, decision, at):
        """Historical capture path is disabled under the MR authority."""
        from .protocol_authority import require_active, PHASE2
        from .protocol_authority import reject_mr_rows, SUPERSEDED
        reject_mr_rows([decision["payload"]])
        try: require_active(PHASE2)
        except Refused as exc:
            if str(exc)!=SUPERSEDED: raise
            return
        d = decision["payload"]
        if d["model_probability"] is None: return
        for candidate, event in self.challengers.items():
            c = event["payload"]
            start,end,cutoff = oos_window(event["recorded_at"])
            if (c["family"] != d["model_version"] or candidate in self.observer.disqualified or self.store.get("phase2:invalidate:"+candidate)
                    or not utc(start) <= utc(d["market_observed_at"]) < utc(end)):
                continue
            self.store.append("phase2:prediction:"+digest([candidate,decision["event_id"]]),"L_CHALLENGER_PREDICTION",{
                "decision_id":decision["event_id"], "decision_hash":decision["hash"], "challenger_hash":candidate,
                "challenger_lock_hash":event["hash"], "probability":str(predict(c["parameters"],d["model_probability"])),
                "features_hash":d["features_hash"], "protocol_hash":self.protocol_hash, "prediction_at":at,
                "acceptance":"REJECTED", "economic_admission":False, "promotion":False})

    def rows(self):
        return [e["payload"]["row"] for i,e in self.outcomes.items()
                if i not in self.observer.invalid and e["payload"]["row"]["candidate_hash"] not in self.observer.disqualified]

    def decisions(self):
        return [e["payload"] for i,e in self.admitted.items() if i not in self.observer.invalid
                and e["payload"]["candidate_hash"] not in self.observer.disqualified]

    def tick(self):
        at = now()
        pending = [e for i,e in self.observer.decisions.items() if i not in self.checked]
        if pending:
            observations = [{"hash":e["hash"],**e["payload"]} for e in self.observations.events("OBSERVATION")]
            selected = {r["observation"]["hash"]:r for r in cohort(observations)[0]}
            for decision in pending:
                identity = decision["event_id"]
                try:
                    row = self._admit(decision,selected,observations)
                except (Refused,KeyError,TypeError,ValueError) as exc:
                    self.store.append("phase2:exclude:"+digest(identity),"L_PHASE2_EXCLUSION",{
                        "decision_id":identity,"decision_hash":decision["hash"],"reason":str(exc)[:240]})
                else:
                    self.admitted[identity] = self.store.append("phase2:decision:"+digest(identity),"L_QUALIFIED_DECISION",row)
                self.checked.add(identity)
        for identity, decision in self.admitted.items():
            if identity in self.outcomes or identity not in self.observer.outcomes or identity in self.observer.invalid: continue
            outcome = self.observer.outcomes[identity]
            p = outcome["payload"]
            q = _find(self.qualification,p["source_event"],"Q_LABEL")
            raw = _raw(self.qualification,q["payload"]["source_receipt"],q,at)
            label = settlement(raw,self.observer.decisions[identity]["payload"]["observation"])
            if label != q["payload"] or label != p["label"]: raise Refused("authoritative label changed")
            available = max(p["knowledge_at"],outcome["recorded_at"],q["recorded_at"],label["published_at"] ,key=utc)
            row = {**decision["payload"], "outcome":label["outcome"], "label_available_at":available,
                   "settlement_hash":outcome["hash"], "settlement_receipt_hash":raw["hash"]}
            self.outcomes[identity] = self.store.append("phase2:outcome:"+digest(identity),"L_QUALIFIED_OUTCOME",{
                "decision_id":identity, "row":row, "gross_pnl":None,"fees":None,"slippage":None,"net_pnl":None,
                "brier_contribution":p["model_brier"], "market_brier":p["market_brier"],
                "market_baseline_comparison":p["brier_improvement"], "prediction_residual":float(row["probability"])-row["outcome"],
                "calibration_error":None,"calibration_reason":"ECE_IS_COHORT_STATISTIC", "drawdown_contribution":None,
                "reward":None,"economic_reason":"REJECTED_NO_QUALIFIED_EXECUTION"})
        self._lifecycle(at)
        self._reports(at)
        return self.status()

    def _lifecycle(self, at):
        from .protocol_authority import require_active, PHASE2, SUPERSEDED
        try: require_active(PHASE2)
        except Refused as exc:
            if str(exc)!=SUPERSEDED: raise
            return  # historical evidence retained; no new fitting, locks or OOS
        batch = self.store.get("phase2:batch")
        if batch:
            members = [m for r in batch["payload"]["results"] for m in r.get("result",{}).get("training_members",[])]
            bad = sorted(m["decision_id"] for m in members if m["decision_id"] in self.observer.invalid or m["candidate_hash"] in self.observer.disqualified)
            if bad:
                identity="phase2:data-invalid:"+digest(bad)
                if not self.store.get(identity): self.store.append(identity,"L_PHASE2_DATA_INVALIDATION",{
                    "invalid_decisions":bad,"at":at,"reason":"EVALUATED_TRAINING_EVIDENCE_INVALIDATED","retraining":False})
        for candidate,event in self.challengers.items():
            evaluated = self.store.get("phase2:oos:"+candidate)
            members = event["payload"]["training_members"]+(evaluated["payload"].get("oos_members",[]) if evaluated else [])
            start,end,_ = oos_window(event["recorded_at"])
            members += [e["payload"] for e in self.admitted.values() if e["payload"]["family"]==event["payload"]["family"]
                        and utc(start)<=utc(e["payload"]["observed_at"])<utc(end)]
            bad = [m["decision_id"] for m in members if m["decision_id"] in self.observer.invalid
                   or m["candidate_hash"] in self.observer.disqualified]
            if (bad or candidate in self.observer.disqualified) and not self.store.get("phase2:invalidate:"+candidate):
                self.store.append("phase2:invalidate:"+candidate,"L_CHALLENGER_INVALIDATION",{
                    "candidate_hash":candidate,"invalid_training_decisions":bad,"at":at,
                    "reason":"TRAINING_EVIDENCE_OR_CONTROL_INVALIDATED","permanent":True,"promotion":False})
        if self.store.get("phase2:terminal"): return
        batch = self.store.get("phase2:batch")
        if batch is None and utc(at) >= utc(FIT_AT):
            if self.store.get("phase2:batch-started"):
                self._terminal("BATCH_INTERRUPTED",at); return
            if utc(at) >= utc(DEADLINE): self._terminal("DATA_QUALIFICATION_FAILED",at); return
            self.store.append("phase2:batch-started","L_TRAINING_BATCH_STARTED",{
                "protocol_hash":self.protocol_hash,"knowledge_cutoff":FIT_AT,"started_at":at,
                "dataset_anchor":self.store.anchor(),"maximum_batches":1})
            rows, results = self.rows(), []
            for family in HYPOTHESES:
                available = [r for r in rows if r["family"] == family and utc(START)<=utc(r["observed_at"])<utc(VALID_END)]
                expected = {r["decision_id"] for r in (e["payload"] for e in self.admitted.values())
                            if r["family"]==family and utc(START)<=utc(r["observed_at"])<utc(VALID_END)}
                try:
                    if expected != {r["decision_id"] for r in available}: raise Refused("unsettled qualified training decisions")
                    result = train_family(family,available,at)
                except (Refused,KeyError,ValueError) as exc:
                    results.append({"family":family,"status":"DATA_QUALIFICATION_FAILED","reason":str(exc)[:240]}); continue
                result["native_dataset_admission"] = True
                result["training_members"] = [{k:r[k] for k in ("decision_id","candidate_hash","settlement_hash")} for r in available]
                if result["validation"]["predictive_pass"]:
                    value = {**result,"locked_at":now(),"source_sha":self.observer.source_sha,"champion":None,
                             "model_approved":False,"promotion":False}
                    value["candidate_hash"] = digest(value)
                    locked = self.store.append("phase2:lock:"+value["candidate_hash"],"L_CHALLENGER_LOCK",value)
                    self.challengers[value["candidate_hash"]] = locked
                    results.append({"family":family,"status":"AWAITING_PROSPECTIVE_OOS","candidate_hash":value["candidate_hash"]})
                else:
                    results.append({"family":family,"status":"VALIDATION_REJECTED","result":result})
            batch = self.store.append("phase2:batch","L_TRAINING_BATCH",{"at":at,"results":results,"protocol_hash":self.protocol_hash,"promotion":False})
        if not batch: return
        for candidate,event in self.challengers.items():
            identity = "phase2:oos:"+candidate
            if self.store.get(identity): continue
            c = {**event["payload"],"locked_at":event["recorded_at"]}; start,end,cutoff = oos_window(c["locked_at"])
            if utc(at)<utc(cutoff) and utc(at)<utc(DEADLINE): continue
            result = {"candidate_hash":candidate,"status":"DATA_QUALIFICATION_FAILED"}
            try:
                if candidate in self.observer.disqualified or self.store.get("phase2:invalidate:"+candidate): raise Refused("candidate disqualified or invalidated")
                if utc(cutoff)>utc(DEADLINE): raise Refused("OOS exceeds finite deadline")
                members = [r for r in self.rows() if r["family"]==c["family"] and utc(start)<=utc(r["observed_at"])<utc(end)]
                expected = {r["decision_id"] for r in (e["payload"] for e in self.admitted.values())
                            if r["family"]==c["family"] and utc(start)<=utc(r["observed_at"])<utc(end)}
                if expected != {r["decision_id"] for r in members}: raise Refused("incomplete OOS outcomes")
                bound = []
                for row in members:
                    prediction = self.store.get("phase2:prediction:"+digest([candidate,row["decision_id"]]))
                    if (prediction is None or prediction["payload"]["decision_hash"]!=row["decision_hash"]
                            or prediction["payload"]["challenger_lock_hash"]!=event["hash"]):
                        raise Refused("missing native prospective OOS prediction")
                    bound.append({**row,"challenger_hash":candidate,"prospective_probability":prediction["payload"]["probability"],
                                  "prediction_recorded_at":prediction["recorded_at"]})
                evaluated = evaluate_oos(c,bound,at)
                result.update(evaluation=evaluated,
                              oos_members=[{k:r[k] for k in ("decision_id","candidate_hash","settlement_hash")} for r in members],
                              status="PREDICTIVE_PASS_ECONOMICS_BLOCKED" if evaluated["predictive_pass"] else "OOS_REJECTED")
            except (Refused,KeyError,ValueError) as exc: result["reason"] = str(exc)[:240]
            self.store.append(identity,"L_OOS_EVALUATION",result)
        evaluated = self.store.events("L_OOS_EVALUATION")
        if len(evaluated)==len(self.challengers):
            statuses = [r["status"] for r in batch["payload"]["results"] if r["status"]!="AWAITING_PROSPECTIVE_OOS"]+[e["payload"]["status"] for e in evaluated]
            if any(s=="PREDICTIVE_PASS_ECONOMICS_BLOCKED" for s in statuses): verdict="BLOCKED_BY_EXTERNAL_ECONOMIC_EVIDENCE"
            elif any(s=="DATA_QUALIFICATION_FAILED" for s in statuses): verdict="DATA_QUALIFICATION_FAILED"
            else: verdict="NO_LEARNABLE_EDGE_DEMONSTRATED"
            self._terminal(verdict,at)

    def _terminal(self, verdict, at):
        self.store.append("phase2:terminal","L_LEARNING_TERMINAL",{"verdict":verdict,"at":at,"protocol_hash":self.protocol_hash,
            "further_retraining":False,"auto_promotion":False,"capital":"OFF","broker_writes":0,"real_orders_submitted":0})

    def _reports(self, at):
        day = utc(at).date()
        last = self.store.latest("L_DAILY_LEARNING_REPORT")
        first = utc(last["payload"]["day_utc"]+"T00:00:00Z").date()+timedelta(days=1) if last else utc(START).date()
        decisions,rows = self.decisions(),self.rows()
        state = self.status()
        rejected = {"control_disqualified":sorted(self.observer.disqualified),
                    "evidence_invalidated":state["invalidated_challengers"],
                    "statistically_rejected":[r for r in state["challengers"] if r["status"]=="VALIDATION_REJECTED"]+
                                             [r for r in state["oos_evaluations"] if r["status"]=="OOS_REJECTED"]}
        for offset in range(7):
            completed = first+timedelta(days=offset)
            if completed>=day: break
            report = daily_report(decisions,rows,completed.isoformat(),at=at,challengers=state["challengers"],disqualified=rejected)
            report.update(protocol_hash=self.protocol_hash,phase2_status=state["status"],historical_report=True)
            event = self.store.append("phase2:daily:"+completed.isoformat(),"L_DAILY_LEARNING_REPORT",report)
            path = self.directory/(completed.isoformat()+".json")
            content = json.dumps({**report,"report_event_hash":event["hash"]},sort_keys=True)
            if not path.exists(): path.write_text(content)
            elif path.read_text()!=content: raise Refused("daily report artifact changed")
        report = daily_report(decisions,rows,day.isoformat(),at=at,challengers=state["challengers"],disqualified=rejected)
        report.update(protocol_hash=self.protocol_hash,phase2_status=state["status"],partial_day=True,
                      learning_anchor=self.store.anchor(),protocol_freeze_hash=self.freeze["hash"])
        self.report_hash = digest(report)
        temp = self.directory/"latest.tmp"
        temp.write_text(json.dumps({**report,"report_hash":self.report_hash},sort_keys=True))
        temp.replace(self.directory/"latest.json")

    def status(self):
        batch,terminal = self.store.get("phase2:batch"),self.store.get("phase2:terminal")
        status = terminal["payload"]["verdict"] if terminal else "PROSPECTIVE_OOS" if batch else "COLLECTING_FROZEN_TRAINING_DATA"
        invalidated = [e["payload"]["candidate_hash"] for e in self.store.events("L_CHALLENGER_INVALIDATION")]
        evaluations = [{k:v for k,v in e["payload"].items() if k!="oos_members"} for e in self.store.events("L_OOS_EVALUATION")]
        if invalidated or self.store.latest("L_PHASE2_DATA_INVALIDATION"): status="EVIDENCE_INVALIDATED_NO_RETRAIN"
        return {"status":status,"authority_status":"SUPERSEDED_FOR_MODEL_RECONSTRUCTION","mr_eligible":False,"protocol_version":protocol()["version"],"protocol_hash":self.protocol_hash,
                "protocol_frozen_at":self.freeze["recorded_at"],"protocol_freeze_hash":self.freeze["hash"],
                "eligible_not_before":START,"training_at":FIT_AT,"deadline":DEADLINE,
                "qualified_predictive_decisions":len(self.decisions()),"qualified_settlements":len(self.rows()),
                "qualified_economic_decisions":0,"reward_total":None,"net_hypothetical_pnl":None,
                "batch_count":int(batch is not None),"maximum_batches":1,"champion":self.freeze["payload"]["champion"],
                "champion_reason":"NO_ACTIVE_RESEARCH_MODEL_AT_FREEZE", "auto_promotion":False,
                "challengers":[{k:v for k,v in r.items() if k!="result"} for r in batch["payload"]["results"]] if batch else [],
                "invalidated_challengers":invalidated,"oos_evaluations":evaluations,
                "blocking_reasons":["FIXED_COLLECTION_PERIOD_INCOMPLETE"] if not batch and not terminal else [status],
                "economic_blockers":list(BLOCKERS),
                "report_path":str(self.directory/"latest.json"),"report_hash":self.report_hash,
                "capital":"OFF","broker_writes":0,"real_orders_submitted":0}
