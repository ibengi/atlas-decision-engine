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
  - aucune cle ni signature n'est journalisee ni ecrite dans le rapport ;
  - AUD-PROBE-001 : attente BORNEE d'un marche conforme (intervalle
    DEMO_PROBE_POLL_INTERVAL_SECONDS, plafond DEMO_PROBE_MAX_WAIT_SECONDS)
    au lieu d'une sortie immediate ; timeout = NO_ELIGIBLE_MARKET (exit 0,
    pas un echec) ; criteres ask<=30c / spread<=5c INCHANGES ; UN SEUL
    ordre de test par execution ; reponse POST ambigue -> interrogation
    broker par client_order_id UNIQUEMENT, jamais de re-POST.

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


from kalshi_client import (KalshiClient, KalshiAPIError, pick, pick_int)  # noqa: E402
from kalshi_alpha_bot import _client_is_genuine
from config import CFG

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
MAX_ASK_CENTS = 30          # cout maximal accepte pour 1 contrat (fonds DEMO)
MAX_SPREAD_CENTS = 5        # spread maximal accepte — INCHANGE (audit 26/08)
FILL_TIMEOUT_S = float(os.getenv("ORDER_FILL_TIMEOUT_SECONDS", "45"))
VERIFY_EVERY_S = max(2.0, min(5.0, float(
    os.getenv("ORDER_VERIFY_INTERVAL_SECONDS", "3"))))
CANDIDATE_SERIES = ("KXBTCD", "KXBTC15M", "KXETHD")

# AUD-PROBE-001 (2026-08-26) : attente BORNEE d'un marche naturellement
# conforme, au lieu d'une sortie immediate + relances externes toutes les
# quelques secondes. Les criteres (ask <= 30c, spread <= 5c) sont
# STRICTEMENT inchanges ; seul le "quand re-regarder" change. Chaque
# tentative coute len(CANDIDATE_SERIES) GET /markets ; l'intervalle est
# borne pour ne jamais marteler l'API.
POLL_INTERVAL_MIN_S = 15.0
POLL_INTERVAL_MAX_S = 300.0
MAX_WAIT_CAP_S = 24 * 3600.0


def _env_f(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def poll_bounds():
    """(intervalle, attente_max) en secondes, bornes garanties.
    DEMO_PROBE_POLL_INTERVAL_SECONDS (defaut 60, borne [15, 300]) ;
    DEMO_PROBE_MAX_WAIT_SECONDS (defaut 21600 = 6 h, borne
    [0, 86400] — 0 = un seul scan, comportement historique)."""
    interval = max(POLL_INTERVAL_MIN_S,
                   min(POLL_INTERVAL_MAX_S,
                       _env_f("DEMO_PROBE_POLL_INTERVAL_SECONDS", 60.0)))
    max_wait = max(0.0, min(MAX_WAIT_CAP_S,
                            _env_f("DEMO_PROBE_MAX_WAIT_SECONDS", 21600.0)))
    return interval, max_wait


def market_is_eligible(m):
    """Criteres EXACTS et inchanges : 1 <= yes_ask <= 30c, 1 <= yes_bid
    <= 99, spread (ask-bid) <= 5c. Retourne (ask, bid) ou None."""
    try:
        ask, bid = int(m.get("yes_ask")), int(m.get("yes_bid"))
    except (TypeError, ValueError):
        return None
    if not (1 <= ask <= MAX_ASK_CENTS and 1 <= bid <= 99):
        return None
    if ask - bid > MAX_SPREAD_CENTS:
        return None
    return ask, bid


def find_eligible_market(client, series_list=CANDIDATE_SERIES):
    """Un passage de scan (logique historique inchangee) : premier series
    avec candidat, ask le plus bas. Retourne (ticker, ask) ou None,
    plus (marches_vus, meilleur_ask_vu) pour le log de polling."""
    candidate, scanned, best_ask_seen = None, 0, None
    for series in series_list:
        for m in client.get_markets(series, status="open", limit=100):
            scanned += 1
            try:
                a = int(m.get("yes_ask"))
                if 1 <= a <= 99:
                    best_ask_seen = a if best_ask_seen is None \
                        else min(best_ask_seen, a)
            except (TypeError, ValueError):
                pass
            elig = market_is_eligible(m)
            if elig is None:
                continue
            ask, _bid = elig
            if candidate is None or ask < candidate[1]:
                candidate = (m["ticker"], ask)
        if candidate:
            break
    return candidate, scanned, best_ask_seen


def wait_for_eligible_market(client, interval_s, max_wait_s,
                             sleep_fn=time.sleep,
                             monotonic_fn=time.monotonic):
    """Polling BORNE : scanne, journalise une ligne concise par tentative,
    attend interval_s entre les tentatives, s'arrete a max_wait_s.
    Retourne (ticker, ask) ou None (timeout = NO_ELIGIBLE_MARKET, pas un
    echec). Une erreur API pendant un scan (get_markets -> []) ne casse
    rien : on continue d'attendre en securite."""
    t0 = monotonic_fn()
    attempt = 0
    while True:
        attempt += 1
        candidate, scanned, best_ask = find_eligible_market(client)
        elapsed = monotonic_fn() - t0
        if candidate:
            print(f"[PROBE_ELIGIBLE] ticker={candidate[0]} "
                  f"yes_ask={candidate[1]}c attempt={attempt} "
                  f"elapsed_s={elapsed:.0f}")
            return candidate
        remaining = max_wait_s - elapsed
        if remaining <= 0:
            print(f"[NO_ELIGIBLE_MARKET] attempts={attempt} "
                  f"elapsed_s={elapsed:.0f} max_wait_s={max_wait_s:.0f} "
                  f"criteria=ask<={MAX_ASK_CENTS}c,"
                  f"spread<={MAX_SPREAD_CENTS}c (INCHANGES)")
            return None
        wait = min(interval_s, remaining)
        print(f"[PROBE_POLL] attempt={attempt} elapsed_s={elapsed:.0f} "
              f"eligible=false markets_scanned={scanned} "
              f"best_ask_seen={best_ask if best_ask is not None else 'n/a'} "
              f"next_poll_in_s={wait:.0f}")
        sleep_fn(wait)


def resolve_ambiguous_submission(client, ticker, coid):
    """Reponse POST AMBIGUE (erreur reseau, statut inconnu) : la SEULE
    action autorisee est d'interroger le broker par client_order_id —
    JAMAIS de re-POST (un doublon d'ordre est irrecuperable, un ordre
    manque ne l'est pas). Retourne l'ordre broker (dict) ou None si le
    broker confirme qu'aucun ordre ne porte ce coid ; propage
    KalshiAPIError si la resolution elle-meme echoue (on ne conclut
    JAMAIS 'aucun ordre' sur un echec reseau)."""
    for o in client.get_orders(ticker):
        if str(o.get("client_order_id") or "") == coid:
            return o
    return None


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

    # ── 2. attente BORNEE d'un marche naturellement conforme ──────────────
    # AUD-PROBE-001 : criteres INCHANGES ; seule l'attente est nouvelle.
    interval_s, max_wait_s = poll_bounds()
    print(f"[PROBE_CONFIG] poll_interval_s={interval_s:.0f} "
          f"max_wait_s={max_wait_s:.0f} series={','.join(CANDIDATE_SERIES)} "
          f"criteria=ask<={MAX_ASK_CENTS}c,spread<={MAX_SPREAD_CENTS}c")
    candidate = wait_for_eligible_market(client, interval_s, max_wait_s)
    if not candidate:
        # Timeout SANS marche conforme : sortie PROPRE, pas un echec
        # d'execution — les bornes n'ont pas ete elargies pour forcer
        # un ordre. Relancer la sonde plus tard (heures liquides US).
        proof.update({"outcome": "NO_ELIGIBLE_MARKET",
                      "poll_interval_s": interval_s,
                      "max_wait_s": max_wait_s})
        _write(proof)
        sys.exit(0)
    ticker, ask = candidate
    print(f"[CHECK] market={ticker} yes_ask={ask}c (cout max "
          f"{ask / 100:.2f}$ pour 1 contrat)")
    proof["ticker"] = ticker

    # ── 3. soumission REELLE d'un ordre minimal (1 contrat, au ask) ───────
    coid = f"democheck_{uuid.uuid4().hex}"
    # AUD-OBS-003: l'endpoint affiche etait /portfolio/orders (V1,
    # deprecie) alors que create_order POSTe sur ORDERS_V2_PATH — un
    # diagnostic 404 mene sur le mauvais chemin. Afficher le chemin REEL.
    print(f"[ORDER_SUBMIT_ATTEMPT] ticker={ticker} side=yes action=buy "
          f"count=1 price_cents={ask} client_order_id={coid} "
          f"environment=DEMO endpoint={client.ORDERS_V2_PATH}")
    proof.update({"order_submission_attempted": True,
                  "client_order_id": coid})
    try:
        order = client.create_order(ticker, "yes", 1, ask,
                                    client_order_id=coid)
    except KalshiAPIError as e:
        if e.status == 0:
            # AMBIGU (erreur reseau : le POST a PU atteindre le broker).
            # SEULE action autorisee : interroger le broker par
            # client_order_id. JAMAIS de re-POST. NB : les retries
            # internes de _req reutilisent le MEME client_order_id
            # (idempotence preservee cote broker).
            print(f"[ORDER_SUBMIT_AMBIGUOUS] client_order_id={coid} "
                  f"error={e} action=broker_query_only never_repost=true")
            try:
                resolved = resolve_ambiguous_submission(client, ticker, coid)
            except KalshiAPIError as e2:
                print(f"[ORDER_SUBMIT_AMBIGUOUS_UNRESOLVED] "
                      f"client_order_id={coid} lookup_error={e2} — statut "
                      f"INDETERMINE, aucun re-POST. Resolution operateur "
                      f"requise (GET /portfolio/orders).")
                proof.update({"outcome": "AMBIGUOUS_UNRESOLVED",
                              "order_verified_from_api": False,
                              "error": str(e)[:300]})
                _write(proof)
                sys.exit(3)
            if resolved is None:
                print(f"[ORDER_SUBMIT_AMBIGUOUS_RESOLVED] "
                      f"client_order_id={coid} broker_has_order=false — "
                      f"aucun ordre place (confirme par le broker), "
                      f"aucun re-POST (une seule tentative par execution).")
                proof.update({"outcome": "AMBIGUOUS_RESOLVED_NOT_PLACED",
                              "order_verified_from_api": False,
                              "error": str(e)[:300]})
                _write(proof)
                sys.exit(1)
            print(f"[ORDER_SUBMIT_AMBIGUOUS_RESOLVED] "
                  f"client_order_id={coid} broker_has_order=true "
                  f"kalshi_order_id={resolved.get('order_id')} — reprise "
                  f"du cycle de vie sur l'ordre retrouve, aucun re-POST.")
            order = resolved
        else:
            print(f"[ORDER_SUBMIT_FAILED] http_status={e.status} "
                  f"error_message={e} "
                  f"response_body_sanitized={str(e.body)[:300]}")
            proof.update({"http_status": e.status,
                          "order_verified_from_api": False,
                          "error": str(e.body)[:300]})
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
