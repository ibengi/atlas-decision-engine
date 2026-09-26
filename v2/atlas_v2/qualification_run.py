"""Evidence joins for the frozen Alpha Lab. No candidate selection or approval.

Two native final anchors are mandatory. Reconstruct derived evidence from raw
receipts; never accept caller probabilities, normalized features or costs alone.
"""
import argparse
from collections import Counter
from pathlib import Path

from .alpha_lab import HYPOTHESES, cohort, plan, probability, labelled_diagnostics, drawdown
from .domain import Refused, canonical, decimal, digest, strict_json, utc
from .execution import Quote, Limits, reprice
from .qualification import (verified_events, settlement, reference, candles, ladder,
                            refreshed, fee_evidence)
from .research_export import observations_from_snapshot


def costed_scenario(rows, family, features, labels, refreshes, fees, raw_events, equity):
    """Explicit allocation, full cohort, common reprice guards; no financial authority.

    Mark open contracts at zero (worst-case liquidation). Release simulated cash
    only on evidenced final settlement. Slippage/fee qualification stays separate.
    """
    cash=decimal(equity)
    if cash<=0:raise Refused("explicit positive hypothetical equity required")
    events=[];intents=[];seen=set();changes=[];open_positions=[];realized=decimal(0)
    def settle_before(at):
        nonlocal cash,realized
        settled=sorted([x for x in open_positions if utc(x["settlement_at"])<=at],key=lambda x:(utc(x["settlement_at"]),x["ticker"]))
        for x in settled:
            cash+=x["payout"];changes.append(x["payout"]);realized+=x["payout"]-x["cost"]
            open_positions.remove(x)
    for row in rows:
        o=row["observation"];h=o["hash"];p=probability(family,row,features[h])
        # Missing economic evidence blocks the FULL scenario, not selected rows.
        if h not in refreshes or h not in fees or o["ticker"] not in labels:
            raise Refused("complete same-row refresh/fee/settlement receipts required")
        q=refreshes[h];f=fees[h];label=labels[o["ticker"]]
        # Metadata captured afterward cannot be backdated into a decision.
        if any(utc(raw_events[f[k]]["payload"]["received_at"])>utc(q["observed_at"]) for k in ("series_receipt","schedule_receipt")):
            raise Refused("fee source unavailable at refreshed decision")
        fee=fee_evidence(raw_events[f["series_receipt"]],raw_events[f["schedule_receipt"]],q["ask"])
        at=utc(q["observed_at"]);settle_before(at)
        event={"ticker":o["ticker"],"observation_hash":h,"probability":str(p),"decision_price":o["ask"],
               "refreshed_price":q["ask"],"spread":q["spread"],"available":q["available"],
               "fees":fee["fee_bound"],"slippage_assumption":q["slippage_assumption"],
               "gross_edge":str(p-decimal(q["ask"])),"net_edge":str(p-decimal(q["ask"])-decimal(fee["fee_bound"])-decimal(q["slippage_assumption"])-decimal(Limits().uncertainty)),
               "would_submit":False,"hypothetical_size":0,"settlement_result":label["outcome"]}
        try:
            if o["ticker"] in seen:raise Refused("one trade per market")
            if len(open_positions)>=3:raise Refused("three-position limit")
            if decimal(drawdown(changes,equity)["maximum_drawdown_fraction"])>=decimal("0.20"):raise Refused("20% drawdown guard")
            quote=Quote(**{k:q[k] for k in Quote.__dataclass_fields__})
            econ=reprice(quote,p,cash,fee["fee_bound"],q["slippage_assumption"],Limits(),q["observed_at"])
        except Refused as exc:event["reason"]=str(exc)
        else:
            cost=decimal(econ["cost_bound"]);cash-=cost;changes.append(-cost);seen.add(o["ticker"])
            open_positions.append({"ticker":o["ticker"],"settlement_at":label["settlement_at"],"cost":cost,"payout":decimal(label["outcome"])*econ["count"]})
            event.update(hypothetical_size=econ["count"],economics=econ,reason="HYPOTHETICAL_GUARDS_SATISFIED")
            intents.append(event)
        events.append(event)
    if open_positions:settle_before(max(utc(x["settlement_at"]) for x in open_positions))
    return {"orders":events,"opportunity_count":len(intents),"net_hypothetical_pnl":str(realized),
            **drawdown(changes,equity),"marking":"open positions marked at zero until authoritative settlement",
            "qualification":"DIAGNOSTIC_ONLY_UNQUALIFIED_FEE_CLASS_AND_SLIPPAGE", "financial_authority":False}


def evaluate(snapshot, anchor, supplements, supplement_anchor, registration, cutoff, hypothetical_equity=None):
    if registration["plan_hash"]!=digest(plan()) or registration["plan"]!=plan():
        raise Refused("frozen preregistration mismatch")
    if utc(cutoff)<utc(registration["registered_at"]):raise Refused("cutoff before registration")
    observations=observations_from_snapshot(snapshot,anchor)
    ev=verified_events(supplements,supplement_anchor)
    if any(utc(o["observed_at"])>utc(cutoff) for o in observations) or any(utc(e["recorded_at"])>utc(cutoff) for e in ev.values()):
        raise Refused("evidence after fixed cutoff")
    rows,excluded=cohort(observations)
    by_ticker={o["ticker"]:o for o in observations}
    by_obs={o["hash"]:o for o in observations}
    labels, refs, bars, ladders, refreshes, fees={},{},[],{},{},{}
    def raw(h,event):
        r=ev.get(h)
        if not r or r["kind"]!="Q_RAW" or r["seq"]>=event["seq"]:
            raise Refused("derived evidence missing earlier raw receipt")
        if utc(r["payload"]["received_at"])>utc(event["recorded_at"]):
            raise Refused("receipt received after derived evidence")
        return r
    for e in ev.values():
        p,k=e["payload"],e["kind"]
        if k=="Q_LABEL":
            value=settlement(raw(p["source_receipt"],e),by_ticker[p["ticker"]])
            if p!=value:raise Refused("derived settlement changed")
            previous=labels.get(p["ticker"])
            if previous and (previous["outcome"],previous["settlement_at"])!=(p["outcome"],p["settlement_at"]):
                raise Refused("conflicting finalized outcomes; append-only review required")
            labels[p["ticker"]]=p
        elif k=="Q_REFERENCE":
            value=reference(raw(p["receipt"],e))
            if p!=value:raise Refused("derived BTC price changed")
            if p["at"] in refs and refs[p["at"]]["price"]!=p["price"]:raise Refused("conflicting BTC reference")
            refs[p["at"]]=p
        elif k=="Q_CANDLES":
            r=raw(p["closed_candles"][0]["receipt"],e)
            if candles(r,p["end"])!=p:raise Refused("derived candles changed")
            bars.append((p,r))
        elif k=="Q_LADDER":
            o=by_obs[p["decision_hash"]]
            value={"decision_hash":o["hash"],**ladder([raw(h,e) for h in p["pages"]],o)}
            if p!=value:raise Refused("derived ladder changed")
            ladders[o["hash"]]=p
        elif k=="Q_REFRESH":
            o=by_obs[p["decision_hash"]]
            if refreshed(raw(p["receipt_hash"],e),o)!=p:raise Refused("derived execution quote changed")
            refreshes[o["hash"]]=p
        elif k=="Q_FEE":
            o=by_obs[p["decision_hash"]]
            value={"decision_hash":o["hash"],"price":o["ask"],**fee_evidence(raw(p["series_receipt"],e),raw(p["schedule_receipt"],e),o["ask"])}
            if p!=value:raise Refused("derived fee evidence changed")
            fees[o["hash"]]=p
    features={}
    for row in rows:
        o=row["observation"]; at=utc(o["observed_at"]); f={}
        current=[r for r in refs.values() if utc(r["received_at"])<=at and 0<=(at-utc(r["at"])).total_seconds()<=5]
        if current:
            a=max(current,key=lambda r:utc(r["at"]))
            prior=[r for r in refs.values() if (utc(a["at"])-utc(r["at"])).total_seconds()==60 and utc(r["received_at"])<=at]
            if len(prior)==1:f.update(reference_now=a,reference_previous=prior[0])
        eligible=[b for b,r in bars if utc(r["payload"]["received_at"])<=at and 0<=(at-utc(b["end"])).total_seconds()<=60]
        if eligible:f["closed_candles"]=max(eligible,key=lambda b:utc(b["end"]))["closed_candles"]
        if o["hash"] in ladders:f["strike_triple"]=ladders[o["hash"]]["strike_triple"]
        features[o["hash"]]=f
    experiments=[]
    for family in HYPOTHESES:
        predictions=[]; missing=Counter()
        for row in rows:
            o=row["observation"]
            try:p=probability(family,row,features[o["hash"]])
            except Refused as exc:missing[str(exc)]+=1
            else:predictions.append({"observation_hash":o["hash"],"p":str(p),"market_probability":row["mid"]})
        missing_labels=sum(r["observation"]["ticker"] not in labels for r in rows)
        scores=None
        economic=None;economic_reason="explicit hypothetical equity and complete economic evidence required"
        if rows and not missing and not missing_labels:
            scores=labelled_diagnostics(family,rows,features,labels)
            scores["source_authority"]="RECONSTRUCTED_KALSHI_FINALIZED_RECEIPTS_BOUND_TO_NATIVE_ANCHOR"
            if hypothetical_equity is not None:
                try:economic=costed_scenario(rows,family,features,labels,refreshes,fees,ev,hypothetical_equity)
                except Refused as exc:economic_reason=str(exc)
        experiments.append({"hypothesis_id":family,"status":"NOT_TESTABLE","predictions":predictions,
                            "missing_features":dict(missing),"missing_labels":missing_labels,"paired_metrics":scores,
                            "economic_diagnostics":economic,"economic_blocker":economic_reason if economic is None else None,
                            "predictive_gate":"NOT_QUALIFIED","executable_gate":"NOT_QUALIFIED",
                            "net_edge":None,"hypothetical_pnl":None,"drawdown_dollars":None,"drawdown_percent":None,
                            "reasons":["full chronological split/independence still require qualification",
                                       "fee schedule/account class and execution slippage not qualified"],
                            "candidate_locked":False})
    return {"schema":"atlas-v2-qualified-diagnostics/1","plan_hash":digest(plan()),"dataset_hash":digest(snapshot),
            "qualification_dataset_hash":digest(supplements),"cutoff":cutoff,"observations":len(observations),
            "cohort_rows":len(rows),"unique_markets":len({r["observation"]["ticker"] for r in rows}),
            "calendar_days":sorted({r["day"] for r in rows}),"independent_periods":None,"exclusions":excluded,
            "authoritative_labels":len(labels),"refresh_receipts":len(refreshes),"fee_receipts":len(fees),
            "experiments":experiments,"candidate_lock":None,"model_approved":False,
            "verdict":"NO_CANDIDATE_READY_FOR_LOCK"}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ("snapshot","anchor","supplements","supplement_anchor","registration","output"):p.add_argument(arg)
    p.add_argument("--cutoff",required=True)
    p.add_argument("--hypothetical-equity",help="Explicit simulation dollars only; never account starting equity")
    a=p.parse_args()
    values=[strict_json(Path(getattr(a,n)).read_bytes()) for n in ("snapshot","anchor","supplements","supplement_anchor","registration")]
    result=evaluate(*values,a.cutoff,a.hypothetical_equity)
    with open(a.output,"xb") as f:f.write(canonical(result))


if __name__=="__main__":main()
