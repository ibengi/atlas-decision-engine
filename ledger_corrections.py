"""Broker-authoritative ledger corrections (append-only, audit-preserving).

When the broker's final record of an order diverges from what the engine
observed live, the ledger is corrected by APPENDING an explicit corrective
event to the trade journal -- never by rewriting or deleting historical
rows. The corrective row carries the broker order/fill identifiers and the
evidence that justifies it, so the journal remains a complete audit trail:
what the engine believed at the time, and the correction, both stay
readable forever.

Every correction is declarative and surgical:

  - keyed to ONE historical trade row (trade_id + order_id + ticker), with
    strict preconditions on quantity/side/result -- any other ledger, any
    already-corrected ledger, and any not-yet-settled target gets ZERO
    writes
  - idempotent: the appended row's ``correction_id`` IS the applied-marker;
    it lives in the journal itself so it cannot desynchronize from it
  - restart-safe: append + atomic flush; a crash before the flush leaves
    the pre-correction journal and the next boot applies it cleanly
  - purely local: this module never touches a broker client, never opens a
    position, and risk history follows automatically because every risk
    metric is recomputed from the journal (single source of truth)

Registered corrections
----------------------
SIXTH_CONTRACT_CORRECTION -- incident 2026-08-28, order 01a04823 on
KXBTCD-26AUG2808-T79599.99: the engine recorded 5/6 no-contracts (the five
taker fills returned at submission), the cancel of the resting remainder
failed (DELETE -> HTTP 404) at 11:32:11Z and tracking stopped; the broker's
order record proves the sixth contract filled as a MAKER fill at
11:34:21.249018Z (taker_fill_cost 0.95$ = 5 x 19c + maker_fill_cost 0.19$ =
1 x 19c; fees 0.0539$ total, maker share 0.0000$; status executed 6/6).
The market settled "no": broker paid 6.00$, the ledger accounted 5.00$.
Root cause: FILL_ARRIVED_AFTER_CANCEL_FAILURE. The corrective event adds
the missing contract (+1 @ 19c -> gross +0.81$) and reconciles fees to the
broker's actuals (0.0539$ vs 0.06$ recorded -> -0.0061$), i.e. net
+0.8161$, bringing this market's ledger to the broker basis
6 x (1.00 - 0.19) - 0.0539 = 4.8061$.
"""

import logging

from trade_logger import TradeLogger, now_iso

log = logging.getLogger("LEDGER")

SIXTH_CONTRACT_CORRECTION = {
    "correction_id": "corr-01a04823-sixth-fill-v1",
    # target row (ALL must match, or the correction refuses to run)
    "target_trade_id": "c91a43f56c9c",
    "order_id": "01a04823-8ec8-74c2-82de-f7ecd236fc01",
    "ticker": "KXBTCD-26AUG2808-T79599.99",
    "side": "no",
    "expected_filled_count": 5,      # what the ledger wrongly shows
    "expected_result": "no",         # the PnL below is only true for this
    # the corrective event (the missing maker fill, broker-priced)
    "filled_delta": 1,
    "avg_fill_price": 19,            # cents
    "gross_pnl": 0.81,               # payout 1.00 - entry 0.19
    "fees": -0.0061,                 # broker 0.0539 actual vs 0.06 recorded
    "net_pnl": 0.8161,
    "fill_time": "2026-08-28T11:34:21+00:00",
    "broker_evidence": {
        "source": ("GET /portfolio/orders/01a04823-... (RAW:get_order, "
                   "logged 2026-08-31T18:47:59Z, deployment 1b650679) + "
                   "original submit/cancel logs (deployment 23457570, "
                   "2026-08-28T11:31:25-11:32:11Z)"),
        "order_id": "01a04823-8ec8-74c2-82de-f7ecd236fc01",
        "client_order_id": "alpha_0549f33103f403be",
        "status": "executed",
        "initial_count": 6,
        "fill_count": 6,
        "remaining_count": 0,
        "taker_fill_cost_dollars": 0.95,
        "taker_fees_dollars": 0.0539,
        "maker_fill_cost_dollars": 0.19,
        "maker_fees_dollars": 0.0,
        "no_price_dollars": 0.19,
        "created_time": "2026-08-28T11:31:25.901608Z",
        "last_update_time": "2026-08-28T11:34:21.249018Z",
        "local_last_observation": ("ORDER_CANCEL_FAILED HTTP 404 at "
                                   "2026-08-28T11:32:11Z, known_filled=5/6"),
        "root_cause": "FILL_ARRIVED_AFTER_CANCEL_FAILURE",
    },
}

#: Ordered registry of every authorized correction. Adding one requires the
#: same operator review as a migration: broker evidence first, code second.
CORRECTIONS = (SIXTH_CONTRACT_CORRECTION,)


def _find_target(trades: list, corr: dict):
    for t in trades:
        if (t.get("trade_id") == corr["target_trade_id"]
                and t.get("order_id") == corr["order_id"]
                and t.get("ticker") == corr["ticker"]):
            return t
    return None


def _preconditions_unmet(target: dict, corr: dict):
    """-> None when the target row is exactly the state the correction was
    written for, else the human-readable reason to refuse."""
    if target is None:
        return "ligne cible absente du journal (autre ledger)"
    if target.get("state") != "settled":
        return (f"cible non reglee (state={target.get('state')}): la "
                f"correction attend le reglement")
    if target.get("filled_count") != corr["expected_filled_count"]:
        return (f"filled_count={target.get('filled_count')} != "
                f"{corr['expected_filled_count']} attendu: quantite deja "
                f"corrigee autrement -- appliquer +{corr['filled_delta']} "
                f"double-compterait")
    if target.get("side") != corr["side"]:
        return f"side={target.get('side')} != {corr['side']} attendu"
    if target.get("result") != corr["expected_result"]:
        return (f"result={target.get('result')} != "
                f"{corr['expected_result']} attendu: le PnL correctif ne "
                f"vaut que pour ce reglement")
    return None


def apply_ledger_corrections(tlog: TradeLogger) -> list:
    """Applies every registered correction whose exact target ledger is
    loaded. Returns the corrective rows appended (possibly empty). Pure
    local bookkeeping: no broker client is ever seen here."""
    applied = []
    for corr in CORRECTIONS:
        cid = corr["correction_id"]
        if any(t.get("correction_id") == cid for t in tlog.trades):
            continue                       # already applied: marker row found
        target = _find_target(tlog.trades, corr)
        reason = _preconditions_unmet(target, corr)
        if reason:
            # Normal on every ledger except the one the correction targets.
            (log.warning if target is not None else log.debug)(
                f"[LEDGER_CORRECTION] {cid} NON appliquee: {reason}")
            continue

        rec = {
            "schema": tlog.SCHEMA,
            "trade_id": cid,
            "decision_id": None,
            "timestamp": corr["fill_time"],
            "ticker": corr["ticker"],
            "market": target.get("market"),
            "side": corr["side"],
            "requested_price": None,
            "avg_fill_price": corr["avg_fill_price"],
            "requested_count": corr["filled_delta"],
            "filled_count": corr["filled_delta"],
            "spread": None, "fees": corr["fees"],
            "edge": None, "ev": None, "confidence": None, "grade": None,
            "reason": "broker_authoritative_correction",
            "analysis": ("evenement correctif broker-authoritative: fill "
                         "manquant au journal, preuve dans broker_evidence; "
                         "la ligne d'origine reste intacte (append-only)"),
            "order_id": corr["order_id"],
            "order_status": corr["broker_evidence"]["status"],
            "state": "settled",
            "result": corr["expected_result"],
            "won": corr["net_pnl"] >= 0,
            "gross_pnl": corr["gross_pnl"],
            "net_pnl": corr["net_pnl"],
            "roi": None, "holding_seconds": None,
            "settled_at": now_iso(),
            "correction": True,
            "correction_id": cid,
            "corrects_trade_id": corr["target_trade_id"],
            "broker_evidence": dict(corr["broker_evidence"]),
        }
        tlog.trades.append(rec)
        tlog.flush()
        applied.append(rec)
        log.warning(
            f"[LEDGER_CORRECTION] {cid} APPLIQUEE: {corr['ticker']} "
            f"{corr['side'].upper()} +{corr['filled_delta']} @ "
            f"{corr['avg_fill_price']}c -> gross {corr['gross_pnl']:+.4f}$ "
            f"fees {corr['fees']:+.4f}$ net {corr['net_pnl']:+.4f}$ "
            f"(corrige {corr['target_trade_id']}, ordre {corr['order_id']}; "
            f"cause: {corr['broker_evidence']['root_cause']}). Lignes "
            f"historiques INTACTES, aucune position ouverte, aucune "
            f"ecriture broker.")
    return applied
