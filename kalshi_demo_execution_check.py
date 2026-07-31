#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kalshi_demo_execution_check.py — Test d'integration REEL sur Kalshi DEMO.

TEST A (execution technique) UNIQUEMENT : prouve qu'un ordre part reellement
vers https://demo-api.kalshi.co/trade-api/v2, est accepte, relu, rempli (ou
proprement annule), et que la position est retrouvee via l'API. Ce script ne
mesure PAS la rentabilite (test B) et ne touche a AUCUN seuil du moteur.

Garde-fous :
  - exige ENABLE_DEMO_INTEGRATION_TEST=true (explicite) ;
  - refuse de demarrer si l'URL n'est pas demo-api.kalshi.co ;
  - refuse tout environnement de production ;
  - 1 contrat maximum, sur le marche liquide le moins cher trouve ;
  - aucune cle ni signature n'est journalisee ni ecrite dans le rapport.

Usage (Railway one-off ou local) :
  ENABLE_DEMO_INTEGRATION_TEST=true python scripts/kalshi_demo_execution_check.py
Sortie : demo_execution_proof.json dans DATA_DIR (ou repertoire courant).
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone


from kalshi_alpha_bot import (KalshiClient, KalshiAPIError, CFG,  # noqa: E402
                              pick, pick_int, _client_is_genuine)

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
MAX_ASK_CENTS = 30          # cout maximal accepte pour 1 contrat (fonds DEMO)
FILL_TIMEOUT_S = float(os.getenv("ORDER_FILL_TIMEOUT_SECONDS", "45"))
VERIFY_EVERY_S = max(2.0, min(5.0, float(
    os.getenv("ORDER_VERIFY_INTERVAL_SECONDS", "3"))))
CANDIDATE_SERIES = ("KXBTCD", "KXBTC15M", "KXETHD")


def fatal(msg):
    print(f"[FATAL] {msg}")
    sys.exit(2)


def main():
    # ── garde-fous stricts ────────────────────────────────────────────────
    if os.getenv("ENABLE_DEMO_INTEGRATION_TEST", "").lower() != "true":
        fatal("ENABLE_DEMO_INTEGRATION_TEST=true est requis (explicite).")
    for var in ("LIVE_TRADING", "KALSHI_ENV_CONFIRM"):
        if os.getenv(var, "").upper() in ("1", "LIVE"):
            fatal(f"{var} indique un contexte LIVE — ce script est "
                  f"DEMO uniquement, arret.")
    client = KalshiClient("demo")
    if client.base_url.rstrip("/") != DEMO_BASE:
        fatal(f"URL inattendue: {client.base_url} — seul {DEMO_BASE} "
              f"est autorise. Arret sans aucun appel d'ordre.")
    if not _client_is_genuine(client):
        fatal("Client non authentique (mock/patch detecte).")

    proof = {"environment": "DEMO", "mock_enabled": False, "dry_run": False,
             "api_base_url": client.base_url,
             "order_submission_attempted": False,
             "timestamp_utc": datetime.now(timezone.utc).isoformat()}

    # ── 1. solde ──────────────────────────────────────────────────────────
    bal = client.get_balance()
    print(f"[CHECK] balance_demo=${bal:.2f}")
    proof["balance_usd"] = round(bal, 2)
    if bal < 1.0:
        fatal("Solde DEMO insuffisant (< 1$).")

    # ── 2. marche ouvert, liquide, bon marche ─────────────────────────────
    candidate = None
    for series in CANDIDATE_SERIES:
        for m in client.get_markets(series, status="open", limit=100):
            ask = m.get("yes_ask")
            bid = m.get("yes_bid")
            try:
                ask, bid = int(ask), int(bid)
            except (TypeError, ValueError):
                continue
            if not (1 <= ask <= MAX_ASK_CENTS and 1 <= bid <= 99):
                continue
            if ask - bid > 5:
                continue
            if candidate is None or ask < candidate[1]:
                candidate = (m["ticker"], ask)
        if candidate:
            break
    if not candidate:
        fatal(f"Aucun marche ouvert avec ask ≤ {MAX_ASK_CENTS}c et spread "
              f"≤ 5c dans {CANDIDATE_SERIES}. Relancer plus tard ou "
              f"elargir CANDIDATE_SERIES.")
    ticker, ask = candidate
    print(f"[CHECK] market={ticker} yes_ask={ask}c (cout max "
          f"{ask / 100:.2f}$ pour 1 contrat)")
    proof["ticker"] = ticker

    # ── 3. soumission REELLE d'un ordre minimal (1 contrat, au ask) ───────
    coid = f"democheck_{uuid.uuid4().hex}"
    print(f"[ORDER_SUBMIT_ATTEMPT] ticker={ticker} side=yes action=buy "
          f"count=1 price_cents={ask} client_order_id={coid} "
          f"environment=DEMO endpoint=/portfolio/orders")
    proof.update({"order_submission_attempted": True,
                  "client_order_id": coid})
    try:
        order = client.create_order(ticker, "yes", 1, ask,
                                    client_order_id=coid)
    except KalshiAPIError as e:
        print(f"[ORDER_SUBMIT_FAILED] http_status={e.status} "
              f"error_message={e} response_body_sanitized={str(e.body)[:300]}")
        proof.update({"http_status": e.status, "order_verified_from_api":
                      False, "error": str(e.body)[:300]})
        _write(proof)
        sys.exit(1)
    http_status = getattr(client, "last_http_status", None)
    oid = str(pick(order, "order_id", "id", default="") or "")
    print(f"[ORDER_SUBMIT_RESPONSE] http_status={http_status} "
          f"kalshi_order_id={oid} client_order_id={coid} "
          f"status={pick(order, 'status', default='-')} "
          f"raw_response_sanitized={json.dumps(order, default=str)[:400]}")
    proof.update({"http_status": http_status, "kalshi_order_id": oid})
    if not oid:
        fatal("Reponse sans order_id — soumission NON prouvee.")

    # ── 4. relecture de l'ordre (2e requete API, meme id) ─────────────────
    try:
        verified = client.get_order(oid)
    except KalshiAPIError as e:
        print(f"[ORDER_VERIFY_FAILED] reason=order_not_found_after_"
              f"submission ({e})")
        proof["order_verified_from_api"] = False
        _write(proof)
        sys.exit(1)
    status = str(pick(verified, "status", "order_status", default="?"))
    print(f"[ORDER_VERIFY] kalshi_order_id={oid} status={status} "
          f"remaining_count={pick_int(verified, 'remaining_count', default=-1)} "
          f"filled_count={pick_int(verified, 'taker_fill_count', 'fill_count', default=-1)}")
    proof["order_verified_from_api"] = True
    proof["order_status"] = status

    # ── 5. attente de fill (accepted != filled), annulation sinon ─────────
    t0 = time.time()
    filled = 0
    while time.time() - t0 < FILL_TIMEOUT_S:
        fills = client.get_fills(oid)
        filled = sum(pick_int(f, "count", "quantity", default=0)
                     for f in fills)
        if filled >= 1:
            fees = sum(float(f.get("fees") or 0) for f in fills)
            print(f"[FILL_VERIFY] kalshi_order_id={oid} "
                  f"fills_count={len(fills)} filled_contracts={filled} "
                  f"fees={fees}")
            print(f"[ORDER_FILLED] kalshi_order_id={oid}")
            break
        print(f"[ORDER_WAITING_FOR_FILL] "
              f"elapsed_seconds={time.time() - t0:.0f} status={status} "
              f"remaining_count={1 - filled}")
        time.sleep(VERIFY_EVERY_S)
        try:
            status = str(pick(client.get_order(oid), "status", default=status))
        except KalshiAPIError:
            pass
    proof["fills_verified_from_api"] = filled >= 1
    proof["filled_contracts"] = filled
    if filled < 1:
        print(f"[ORDER_CANCELED_UNFILLED] kalshi_order_id={oid} "
              f"timeout_seconds={FILL_TIMEOUT_S:.0f}")
        try:
            client.cancel_order(oid)
        except KalshiAPIError as e:
            print(f"[WARN] annulation: {e}")
        proof["position_verified_from_api"] = False
        _write(proof)
        print("[RESULT] soumission + acceptation + relecture PROUVEES ; "
              "fill non obtenu dans le delai (ordre limite non croise) — "
              "ceci n'est PAS un echec technique. Relancer, ou le carnet "
              "etait vide au niveau demande.")
        sys.exit(0)

    # ── 6. position retrouvee depuis l'API ────────────────────────────────
    found = False
    for p in client.get_positions():
        if str(pick(p, "ticker", "market_ticker", default="")) == ticker:
            found = True
            print(f"[POSITION_VERIFY] ticker={ticker} position_found=true "
                  f"net_position={pick_int(p, 'position', default=filled)} "
                  f"market_exposure={p.get('market_exposure', '-')} "
                  f"realized_pnl={p.get('realized_pnl', '-')} "
                  f"fees_paid={p.get('fees_paid', '-')}")
            break
    if not found:
        print(f"[POSITION_VERIFY] ticker={ticker} position_found=false")
    proof["position_verified_from_api"] = found

    _write(proof)
    if found and proof["order_verified_from_api"]:
        print("[DEMO_EXECUTION_PROVED]")
        sys.exit(0)
    sys.exit(1)


def _write(proof):
    out = os.path.join(os.getenv("DATA_DIR", "."),
                       "demo_execution_proof.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(proof, f, indent=1)
    print(f"[REPORT] {out}")


if __name__ == "__main__":
    main()
