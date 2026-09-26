"""Prospective frozen-hypothesis observer; no allocation, trainer or promotion.

The append-only ledger separates decisions, later knowledge and invalidations.
Quoted paths are sparse observations, never fills or a claim of true extrema.
"""
from datetime import timedelta
import re
import threading

from .alpha_lab import HYPOTHESES, cohort, plan, probability
from .domain import Refused, decimal, digest, hash_id, now, utc
from .execution import Limits
from .qualification import reference, candles, ladder, refreshed, fee_evidence, settlement

BLOCKERS = ["MODEL_APPROVAL_MISSING", "RECONCILIATION_EVIDENCE_MISSING",
            "FEE_CLASS_UNQUALIFIED", "SLIPPAGE_UNQUALIFIED", "SIMULATION_ALLOCATION_MISSING"]
RETRAIN_BLOCKERS = ["QUALIFIED_SETTLED_TRAINING_DATA_MISSING",
                    "INDEPENDENT_TRAIN_VALIDATION_SPLIT_UNQUALIFIED",
                    "FROZEN_TRAINING_RECIPE_MISSING", "QUALIFIED_ECONOMIC_REWARD_DATA_MISSING"]
BYPASS_GUARDS = {"reconciliation", "price_cap", "spread_limit", "liquidity",
                 "duplicate_protection", "drawdown", "model_approval"}


def _events(store, kinds, after=0, limit=512, latest=False):
    with store.mutex:
        rows = store.db.execute(
            "SELECT * FROM events WHERE kind IN (" + ",".join("?" for _ in kinds) +
            ") AND seq>? ORDER BY seq " + ("DESC" if latest else "ASC") + " LIMIT ?",
            (*kinds, after, limit)).fetchall()
        return [store._decode(r) for r in rows]


def _raw(store, value, event, at):
    with store.mutex:
        row = store.db.execute("SELECT * FROM events WHERE hash=?", (value,)).fetchone()
        raw = store._decode(row)
    if (not raw or raw["kind"] != "Q_RAW" or raw["seq"] >= event["seq"]
            or utc(raw["payload"]["received_at"]) > utc(event["recorded_at"])
            or utc(raw["recorded_at"]) > utc(at)
            or utc(raw["payload"]["received_at"]) > utc(at)):
        raise Refused("missing earlier available raw receipt")
    return raw


class LearningObserver:
    def __init__(self, store, source_sha):
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise Refused("exact source SHA required")
        self.store, self.source_sha, self.lock = store, source_sha, threading.RLock()
        activation = store.get("learning:activation")
        if activation is None:
            activation = store.append("learning:activation", "L_ACTIVATION", {
                "activated_at": now(), "source_sha": source_sha, "plan_hash": digest(plan()),
                "mode": "LIVE_MARKET_LEARNING", "capital": "OFF", "broker_writes": 0,
                "real_orders_submitted": 0, "scope": plan()["scope"], "active_models": [],
                "champion": None, "self_promotion": False})
        if activation["payload"]["plan_hash"] != digest(plan()):
            raise Refused("learning plan differs from original activation")
        self.activation = activation["payload"]
        self.decisions = {e["event_id"]: e for e in store.events("L_DECISION")}
        self.seen = {e["payload"]["event_id"] for e in store.events("L_EXCLUSION")}
        self.seen.update(e["payload"]["event_id"] for e in self.decisions.values())
        self.disqualified = {e["payload"]["candidate_hash"] for e in store.events("L_DISQUALIFICATION")}
        self.invalid = {e["payload"]["decision_id"] for e in store.events("L_INVALIDATION")}
        self.outcomes = {e["payload"]["decision_id"]: e for e in store.events("L_SETTLEMENT")}
        self.paths = {e["payload"]["observation_hash"] for e in store.events("L_PATH")}
        cursor = store.latest("L_LABEL_CURSOR")
        self.cursor = cursor["payload"]["sequence"] if cursor else 0

    def _sources(self, observation, supplements, at, event_hashes=None):
        """Reconstruct bounded recent evidence; normalized payloads cannot attest."""
        features, refs, bars, refresh, fee, errors, provenance = {}, [], [], None, None, [], []
        eligible = _events(supplements, ("Q_REFERENCE", "Q_CANDLES", "Q_LADDER", "Q_REFRESH", "Q_FEE"), latest=True)
        if event_hashes is not None:
            if len(event_hashes)>512: raise Refused("feature receipt bound")
            with supplements.mutex:
                eligible = [supplements._decode(supplements.db.execute("SELECT * FROM events WHERE hash=?",(h,)).fetchone()) for h in event_hashes]
            if any(e is None for e in eligible): raise Refused("feature receipt missing")
            eligible.sort(key=lambda e:e["seq"],reverse=True)
        for event in reversed(eligible):
            p, kind = event["payload"], event["kind"]
            if utc(event["recorded_at"]) > utc(at):
                continue
            if kind in {"Q_LADDER", "Q_REFRESH", "Q_FEE"} and p.get("decision_hash") != observation["hash"]:
                continue
            try:
                raw = lambda h: _raw(supplements, h, event, at)
                if kind == "Q_REFERENCE":
                    value = reference(raw(p["receipt"]))
                    if p != value: raise Refused("derived reference changed")
                    if utc(value["received_at"]) <= utc(observation["observed_at"]): refs.append(value)
                elif kind == "Q_CANDLES":
                    receipt = raw(p["closed_candles"][0]["receipt"])
                    value = candles(receipt, p["end"])
                    if value != p: raise Refused("derived candle evidence changed")
                    if utc(receipt["payload"]["received_at"]) <= utc(observation["observed_at"]): bars.append(value)
                elif kind == "Q_LADDER":
                    value = {"decision_hash": observation["hash"], **ladder([raw(h) for h in p["pages"]], observation)}
                    if value != p: raise Refused("derived ladder changed")
                    features["strike_triple"] = value["strike_triple"]
                elif kind == "Q_REFRESH":
                    value = refreshed(raw(p["receipt_hash"]), observation)
                    if value != p: raise Refused("derived quote changed")
                    refresh = value
                else:
                    value = {"decision_hash": observation["hash"], "price": observation["ask"],
                             **fee_evidence(raw(p["series_receipt"]), raw(p["schedule_receipt"]), observation["ask"])}
                    if value != p: raise Refused("derived fees changed")
                    # Preserve raw fee receipts; recompute at refreshed execution price below.
                    fee = (raw(p["series_receipt"]), raw(p["schedule_receipt"]))
                provenance.append(event["hash"])
            except (Refused, KeyError, TypeError, IndexError) as exc:
                errors.append(kind + ":" + str(exc)[:160])
        observed = utc(observation["observed_at"])
        current = [r for r in refs if 0 <= (observed-utc(r["at"])).total_seconds() <= 5]
        if current:
            newest = max(current, key=lambda r: utc(r["at"]))
            previous = [r for r in refs if (utc(newest["at"])-utc(r["at"])).total_seconds() == 60]
            if len({r["price"] for r in current if r["at"] == newest["at"]}) == 1 and len({r["price"] for r in previous}) == 1:
                features.update(reference_now=newest, reference_previous=previous[0])
        bars = [b for b in bars if 0 <= (observed-utc(b["end"])).total_seconds() <= 60]
        if bars: features["closed_candles"] = max(bars, key=lambda b: utc(b["end"]))["closed_candles"]
        cost = None
        if fee and refresh:
            if all(utc(r["payload"]["received_at"]) <= utc(refresh["observed_at"]) for r in fee):
                cost = fee_evidence(*fee, refresh["ask"])
            else: errors.append("Q_FEE:fee metadata unavailable at refresh")
        return features, refresh, cost, errors, provenance

    def observe(self, observations, qualification_store):
        with self.lock:
            at = now()
            rows, _ = cohort(observations)
            pending = [r for r in rows if r["observation"]["event_id"] not in self.seen]
            # Fresh records first; old cohorts receive explicit exclusions in bounded batches.
            pending.sort(key=lambda r: utc(r["observation"]["observed_at"]), reverse=True)
            for row in pending[:64]:
                o = row["observation"]
                reason = None
                if utc(o["observed_at"]) < utc(self.activation["activated_at"]): reason = "PRE_ACTIVATION_COHORT"
                elif not 0 <= (utc(at)-utc(o["observed_at"])).total_seconds() <= 5: reason = "DECISION_NOT_FRESH"
                elif utc(at) >= utc(o["close_at"]): reason = "DECISION_AFTER_CLOSE"
                if reason:
                    self.store.append("learning:exclude:" + digest(o["event_id"]), "L_EXCLUSION", {
                        "event_id": o["event_id"], "observation_hash": o["hash"], "reason": reason,
                        "observed_at": o["observed_at"], "checked_at": at})
                    self.seen.add(o["event_id"])
                    continue
                features, quote, fee, errors, provenance = self._sources(o, qualification_store, at)
                recorded = {}
                with self.store.transaction():
                    for family in HYPOTHESES:
                        candidate = digest({"source_sha": self.source_sha, "family": family, "plan_hash": digest(plan())})
                        p, missing = None, None
                        try: p = str(probability(family, row, features))
                        except Refused as exc: missing = str(exc)
                        reasons = list(BLOCKERS)
                        if missing: reasons.append("MISSING_FEATURES:" + missing)
                        if candidate in self.disqualified: reasons.append("CANDIDATE_DISQUALIFIED")
                        if not quote: reasons.append("REFRESH_EVIDENCE_MISSING")
                        else:
                            if decimal(quote["ask"]) > decimal(Limits().max_price): reasons.append("PRICE_CAP")
                            if decimal(quote["spread"]) > decimal(Limits().max_spread): reasons.append("SPREAD_LIMIT")
                            if decimal(quote["available"]) < 1: reasons.append("LIQUIDITY")
                        if not fee: reasons.append("FEE_EVIDENCE_MISSING")
                        payload = {
                            "decision_timestamp": at, "market_observed_at": o["observed_at"],
                            "decision_age_seconds": (utc(at)-utc(o["observed_at"])).total_seconds(),
                            "refreshed_age_seconds": (utc(at)-utc(quote["observed_at"])).total_seconds() if quote else None,
                            "stale_price_exposure": None, "stale_exposure_reason": "NO_ACCEPTED_EXECUTION",
                            "ticker": o["ticker"], "market": o["ticker"], "event_id": o["event_id"], "domain": "BTC_15M",
                            "source_sha": self.source_sha, "candidate_version": candidate, "candidate_hash": candidate,
                            "model_version": family, "model_probability": p, "probability_missing_reason": missing,
                            "market_probability": row["mid"], "market_ask_baseline": row["ask"],
                            "decision_bid": o["bid"], "decision_ask": o["ask"],
                            "decision_spread": str(decimal(o["ask"])-decimal(o["bid"])),
                            "refreshed_bid": quote["bid"] if quote else None, "refreshed_ask": quote["ask"] if quote else None,
                            "spread": quote["spread"] if quote else None, "fees": fee["fee_bound"] if fee else None,
                            "liquidity": quote["available"] if quote else None,
                            "liquidity_kind": "DISPLAYED_ONLY_NOT_FILL" if quote else "MISSING",
                            "fee_evidence": fee, "slippage_assumption": quote["slippage_assumption"] if quote else None,
                            "slippage_policy": quote["slippage_policy"] if quote else None,
                            "intended_size": 1, "hypothetical_size": 0, "accepted_size": 0,
                            "settlement_result": None, "hypothetical_realized_pnl": None, "economic_reward": None,
                            "maximum_adverse_excursion": None, "maximum_favorable_excursion": None,
                            "excursion_reason": "NO_ACCEPTED_EXECUTION; observed paths are sparse, not true extrema",
                            "outcome_reason": "PENDING_AUTHORITATIVE_SETTLEMENT", "pnl_reason": "REJECTED_NO_EXECUTION",
                            "acceptance": "REJECTED", "rejection_reasons": reasons, "acceptance_reason": None,
                            "would_submit": False, "model_approved": False, "prospective_oos_qualified": False,
                            "observation": o, "observation_hash": o["hash"], "source_errors": errors,
                            "qualification_anchor": qualification_store.anchor(), "source_evidence": provenance,
                            "features_hash": digest({"cohort":{k:v for k,v in row.items() if k != "observation"},"sources":features}), "feature_snapshot": features,
                            "cohort_snapshot": {k:v for k,v in row.items() if k != "observation"},
                            "plan_hash": digest(plan()), "champion": None}
                        if getattr(self,"phase2",None): payload["training_protocol_hash"] = self.phase2.protocol_hash
                        identity = "learning:decision:" + digest([o["event_id"], family])
                        written = self.store.append(identity, "L_DECISION", payload)
                        if getattr(self,"phase2",None): self.phase2.capture_challengers(written, at)
                        commit_at = utc(now())
                        if (commit_at >= utc(o["close_at"]) or
                                not 0 <= (commit_at-utc(o["observed_at"])).total_seconds() <= 5 or
                                not utc(self.activation["activated_at"]) <= utc(written["recorded_at"]) <= commit_at):
                            raise Refused("decision missed prospective append deadline")
                        recorded[identity] = written
                self.decisions.update(recorded)
                self.seen.add(o["event_id"])
            self._paths(observations, at)
            self._checkpoints(at)
            return self.status()

    def _paths(self, observations, at):
        by_ticker = {}
        for identity, event in self.decisions.items():
            by_ticker.setdefault(event["payload"]["ticker"], []).append((identity, event["payload"]))
        fresh = [o for o in observations if o["hash"] not in self.paths and o["ticker"] in by_ticker]
        for o in sorted(fresh, key=lambda o: utc(o["observed_at"]), reverse=True)[:512]:
            ids = [i for i, d in by_ticker[o["ticker"]] if utc(d["decision_timestamp"]) < utc(o["observed_at"]) <= min(utc(at), utc(d["observation"]["close_at"]))]
            if not ids: continue
            self.store.append("learning:path:" + o["hash"], "L_PATH", {"observation_hash": o["hash"],
                "decision_ids": sorted(ids), "observed_at": o["observed_at"], "bid": o["bid"], "ask": o["ask"],
                "provenance": {k:o.get(k) for k in ("receipt_hash", "scan_hash", "raw_market_hash")},
                "sampling": "OBSERVED_QUOTES_ONLY_NO_FILL_OR_TRUE_EXTREMA"})
            self.paths.add(o["hash"])

    def settle(self, observations, qualification_store):
        with self.lock:
            at = now()
            labels = _events(qualification_store, ("Q_LABEL",), after=self.cursor, limit=256)
            for event in labels:
                if utc(event["recorded_at"]) > utc(at): break
                relevant = [(i,e) for i,e in self.decisions.items() if e["payload"]["ticker"] == event["payload"].get("ticker")]
                for identity, decision in relevant:
                    d = decision["payload"]
                    try:
                        raw = _raw(qualification_store, event["payload"]["source_receipt"], event, at)
                        label = settlement(raw, d["observation"])
                        if label != event["payload"]: raise Refused("derived settlement changed")
                        if utc(at) <= utc(d["decision_timestamp"]): raise Refused("settlement knowledge not after decision")
                    except (Refused, KeyError, TypeError) as exc:
                        self.store.append("learning:label-rejected:" + digest([identity, event["hash"]]), "L_LABEL_REJECTED",
                                          {"decision_id": identity, "source_event": event["hash"], "reason": str(exc)[:200]})
                        continue
                    prior = self.outcomes.get(identity)
                    if prior:
                        old = prior["payload"]["label"]
                        if (old["outcome"], old["settlement_at"]) != (label["outcome"], label["settlement_at"]):
                            self.store.append("learning:invalidate:" + digest([identity, event["hash"]]), "L_INVALIDATION", {
                                "decision_id": identity, "reason": "CONFLICTING_AUTHORITATIVE_LABELS", "knowledge_at": at,
                                "earlier_settlement_hash": prior["hash"], "conflicting_label": label,
                                "decision_valid": False, "reward_valid": False, "permanent": True})
                            self.invalid.add(identity)
                        continue
                    y = label["outcome"]
                    base = (decimal(d["market_probability"])-y)**2
                    model = (decimal(d["model_probability"])-y)**2 if d["model_probability"] is not None else None
                    payload = {"decision_id": identity, "decision_hash": decision["hash"], "knowledge_at": at,
                        "label": label, "source_event": event["hash"], "settlement_result": y,
                        "market_brier": str(base), "model_brier": str(model) if model is not None else None,
                        "brier_improvement": str(base-model) if model is not None else None,
                        "diagnostic_only": True,
                        "predictive_training_eligible_at_knowledge_time": model is not None and d["candidate_hash"] not in self.disqualified and identity not in self.invalid,
                        "eligibility_policy": "current invalidation/disqualification ledger overrides this historical snapshot",
                        "economic_reward": None, "hypothetical_realized_pnl": None,
                        "settlement_receipt_hash": label["source_receipt"], "authoritative_outcome": y,
                        "gross_pnl": None, "fees_realized": None, "slippage_realized": None, "net_pnl": None,
                        "brier_contribution": str(model) if model is not None else None,
                        "prediction_residual": str(decimal(d["model_probability"])-y) if model is not None else None,
                        "calibration_error": None, "calibration_reason": "AGGREGATE_ECE_IN_PHASE2_REPORT",
                        "drawdown_contribution": None,
                        "pnl_reason": "REJECTED_NO_EXECUTION", "maximum_adverse_excursion": None,
                        "maximum_favorable_excursion": None, "excursion_reason": "NO_ACCEPTED_EXECUTION",
                        "qualification_anchor": qualification_store.anchor()}
                    self.outcomes[identity] = self.store.append("learning:settlement:" + digest(identity), "L_SETTLEMENT", payload)
                self.cursor = event["seq"]
            if labels and self.cursor:
                identity = "learning:label-cursor:" + str(self.cursor)
                if not self.store.get(identity): self.store.append(identity, "L_LABEL_CURSOR", {"sequence": self.cursor})
            self._paths(observations, at)
            self._checkpoints(at)
            return self.status()

    def disqualify(self, candidate_hash, reason, evidence):
        """An explicit evidenced bypass finding; guard rejection is not a bypass."""
        with self.lock:
            hash_id(candidate_hash)
            if reason not in BYPASS_GUARDS or not isinstance(evidence, str) or not evidence.strip():
                raise Refused("named bypass guard and evidence required")
            value = {"candidate_hash": candidate_hash, "guard": reason, "evidence": evidence,
                     "permanent": True, "self_reapproval": False}
            event = self.store.append("learning:disqualify:" + digest(value), "L_DISQUALIFICATION", value)
            self.disqualified.add(candidate_hash)
            return event

    def _checkpoints(self, at):
        if getattr(self,"phase2",None): return
        first = utc(self.activation["activated_at"]).date() + timedelta(days=1)
        latest = self.store.latest("L_DATASET_CHECKPOINT")
        if latest: first = utc(latest["payload"]["day"]+"T00:00:00Z").date() + timedelta(days=1)
        # Bounded catchup, with actual knowledge cutoff; never backdate late labels.
        for offset in range(7):
            day = first + timedelta(days=offset)
            if day >= utc(at).date(): break
            members = sorted(e["hash"] for e in self.decisions.values() if utc(e["payload"]["market_observed_at"]).date() == day)
            invalid = sorted(i for i in self.invalid)
            payload = {"day": day.isoformat(), "knowledge_cutoff": at, "decision_hashes": members,
                "dataset_anchor": self.store.anchor(), "invalid_decisions": invalid,
                "disqualified_candidate_hashes": sorted(self.disqualified),
                "eligible_predictive_decision_ids": sorted(i for i in self._eligible() if self.decisions[i]["hash"] in members),
                "settlement_hashes": sorted(e["hash"] for e in self.outcomes.values()),
                "retraining_status": "BLOCKED", "retraining_reasons": RETRAIN_BLOCKERS,
                "trainer_started": False, "challenger_created": False, "active_candidate_modified": False,
                "promotion": False}
            self.store.append("learning:checkpoint:" + day.isoformat(), "L_DATASET_CHECKPOINT", payload)

    def _eligible(self):
        return {identity for identity in self.outcomes if identity not in self.invalid
                and self.decisions[identity]["payload"]["candidate_hash"] not in self.disqualified
                and self.decisions[identity]["payload"]["model_probability"] is not None}

    def status(self):
        with self.lock:
            result = {"mode": "LIVE_MARKET_LEARNING", "learner_enabled": True,
                "activated_at": self.activation["activated_at"], "capital": "OFF", "broker_writes": 0,
                "real_orders_submitted": 0, "active_models": [], "champion": None, "model_approved": False,
                "hypothesis_observers": list(HYPOTHESES), "decisions": len(self.decisions),
                "accepted_decisions": 0, "settled_decisions": len(self.outcomes), "invalid_decisions": len(self.invalid),
                "valid_labelled_decisions": len(set(self.outcomes)-self.invalid),
                "excluded_cohorts": len(self.seen)-len({e["payload"]["event_id"] for e in self.decisions.values()}),
                "disqualified_candidates": len(self.disqualified), "economic_reward": None,
                "eligible_predictive_predictions": len(self._eligible()), "eligibility_is_training_approval": False,
                "retraining_status": "BLOCKED", "retraining_blocking_reasons": RETRAIN_BLOCKERS,
                "execution_blocking_reasons": BLOCKERS, "blocking_reasons": BLOCKERS,
                "self_promotion": False, "learning_anchor": self.store.anchor()}
            if getattr(self,"phase2",None):
                result["phase2"] = self.phase2.status()
                result["retraining_status"] = result["phase2"]["status"]
                result["retraining_blocking_reasons"] = result["phase2"]["blocking_reasons"]
            return result
