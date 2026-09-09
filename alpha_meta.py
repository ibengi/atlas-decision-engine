"""Meta Alpha Engine: ensemble, disagreement, uncertainty, shadow edge.

Alpha Gateway v1, sections 8-11, 15, 16. SHADOW ONLY -- every number here
is an estimate of what a trade WOULD have been worth. Nothing in this module
sizes, prices or submits anything, and it imports no execution code.

WEIGHTING (section 8)
    Permanent equal weighting is refused, and so is early
    performance-chasing. A weight has two parts:

      prior   what we are willing to assume before evidence exists. Equal
              across models, capped so no single LLM can dominate.
      earned  a multiplier from THIS model's calibration in THIS market
              category, and only once it has enough resolved predictions to
              mean something (`ALPHA_CALIBRATION_MIN_SAMPLES`). Below that
              threshold the multiplier is exactly 1.0: twenty resolved
              markets is a mood, not a calibration.

    The per-signal quality terms (evidence, completeness, freshness,
    latency, interval width) then modulate the weight downward only. A model
    cannot argue its way to more weight by claiming high confidence: high
    confidence with a wide interval is worth less, not more.

    Finally the weights are capped and floored and renormalised. The cap is
    what stops one talkative model from becoming the ensemble; the floor is
    what stops a model from being weighted out of existence by a run of bad
    luck before it has a real sample.

DISAGREEMENT (section 9)
    Dispersion is reported, not smoothed away. It reduces ensemble
    confidence and, past a threshold, becomes the opportunity's own state
    (`MODEL_DISAGREEMENT`). Four models that disagree sharply are telling us
    the market is genuinely uncertain; forcing them to a consensus number
    would destroy the only honest thing about that situation.

EDGE (sections 10-11)
    `raw_edge` is the naive `P_META - ask`. `shadow_net_edge` is what is
    left after spread, fees, slippage, uncertainty, latency and the
    inference cost of producing the opinion. The second number is the one
    that answers the question this subsystem exists for; the first is kept
    only so the difference between them is visible.
"""

import logging
import math

from config import CFG

log = logging.getLogger("ALPHA")

SIDE_YES = "yes"
SIDE_NO = "no"


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def dispersion(probabilities) -> float:
    """Population standard deviation of the valid estimates.

    Population rather than sample: these are the estimates we actually have,
    not a sample from a larger pool of models we might have asked. With one
    estimate the dispersion is 0.0, which is honest -- there is no
    disagreement in a single opinion -- and the `MIN_VALID_MODELS` gate is
    what stops a lone model from being read as consensus.
    """
    values = [float(p) for p in probabilities]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def freshness_factor(signal, now, snapshot) -> float:
    """1.0 for an answer that is fully inside its validity, decaying to a
    floor as it approaches expiry. An expired signal never reaches here --
    the validator already excluded it."""
    try:
        effective = snapshot.effective_valid_until(signal.valid_until_utc)
    except Exception:                                         # noqa: BLE001
        return 1.0
    total = (effective - snapshot.snapshot_time).total_seconds()
    if total <= 0:
        return 1.0
    left = (effective - now).total_seconds()
    return _clamp(left / total, 0.25, 1.0)


def latency_factor(signal, snapshot) -> float:
    """A model that used most of its budget answered a staler market than
    one that answered immediately. Mild, and never zero."""
    from alpha_snapshot import analysis_budget_seconds
    budget_ms = analysis_budget_seconds(snapshot.market_class) * 1000.0
    if budget_ms <= 0:
        return 1.0
    used = _clamp(float(signal.analysis_latency_ms) / budget_ms, 0.0, 1.0)
    return _clamp(1.0 - 0.3 * used, 0.7, 1.0)


def interval_factor(signal) -> float:
    """Section 11: a wide interval contributes less actionable confidence.

    p=0.52 [0.38, 0.66] and p=0.52 [0.49, 0.55] are not the same claim, and
    weighting them equally is how an ensemble ends up confidently wrong.
    """
    width = signal.interval_width
    if width is None:
        return 0.5
    return _clamp(1.0 - float(width), 0.2, 1.0)


def calibration_multiplier(model: str, category: str, history) -> float:
    """Earned weight from resolved history, or exactly 1.0.

    `history.calibration(model, category)` returns `{"samples": n,
    "brier": x}` or None. Below `ALPHA_CALIBRATION_MIN_SAMPLES` the answer
    is 1.0 -- no credit and no penalty -- because a weight learned from a
    handful of resolutions is noise dressed as evidence.

    Above it, the multiplier is referenced to the Brier score of a
    always-0.5 forecaster (0.25): better than that earns up to 1.5x, worse
    decays toward 0.5x. Bounded on both sides so one bad category cannot
    silence a model everywhere.
    """
    if history is None:
        return 1.0
    try:
        record = history.calibration(model, category)
    except Exception:                                         # noqa: BLE001
        return 1.0
    if not record:
        return 1.0
    samples = int(record.get("samples") or 0)
    brier = record.get("brier")
    if samples < int(CFG.ALPHA_CALIBRATION_MIN_SAMPLES) or brier is None:
        return 1.0
    if not math.isfinite(float(brier)):
        return 1.0
    # 0.25 is the reference (a constant 0.5 forecaster). Lower is better.
    ratio = _clamp(1.0 + (0.25 - float(brier)) * 2.0, 0.5, 1.5)
    return ratio


def model_weights(signals, snapshot, now, *, history=None) -> dict:
    """Final normalised weight per model. Capped, floored, renormalised."""
    if not signals:
        return {}
    raw = {}
    for signal in signals:
        prior = 1.0
        earned = calibration_multiplier(signal.model, snapshot.market_class,
                                        history)
        quality = (_clamp(signal.evidence_quality)
                   * _clamp(signal.data_completeness))
        # A model with zero self-reported quality is not silenced entirely:
        # it reported honestly, and the floor below keeps it in the mix.
        raw[signal.model] = (prior * earned
                             * max(quality, 0.1)
                             * freshness_factor(signal, now, snapshot)
                             * latency_factor(signal, snapshot)
                             * interval_factor(signal))
    total = sum(raw.values())
    if total <= 0:
        equal = 1.0 / len(raw)
        return {model: equal for model in raw}
    weights = {model: value / total for model, value in raw.items()}
    return _cap_and_floor(weights)


def _cap_and_floor(weights: dict) -> dict:
    """Enforce the bootstrap policy: no model dominates, none disappears.

    Water-filling, not clip-and-renormalise. Clipping to the cap and then
    renormalising the whole set re-inflates the value that was just clipped,
    so the result converges to slightly ABOVE the cap and the policy is
    quietly violated by a fraction of a percent. Here a model that hits a
    bound is PINNED there and the remaining mass is redistributed among the
    models that are still free, which terminates exactly on the bound.
    """
    cap = float(CFG.ALPHA_MAX_MODEL_WEIGHT)
    floor = float(CFG.ALPHA_MIN_MODEL_WEIGHT)
    n = len(weights)
    if n == 0:
        return {}
    # A cap below the equal share is arithmetically unsatisfiable: two
    # models cannot both be <= 0.4 and still sum to 1. Relax it ONLY then,
    # and to the midpoint between the equal share and 1.0 rather than to the
    # equal share itself -- pinning every model at exactly 1/n would force
    # perfect uniformity, which is the permanent equal weighting section 8
    # forbids. With the shipped four providers the configured cap is
    # feasible (0.40 >= 0.25) and is used unchanged.
    if cap < 1.0 / n:
        cap = (1.0 / n + 1.0) / 2.0
    if floor > 1.0 / n:
        floor = 1.0 / (2.0 * n)

    current = ({m: w / sum(weights.values()) for m, w in weights.items()}
               if sum(weights.values()) > 0 else {m: 1.0 / n for m in weights})
    # Clamp, then redistribute the shortfall or excess among the models that
    # still have room in that direction, in proportion to what they already
    # hold. Iterated because giving mass to a free model can push it into a
    # bound, which frees less room than the pass assumed. Terminates in at
    # most one pass per model: every pass pins at least one more.
    for _ in range(n + 2):
        clamped = {m: _clamp(w, floor, cap) for m, w in current.items()}
        diff = 1.0 - sum(clamped.values())
        if abs(diff) <= 1e-12:
            return clamped
        if diff > 0:
            free = [m for m, w in clamped.items() if w < cap - 1e-12]
        else:
            free = [m for m, w in clamped.items() if w > floor + 1e-12]
        if not free:
            # Both bounds cannot be honoured at once for this many models.
            # The cap wins: "no model dominates" is the stronger of the two
            # policies, and the feasibility relaxation above means this is
            # unreachable for any n with the shipped values.
            return clamped
        base = sum(clamped[m] for m in free)
        for m in free:
            share = (clamped[m] / base) if base > 0 else (1.0 / len(free))
            clamped[m] += diff * share
        current = clamped
    return {m: _clamp(w, floor, cap) for m, w in current.items()}


def ensemble(signals, snapshot, now, *, history=None) -> dict:
    """P_META and everything needed to judge how much to believe it.

    Only VALID signals reach here. An empty list yields `p_meta = None` --
    never 0.5, never the market price.
    """
    valid = [s for s in signals if s.valid]
    if not valid:
        return {"p_meta": None, "models": 0, "weights": {},
                "disagreement": None, "confidence": 0.0,
                "reason": "no valid model signal"}
    weights = model_weights(valid, snapshot, now, history=history)
    p_meta = sum(weights[s.model] * float(s.p_yes) for s in valid)
    spread = dispersion([s.p_yes for s in valid])

    base_confidence = sum(weights[s.model] * _clamp(s.confidence)
                          for s in valid)
    # Disagreement penalty: at the configured maximum dispersion the
    # ensemble keeps only a quarter of its confidence.
    ceiling = max(1e-9, float(CFG.ALPHA_DISAGREEMENT_MAX))
    disagreement_penalty = _clamp(1.0 - 0.75 * (spread / ceiling), 0.1, 1.0)
    freshness_penalty = min(freshness_factor(s, now, snapshot) for s in valid)
    evidence = sum(weights[s.model] * _clamp(s.evidence_quality)
                   for s in valid)
    confidence = _clamp(base_confidence * disagreement_penalty
                        * freshness_penalty * max(evidence, 0.1))

    interval_low = min(float(s.probability_low) for s in valid)
    interval_high = max(float(s.probability_high) for s in valid)
    return {
        "p_meta": round(p_meta, 6),
        "models": len(valid),
        "weights": {m: round(w, 6) for m, w in weights.items()},
        "disagreement": round(spread, 6),
        "disagreement_penalty": round(disagreement_penalty, 6),
        "freshness_penalty": round(freshness_penalty, 6),
        "evidence_quality": round(evidence, 6),
        "base_confidence": round(base_confidence, 6),
        "confidence": round(confidence, 6),
        "envelope_low": round(interval_low, 6),
        "envelope_high": round(interval_high, 6),
        "envelope_width": round(interval_high - interval_low, 6),
        "per_model": {s.model: {"p_yes": s.p_yes,
                                "low": s.probability_low,
                                "high": s.probability_high,
                                "confidence": s.confidence,
                                "latency_ms": s.analysis_latency_ms}
                      for s in valid},
        "reason": None,
    }


def shadow_edge(meta: dict, snapshot, *, side: str,
                inference_cost_usd: float = 0.0) -> dict:
    """Raw edge and the edge that survives every estimated cost.

    Both are reported because the gap between them IS the finding. An
    ensemble that beats the market by two cents and costs three cents to
    produce has no alpha, and only the second number says so.
    """
    p_meta = meta.get("p_meta")
    if p_meta is None:
        return {"side": side, "raw_edge": None, "shadow_net_edge": None,
                "components": {}, "reason": "no ensemble probability"}
    if side == SIDE_YES:
        ask = float(snapshot.yes_ask)
        raw = float(p_meta) - ask
    else:
        ask = float(snapshot.no_ask)
        raw = (1.0 - float(p_meta)) - ask

    spread_cost = max(0.0, float(snapshot.spread)) / 2.0
    fee_cost = ask * float(CFG.ALPHA_FEE_RATE)
    slippage = ask * float(CFG.ALPHA_SLIPPAGE_RATE)
    # Uncertainty: how much of the estimate is envelope rather than signal.
    uncertainty = float(CFG.ALPHA_UNCERTAINTY_K) * float(
        meta.get("envelope_width") or 0.0)
    # Latency: what the ensemble spent of its own budget getting here.
    latency_ms = max((v.get("latency_ms") or 0)
                     for v in (meta.get("per_model") or {}).values()) \
        if meta.get("per_model") else 0
    from alpha_snapshot import analysis_budget_seconds
    budget_ms = analysis_budget_seconds(snapshot.market_class) * 1000.0
    latency_penalty = float(CFG.ALPHA_LATENCY_PENALTY_K) * (
        _clamp(latency_ms / budget_ms, 0.0, 1.0) if budget_ms > 0 else 0.0)
    notional = max(1e-9, float(CFG.ALPHA_SHADOW_NOTIONAL_USD))
    inference_penalty = float(inference_cost_usd) / notional

    net = (raw - spread_cost - fee_cost - slippage - uncertainty
           - latency_penalty - inference_penalty)
    return {
        "side": side,
        "ask": round(ask, 6),
        "raw_edge": round(raw, 6),
        "shadow_net_edge": round(net, 6),
        "components": {
            "spread_cost": round(spread_cost, 6),
            "estimated_fees": round(fee_cost, 6),
            "estimated_slippage": round(slippage, 6),
            "uncertainty_penalty": round(uncertainty, 6),
            "latency_penalty": round(latency_penalty, 6),
            "inference_cost_penalty": round(inference_penalty, 6),
        },
        "reason": None,
    }


def best_side(meta: dict, snapshot, *, inference_cost_usd: float = 0.0) -> dict:
    """The better of the two sides on SHADOW NET edge, not on raw edge.

    Choosing on raw edge would pick the side whose costs happen to be
    larger often enough to matter.
    """
    candidates = [shadow_edge(meta, snapshot, side=s,
                              inference_cost_usd=inference_cost_usd)
                  for s in (SIDE_YES, SIDE_NO)]
    scored = [c for c in candidates if c["shadow_net_edge"] is not None]
    if not scored:
        return candidates[0]
    return max(scored, key=lambda c: c["shadow_net_edge"])
