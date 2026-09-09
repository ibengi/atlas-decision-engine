"""AI Alpha Gateway v1 — orchestration. SHADOW ONLY, NO EXECUTION PATH.

Alpha Gateway v1, sections 1, 16, 17, 20.

    Market Scanner -> Candidate Filter -> Immutable Market Snapshot
      -> Alpha Gateway -> Parallel AI Analysis -> Signal Validator
      -> Meta Alpha Engine -> Shadow Opportunity Record -> Calibration DB

THE HARD BOUNDARY (section 20)
    This module -- and every `alpha_*` module -- imports no broker, no order
    manager and no execution engine. There is no code path from an Alpha
    signal to `create_order`, `cancel_order`, `place_and_track`,
    `apply_rebase`, position sizing or CAPITAL enablement, and no state this
    subsystem writes is read by any of them.

    That is asserted structurally rather than promised:
    `tests/test_alpha_safety_boundary.py` parses every alpha module's AST
    and fails the build on a forbidden import or attribute, and
    `tests/test_alpha_zero_broker_writes.py` runs a full cycle against a
    transport tripwire and asserts the broker adapter call count is exactly
    zero.

    NO STATE PRODUCED HERE MEANS "TRADE". The eight terminal states in
    section 16 are observations. `POSITIVE_EDGE_HIGH_CONFIDENCE` is a
    hypothesis awaiting resolution, not an instruction, and nothing in this
    repository consumes it.

WHY THE GATEWAY IS OFF BY DEFAULT
    `CFG.ALPHA_GATEWAY_ENABLED` defaults to False and is read through
    `_env_gate`, the strict fail-closed reader. Enabling it starts paid
    inference calls against third-party APIs; that is an operator decision,
    not a deployment side effect.
"""

import hashlib
import logging
from datetime import datetime, timezone

from alpha_dispatcher import dispatch, quote_movement
from alpha_ledger import AlphaLedger
from alpha_meta import best_side, ensemble
from alpha_providers import default_providers
from alpha_snapshot import MarketSnapshot, redacted
from config import CFG

log = logging.getLogger("ALPHA")

# ── Section 16: the complete set of terminal states. None means TRADE. ──
STATE_NO_EDGE = "NO_EDGE"
STATE_POSITIVE_LOW = "POSITIVE_EDGE_LOW_CONFIDENCE"
STATE_POSITIVE_HIGH = "POSITIVE_EDGE_HIGH_CONFIDENCE"
STATE_STALE = "STALE"
STATE_INSUFFICIENT = "INSUFFICIENT_DATA"
STATE_DISAGREEMENT = "MODEL_DISAGREEMENT"
STATE_MARKET_MOVED = "MARKET_MOVED"
STATE_TIMEOUT = "ANALYSIS_TIMEOUT"
#: Phase 2, section 5. Every provider was refused BEFORE being called, on
#: spend. Its own state because "we could not afford to ask" is a different
#: fact from "the models had no opinion", and conflating them would make a
#: billing problem look like a market observation.
STATE_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
SHADOW_STATES = (STATE_NO_EDGE, STATE_POSITIVE_LOW, STATE_POSITIVE_HIGH,
                 STATE_STALE, STATE_INSUFFICIENT, STATE_DISAGREEMENT,
                 STATE_MARKET_MOVED, STATE_TIMEOUT, STATE_BUDGET_EXHAUSTED)

#: Not one of these states authorizes anything. Kept as an explicit,
#: assertable constant so a future state cannot be quietly added as
#: actionable without a test noticing.
EXECUTABLE_STATES = frozenset()


class AlphaGateway:
    """Analyse one market snapshot and produce one shadow record.

    Holds no broker client, no order manager and no risk manager. The only
    collaborators are providers (outbound HTTP to AI vendors), a ledger
    (local append-only files) and an optional read-only quote function.
    """

    def __init__(self, *, providers=None, ledger=None, session=None,
                 quant_estimator=None, now_fn=None):
        self.providers = list(providers) if providers is not None \
            else default_providers(session=session,
                                   quant_estimator=quant_estimator)
        self.ledger = ledger if ledger is not None else AlphaLedger()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    # ── the one public entry point ──────────────────────────────────────
    def analyze(self, snapshot: MarketSnapshot, *, quote_fn=None,
                record: bool = True, gate=None, on_signal=None) -> dict:
        """Snapshot -> shadow opportunity record.

        Never raises for a provider problem and never returns an
        instruction. `record=False` runs the full analysis without touching
        the ledger, for dry runs and for tests that assert immutability.

        `gate` and `on_signal` are passed straight to the dispatcher: the
        gateway does not interpret spend policy, it only carries it to the
        place where a provider is about to be called.
        """
        if not isinstance(snapshot, MarketSnapshot):
            raise TypeError("analyze() requires a MarketSnapshot built by "
                            "alpha_snapshot.build_snapshot")
        # The snapshot must still be the snapshot it claims to be. A
        # tampered premise invalidates all four analyses at once, so it is
        # checked before a single token is spent.
        snapshot.verify()

        result = dispatch(snapshot, self.providers, quote_fn=quote_fn,
                          now_fn=self.now_fn, gate=gate, on_signal=on_signal)
        now = self.now_fn()
        cost_usd = self.ledger.cycle_cost_usd(result)
        meta = ensemble(result.valid, snapshot, now)
        edge = best_side(meta, snapshot, inference_cost_usd=cost_usd)
        movement = quote_movement(result)
        state, reason = self._classify(snapshot, result, meta, edge, movement,
                                       now)

        opportunity = self._record(snapshot, result, meta, edge, movement,
                                   state, reason, cost_usd, now)
        if record:
            try:
                self.ledger.record_costs(snapshot, result)
                self.ledger.record_prediction(opportunity)
            except Exception as e:                            # noqa: BLE001
                # A ledger failure loses evidence; it must be loud. It does
                # NOT change the verdict, because there is no verdict to
                # protect: nothing downstream acts on this record.
                log.error(f"[ALPHA_LEDGER] could not persist "
                          f"{opportunity['prediction_id']}: "
                          f"{type(e).__name__}: {e}")
                opportunity["persisted"] = False
        else:
            opportunity["persisted"] = False
        log.info(f"[ALPHA_SHADOW] {snapshot.contract_id} state={state} "
                 f"p_meta={meta.get('p_meta')} "
                 f"net_edge={edge.get('shadow_net_edge')} "
                 f"models={meta.get('models')} cost_usd={cost_usd}")
        return opportunity

    # ── section 16 classification ───────────────────────────────────────
    def _classify(self, snapshot, result, meta, edge, movement, now) -> tuple:
        """Exactly one terminal state, and the sentence that justifies it.

        Order matters: the earliest condition that is true wins, and the
        order runs from "we could not analyse" through "we analysed but
        cannot conclude" to "we concluded". Reporting an edge from a cycle
        that timed out would be the same class of error as reporting a
        flat portfolio from an unreadable broker response.
        """
        valid = result.valid
        if not valid:
            reasons = {s.rejected_reason for s in result.excluded}
            if reasons and reasons.issubset({"budget_exhausted",
                                             "pricing_unconfigured"}):
                # Nobody was asked. Reporting INSUFFICIENT_DATA here would
                # blame the models for a spend limit.
                return STATE_BUDGET_EXHAUSTED, (
                    "every provider was refused before being called: "
                    + ", ".join(sorted(reasons)))
            if reasons & {"analysis_timeout", "provider_timeout", "stale",
                          "late_response"}:
                return STATE_TIMEOUT, "no model answered before the deadline"
            return STATE_INSUFFICIENT, "no valid model signal"
        if snapshot.is_expired(at=now):
            return STATE_STALE, ("the snapshot's effective validity ended "
                                 "before the analysis completed")
        if len(valid) < int(CFG.ALPHA_MIN_VALID_MODELS):
            return STATE_INSUFFICIENT, (
                f"{len(valid)} valid model signal(s), "
                f"{CFG.ALPHA_MIN_VALID_MODELS} required")
        spread = meta.get("disagreement")
        if spread is not None and spread >= float(CFG.ALPHA_DISAGREEMENT_MAX):
            return STATE_DISAGREEMENT, (
                f"model dispersion {spread:.4f} >= "
                f"{float(CFG.ALPHA_DISAGREEMENT_MAX):.4f}")
        net = edge.get("shadow_net_edge")
        if net is None:
            return STATE_INSUFFICIENT, "no ensemble probability"
        if net <= 0:
            return STATE_NO_EDGE, (f"shadow net edge {net:+.4f} after costs")
        # There WAS an edge. Did it survive the analysis window?
        if self._edge_evaporated(edge, movement):
            return STATE_MARKET_MOVED, (
                "the price moved against the edge while the models were "
                "thinking")
        if float(meta.get("confidence") or 0.0) >= float(CFG.ALPHA_HIGH_CONFIDENCE):
            return STATE_POSITIVE_HIGH, (
                f"shadow net edge {net:+.4f} at confidence "
                f"{meta.get('confidence')}")
        return STATE_POSITIVE_LOW, (
            f"shadow net edge {net:+.4f} at confidence "
            f"{meta.get('confidence')}")

    @staticmethod
    def _edge_evaporated(edge, movement) -> bool:
        """True when the ask we would have paid rose by more than the edge.

        Requires a MEASURED movement: `measured=False` means the quotes were
        unavailable, and an unmeasured move is never reported as a move.
        This is the direct measurement of latency decay section 17 asks for.
        """
        if not movement.get("measured"):
            return False
        delta = movement.get(f"delta_{edge['side']}_ask")
        if delta is None:
            return False
        return float(delta) >= float(edge.get("shadow_net_edge") or 0.0)

    # ── the shadow record (section 13 field list) ───────────────────────
    def _record(self, snapshot, result, meta, edge, movement, state, reason,
                cost_usd, now) -> dict:
        prediction_id = "pred-" + hashlib.sha256(
            f"{snapshot.market_snapshot_id}|{now.isoformat()}".encode()
        ).hexdigest()[:20]
        horizon = None
        try:
            from alpha_snapshot import parse_utc
            horizon = (parse_utc(snapshot.expected_resolution_time_utc)
                       - now).total_seconds()
        except Exception:                                     # noqa: BLE001
            horizon = None
        return {
            "prediction_id": prediction_id,
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id,
            "event_id": snapshot.event_id,
            "snapshot": snapshot.as_dict(),
            "prediction_time": now.isoformat(timespec="seconds"),
            "market_class": snapshot.market_class,
            "time_to_resolution_s": horizon,

            "p_meta": meta.get("p_meta"),
            "confidence": meta.get("confidence"),
            "disagreement": meta.get("disagreement"),
            "weights": meta.get("weights"),
            "per_model": meta.get("per_model"),
            "envelope_low": meta.get("envelope_low"),
            "envelope_high": meta.get("envelope_high"),

            "market_yes_bid": snapshot.yes_bid,
            "market_yes_ask": snapshot.yes_ask,
            "market_no_bid": snapshot.no_bid,
            "market_no_ask": snapshot.no_ask,

            "side": edge.get("side"),
            "entry_price": edge.get("ask"),
            "raw_edge": edge.get("raw_edge"),
            "shadow_net_edge": edge.get("shadow_net_edge"),
            "edge_components": edge.get("components"),

            "state": state,
            "state_reason": reason,
            "executed": False,
            "execution_authorized": False,

            "model_latency_ms": {s.model: s.analysis_latency_ms
                                 for s in result.signals},
            "model_cost_usd": {s.model: (s.cost or {}).get("api_cost_usd", 0.0)
                               for s in result.signals},
            "cycle_cost_usd": cost_usd,
            "dispatch": result.as_dict(),
            "market_movement": movement,

            # Filled in later by a RESOLUTION row, never by editing this one.
            "actual_outcome": None,
            "persisted": True,
        }


def analyze_market(snapshot: MarketSnapshot, **kw) -> dict:
    """Convenience entry point that honours the feature gate.

    Returns None when the gateway is disabled, so a caller that forgets to
    check gets nothing rather than a silent paid analysis.
    """
    if not CFG.ALPHA_GATEWAY_ENABLED:
        log.info(f"[ALPHA_GATEWAY] disabled; {redacted(snapshot)} not analysed")
        return None
    return AlphaGateway(**{k: v for k, v in kw.items()
                           if k in ("providers", "ledger", "session",
                                    "quant_estimator", "now_fn")}
                        ).analyze(snapshot,
                                  quote_fn=kw.get("quote_fn"),
                                  record=kw.get("record", True))
