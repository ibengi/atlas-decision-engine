"""Opt-in OFFLINE reconstruction. Never imported by the deployed service.

Native receipt reconstruction, fixed small research search and append-only
prediction lineage. Hash verification establishes integrity, not provider truth;
independent source/cost authentication remains mandatory before qualification.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import itertools
import math
from pathlib import Path

from .domain import Refused, canonical, decimal, digest, hash_id, strict_json, utc, now
from . import qualification as q
from . import protocol_authority as authority
from .execution import Quote, Limits, reprice
from .model_diagnosis import score, block_ci

PROTOCOL_HASH = "ae644e7fa113b7d5177626d43408cd6ac4f6f3913d5a679a076ec1793a534074"


def protocol():
    value = authority.authority()
    if digest(value) != PROTOCOL_HASH: raise Refused("reconstruction protocol changed")
    return value


def source_sha(value):
    import re
    if not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{40}",value): raise Refused("source SHA required")
    return value


def native_features(store, anchor, market_receipt_id, reference_receipt_id, candle_receipt_id, at):
    store.verify(anchor)
    mr,rr,cr=(store.get(i) for i in (market_receipt_id,reference_receipt_id,candle_receipt_id))
    if not all(r and r["kind"]=="Q_RAW" for r in (mr,rr,cr)): raise Refused("native receipts required")
    for r in (mr,rr,cr):
        if r["seq"] > anchor["seq"] or utc(r["recorded_at"])>utc(at) or utc(r["payload"]["received_at"])>utc(at):
            raise Refused("input unavailable at decision/anchor")
    body=q.body(mr,mr["payload"]["url"])
    if set(body)!={"market"}: raise Refused("market envelope")
    market=body["market"];ticker=q.ticker(market["ticker"])
    if not ticker.startswith("KXBTC15M-"): raise Refused("outside frozen market scope")
    if mr["payload"]["url"]!=q.ORIGIN+"/trade-api/v2/markets/"+ticker: raise Refused("ticker receipt mismatch")
    if market["status"]!="active" or market["market_type"]!="binary" or market["strike_type"]!="greater":
        raise Refused("market/strike semantics")
    strike=decimal(str(market["floor_strike"]))
    if strike<=0: raise Refused("authoritative field strike required")
    if not market.get("rules_primary") or not market.get("rules_secondary"): raise Refused("settlement rules unavailable")
    decision=utc(at);remaining=(utc(market["close_time"])-decision).total_seconds()
    if not 240<=remaining<=300: raise Refused("outside frozen decision cohort")
    if not 0<=(decision-utc(market["updated_time"])).total_seconds()<=5: raise Refused("stale market")
    ref=q.reference(rr)
    if not 0<=(decision-utc(ref["at"])).total_seconds()<=5: raise Refused("stale reference at decision")
    end=decision.replace(second=0,microsecond=0).isoformat()
    window=q.candles(cr,end)
    closes=[float(decimal(c["close"])) for c in window["closed_candles"]]
    returns=[math.log(b/a) for a,b in zip(closes,closes[1:])]
    avg=sum(returns)/len(returns)
    sigma=math.sqrt(sum((x-avg)**2 for x in returns)/(len(returns)-1))
    if not math.isfinite(sigma) or sigma<=0: raise Refused("nonpositive volatility")
    bid,ask=map(decimal,(market["yes_bid_dollars"],market["yes_ask_dollars"]))
    if not 0<=bid<=ask<=1: raise Refused("invalid quote")
    value={"feature_schema":"MR-FEATURES-1","ticker":ticker,"event_id":market["event_ticker"],
        "decision_at":at,"close_at":market["close_time"],"strike_source":"field","strike":str(strike),
        "spot":ref["price"],"sigma_1m":str(sigma),"minutes_remaining":str(remaining/60),
        "provider":"Coinbase Exchange BTC-USD","quote_source":"Kalshi market endpoint",
        "bid":str(bid),"ask":str(ask),"market_probability":str((bid+ask)/2),
        "rules_hash":digest({k:market[k] for k in ("rules_primary","rules_secondary")}),
        "receipts":{k:r["hash"] for k,r in zip(("market","reference","candles"),(mr,rr,cr))},
        "collector_anchor":anchor,"candle_count":31,"candle_end":end,
        "fee_slippage_policy":"MR-COST-1","data_quality":{"complete":True,"lineage_reconstructed":True},
        "basis_qualification":"UNPROVEN_REFERENCE_VENUE_VS_SETTLEMENT_INDEX"}
    return {**value,"feature_hash":digest(value)}


def check_features(f):
    body={k:v for k,v in f.items() if k!="feature_hash"}
    if digest(body)!=f.get("feature_hash"): raise Refused("feature hash mismatch")
    if f.get("strike_source")!="field" or f.get("feature_schema")!="MR-FEATURES-1" or f.get("provider")!="Coinbase Exchange BTC-USD":
        raise Refused("unqualified feature lineage")
    if f.get("candle_count")!=31 or not 0<=(utc(f["decision_at"])-utc(f["candle_end"])).total_seconds()<60:
        raise Refused("candle age/completeness")
    for v in f["receipts"].values():hash_id(v)
    if set(f["receipts"])!={"market","reference","candles"}: raise Refused("missing source provenance")
    if not 240<=(utc(f["close_at"])-utc(f["decision_at"])).total_seconds()<=300: raise Refused("cohort changed")


def structural(f, scales, family):
    check_features(f)
    sigma=float(decimal(f["sigma_1m"]))
    s,k,t=map(float,(decimal(f["spot"]),decimal(f["strike"]),decimal(f["minutes_remaining"])))
    if min(s,k,t,sigma)<=0: raise Refused("model domain")
    if family not in ("structural","regime") or len(scales)!=(1 if family=="structural" else 2) or any(x not in (.75,1.,1.25) for x in scales):
        raise Refused("unregistered model parameters")
    scale=scales[0] if family=="structural" or sigma<.001 else scales[1]
    z=math.log(s/k)/(sigma*scale*math.sqrt(t))
    return min(1-1e-6,max(1e-6,.5*(1+math.erf(z/math.sqrt(2)))))


def calibrated(p, a, b):
    z=a*math.log(p/(1-p))+b
    return min(1-1e-6,max(1e-6,1/(1+math.exp(-z))))


def check_rows(rows, stage, at, family=None):
    plan=protocol();start,end=map(utc,plan["windows"][stage]);counts={}
    seen=set()
    if utc(at)<end: raise Refused("stage has not ended")
    for r in rows:
        authority.assert_row(r,stage,family)
        f=r["features"];check_features(f)
        if not start<=utc(f["decision_at"])<end or r.get("consumed_v1") is not False: raise Refused("wrong split/consumed row")
        if r["event_id"]!=f["event_id"] or r["event_id"] in seen: raise Refused("duplicate/mismatched event")
        if type(r["outcome"]) is not int or r["outcome"] not in (0,1): raise Refused("label")
        if not utc(f["close_at"])<=utc(r["settled_at"])<=utc(r["label_received_at"])<end:
            raise Refused("label not available at fixed cutoff")
        hash_id(r["settlement_receipt"]);source_sha(r["collector_source_sha"])
        seen.add(r["event_id"]);day=utc(f["decision_at"]).date().isoformat();counts[day]=counts.get(day,0)+1
    for i in range((end-start).days):
        if counts.get((start+timedelta(days=i)).date().isoformat(),0)<plan["minimum_events_per_day"]:
            raise Refused("minimum independent daily events")
    return seen


def reconstruct_features(store, features):
    if store is None: raise Refused("native receipt store required")
    store.verify(features["collector_anchor"])
    ids=[]
    for key in ("market","reference","candles"):
        matches=[e for e in store.events("Q_RAW") if e["hash"]==features["receipts"][key]]
        if len(matches)!=1: raise Refused("feature receipt absent/ambiguous")
        ids.append(matches[0]["event_id"])
    rebuilt=native_features(store,features["collector_anchor"],*ids,features["decision_at"])
    if canonical(rebuilt)!=canonical(features): raise Refused("feature does not reproduce native receipts")


def reconstruct_row(store, row):
    reconstruct_features(store,row["features"])
    decision=store.get(row.get("mr_decision_id"))
    if not decision or decision["kind"]!="MR_PREDICTION": raise Refused("MR_NATIVE_DECISION_REQUIRED")
    payload=decision["payload"]
    keys=("protocol_id","protocol_hash","candidate_family","feature_schema","stage","source_protocol_id","prior_candidate_uses")
    if any(payload.get(k)!=row.get(k) for k in keys) or payload["features"]!=row["features"]:
        raise Refused("MR_NATIVE_DECISION_BINDING_MISMATCH")
    if not utc(decision["recorded_at"])<utc(row["features"]["close_at"]): raise Refused("MR_LATE_DECISION")
    reject_phase2_exposure(store,row["features"])
    matches=[e for e in store.events("Q_RAW") if e["hash"]==row["settlement_receipt"]]
    if len(matches)!=1: raise Refused("native settlement missing")
    f=row["features"]
    label=q.settlement(matches[0],{"ticker":f["ticker"],"event_id":f["event_id"],"close_at":f["close_at"]})
    if (row["outcome"]!=label["outcome"] or utc(row["settled_at"])!=utc(label["settlement_at"])
            or utc(row["label_received_at"])!=utc(label["published_at"])): raise Refused("label receipt mismatch")


def fit(train, calibration, family, git_sha, at, native_store):
    plan=protocol();source_sha(git_sha)
    if family not in {c["family"] for c in plan["candidates"]}: raise Refused("unregistered family")
    a=check_rows(train,"TRAIN",at,family);b=check_rows(calibration,"CALIBRATION",at,family)
    if a & b: raise Refused("event leakage")
    for row in train+calibration: reconstruct_row(native_store,row)
    options=list(itertools.product((.75,1.,1.25),repeat=1 if family=="structural" else 2))
    trials=[]
    for scales in options:
        ps=[structural(r["features"],scales,family) for r in train]
        loss=score(ps,[r["outcome"] for r in train])["brier"]
        trials.append((loss,scales))
    scales=min(trials)[1]
    ps=[structural(r["features"],scales,family) for r in calibration];ys=[r["outcome"] for r in calibration]
    cal=[(score([calibrated(p,a,b) for p in ps],ys)["brier"],a,b) for a in (.75,1.,1.25) for b in (-.25,0.,.25)]
    _,slope,intercept=min(cal)
    artifact={"protocol_id":authority.MR,"candidate_family":authority.FAMILIES[family],"family":family,"candidate_identity":"MR-"+family.upper()+"-1","scales":scales,
        "slope":slope,"intercept":intercept,"protocol_hash":PROTOCOL_HASH,"source_git_sha":git_sha,
        "implementation_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "feature_schema":"MR-FEATURES-1","train_dataset_sha256":digest(train),"calibration_dataset_sha256":digest(calibration),
        "train_trials":trials,"calibration_trials":cal,"training_cutoff":plan["windows"]["TRAIN"][1],
        "calibration_cutoff":plan["windows"]["CALIBRATION"][1],"approved":False,"status":"UNVALIDATED_RESEARCH_CANDIDATE"}
    return {"artifact":artifact,"model_artifact_sha256":digest(artifact)}


def predict(model, features, git_sha):
    protocol()
    artifact=model["artifact"]
    if artifact.get("protocol_id")!=authority.MR or artifact.get("candidate_family")!=authority.FAMILIES.get(artifact["family"]):
        raise Refused("MR_MODEL_PROTOCOL_BINDING_MISMATCH")
    if digest(artifact)!=model["model_artifact_sha256"]: raise Refused("model artifact hash")
    if artifact["source_git_sha"]!=source_sha(git_sha) or artifact["protocol_hash"]!=PROTOCOL_HASH:
        raise Refused("release/protocol lineage")
    if artifact["implementation_sha256"]!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(): raise Refused("implementation changed")
    if artifact["feature_schema"]!="MR-FEATURES-1" or artifact["candidate_identity"]!="MR-"+artifact["family"].upper()+"-1" or artifact["slope"] not in (.75,1.,1.25) or artifact["intercept"] not in (-.25,0.,.25):
        raise Refused("unregistered calibration/schema/identity")
    if artifact["approved"] is not False: raise Refused("no model approval")
    return calibrated(structural(features,artifact["scales"],artifact["family"]),artifact["slope"],artifact["intercept"])


def reject_phase2_exposure(store, features):
    for event in store.events():
        if event["kind"].startswith("L_"):
            encoded=canonical(event["payload"])
            if canonical(features["ticker"]) in encoded or canonical(features["event_id"]) in encoded:
                raise Refused("MR_PHASE2_EXPOSURE")


def record_prediction(store, model, features, git_sha, *, lock_id=None):
    # Caller probabilities are not an input. Artifact implementation recomputes.
    reconstruct_features(store,features)
    reject_phase2_exposure(store,features)
    lock=None
    if lock_id is not None:
        lock=store.get(lock_id)
        if (not lock or lock["kind"]!="MR_LOCK" or lock["payload"].get("protocol_id")!=authority.MR
                or lock["payload"].get("protocol_hash")!=PROTOCOL_HASH
                or lock["payload"].get("model_artifact_sha256")!=model["model_artifact_sha256"]):
            raise Refused("MR_IMMUTABLE_LOCK_BINDING_REQUIRED")
    bound=authority.binding(features["decision_at"],model["artifact"]["family"],lock["recorded_at"] if lock else None)
    p=predict(model,features,git_sha)
    at=now()
    if not 0<=(utc(at)-utc(features["decision_at"])).total_seconds()<=5: raise Refused("prediction not fresh")
    with store.transaction():
        store.verify()
        identity="MR-PRED:"+model["model_artifact_sha256"]+":"+features["ticker"]
        if store.get(identity): raise Refused("one prediction per market")
        value=store.append(identity,"MR_PREDICTION",{**bound,"source_protocol_id":authority.MR,"prior_candidate_uses":[],"lock_hash":lock["hash"] if lock else None,"model_artifact_sha256":model["model_artifact_sha256"],
            "candidate_identity":model["artifact"]["candidate_identity"],"source_git_sha":git_sha,
            "features":features,"feature_hash":features["feature_hash"],"feature_schema":"MR-FEATURES-1",
            "probability":str(p),"market_probability":features["market_probability"],
            "fee_slippage_policy":"MR-COST-1","settlement_receipt":None,
            "model_uncertainty":{"status":"UNVALIDATED"},"calibration_confidence":{"status":"CALIBRATION_FIT_NOT_OOS"},
            "economic_edge_confidence":{"status":"COSTS_AND_BASIS_UNQUALIFIED"},"approved":False})
        if not utc(value["recorded_at"])<utc(features["close_at"]): raise Refused("persisted after close")
        return value


def attach_settlement(store, prediction_id, receipt, observation):
    protocol()
    label=q.settlement(receipt,observation)
    with store.transaction():
        store.verify();prediction=store.get(prediction_id)
        if not prediction or prediction["kind"]!="MR_PREDICTION": raise Refused("prediction missing")
        f=prediction["payload"]["features"]
        if (label["ticker"]!=f["ticker"] or observation["event_id"]!=f["event_id"]
                or label["close_at"]!=f["close_at"] or utc(label["published_at"])<=utc(prediction["recorded_at"])):
            raise Refused("settlement/prediction binding")
        return store.append("MR-LABEL:"+prediction["hash"],"MR_LABEL",{"prediction":prediction["hash"],"label":label})


def fresh_economics(quote, decision_ask, p, budget, fee_policy, slippage_policy, limits, at):
    # Both policies require separately reviewed, authoritative receipt bindings.
    # Hashes alone are NOT independent cost qualification; output labels say so.
    for policy in (fee_policy,slippage_policy):
        hash_id(policy["receipt"])
        if policy["ticker"]!=quote.ticker or not utc(policy["valid_from"])<=utc(at)<utc(policy["valid_until"]): raise Refused("cost policy scope/time")
    if fee_policy["version"]!="MR-COST-1" or slippage_policy["version"]!="MR-COST-1": raise Refused("cost policy version")
    price=decimal(quote.ask);rate=decimal(fee_policy["rate"])
    if rate<=0 or fee_policy["rounding"]!="CEIL_CENT_PER_ORDER": raise Refused("fee authority/formula")
    fee=(rate*price*(1-price)).quantize(Decimal('.01'),rounding=ROUND_CEILING)
    slip=decimal(slippage_policy["positive_bound"])+max(Decimal(0),price-decimal(decision_ask))
    if decimal(slippage_policy["positive_bound"])<=0: raise Refused("positive slippage required")
    value=reprice(quote,p,budget,str(fee),str(slip),limits,at)
    return {**value,"spread":str(decimal(quote.ask)-decimal(quote.bid)),"uncertainty_buffer":limits.uncertainty,
            "fee_policy_hash":digest(fee_policy),"slippage_policy_hash":digest(slippage_policy),
            "cost_authority":"REQUIRES_INDEPENDENT_AUTHENTICATION","would_submit":False}


def settled_pnl(economics, label):
    if label.get("authority")!="Kalshi finalized market" or type(label.get("outcome")) is not int or label["outcome"] not in (0,1):
        raise Refused("unreadable/nonfinal settlement")
    if label["ticker"]!=economics["ticker"]: raise Refused("settlement identity")
    hash_id(label["source_receipt"])
    count=decimal(economics["count"])
    outcome=label["outcome"] if economics["side"]=="yes" else 1-label["outcome"]
    # cost_bound already contains entry + fees + slippage. Deduct ONCE.
    return count*outcome-decimal(economics["cost_bound"])


def validation_binding(model, dataset_sha, candidate_id, feature_schema, git_sha, results):
    protocol()
    if candidate_id!=model["artifact"]["candidate_identity"] or feature_schema!=model["artifact"]["feature_schema"]:
        raise Refused("validation candidate/schema mismatch")
    if git_sha!=model["artifact"]["source_git_sha"] or digest(model["artifact"])!=model["model_artifact_sha256"]:
        raise Refused("validation release/model mismatch")
    hash_id(dataset_sha);source_sha(git_sha)
    body={"protocol_id":authority.MR,"candidate_family":model["artifact"]["candidate_family"],"candidate_identity":candidate_id,"model_artifact_sha256":model["model_artifact_sha256"],
          "source_git_sha":git_sha,"dataset_sha256":dataset_sha,"feature_schema":feature_schema,
          "protocol_hash":PROTOCOL_HASH,"results_sha256":digest(results),"results":results,"approved":False}
    return {"payload":body,"sha256":digest(body),"authority":"INTEGRITY_ONLY_REQUIRES_INDEPENDENT_SIGNED_REVIEW"}


def validate(model, rows, git_sha, at, native_store):
    check_rows(rows,"VALIDATION",at,model["artifact"]["family"])
    for row in rows: reconstruct_row(native_store,row)
    ps=[predict(model,r["features"],git_sha) for r in rows]
    ys=[r["outcome"] for r in rows]
    bs=[float(decimal(r["features"]["market_probability"])) for r in rows]
    days=[utc(r["features"]["decision_at"]).date().isoformat() for r in rows]
    events=[r["event_id"] for r in rows]
    brier_delta=[(p-y)**2-(b-y)**2 for p,b,y in zip(ps,bs,ys)]
    log_delta=[score([p],[y])["log_loss"]-score([b],[y])["log_loss"] for p,b,y in zip(ps,bs,ys)]
    blocks={name:{"brier":block_ci(brier_delta,groups,confidence=.975),"log_loss":block_ci(log_delta,groups,confidence=.975)}
            for name,groups in (("day",days),("event",events))}
    result={"blocks":blocks,"model":score(ps,ys),"market":score(bs,ys),"rows":len(rows),
            "status":"NOT_QUALIFIED","approved":False,
            "remaining":["qualified costs","basis qualification","fresh prospective OOS","Claude independent reproduction"]}
    if result["model"]["brier"]>=result["market"]["brier"] or result["model"]["log_loss"]>=result["market"]["log_loss"]:
        result["status"]="REJECTED"
    result["brier_delta"]=result["model"]["brier"]-result["market"]["brier"]
    result["log_loss_delta"]=result["model"]["log_loss"]-result["market"]["log_loss"]
    result["predictive_gate"]=all(metric["ci"] is not None and metric["ci"][1]<0 for block in blocks.values() for metric in block.values()) and result["model"]["ece"]<=.05
    result["executable_gate"]=False
    if not result["predictive_gate"]: result["status"]="REJECTED"
    return validation_binding(model,digest(rows),model["artifact"]["candidate_identity"],"MR-FEATURES-1",git_sha,result)
