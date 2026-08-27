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
from datetime import datetime, timezone


from kalshi_client import (KalshiClient, KalshiAPIError, pick, pick_int)  # noqa: E402
from kalshi_alpha_bot import _client_is_genuine
from market_scanner import read_price          # parseur PROUVE du moteur
from config import CFG, DEMO_URLS_ALLOWED

DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
# AUD-DEMO-ORDER-IDENTITY-002 : deux racines DEMO officielles connues
# (hote partage historique + hote Trade API dedie recommande par la doc).
# La sonde n'accepte QUE ces deux racines — jamais prod/LIVE.
DEMO_ALLOWED_BASES = DEMO_URLS_ALLOWED
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


# AUD-PROBE-002 (2026-08-27) : la decouverte lisait les cotes par
# int(m["yes_ask"]) brut — invisible des que l'API sert les variantes
# reelles du schema V2 (chaines decimales "48.00", champs fixed-point
# *_dollars "0.4800", cote vide encodee 0 ou "1.0000", cote YES derivable
# du carnet NO). Le MOTEUR gere ces formats depuis la regression corrigee
# du 2026-07-25 (market_scanner.read_price) ; la sonde ne les heritait
# pas — d'ou best_ask_seen=n/a sur 361 tentatives x ~202 marches alors
# que le moteur burn-in mesure des prix sur la MEME API. La sonde utilise
# desormais le MEME parseur, plus un entonnoir de rejets TYPES.
# Criteres ask<=30c / spread<=5c STRICTEMENT inchanges.

#: statuts explicitement NON ouverts -> rejet type not_open (renforce la
#: validation, ne l'affaiblit pas) ; statut absent/inconnu = tolere (la
#: requete filtre deja status=open) mais COMPTE (status_unknown).
NOT_OPEN_STATUSES = ("closed", "settled", "finalized", "determined",
                     "inactive", "unopened", "paused")
OPEN_STATUSES = ("open", "active")


def _raw_quote_reason(m, names, side):
    """Cote non parsable : invalid_price si une valeur presente est
    illisible (garbage) ; sinon missing_<side> (champ absent, ou cote
    VIDE encodee 0 / "1.0000" — semantique 'aucune cote')."""
    for k in names:
        v = m.get(k)
        if v is None:
            continue
        try:
            float(v)
        except (TypeError, ValueError):
            return "invalid_price"
    return f"missing_{side}"


def parse_quote(m):
    """Cote YES NORMALISEE en cents entiers 1..99 via le parseur du
    moteur : cents legacy, chaines decimales, variantes *_dollars, et
    derivation depuis le carnet NO (acheter YES a p == croiser un bid NO
    a 100-p ; meme regle que market_scanner.liquidity_diag).
    Retourne (ask, bid, reason) — reason None si les deux cotes existent."""
    ya, yb = read_price(m, "yes_ask"), read_price(m, "yes_bid")
    na, nb = read_price(m, "no_ask"), read_price(m, "no_bid")
    ask = ya if ya is not None else (100 - nb if nb is not None else None)
    bid = yb if yb is not None else (100 - na if na is not None else None)
    if ask is None:
        return None, bid, _raw_quote_reason(
            m, ("yes_ask", "yes_ask_dollars", "no_bid", "no_bid_dollars"),
            "ask")
    if bid is None:
        return ask, None, _raw_quote_reason(
            m, ("yes_bid", "yes_bid_dollars", "no_ask", "no_ask_dollars"),
            "bid")
    return ask, bid, None


def classify_market(m, series=None):
    """Classement TYPE d'un marche. Retourne (reason, ask, bid) ;
    reason None => ELIGIBLE (ask <= 30c ET spread <= 5c, INCHANGES).
    NB : un prefixe de ticker inattendu est COMPTE (series_mismatch, via
    l'entonnoir) mais n'exclut PAS — le filtre series_ticker de l'API est
    la reference ; une heuristique de prefixe pourrait elle-meme casser
    silencieusement la decouverte (risque Q8 de l'audit)."""
    status = str(m.get("status") or "").lower()
    if status in NOT_OPEN_STATUSES:
        return "not_open", None, None
    ask, bid, reason = parse_quote(m)
    if reason:
        return reason, ask, bid
    if ask > MAX_ASK_CENTS:
        return "ask_too_high", ask, bid
    if ask - bid > MAX_SPREAD_CENTS:
        return "spread_too_wide", ask, bid
    return None, ask, bid


def market_is_eligible(m):
    """Compat : (ask, bid) si eligible, sinon None. Criteres EXACTS
    inchanges (ask <= 30c, spread <= 5c)."""
    reason, ask, bid = classify_market(m)
    return (ask, bid) if reason is None else None


def _sanitized_sample(m, series):
    """Echantillon de debug : UNIQUEMENT les champs de cotation et
    d'identite (jamais de payload brut complet, jamais de secret)."""
    reason, ask, bid = classify_market(m, series)
    return {"ticker": m.get("ticker"), "event": m.get("event_ticker"),
            "series": series, "status": m.get("status"),
            "exchange_index": m.get("exchange_index"),
            "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
            "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
            "yes_ask_dollars": m.get("yes_ask_dollars"),
            "parsed_ask": ask, "parsed_bid": bid,
            "parsed_spread": (ask - bid) if ask is not None
            and bid is not None else None,
            "reason": reason or "eligible"}


def find_eligible_market(client, series_list=CANDIDATE_SERIES):
    """Un passage de scan : premier series avec candidat, ask le plus
    bas. Retourne (candidate|None, funnel) ; funnel expose les comptes
    d'entonnoir, les raisons de rejet typees, best_ask_seen (numerique
    des qu'UN ask est parsable, meme sans marche eligible) et un
    echantillon sanitise de 1-3 marches."""
    funnel = {"markets_total": 0, "series_mismatch": 0,
              "status_unknown": 0, "open": 0, "ask_available": 0,
              "quote_available": 0, "spread_available": 0, "ask_pass": 0,
              "spread_pass": 0, "eligible": 0, "rejections": {},
              "best_ask_seen": None, "sample": [],
              "candidate_exchange_index": None}
    candidate = None
    for series in series_list:
        for m in client.get_markets(series, status="open", limit=100):
            funnel["markets_total"] += 1
            if len(funnel["sample"]) < 3:
                funnel["sample"].append(_sanitized_sample(m, series))
            if series and not str(m.get("ticker") or "").startswith(series):
                funnel["series_mismatch"] += 1
            status = str(m.get("status") or "").lower()
            if status not in OPEN_STATUSES:
                if status not in NOT_OPEN_STATUSES:
                    funnel["status_unknown"] += 1
            reason, ask, bid = classify_market(m, series)
            if reason != "not_open":
                funnel["open"] += 1
            if ask is not None:
                funnel["ask_available"] += 1
                funnel["best_ask_seen"] = ask \
                    if funnel["best_ask_seen"] is None \
                    else min(funnel["best_ask_seen"], ask)
            if ask is not None and bid is not None:
                funnel["quote_available"] += 1
                funnel["spread_available"] += 1
                if ask <= MAX_ASK_CENTS:
                    funnel["ask_pass"] += 1
                if ask - bid <= MAX_SPREAD_CENTS:
                    funnel["spread_pass"] += 1
            if reason is not None:
                funnel["rejections"][reason] = \
                    funnel["rejections"].get(reason, 0) + 1
                continue
            funnel["eligible"] += 1
            if candidate is None or ask < candidate[1]:
                candidate = (m["ticker"], ask)
                # AUD-DEMO-TRADING-IDENTITY-003 : instance d'echange du
                # marche retenu (contrat sharding 2026-08 — le Create
                # Order route vers l'instance du marche).
                funnel["candidate_exchange_index"] = m.get("exchange_index")
        if candidate:
            break
    return candidate, funnel


def _funnel_line(funnel):
    rej = ",".join(f"{k}:{v}" for k, v in
                   sorted(funnel["rejections"].items())) or "none"
    return (f"[PROBE_FUNNEL] markets_total={funnel['markets_total']} "
            f"open={funnel['open']} "
            f"quote_available={funnel['quote_available']} "
            f"ask_available={funnel['ask_available']} "
            f"spread_available={funnel['spread_available']} "
            f"ask_pass={funnel['ask_pass']} "
            f"spread_pass={funnel['spread_pass']} "
            f"eligible={funnel['eligible']} "
            f"series_mismatch={funnel['series_mismatch']} "
            f"status_unknown={funnel['status_unknown']} "
            f"rejections={rej}")


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
        candidate, funnel = find_eligible_market(client)
        elapsed = monotonic_fn() - t0
        if attempt == 1:
            # AUD-PROBE-002 : echantillon sanitise (1-3 marches, champs
            # de cotation/identite uniquement) UNE FOIS par execution.
            for s in funnel["sample"]:
                print(f"[PROBE_SAMPLE] {json.dumps(s, default=str)}")
        print(_funnel_line(funnel))
        if candidate:
            print(f"[PROBE_ELIGIBLE] ticker={candidate[0]} "
                  f"yes_ask={candidate[1]}c "
                  f"exchange_index={funnel.get('candidate_exchange_index')} "
                  f"attempt={attempt} elapsed_s={elapsed:.0f}")
            return candidate, funnel
        remaining = max_wait_s - elapsed
        best_ask = funnel["best_ask_seen"]
        if remaining <= 0:
            print(f"[NO_ELIGIBLE_MARKET] attempts={attempt} "
                  f"elapsed_s={elapsed:.0f} max_wait_s={max_wait_s:.0f} "
                  f"criteria=ask<={MAX_ASK_CENTS}c,"
                  f"spread<={MAX_SPREAD_CENTS}c (INCHANGES)")
            return None, funnel
        wait = min(interval_s, remaining)
        print(f"[PROBE_POLL] attempt={attempt} elapsed_s={elapsed:.0f} "
              f"eligible=false markets_scanned={funnel['markets_total']} "
              f"best_ask_seen={best_ask if best_ask is not None else 'n/a'} "
              f"next_poll_in_s={wait:.0f}")
        sleep_fn(wait)


def shard_preflight(client, market_exchange_index):
    """AUD-DEMO-TRADING-IDENTITY-003 — pre-vol LECTURE SEULE (sharding).
    Contrat 2026-08 : les soldes sont LOCAUX a une instance d'echange et
    le Create Order route vers l'instance du marche vise. Un compte
    jamais provisionne sur cette instance ne peut pas y resoudre son
    utilisateur (404 user_not_found observe). Retourne (state, funds) :
      PROVISIONED                     fonds presents sur l'instance ;
      NOT_PROVISIONED_ON_MARKET_SHARD 0 sur l'instance du marche ;
      UNAVAILABLE                     diagnostic impossible (marche sans
                                      exchange_index, endpoint absent,
                                      erreur) -> comportement historique
                                      INCHANGE, la sonde continue."""
    if market_exchange_index is None:
        return "UNAVAILABLE", None
    try:
        idx = int(market_exchange_index)
    except (TypeError, ValueError):
        return "UNAVAILABLE", None
    try:
        rows = client.get_subaccounts_balances()
    except Exception as e:                    # noqa: BLE001 — diagnostic
        print(f"[SHARD_DIAG] state=UNAVAILABLE "
              f"reason={type(e).__name__}:{str(e)[:120]}")
        return "UNAVAILABLE", None
    funds = {}
    for row in rows or []:
        try:
            i = int(row.get("exchange_index"))
        except (TypeError, ValueError):
            continue
        raw = row.get("balance_dollars", row.get("balance", 0))
        try:
            v = float(raw)
        except (TypeError, ValueError):
            v = 0.0
        if "balance_dollars" not in row:
            v = v / 100.0                     # champ legacy en cents
        funds[i] = funds.get(i, 0.0) + v
    if not funds:
        print("[SHARD_DIAG] state=UNAVAILABLE reason=aucune_entree")
        return "UNAVAILABLE", None
    state = ("PROVISIONED" if funds.get(idx, 0.0) > 0.0
             else "NOT_PROVISIONED_ON_MARKET_SHARD")
    print(f"[SHARD_DIAG] state={state} market_exchange_index={idx} "
          f"funds_by_index="
          + ",".join(f"{i}:{funds[i]:.2f}" for i in sorted(funds)))
    return state, funds


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


# ── AUD-DEMO-LIFECYCLE-005 : garde de soumission PERSISTANTE ────────────────
# Cause mesuree des fills repetes (11 contrats YES) : chaque redemarrage
# Railway relancait la sonde avec un client_order_id ALEATOIRE neuf —
# l'idempotence en memoire ne survit pas au restart. Desormais :
#   1. client_order_id DETERMINISTE par run-id (defaut : date UTC) ;
#   2. journal d'intention PERSISTANT (DATA_DIR) ecrit AVANT le POST ;
#   3. pre-verification cote BROKER par client_order_id (survit meme a
#      un systeme de fichiers neuf) ;
#   4. toute trace de soumission anterieure non explicitement echouee
#      -> AUCUN nouveau POST, reconciliation LECTURE SEULE.

GUARD_FILE = "demo_probe_submission_guard.json"


def probe_run_id():
    """Identifiant logique de la sonde : DEMO_PROBE_RUN_ID, sinon la
    date UTC — un seul ordre de test logique par run-id, quel que soit
    le nombre de redemarrages du conteneur."""
    rid = os.getenv("DEMO_PROBE_RUN_ID", "").strip()
    if not rid:
        rid = datetime.now(timezone.utc).strftime("%Y%m%d")
    return "".join(c for c in rid if c.isalnum() or c in "-_")[:40]


def _guard_path():
    return os.path.join(os.getenv("DATA_DIR", "."), GUARD_FILE)


def load_guard():
    """(records, lisible). Un fichier CORROMPU rend lisible=False : la
    sonde doit alors rester fail-closed (on ne conclut jamais 'aucune
    soumission anterieure' sur un journal illisible)."""
    try:
        with open(_guard_path(), encoding="utf-8") as f:
            data = json.load(f)
        return (data, True) if isinstance(data, list) else ([], False)
    except FileNotFoundError:
        return [], True
    except Exception:                      # noqa: BLE001 — corrompu
        return [], False


def save_guard(records):
    tmp = _guard_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _guard_path())


def guard_record_for(records, run_id):
    for r in reversed(records):
        if r.get("run_id") == run_id:
            return r
    return None


def update_guard(records, run_id, **fields):
    rec = guard_record_for(records, run_id)
    if rec is not None:
        rec.update(fields)
        save_guard(records)


def find_order_by_coid(client, coid):
    """LECTURE SEULE : retrouve un ordre broker par client_order_id.
    Les erreurs REMONTENT (jamais 'aucun ordre' sur un echec reseau)."""
    for o in client.get_orders():
        if str(o.get("client_order_id") or "") == coid:
            return o
    return None


def reconcile_prior_submission(client, coid, proof, why):
    """Une soumission anterieure existe (journal local ou broker) :
    AUCUN nouveau POST. Etat reconstitue par lectures uniquement."""
    print(f"[SUBMISSION_GUARD] blocked=true reason={why} "
          f"client_order_id={coid} — aucun nouveau POST ; "
          f"reconciliation lecture seule.")
    order, fills = None, []
    try:
        order = find_order_by_coid(client, coid)
    except KalshiAPIError as e:
        print(f"[GUARD_RECONCILE] lookup_error={e} — statut broker "
              f"INDETERMINE, resolution operateur requise.")
    oid = str((order or {}).get("order_id") or "")
    if oid:
        try:
            fills = client.get_fills(oid)
        except KalshiAPIError:
            fills = []
    filled = sum(pick_int(f, "count", "quantity", default=0)
                 for f in fills)
    print(f"[GUARD_RECONCILE] broker_has_order={bool(order)} "
          f"kalshi_order_id={oid or 'n/a'} "
          f"status={(order or {}).get('status', 'n/a')} "
          f"filled_contracts={filled} "
          f"outcome=ALREADY_SUBMITTED_RECONCILED")
    proof.update({"outcome": "ALREADY_SUBMITTED_RECONCILED",
                  "client_order_id": coid,
                  "kalshi_order_id": oid or None,
                  "filled_contracts": filled})
    _write(proof)
    sys.exit(0)


def identity_fingerprint(client):
    """AUD-DEMO-ORDER-IDENTITY-002 : empreinte SANS SECRET de l'identite
    API effectivement utilisee — permet de comparer la cle Railway avec
    la cle visible dans l'UI demo (sha256/suffixe du key id, sha256 de
    la cle PUBLIQUE derivee). Aucun materiau prive n'est expose."""
    import hashlib
    kid = str(getattr(client, "key_id", "") or "")
    fp = {"key_id_sha256_12": hashlib.sha256(kid.encode()).hexdigest()[:12],
          "key_id_suffix4": kid[-4:] if len(kid) >= 4 else "?",
          "pubkey_sha256_12": None}
    pk = getattr(client, "_pk", None)
    if pk is not None:
        from cryptography.hazmat.primitives import serialization
        der = pk.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        fp["pubkey_sha256_12"] = hashlib.sha256(der).hexdigest()[:12]
    return fp


def main():
    # ── garde-fous stricts ────────────────────────────────────────────────
    if os.getenv("ENABLE_DEMO_INTEGRATION_TEST", "").lower() != "true":
        fatal("ENABLE_DEMO_INTEGRATION_TEST=true est requis (explicite).")
    for var in ("LIVE_TRADING", "KALSHI_ENV_CONFIRM"):
        if os.getenv(var, "").upper() in ("1", "LIVE"):
            fatal(f"{var} indique un contexte LIVE — ce script est "
                  f"DEMO uniquement, arret.")
    client = KalshiClient("demo")
    if client.base_url.rstrip("/") not in DEMO_ALLOWED_BASES:
        fatal(f"URL inattendue: {client.base_url} — seules les racines "
              f"DEMO {DEMO_ALLOWED_BASES} sont autorisees. Arret sans "
              f"aucun appel d'ordre.")
    if not _client_is_genuine(client):
        fatal("Client non authentique (mock/patch detecte).")
    fp = identity_fingerprint(client)
    print(f"[IDENTITY_FINGERPRINT] key_id_sha256_12={fp['key_id_sha256_12']} "
          f"key_id_suffix4={fp['key_id_suffix4']} "
          f"pubkey_sha256_12={fp['pubkey_sha256_12']} "
          f"base={client.base_url}")

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
    # AUD-PROBE-002 : mode DECOUVERTE SEULE — observe les cotes reelles et
    # l'entonnoir, N'EMET JAMAIS d'ordre (sonde courte de visibilite).
    discovery_only = os.getenv(
        "DEMO_PROBE_DISCOVERY_ONLY", "").lower() == "true"
    if discovery_only:
        print("[PROBE_MODE] discovery_only=true — AUCUNE soumission "
              "d'ordre dans cette execution, quel que soit le resultat.")
    candidate, funnel = wait_for_eligible_market(client, interval_s,
                                                 max_wait_s)
    if not candidate:
        # Timeout SANS marche conforme : sortie PROPRE, pas un echec
        # d'execution — les bornes n'ont pas ete elargies pour forcer
        # un ordre. Relancer la sonde plus tard (heures liquides US).
        proof.update({"outcome": "NO_ELIGIBLE_MARKET",
                      "discovery_only": discovery_only,
                      "poll_interval_s": interval_s,
                      "max_wait_s": max_wait_s})
        _write(proof)
        sys.exit(0)
    ticker, ask = candidate
    if discovery_only:
        print(f"[DISCOVERY_ONLY] marche eligible OBSERVE ticker={ticker} "
              f"yes_ask={ask}c — soumission DESACTIVEE, arret propre "
              f"sans POST.")
        proof.update({"outcome": "DISCOVERY_ONLY_ELIGIBLE_SEEN",
                      "ticker": ticker, "discovery_only": True})
        _write(proof)
        sys.exit(0)

    # ── 2bis. pre-vol sharding (LECTURE SEULE, AUD-DEMO-TRADING-003) ──────
    mkt_idx = funnel.get("candidate_exchange_index")
    shard_state, shard_funds = shard_preflight(client, mkt_idx)
    if shard_state == "NOT_PROVISIONED_ON_MARKET_SHARD":
        print(f"[SHARD_NOT_PROVISIONED] le compte demo n'a AUCUN solde "
              f"sur l'instance d'echange {mkt_idx} du marche {ticker} — "
              f"le contrat 2026-08 (exchange sharding) route le Create "
              f"Order vers cette instance ; un POST y echoue "
              f"(user_not_found observe). AUCUN ordre envoye. Remede "
              f"operateur : transferer des fonds demo vers l'instance "
              f"{mkt_idx} (UI demo, ou POST "
              f"/portfolio/intra_exchange_instance_transfer), puis "
              f"relancer la sonde.")
        proof.update({"outcome": "SHARD_NOT_PROVISIONED",
                      "ticker": ticker,
                      "market_exchange_index": mkt_idx,
                      "funds_by_index": shard_funds})
        _write(proof)
        sys.exit(0)
    print(f"[CHECK] market={ticker} yes_ask={ask}c (cout max "
          f"{ask / 100:.2f}$ pour 1 contrat)")
    proof["ticker"] = ticker

    # ── 3. garde PERSISTANTE puis soumission (1 contrat, au ask) ──────────
    # AUD-DEMO-LIFECYCLE-005 : client_order_id DETERMINISTE (run-id) +
    # journal d'intention persistant + pre-verification broker — un
    # redemarrage Railway ne peut plus produire un ordre duplique.
    run_id = probe_run_id()
    coid = f"democheck_{run_id}"
    records, readable = load_guard()
    if not readable:
        print(f"[SUBMISSION_GUARD] outcome=GUARD_UNREADABLE_FAIL_CLOSED "
              f"journal ILLISIBLE ({_guard_path()}) — fail-closed, AUCUN "
              f"POST. Verifier l'etat broker puis restaurer/supprimer le "
              f"fichier manuellement.")
        proof.update({"outcome": "GUARD_UNREADABLE_FAIL_CLOSED"})
        _write(proof)
        sys.exit(3)
    prior = guard_record_for(records, run_id)
    if prior and not str(prior.get("outcome", "")).startswith("failed"):
        reconcile_prior_submission(
            client, coid, proof,
            f"journal_local(outcome={prior.get('outcome')})")
    try:
        existing = find_order_by_coid(client, coid)
    except KalshiAPIError as e:
        print(f"[SUBMISSION_GUARD] outcome=GUARD_UNVERIFIABLE_FAIL_CLOSED "
              f"pre-verification broker IMPOSSIBLE ({e}) — fail-closed, "
              f"AUCUN POST (on ne conclut jamais 'aucune soumission "
              f"anterieure' sur un echec de lecture).")
        proof.update({"outcome": "GUARD_UNVERIFIABLE_FAIL_CLOSED"})
        _write(proof)
        sys.exit(3)
    if existing:
        reconcile_prior_submission(client, coid, proof, "ordre_broker")
    # intention persistee AVANT le POST (semantique journal d'intention :
    # un crash pendant le POST laisse 'attempting' -> le prochain run
    # BLOQUE et reconcilie au lieu de re-POSTer)
    records.append({"run_id": run_id, "client_order_id": coid,
                    "ticker": ticker,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "order_id": None, "outcome": "attempting"})
    save_guard(records)
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
                update_guard(records, run_id, outcome="ambiguous")
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
                update_guard(records, run_id,
                             outcome="failed:ambiguous_not_placed")
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
            # echec DETERMINISTE (le broker a refuse : aucun ordre créé)
            # -> la garde enregistre 'failed:<status>' ; une relance
            # deliberee du MEME run-id reste possible, mais jamais de
            # retry automatique dans cette execution.
            update_guard(records, run_id, outcome=f"failed:{e.status}")
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
        # 201 sans order_id lisible : l'ordre a PU etre cree — etat
        # AMBIGU persiste, jamais de re-POST, resolution operateur.
        update_guard(records, run_id, outcome="ambiguous")
        print("[AMBIGUOUS_POST_CREATED_STATE] reponse 201 sans order_id "
              "lisible — aucun re-POST, reconciliation au prochain run.")
        proof.update({"outcome": "AMBIGUOUS_POST_CREATED_STATE"})
        _write(proof)
        sys.exit(3)
    # AUD-DEMO-LIFECYCLE-005 : un HTTP 201 avec order_id est la preuve
    # AUTORITAIRE que l'ordre existe. Etat ORDER_CREATED_CONFIRMED,
    # persiste AVANT toute relecture faillible.
    update_guard(records, run_id, outcome="created", order_id=oid)
    print(f"[ORDER_CREATED_CONFIRMED] kalshi_order_id={oid} "
          f"client_order_id={coid} http_status={http_status}")

    # ── 4. relecture de l'ordre (2e requete API, meme id) ─────────────────
    # Moteur 12.5.0 (2026-07-26, logs reels) : GET /portfolio/orders/{id}
    # peut repondre 404 pour un ordre V2 REEL et REMPLI. Apres un 201,
    # un 404 de relecture est ORDER_LOOKUP_UNAVAILABLE — JAMAIS
    # 'ordre inexistant', JAMAIS un echec, JAMAIS un re-POST : la
    # reponse de creation sert de verification
    # (source=create_response_v2), comme dans le moteur.
    try:
        verified = client.get_order(oid)
        verify_source = "get_order"
    except KalshiAPIError as e:
        print(f"[ORDER_LOOKUP_UNAVAILABLE] kalshi_order_id={oid} "
              f"http_status={e.status} — l'ordre reste CONFIRME CREE "
              f"(201) ; verification par la reponse de creation "
              f"(source=create_response_v2).")
        verified = order
        verify_source = "create_response_v2"
    status = str(pick(verified, "status", "order_status", default="?"))
    print(f"[ORDER_VERIFY] kalshi_order_id={oid} status={status} "
          f"remaining_count={pick_int(verified, 'remaining_count', default=-1)} "
          f"filled_count={pick_int(verified, 'taker_fill_count', 'fill_count', default=-1)} "
          f"source={verify_source}")
    proof["order_verified_from_api"] = True
    proof["order_verify_source"] = verify_source
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
    if filled >= 1:
        update_guard(records, run_id, outcome="filled")
    if filled < 1:
        print(f"[ORDER_CANCELED_UNFILLED] kalshi_order_id={oid} "
              f"timeout_seconds={FILL_TIMEOUT_S:.0f}")
        try:
            client.cancel_order(oid)
            update_guard(records, run_id, outcome="canceled_unfilled")
        except KalshiAPIError as e:
            # annulation NON prouvee : l'ordre peut rester au carnet —
            # etat conserve 'created' (bloquant), jamais efface.
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
