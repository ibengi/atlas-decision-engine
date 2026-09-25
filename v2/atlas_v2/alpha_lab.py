"""Five fixed offline experiments, not a model search or approval mechanism.

Inputs remain immutable. Missing features/labels/costs are reported, not imputed.
Caller probabilities, period names and in-sample winner selection are forbidden.
Outcome scoring is diagnostic: source authentication and future OOS are separate
gates. This research module is never imported by the collector service.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
import json
import math
from pathlib import Path

from .domain import Refused, canonical, decimal, digest, now, strict_json, utc
from .execution import Limits, reprice
from .research_export import observations_from_snapshot

HYPOTHESES = {
    "microstructure_dislocation_v1": {
        "why": "Temporary inventory pressure may overshoot a recent quote midpoint.",
        "why_unpriced": "Thin depth and maker inventory constraints may delay reversion; this is unproven.",
        "formula": "clip(mid + 0.5 * (prior_mid - mid)); prior same-ticker quote age 30..90 seconds",
        "features": ["spread", "prior_mid", "quote_drift", "seconds_since_change", "depth", "seconds_to_close"],
    },
    "underlying_kalshi_lag_v1": {
        "why": "Asynchronous reference and Kalshi updates may leave temporarily stale probabilities.",
        "why_unpriced": "Feed latency and scarce liquidity may prevent immediate correction; fast makers may eliminate it.",
        "formula": "clip(mid + 10 * log(reference_now/reference_60s_ago))",
        "features": ["reference_return_60s", "spread", "depth", "seconds_to_close"],
    },
    "volatility_regime_v1": {
        "why": "A volatility jump can make probabilities overconfident before makers widen distributions.",
        "why_unpriced": "Regime estimation is noisy and inventory costly; no systematic mispricing assumed.",
        "formula": "0.9*mid+0.05 if RMS(last 30 closed 1m log returns)>=0.001, else mid",
        "features": ["rms_30m", "volatility_contraction", "jump", "trend", "reversal"],
    },
    "cross_market_consistency_v1": {
        "why": "Separate books can temporarily disagree on nested strike probabilities.",
        "why_unpriced": "Multi-leg execution, depth and spread may erase apparent arbitrage.",
        "formula": "clip midpoint to interval [higher_strike_mid,lower_strike_mid] only if interval coherent",
        "features": ["same_expiry", "same_rules", "same_reference", "strictly_ordered_strikes", "both_leg_quotes"],
    },
    "time_structure_v1": {
        "why": "Near settlement, thin books may express excessive confidence.",
        "why_unpriced": "Short horizon execution costs may exceed calibration gains.",
        "formula": "logistic(0.9*logit(mid)) in fixed 240..300 second time-to-close cohort",
        "features": ["seconds_to_close", "spread", "depth", "quote_drift"],
    },
}


def plan():
    return {
        "schema": "atlas-alpha-plan/2", "families": HYPOTHESES,
        "scope": "KXBTC15M; first valid observed quote per event in 240..300 seconds before close",
        "baseline": "same-row midpoint (bid+ask)/2; report ask baseline as robustness check",
        "probability_clip": ["0.001", "0.999"],
        "split": "chronological complete UTC days; first floor(2N/3) TRAIN, remaining VALIDATION; no event overlap",
        "minimum": {"days_per_split": 3, "markets_per_split": 30},
        "stopping": "one fixed evaluation at manifest cutoff; preserve all failures; no optional stopping",
        "multiplicity": "five families; prospective one-sided paired day-block test alpha <=0.01 per family",
        "predictive_gate": "beat midpoint and ask Brier and log loss on same full validation cohort; multiplicity-controlled evidence required",
        "economic_gate": "positive costed PnL with sourced costs, refreshed depth, common execution guards, one intent per market, <=3 reserved positions, drawdown <20%",
        "execution_limits": asdict(Limits()),
        "legacy": "V1 TEST is consumed; never final OOS",
        "final_oos": "all observations strictly after immutable lock, predictions recorded before close; no post-lock changes",
        "not_a_candidate_lock": True,
    }


def preregister(path, code_sha):
    if len(code_sha) != 40 or any(c not in "0123456789abcdef" for c in code_sha):
        raise Refused("exact code SHA required")
    value = {"registered_at": now(), "code_sha": code_sha, "plan": plan(), "plan_hash": digest(plan())}
    with open(path, "xb") as file:
        file.write(canonical(value))
    return value


def _clip(p):
    return min(Decimal("0.999"), max(Decimal("0.001"), p))


def _mid(row):
    bid, ask = decimal(row["bid"]), decimal(row["ask"])
    if not 0 <= bid <= ask <= 1:
        raise Refused("invalid quote")
    return (bid + ask) / 2


def cohort(observations):
    """Deterministic sampling before labels are joined; all exclusions retained."""
    history, selected, seen, excluded = {}, [], set(), Counter()
    for row in sorted(observations, key=lambda r: (utc(r["observed_at"]), r["hash"])):
        if "candidate_probability" in row or "period" in row:
            raise Refused("caller prediction/period prohibited")
        mid = _mid(row)
        at = utc(row["observed_at"])
        seconds = (utc(row["close_at"]) - at).total_seconds()
        prior = history.get(row["ticker"])
        history[row["ticker"]] = row
        if not row["ticker"].startswith("KXBTC15M-"):
            excluded["outside_preregistered_scope"] += 1
        elif not 240 <= seconds <= 300:
            excluded["outside_fixed_time_window"] += 1
        elif row["event_id"] in seen:
            excluded["later_observation_same_event"] += 1
        else:
            seen.add(row["event_id"])
            age = (at - utc(prior["observed_at"])).total_seconds() if prior else None
            selected.append({"observation": row, "mid": str(mid), "ask": row["ask"],
                             "day": at.date().isoformat(), "seconds_to_close": seconds,
                             "prior_mid": str(_mid(prior)) if age is not None and 30 <= age <= 90 else None})
    return selected, dict(excluded)


def probability(family, row, features=None):
    """Feature inputs are pre-outcome diagnostics; never authority attestations."""
    if family not in HYPOTHESES:
        raise Refused("unregistered family")
    f, mid = features or {}, decimal(row["mid"])
    if family == "microstructure_dislocation_v1":
        if row["prior_mid"] is None:
            raise Refused("missing 30..90 second prior quote")
        p = mid + Decimal("0.5") * (decimal(row["prior_mid"]) - mid)
    elif family == "underlying_kalshi_lag_v1":
        if "reference_now" not in f or "reference_previous" not in f:
            raise Refused("missing attributable native BTC observations")
        current, prior = f["reference_now"], f["reference_previous"]
        at = utc(row["observation"]["observed_at"])
        if (not current.get("receipt") or not prior.get("receipt")
                or not 0 <= (at - utc(current["at"])).total_seconds() <= 5
                or (utc(current["at"]) - utc(prior["at"])).total_seconds() != 60):
            raise Refused("reference timing/provenance")
        a, b = decimal(current["price"]), decimal(prior["price"])
        if min(a, b) <= 0:
            raise Refused("reference price")
        p = mid + 10 * (a / b).ln()
    elif family == "volatility_regime_v1":
        candles = f.get("closed_candles", [])
        if len(candles) != 31:
            raise Refused("missing 31 attributable closed minute candles")
        at = utc(row["observation"]["observed_at"])
        for i, c in enumerate(candles):
            if (not c.get("receipt") or c.get("closed") is not True or decimal(c["close"]) <= 0
                    or utc(c["closed_at"]).timestamp() % 60 != 0 or utc(c["closed_at"]) > at
                    or i and (utc(c["closed_at"]) - utc(candles[i-1]["closed_at"])).total_seconds() != 60):
                raise Refused("candle timing/provenance")
        if not 0 <= (at - utc(candles[-1]["closed_at"])).total_seconds() <= 60:
            raise Refused("stale candle series")
        returns = [(decimal(b["close"]) / decimal(a["close"])).ln() for a, b in zip(candles, candles[1:])]
        rms = (sum(r*r for r in returns) / 30).sqrt()
        p = Decimal("0.9") * mid + Decimal("0.05") if rms >= Decimal("0.001") else mid
    elif family == "cross_market_consistency_v1":
        # Economic comparability must be supplied as full bound contracts, not a boolean.
        triple = f.get("strike_triple")
        if not isinstance(triple, list) or len(triple) != 3:
            raise Refused("missing comparable same-expiry strike triple")
        low, center, high = triple
        for contract in triple:
            if (not contract.get("rules_receipt") or contract["direction"] != "greater_than"
                    or any(contract[k] != center[k] for k in ("close_at", "rules_receipt", "reference"))
                    or not 0 <= (utc(row["observation"]["observed_at"]) - utc(contract["observed_at"])).total_seconds() <= 5):
                raise Refused("unproved nested contracts or stale quote")
        if (center["ticker"] != row["observation"]["ticker"] or _mid(center) != mid
                or not decimal(low["strike"]) < decimal(center["strike"]) < decimal(high["strike"])):
            raise Refused("strike ordering/center binding")
        lower, upper = _mid(high), _mid(low)
        if lower > upper:
            raise Refused("neighbors themselves incoherent; no selected repair")
        p = min(upper, max(lower, mid))
    else:
        x = _clip(mid)
        p = 1 / (1 + (-(Decimal("0.9") * (x / (1-x)).ln())).exp())
    return _clip(p)


def paired_metrics(probabilities, baselines, outcomes):
    if not probabilities or not len(probabilities) == len(baselines) == len(outcomes):
        raise Refused("complete nonempty paired cohort required")
    if any(type(y) is not int or y not in (0, 1) for y in outcomes):
        raise Refused("binary authoritative labels required")
    def score(values):
        values = [decimal(p) for p in values]
        if any(not 0 <= p <= 1 for p in values):
            raise Refused("invalid probability")
        brier = sum((p-y)**2 for p, y in zip(values, outcomes)) / len(values)
        # Fixed clipping only for log scoring, also applied to market baseline.
        loss = sum(-math.log(float(_clip(p) if y else 1-_clip(p))) for p, y in zip(values, outcomes))/len(values)
        calibration = []
        for bucket in range(10):
            rows = [(p,y) for p,y in zip(values,outcomes) if min(9,int(p*10)) == bucket]
            if rows:
                calibration.append({"bin":bucket,"n":len(rows),"mean_probability":str(sum(p for p,y in rows)/len(rows)),
                                    "observed_rate":str(Decimal(sum(y for p,y in rows))/len(rows))})
        return {"brier":str(brier),"log_loss":loss,"calibration":calibration}
    candidate, baseline = score(probabilities), score(baselines)
    return {"candidate":candidate,"market":baseline,
            "brier_delta":str(decimal(candidate["brier"])-decimal(baseline["brier"])),
            "log_loss_delta":candidate["log_loss"]-baseline["log_loss"]}


def drawdown(pnls, hypothetical_starting_equity):
    """Explicit simulation equity, never fabricated actual account starting cash."""
    equity = decimal(hypothetical_starting_equity)
    if equity <= 0:
        raise Refused("positive explicit scenario equity required")
    peak, dollars, fraction = equity, Decimal(0), Decimal(0)
    for pnl in pnls:
        equity += decimal(pnl)
        peak = max(peak, equity)
        dollars = max(dollars, peak-equity)
        fraction = max(fraction, (peak-equity)/peak)
    return {"maximum_drawdown_dollars":str(dollars), "maximum_drawdown_fraction":str(fraction)}


def labelled_diagnostics(family, rows, features_by_hash, labels_by_ticker):
    """Full-cohort TRAIN/VALIDATION diagnostics; supplied evidence is not authenticated.

    Useful for historical ideation, never final OOS qualification. Missing any
    row blocks the complete result; no scoring a convenient labelled subset.
    """
    days = sorted({r["day"] for r in rows})
    training_days = set(days[:len(days)*2//3])
    partitions = {"TRAIN": [], "VALIDATION": []}
    for row in rows:
        obs = row["observation"]
        label = labels_by_ticker.get(obs["ticker"])
        if (not label or label.get("close_at") != obs["close_at"]
                or not label.get("source_receipt")
                or utc(label["published_at"]) < utc(obs["close_at"])):
            raise Refused("complete same-contract outcome evidence required")
        p = probability(family, row, features_by_hash.get(obs["hash"]))
        partition = "TRAIN" if row["day"] in training_days else "VALIDATION"
        partitions[partition].append((row, str(p), label["outcome"]))
    output = {}
    for name, values in partitions.items():
        if not values:
            output[name] = None
            continue
        probabilities = [p for r,p,y in values]
        outcomes = [y for r,p,y in values]
        mids, asks = [r["mid"] for r,p,y in values], [r["ask"] for r,p,y in values]
        output[name] = {"midpoint_baseline":paired_metrics(probabilities,mids,outcomes),
                        "ask_baseline":paired_metrics(probabilities,asks,outcomes),
                        "rows":len(values),"utc_days":len({r["day"] for r,p,y in values})}
    output.update({"source_authority":"NOT_VERIFIED", "PREDICTIVE_EDGE":"NOT_QUALIFIED",
                   "EXECUTABLE_EDGE":"NOT_EVALUATED", "candidate_locked":False,
                   "paired_cohort_hash":digest(rows)})
    return output


def economic_diagnostic(quote, probability_value, budget, fee, slippage, at):
    # Same implementation as reserve_shadow, not a permissive copied simulator.
    # No account evidence, reservations or position clearance implied by this call.
    return {"economics":reprice(quote, probability_value, budget, fee, slippage, Limits(), at),
            "portfolio_gate":"NOT_EVALUATED", "would_submit":False, "financial_authority":False}


def run_experiments(observations, registration, snapshot_hash, cutoff):
    """Unlabelled native-cohort feasibility run. No probabilities supplied by caller.

    The current collector does not collect authoritative labels, reference bars,
    synchronized strike ladders or fresh execution receipts. Refuse to invent
    those inputs. paired_metrics is available for separately qualified datasets;
    this runner deliberately cannot silently ingest unverified labels/features.
    """
    if registration.get("plan_hash") != digest(plan()) or registration.get("plan") != plan():
        raise Refused("preregistration differs from executable plan")
    if utc(cutoff) < utc(registration["registered_at"]):
        raise Refused("cutoff before preregistration")
    if any(utc(r["observed_at"]) > utc(cutoff) for r in observations):
        raise Refused("data after fixed cutoff")
    rows, excluded = cohort(observations)
    days = sorted({r["day"] for r in rows})
    split = len(days)*2//3
    train_days, validation_days = days[:split], days[split:]
    experiments = []
    for family in HYPOTHESES:
        predictions, missing = [], Counter()
        for row in rows:
            try:
                p = probability(family, row)
            except Refused as exc:
                missing[str(exc)] += 1
            else:
                predictions.append({"observation_hash":row["observation"]["hash"], "p":str(p),
                                    "market_probability":row["mid"], "ask":row["ask"],
                                    "gross_edge_at_decision_ask":str(p-decimal(row["ask"]))})
        experiments.append({"hypothesis_id":family, "status":"FAIL_EVIDENCE",
            "scientific_rejection":False, "predictions":predictions, "missing_features":dict(missing),
            "paired_rows":0,"brier":None,"log_loss":None,"calibration":None,"baseline_brier":None,
            "delta_vs_baseline":None,"gross_edge":None,"expected_edge_after_fees":None,
            "net_hypothetical_pnl":None,"maximum_drawdown_fraction":None,"opportunity_count":None,
            "unique_markets":len({r["observation"]["ticker"] for r in rows}),
            "distinct_utc_days":len(days),"independent_time_periods":None,
            "PREDICTIVE_EDGE":"NOT_ESTIMABLE","EXECUTABLE_EDGE":"NOT_ESTIMABLE",
            "reasons":["authoritative labels not imported", "execution/cost and portfolio evidence not qualified",
                       "statistical independence and multiplicity-controlled significance not established"],
            "candidate_locked":False,"model_approved":False})
    return {"schema":"atlas-alpha-run/2", "registration_hash":digest(registration),
        "snapshot_hash":snapshot_hash,"cutoff":cutoff,"observations":len(observations),
        "cohort_hash":digest(rows),"cohort_rows":len(rows),"exclusions":excluded,
        "train_days":train_days,"validation_days":validation_days,
        "experiments":experiments,"future_oos_start":None,"candidate_lock":None,
        "verdict":"NO_EDGE_FOUND_IN_CURRENT_HYPOTHESES",
        "interpretation":"No edge established; missing evidence is not statistical rejection."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("preregister")
    register.add_argument("output"); register.add_argument("--code-sha", required=True)
    run = sub.add_parser("run")
    for name in ("snapshot", "anchor", "registration", "output"):
        run.add_argument(name)
    run.add_argument("--cutoff", required=True)
    args = parser.parse_args()
    if args.command == "preregister":
        preregister(args.output, args.code_sha)
    else:
        snapshot = strict_json(Path(args.snapshot).read_bytes())
        rows = observations_from_snapshot(snapshot, strict_json(Path(args.anchor).read_bytes()))
        result = run_experiments(rows, strict_json(Path(args.registration).read_bytes()), digest(snapshot), args.cutoff)
        with open(args.output, "xb") as file:
            file.write(canonical(result))


if __name__ == "__main__":
    main()
