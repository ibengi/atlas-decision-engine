#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kalshi_position_state_check.py — Audit LECTURE SEULE des positions DEMO.
(AUD-DEMO-LIFECYCLE-005)

Pour chaque position broker : la quantite PARSEE par le MEME contrat
type que le moteur (exchange.kalshi_contracts.parse_position), l'etat du
marche (status, close_time, resultat de reglement), et si la position
compte comme OUVERTE selon la regle REELLE du moteur (ligne broker non
nulle -> comptee ; les marches regles sortent via la passe de reglement
du moteur, pas via la reconciliation).

GET UNIQUEMENT : positions + metadonnees marche. Aucun POST/PUT/PATCH/
DELETE, aucun ordre, aucune annulation, aucun transfert. Exit 0 sauf
garde-fous d'environnement (exit 2).
"""
import json
import os
import sys

from kalshi_client import KalshiClient, KalshiAPIError
from config import DEMO_URLS_ALLOWED, CFG

SETTLED_STATUSES = ("settled", "finalized", "determined")


def fatal(msg):
    print(f"[FATAL] {msg}")
    sys.exit(2)


def audit_positions(client):
    """(lignes_broker, comptees_ouvertes). Imprime une ligne
    [POSITION_AUDIT] par position et le [POSITION_SUMMARY] final."""
    from exchange.kalshi_contracts.types import (SchemaIncompatible,
                                                 parse_position)
    try:
        rows = client.get_positions()
    except KalshiAPIError as e:
        fatal(f"lecture des positions impossible: {e}")
    if rows is None:
        fatal("lecture des positions impossible (reponse nulle)")
    broker_n, counted = 0, 0
    for raw in rows:
        broker_n += 1
        try:
            pos = parse_position(raw)
        except SchemaIncompatible as exc:
            print(f"[POSITION_AUDIT] ticker={raw.get('ticker')} "
                  f"counted_open=unknown reason=UNKNOWN_SCHEMA({exc})")
            continue
        tk = pos.ticker
        market = {}
        try:
            market = client.get_market(tk) or {}
        except KalshiAPIError:
            market = {}
        status = str(market.get("status") or "?").lower()
        settled = status in SETTLED_STATUSES or \
            market.get("result") not in (None, "", "not_determined")
        if pos.abs_count == 0:
            counted_open, reason = False, "position_nulle(filtree)"
        elif settled:
            # regle moteur : la ligne broker non nulle COMPTE tant que la
            # passe de reglement ne l'a pas soldee ; visibilite honnete.
            counted_open, reason = True, ("regle_mais_encore_compte:"
                                          "attente_passe_de_reglement")
        else:
            counted_open, reason = True, "position_broker_non_nulle"
        if counted_open:
            counted += 1
        print(f"[POSITION_AUDIT] ticker={tk} "
              f"exchange_index={market.get('exchange_index')} "
              f"position_fp={pos.abs_count} side={pos.side} "
              f"market_exposure="
              f"{pos.market_exposure.dollars if pos.market_exposure else 'n/a'} "
              f"market_status={status} "
              f"close_time={market.get('close_time')} "
              f"result={market.get('result')} "
              f"settled={settled} "
              f"counted_open={str(counted_open).lower()} reason={reason}")
    print(f"[POSITION_SUMMARY] broker_positions={broker_n} "
          f"counted_open={counted} "
          f"max_open_positions={CFG.MAX_OPEN_POSITIONS}")
    if counted >= CFG.MAX_OPEN_POSITIONS:
        print(f"[POSITION_SUMMARY] guard=max_open_positions BLOQUANT "
              f"({counted}/{CFG.MAX_OPEN_POSITIONS}) — comportement "
              f"correct tant que ces positions portent un risque reel.")
    return broker_n, counted


def main():
    for var in ("LIVE_TRADING", "KALSHI_ENV_CONFIRM"):
        if os.getenv(var, "").upper() in ("1", "LIVE"):
            fatal(f"{var} indique un contexte LIVE — audit DEMO "
                  f"uniquement, arret.")
    client = KalshiClient("demo")
    if client.base_url.rstrip("/") not in DEMO_URLS_ALLOWED:
        fatal(f"URL inattendue: {client.base_url} — seules les racines "
              f"DEMO {DEMO_URLS_ALLOWED} sont autorisees.")
    print(f"[POSITION_CHECK] base={client.base_url} read_only=true")
    audit_positions(client)
    print("[POSITION_CHECK_DONE] aucune ecriture effectuee "
          "(GET uniquement, zero ordre, zero annulation, zero transfert).")
    sys.exit(0)


if __name__ == "__main__":
    main()
