# -*- coding: utf-8 -*-
"""AIR-001 Wave 2 (DE-P0-003): the final-price economic invariant.

Reproduced defect (permanent regression: tests/test_execution_intent.py):
a decision accepted at ask P1 could be submitted at a fresh ask P2 with
NO economic re-gating — the measured evidence run submitted a YES order
at 95c that had been accepted at 48c, past MAX_ENTRY_CENTS and with
deeply negative net edge, recording zero rejections.

Contract implemented here:
- After the final fresh-book read, the SAME model probability is
  re-gated against the CURRENT executable book by calling the ONE shared
  economic gate the pipeline itself uses (`strategy_router.
  price_and_gate`) with the ONE shared GateConfig instance. No formula
  is duplicated: side selection, spread, MAX_ENTRY, fee estimate,
  slippage buffer, uncertainty buffer, gross/net edge and net EV are all
  recomputed by the identical code path.
- The result is a frozen ValidatedExecutionIntent binding decision,
  market, fresh economics, timestamps/age, engine commit, model and
  calibration identity, and the strategy/risk config hashes.
- Order submission may proceed ONLY from a ValidatedExecutionIntent.
  Any fresh-gate failure — adverse price, spread deterioration, side
  reversal, fee/slippage shift, stale book — means NO ORDER, visibly
  (a counted `fresh_economic_gate` rejection), never silently.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict, replace  # noqa: F401
from typing import Optional, Tuple

from config import CFG
from strategy_router import Decision, ModelOutput, price_and_gate

#: Maximum acceptable age of the fresh book at validation time, seconds.
#: The read is synchronous today so this only fires on pathological
#: stalls or future caching — it is protective instrumentation, not a
#: trading threshold: it can only BLOCK orders, never create them.
FRESH_BOOK_MAX_AGE_S = float(os.getenv("FRESH_BOOK_MAX_AGE_S", "10"))


@dataclass(frozen=True)
class ValidatedExecutionIntent:
    decision_id: Optional[str]
    market_id: str
    side: str
    model_probability: float          # probability of the CHOSEN side
    model_probability_yes: float      # the model's YES-side probability
    fresh_requested_price: int        # cents, from the CURRENT book
    fresh_spread: Optional[int]
    fee_estimate: float
    slippage_estimate: float
    uncertainty_buffer: float
    gross_edge: float
    net_edge: float
    net_ev: float
    validated_at: float
    market_data_timestamp: float
    market_data_age: float
    engine_commit: str
    model_id: Optional[str]
    calibration_id: Optional[str]
    strategy_config_hash: str
    risk_config_hash: str

    def content_hash(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True,
                         default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def _identities():
    """Engine/model/calibration identity, measured — never invented.
    research_export owns these derivations; failures surface as explicit
    UNKNOWN markers rather than fabricated ids."""
    try:
        import research_export
        return (research_export.engine_commit(),
                research_export.model_identity().get("model_hash"),
                research_export.calibration_identity().get(
                    "calibration_version"))
    except Exception:                    # noqa: BLE001 — identity is
        return ("UNKNOWN", None, None)   # reported honestly, not faked


def validate_fresh_execution(dec, fresh_market: dict, fresh_book: dict,
                             gates, *, fetched_at: float,
                             now: Optional[float] = None,
                             max_age_s: Optional[float] = None
                             ) -> Tuple[Optional[ValidatedExecutionIntent],
                                        str]:
    """Re-gate the accepted decision against the CURRENT book.

    Returns (intent, "") or (None, reason). Every failure reason is a
    stable machine string so rejections stay countable evidence.
    """
    now = time.time() if now is None else now
    max_age = FRESH_BOOK_MAX_AGE_S if max_age_s is None else max_age_s
    age = max(0.0, now - fetched_at)
    if age > max_age:
        return None, f"stale_fresh_book:age={age:.1f}s>max={max_age:.1f}s"

    model_raw = dec.model_output if isinstance(dec.model_output, dict) \
        else {}
    p_yes = model_raw.get("probability_yes")
    if not isinstance(p_yes, (int, float)):
        return None, "model_probability_unavailable"
    model = ModelOutput(valid=True, reason=None,
                        probability_yes=float(p_yes),
                        confidence=int(dec.confidence or 0),
                        features=dict(model_raw.get("features") or {}))

    # The ONE shared gate, over a fresh Decision shell — the original
    # decision object is never mutated; its acceptance history stays
    # intact as evidence.
    fresh = Decision(ticker=dec.ticker, strategy=dec.strategy,
                     market_type=getattr(dec, "market_type", None),
                     category=getattr(dec, "category", None))
    fresh = price_and_gate(fresh, model, fresh_book, gates, fresh_market)
    if not fresh.accepted:
        return None, f"fresh_gate_failed:{fresh.rejection_reason}"
    if fresh.side != dec.side:
        return None, f"side_reversal:{dec.side}->{fresh.side}"

    from config_identity import risk_config_hash, strategy_config_hash
    engine_commit, model_id, calibration_id = _identities()
    intent = ValidatedExecutionIntent(
        decision_id=getattr(dec, "decision_id", None),
        market_id=dec.ticker,
        side=fresh.side,
        model_probability=fresh.model_probability,
        model_probability_yes=float(p_yes),
        fresh_requested_price=int(fresh.entry_ask),
        fresh_spread=fresh.spread,
        fee_estimate=fresh.estimated_fees,
        slippage_estimate=fresh.expected_slippage,
        uncertainty_buffer=float(getattr(gates, "UNCERTAINTY_BUFFER",
                                         0.0)),
        gross_edge=fresh.gross_edge,
        net_edge=fresh.net_edge,
        net_ev=fresh.net_ev,
        validated_at=now,
        market_data_timestamp=fetched_at,
        market_data_age=age,
        engine_commit=engine_commit,
        model_id=model_id,
        calibration_id=calibration_id,
        strategy_config_hash=strategy_config_hash(),
        risk_config_hash=risk_config_hash(),
    )
    return intent, ""
