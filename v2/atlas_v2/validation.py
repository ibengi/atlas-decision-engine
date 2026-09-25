"""Prospective research firewall. Metrics never grant financial approval."""
from decimal import Decimal
import math

from .domain import Refused, decimal, digest, hash_id, utc, now

CONSUMED_DATASETS = frozenset({
    "914cd127984769573450b91559a039c7e5672d135e340ab1976323ddffec38b1",
    "02b49988ab47f436cd4858866652e957dde458e9b64a9bea14865853d5338260",
})
LOCK_FIELDS = frozenset({"source", "parameters", "features", "thresholds", "config"})


def register_hypothesis(store, hypothesis_id, declaration):
    required = {"hypothesis", "universe", "inclusion_rule", "split_rule", "baseline_rule",
                "cost_rule", "stopping_rule", "multiplicity_rule", "min_periods", "min_markets"}
    if set(declaration) != required:
        raise Refused("complete preregistration required")
    if any(not isinstance(declaration[k], str) or not declaration[k].strip()
           for k in required - {"min_periods", "min_markets"}):
        raise Refused("empty preregistration")
    if any(type(declaration[k]) is not int or declaration[k] < 2
           for k in ("min_periods", "min_markets")):
        raise Refused("independent periods/markets must be predeclared")
    return store.append("hypothesis:" + hypothesis_id, "HYPOTHESIS", declaration)


def lock_candidate(store, hypothesis_id, bindings, training_manifest, validation_manifest):
    if set(bindings) != LOCK_FIELDS:
        raise Refused("incomplete candidate fingerprint")
    for value in bindings.values():
        hash_id(value)
    for manifest in (training_manifest, validation_manifest):
        if set(manifest) != {"dataset_hash", "events", "label_available_through"}:
            raise Refused("split manifest incomplete")
        hash_id(manifest["dataset_hash"])
        if not manifest["events"] or len(set(manifest["events"])) != len(manifest["events"]):
            raise Refused("event grouping required")
        utc(manifest["label_available_through"])
    if set(training_manifest["events"]) & set(validation_manifest["events"]):
        raise Refused("train/validation event leakage")
    with store.transaction():
        registration = store.get("hypothesis:" + hypothesis_id)
        if not registration:
            raise Refused("hypothesis not registered")
        payload = {"hypothesis": registration["hash"], "bindings": bindings,
                   "training": training_manifest, "validation": validation_manifest,
                   "consumed_datasets": sorted(CONSUMED_DATASETS)}
        result = store.append("lock:" + hypothesis_id, "LOCK", payload)
        if any(utc(m["label_available_through"]) >= utc(result["recorded_at"])
               for m in (training_manifest, validation_manifest)):
            raise Refused("future training labels")
        return result


def record_prediction(store, lock_id, observation_id, probability, baseline):
    p, b = decimal(probability), decimal(baseline)
    if not (0 < p < 1 and 0 < b < 1):
        raise Refused("strict probabilities required; no silent clipping")
    with store.transaction():
        lock, observation = store.get(lock_id), store.get(observation_id)
        if not lock or lock["kind"] != "LOCK" or not observation or observation["kind"] != "OBSERVATION":
            raise Refused("persisted lock and observation required")
        obs = observation["payload"]
        if (utc(observation["recorded_at"]) <= utc(lock["recorded_at"])
                or utc(obs["observed_at"]) <= utc(lock["recorded_at"])
                or utc(obs["close_at"]) <= utc(now())
                or utc(obs["observed_at"]) > utc(now())
                or obs["event_id"] in lock["payload"]["training"]["events"]
                or obs["event_id"] in lock["payload"]["validation"]["events"]):
            raise Refused("not genuinely future or event leakage")
        if b != decimal(obs["baseline_probability"]):
            raise Refused("market baseline must use identical observation")
        result = store.append("prediction:" + lock["hash"] + ":" + observation_id, "PREDICTION",
                            {"lock": lock["hash"], "observation": observation["hash"],
                             "observation_id": observation_id, "p": str(p), "baseline": str(b)})
        if utc(result["recorded_at"]) >= utc(obs["close_at"]):
            raise Refused("prediction persisted after market closed")
        return result


def evaluate_future(store, lock_id, prediction_ids, labels, dataset_hash):
    """Labels are separately qualified inputs; no broker authority is fabricated.

    The caller must independently authenticate receipts before scientific use.
    This evaluator reports a diagnostic, always approved=false.
    """
    hash_id(dataset_hash)
    if dataset_hash in CONSUMED_DATASETS or not prediction_ids or len(set(prediction_ids)) != len(prediction_ids):
        raise Refused("consumed, empty or repeated cohort")
    lock = store.get(lock_id)
    if not lock or lock["kind"] != "LOCK":
        raise Refused("persisted candidate lock required")
    complete_cohort = [e["event_id"] for e in store.events("PREDICTION") if e["payload"]["lock"] == lock["hash"]]
    if prediction_ids != complete_cohort:
        raise Refused("result-dependent cohort selection refused")
    if set(labels) != set(prediction_ids):
        raise Refused("labels must match the entire predeclared cohort")
    rows, markets, periods, events = [], set(), set(), set()
    bins = [[] for _ in range(10)]
    for prediction_id in prediction_ids:
        prediction = store.get(prediction_id)
        if not prediction or prediction["kind"] != "PREDICTION" or prediction["payload"]["lock"] != lock["hash"]:
            raise Refused("candidate lineage mismatch")
        observation = store.get(prediction["payload"]["observation_id"])
        obs, label = observation["payload"], labels[prediction_id]
        if (set(label) != {"outcome", "authority_receipt", "settled_at", "period", "fee", "slippage", "gross_pnl"}
                or type(label["outcome"]) is not int or label["outcome"] not in (0, 1)):
            raise Refused("invalid authoritative label")
        hash_id(label["authority_receipt"])
        if (utc(label["settled_at"]) <= utc(prediction["recorded_at"])
                or utc(label["settled_at"]) > utc(now())
                or utc(label["settled_at"]) < utc(obs["close_at"])):
            raise Refused("outcome already known at prediction")
        if obs["event_id"] in events:
            raise Refused("one independent event per final scoring cohort")
        events.add(obs["event_id"])
        markets.add(obs["ticker"])
        if not isinstance(label["period"], str) or not label["period"]:
            raise Refused("predeclared independent period required")
        periods.add(label["period"])
        p, b = decimal(prediction["payload"]["p"]), decimal(prediction["payload"]["baseline"])
        y = Decimal(label["outcome"])
        fee, slip, gross = (decimal(label[x]) for x in ("fee", "slippage", "gross_pnl"))
        if fee < 0 or slip < 0:
            raise Refused("negative costs")
        rows.append((p, b, y, fee, slip, gross))
        bins[min(9, int(p * 10))].append((p, y))
    actual_hash = digest({"lock": lock["hash"], "predictions": prediction_ids, "labels": labels})
    if actual_hash != dataset_hash:
        raise Refused("dataset hash does not bind calculation inputs")
    n = Decimal(len(rows))
    model_brier = sum((p - y) ** 2 for p, b, y, f, s, g in rows) / n
    market_brier = sum((b - y) ** 2 for p, b, y, f, s, g in rows) / n
    def loss(index):
        return -sum(float(row[2]) * math.log(float(row[index])) +
                    (1 - float(row[2])) * math.log(1 - float(row[index])) for row in rows) / len(rows)
    registration = next(e for e in store.events("HYPOTHESIS") if e["hash"] == lock["payload"]["hypothesis"])
    enough = len(markets) >= registration["payload"]["min_markets"] and len(periods) >= registration["payload"]["min_periods"]
    return {"dataset_hash": actual_hash, "lock_hash": lock["hash"], "count": len(rows),
            "markets": len(markets), "periods": len(periods), "brier": str(model_brier),
            "market_brier": str(market_brier), "delta": str(model_brier - market_brier),
            "log_loss": loss(0), "market_log_loss": loss(1),
            "calibration": [{"count": len(bucket), "mean_p": str(sum(p for p, y in bucket) / len(bucket)),
                             "observed": str(sum(y for p, y in bucket) / len(bucket))} for bucket in bins if bucket],
            "fees": str(sum(r[3] for r in rows)), "slippage": str(sum(r[4] for r in rows)),
            "net_pnl": str(sum(r[5] - r[3] - r[4] for r in rows)),
            "diagnostic_direction_pass": bool(enough and model_brier < market_brier),
            "approved": False,
            "remaining": ["independent receipt authentication", "predeclared stopping/multiplicity verification",
                          "execution cost qualification", "independent review", "external approval authority"]}
