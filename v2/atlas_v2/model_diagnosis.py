"""Offline consumed-V1 diagnostics. No engine imports, approval or network.

One byte-hashed dataset and fixed decision-time interval drive all ablations
and PnL. Missing costs stay unknown. No diagnostic winner qualifies a model.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random

from .domain import Refused, canonical, digest, strict_json, utc

CAPS = (0.02, 0.05, 0.10, 0.25, 0.50)
SIGMAS = (0.5, 0.75, 1.25, 1.5, 2.0)


def number(x):
    if isinstance(x, bool): raise Refused("boolean numeric input")
    try: v = float(x)
    except (ValueError, TypeError): raise Refused("missing numeric input") from None
    if not math.isfinite(v): raise Refused("nonfinite input")
    return v


def field(r, name):
    return r.get(name) if r.get(name) is not None else (r.get("features") or {}).get(name)


def probability(r, cap=0.5, sigma_scale=1):
    s, k, sigma, t = (number(field(r, key)) for key in ("spot", "strike", "sigma_1m", "minutes_remaining"))
    if min(s, k, sigma, t, sigma_scale) <= 0: raise Refused("nonpositive input")
    denom = sigma*sigma_scale*math.sqrt(t)
    ret = field(r, "ret_5m")
    # Legacy model ignores absent momentum. Report coverage explicitly below.
    raw_mu = 0 if ret is None else number(ret)/5*t/denom
    mu = max(-cap, min(cap, raw_mu))
    z = math.log(s/k)/denom+mu
    return min(.9999, max(.0001, .5*(1+math.erf(z/math.sqrt(2))))), raw_mu


def score(ps, ys):
    if not ps: return {"count": 0, "brier": None, "log_loss": None, "ece": None, "calibration": []}
    bins = [[] for _ in range(10)]
    for p,y in zip(ps,ys): bins[min(9,int(p*10))].append((p,y))
    b = [{"bin":i,"n":len(a),"p":sum(p for p,y in a)/len(a),"y":sum(y for p,y in a)/len(a)}
         for i,a in enumerate(bins) if a]
    return {"count":len(ps),"brier":sum((p-y)**2 for p,y in zip(ps,ys))/len(ps),
        "log_loss":-sum(y*math.log(max(1e-6,min(1-1e-6,p)))+(1-y)*math.log(max(1e-6,min(1-1e-6,1-p))) for p,y in zip(ps,ys))/len(ps),
        "ece":sum(a["n"]*abs(a["p"]-a["y"]) for a in b)/len(ps),"calibration":b}


def block_ci(values, groups, repetitions=2000, confidence=.95):
    if len(values)!=len(groups) or not 0<confidence<1: raise Refused("block inputs")
    key="ci95" if confidence==.95 else "ci"
    blocks = defaultdict(list)
    for value,group in zip(values,groups): blocks[group].append(value)
    data = [sum(v)/len(v) for _,v in sorted(blocks.items())]
    if len(data)<2: return {"blocks":len(data),"mean":None,key:None,"confidence":confidence}
    rng=random.Random(260926)
    samples=sorted(sum(rng.choices(data,k=len(data)))/len(data) for _ in range(repetitions))
    return {"blocks":len(data),"mean":sum(data)/len(data),key:[samples[int((1-confidence)/2*repetitions)],samples[min(repetitions-1,int((1+confidence)/2*repetitions))]],"confidence":confidence,
            "estimand":"equal-weight block mean; dependence between blocks remains an assumption"}


def paired(rows, ps):
    ys=[int(r["result"]=="yes") for r in rows]
    asks=[number(r["yes_ask"])/100 for r in rows]
    mids=[(number(r["yes_ask"])+number(r["yes_bid"]))/200 for r in rows]
    model=score(ps,ys); market=score(mids,ys)
    delta=[(p-y)**2-(b-y)**2 for p,b,y in zip(ps,mids,ys)]
    days=[utc(r["settled_at"]).date().isoformat() for r in rows]
    events=[r.get("event_id") or r["ticker"] for r in rows]
    daily={d:paired_summary([i for i,x in enumerate(days) if x==d],ps,mids,ys) for d in sorted(set(days))}
    return {"model":model,"market_midpoint":market,"market_ask":score(asks,ys),
            "brier_delta":model["brier"]-market["brier"] if rows else None,
            "day_block":block_ci(delta,days),"event_block":block_ci(delta,events),"daily":daily}


def paired_summary(indices,ps,bs,ys):
    return {"n":len(indices),"brier":sum((ps[i]-ys[i])**2 for i in indices)/len(indices),
        "baseline_brier":sum((bs[i]-ys[i])**2 for i in indices)/len(indices),
        "delta":sum((ps[i]-ys[i])**2-(bs[i]-ys[i])**2 for i in indices)/len(indices)}


def pnl(rows, allocation=None):
    selected=[r for r in rows if r.get("shadow_decision") in ("yes","no")]
    # Deterministic one-trade-per-market: first selected decision, no rescue by
    # later cheaper prices or later known costs. All duplicate counts reported.
    unique={}
    for r in selected: unique.setdefault(r["ticker"],r)
    trades=list(unique.values()); amounts=[]; daily=defaultdict(float)
    fees=slips=priced=0
    for r in trades:
        side=r["shadow_decision"]
        try:
            ask=number(r.get(side+"_ask"))/100
            if not 0<ask<=1: raise Refused("ask")
            priced+=1
        except Refused: continue
        try:
            fee=number(r.get("estimated_fee"))
            if fee<0: raise Refused("fee")
            fees+=1
        except Refused: fee=None
        try:
            slip=number(r.get("estimated_slippage"))
            if slip<=0: raise Refused("positive slippage evidence required")
            slips+=1
        except Refused: slip=None
        # Legacy estimates are not authenticated cost receipts.
        if fee is not None and slip is not None:
            value=int(r["result"]==side)-ask-fee-slip
            amounts.append((utc(r["settled_at"]),r["ticker"],value))
            daily[utc(r["settled_at"]).date().isoformat()]+=value
    complete=len(amounts)==len(trades) and bool(trades)
    total=sum(v for _,_,v in amounts) if complete else None
    equity=peak=number(allocation) if allocation is not None else 0.0
    if allocation is not None and equity<=0: raise Refused("positive explicit allocation required")
    dd=dd_pct=0.0
    for _,_,v in sorted(amounts):
        equity+=v;peak=max(peak,equity);dd=max(dd,peak-equity)
        if allocation is not None: dd_pct=max(dd_pct,(peak-equity)/peak*100)
    largest=max(daily.values(),default=None)
    return {"selected_row_count":len(selected),"trade_count":len(trades),
        "event_count":len({r.get("event_id") or r["ticker"] for r in trades}),
        "duplicate_market_decisions_excluded":len(selected)-len(trades),"priced_count":priced,
        "fee_coverage_pct":100*fees/len(trades) if trades else None,
        "slippage_coverage_pct":100*slips/len(trades) if trades else None,
        "authenticated_cost_coverage_pct":0 if trades else None,
        "net_hypothetical_pnl":None,"estimated_cost_pnl":total,
        "partial_costed_rows":len(amounts),"partial_costed_daily_diagnostic":dict(sorted(daily.items())),
        "largest_day_share":largest/total if total is not None and total>0 else None,
        "absolute_drawdown":dd if complete else None,"percentage_drawdown":dd_pct if complete and allocation is not None else None,
        "allocation":allocation,"allocation_semantics":"explicit hypothetical starting equity, settlement-order high-water; no intratrade marks",
        "status":"UNQUALIFIED_COSTS_NO_PROFITABILITY_CLAIM","fill_assumption":"one contract at selected-side recorded ask; execution availability unproven"}


def analyse(raw, manifest, start, end, allocation=None):
    if len(raw)>128*1024*1024: raise Refused("dataset exceeds bound")
    records=strict_json(raw)
    if not isinstance(records,list): raise Refused("full shadow store must be a JSON array")
    sha=hashlib.sha256(raw).hexdigest()
    if manifest.get("dataset_sha256")!=sha or manifest.get("row_count")!=len(records):
        raise Refused("dataset manifest mismatch")
    first,last=utc(start),utc(end)
    if first>=last: raise Refused("interval")
    rows=[]; rejected=Counter(); outside=0
    for index,r in enumerate(records):
        try:
            if not isinstance(r,dict): raise Refused("nonobject")
            if not first<=utc(r["ts"])<last: outside+=1;continue
            if r.get("result") not in ("yes","no"): raise Refused("unsettled")
            if utc(r["settled_at"])<=utc(r["ts"]): raise Refused("label timing")
            if not isinstance(r.get("ticker"),str) or not r["ticker"]: raise Refused("event identity")
            p,b,a=map(number,(r.get("probability_yes"),r.get("yes_bid"),r.get("yes_ask")))
            if not 0<=p<=1 or not 0<=b<=a<=100: raise Refused("probability/quote")
            probability(r)
            rows.append(r)
        except (Refused, KeyError, TypeError, ValueError) as exc: rejected[type(exc).__name__+":"+str(exc)]+=1
    rows.sort(key=lambda r:(utc(r["ts"]),r["ticker"]))
    ps=[number(r["probability_yes"]) for r in rows]
    mismatches=sum(abs(probability(r)[0]-p)>1e-6 for r,p in zip(rows,ps))
    definitions=[("as_recorded",None), ("no_momentum",{"cap":0})]+[("momentum_cap_"+str(c),{"cap":c}) for c in CAPS]+[("sigma_scale_"+str(s),{"sigma_scale":s}) for s in SIGMAS]
    variants={name:paired(rows,ps if knobs is None else [probability(r,**knobs)[0] for r in rows]) for name,knobs in definitions}
    contribution={}
    for name,knobs in definitions[1:]:
        q=[probability(r,**knobs)[0] for r in rows]
        errors=[(p-int(r["result"]=="yes"))**2-(v-int(r["result"]=="yes"))**2 for r,p,v in zip(rows,ps,q)]
        contribution[name]={"mean_recorded_error_minus_variant":sum(errors)/len(errors) if errors else None,
                            "causal_claim":False,"note":"one-at-a-time diagnostic contrasts; interactions not additive"}
    strata=defaultdict(list)
    for r in rows:
        t=number(field(r,"minutes_remaining"));sig=number(field(r,"sigma_1m"))
        strata["strike_source="+str(field(r,"strike_source") or "UNKNOWN")].append(r)
        strata["provider="+str(field(r,"klines_provider") or field(r,"provider") or "UNKNOWN")].append(r)
        strata["tte="+("0-5" if t<=5 else "5-10" if t<=10 else "10-15" if t<=15 else "over15")].append(r)
        strata["volatility="+("low" if sig<.0005 else "middle" if sig<.0015 else "high")].append(r)
        quality=field(r,"data_quality")
        try: label="low" if number(quality)<60 else "middle" if number(quality)<80 else "high"
        except Refused: label="UNKNOWN"
        strata["quality="+label].append(r)
    dates=sorted({utc(r["settled_at"]).date().isoformat() for r in rows})
    complete=manifest.get("complete_store") is True and manifest.get("settled_usable_count")==len(rows) and manifest.get("settlement_dates")==dates and outside==0
    return {"dataset_sha256":sha,"manifest_sha256":digest(manifest),"row_count":len(records),"usable_count":len(rows),
        "settlement_dates":dates,"settlement_date_count":len(dates),"interval":{"start_inclusive":start,"end_exclusive":end},
        "outside_interval":outside,"exclusions":dict(rejected),"complete_store_claim_matches_counts":complete,
        "source_authentication":"manifest must be independently bound to native frozen-store export; matching hashes alone do not establish origin",
        "control_mismatches":mismatches,"control_reproduces":bool(rows) and mismatches==0,
        "variants":variants,"component_contrasts":contribution,
        "strata":{k:paired(v,[number(r["probability_yes"]) for r in v]) for k,v in sorted(strata.items())},
        "field_only_variants":{name:paired([r for r in rows if field(r,"strike_source")=="field"],
            [number(r["probability_yes"]) if knobs is None else probability(r,**knobs)[0] for r in rows if field(r,"strike_source")=="field"]) for name,knobs in definitions},
        "momentum_input_coverage":sum(field(r,"ret_5m") is not None for r in rows),
        "momentum_saturation":{str(c):sum(abs(probability(r)[1])>=c for r in rows)/len(rows) if rows else None for c in CAPS},
        "pnl":pnl(rows,allocation),"verdict":"DIAGNOSTIC_ONLY" if rows and not mismatches else "LINEAGE_OR_DATA_BLOCKER",
        "approved":False,"prospective_evidence":False}


def main():
    ap=argparse.ArgumentParser();ap.add_argument("store");ap.add_argument("manifest");ap.add_argument("output")
    ap.add_argument("--start",required=True);ap.add_argument("--end",required=True);ap.add_argument("--allocation",type=float)
    args=ap.parse_args()
    report=analyse(Path(args.store).read_bytes(),strict_json(Path(args.manifest).read_bytes()),args.start,args.end,args.allocation)
    with Path(args.output).open("xb") as f:f.write(canonical(report)+b"\n")


if __name__=="__main__":main()
