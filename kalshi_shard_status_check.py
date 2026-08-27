#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kalshi_shard_status_check.py — Diagnostic LECTURE SEULE du sharding DEMO.
(AUD-DEMO-TRADING-IDENTITY-003, complement operateur 2026-08-27)

Mesure, sur l'hote DEMO courant, pour les exchange_index 0 et 2 :
  - GET /exchange/status?exchange_index=N   (exchange_active,
    trading_active, intra_exchange_transfers_active)
  - GET /portfolio/balance?exchange_index=N (solde par instance)
  - GET /portfolio/subaccounts/balances     (table complete par instance)

CE SCRIPT NE CONTIENT AUCUN CHEMIN D'ORDRE : uniquement des GET signes.
Aucun POST/PUT/PATCH/DELETE, aucun transfert, aucun ordre, jamais LIVE.
Aucun secret journalise. Sort toujours en code 0 (diagnostic) sauf
garde-fous d'environnement (code 2).
"""
import json
import os
import sys

from kalshi_client import KalshiClient, KalshiAPIError
from config import DEMO_URLS_ALLOWED

#: instances a mesurer (surchargable : SHARD_STATUS_INDEXES="0,1,2,3")
DEFAULT_INDEXES = (0, 2)


def fatal(msg):
    print(f"[FATAL] {msg}")
    sys.exit(2)


def _indexes():
    raw = os.getenv("SHARD_STATUS_INDEXES", "")
    if not raw.strip():
        return DEFAULT_INDEXES
    out = []
    for p in raw.split(","):
        try:
            out.append(int(p))
        except ValueError:
            continue
    return tuple(out) or DEFAULT_INDEXES


def _fmt_balance(r):
    """Extrait un solde en dollars depuis une reponse balance (champ
    *_dollars prioritaire, sinon cents legacy)."""
    for k in ("balance_dollars", "available_balance_dollars"):
        if r.get(k) is not None:
            try:
                return f"{float(r[k]):.2f}"
            except (TypeError, ValueError):
                pass
    for k in ("balance", "available_balance"):
        if r.get(k) is not None:
            try:
                return f"{float(r[k]) / 100.0:.2f}"
            except (TypeError, ValueError):
                pass
    return "?"


def check_exchange_status(client, idx):
    """GET /exchange/status?exchange_index=idx — LECTURE SEULE."""
    try:
        r = client._req("GET", "/exchange/status",
                        params={"exchange_index": idx})
    except KalshiAPIError as e:
        print(f"[SHARD_STATUS] index={idx} state=UNAVAILABLE "
              f"http_status={e.status} error={str(e)[:120]}")
        return
    print(f"[SHARD_STATUS] index={idx} "
          f"exchange_active={r.get('exchange_active')} "
          f"trading_active={r.get('trading_active')} "
          f"transfers_active="
          f"{r.get('intra_exchange_transfers_active', r.get('transfers_active'))} "
          f"raw_sanitized={json.dumps(r, default=str)[:300]}")


def check_balance_for_index(client, idx):
    """GET /portfolio/balance?exchange_index=idx — LECTURE SEULE."""
    try:
        r = client._req("GET", "/portfolio/balance",
                        params={"exchange_index": idx})
    except KalshiAPIError as e:
        print(f"[SHARD_BALANCE] index={idx} state=UNAVAILABLE "
              f"http_status={e.status} error={str(e)[:120]}")
        return
    print(f"[SHARD_BALANCE] index={idx} balance={_fmt_balance(r)} "
          f"raw_sanitized={json.dumps(r, default=str)[:300]}")


def check_subaccounts_table(client):
    """GET /portfolio/subaccounts/balances — table complete, une ligne
    par (subaccount, exchange_index)."""
    try:
        rows = client.get_subaccounts_balances()
    except KalshiAPIError as e:
        print(f"[SHARD_TABLE] state=UNAVAILABLE http_status={e.status} "
              f"error={str(e)[:120]}")
        return
    for row in rows or []:
        print(f"[SHARD_TABLE] subaccount={row.get('subaccount')} "
              f"exchange_index={row.get('exchange_index')} "
              f"balance={_fmt_balance(row)}")
    if not rows:
        print("[SHARD_TABLE] aucune_entree")


def main():
    for var in ("LIVE_TRADING", "KALSHI_ENV_CONFIRM"):
        if os.getenv(var, "").upper() in ("1", "LIVE"):
            fatal(f"{var} indique un contexte LIVE — diagnostic DEMO "
                  f"uniquement, arret.")
    client = KalshiClient("demo")
    if client.base_url.rstrip("/") not in DEMO_URLS_ALLOWED:
        fatal(f"URL inattendue: {client.base_url} — seules les racines "
              f"DEMO {DEMO_URLS_ALLOWED} sont autorisees.")
    print(f"[SHARD_CHECK] base={client.base_url} read_only=true "
          f"indexes={','.join(str(i) for i in _indexes())}")
    for idx in _indexes():
        check_exchange_status(client, idx)
        check_balance_for_index(client, idx)
    check_subaccounts_table(client)
    print("[SHARD_CHECK_DONE] aucune ecriture effectuee "
          "(GET uniquement, zero ordre, zero transfert).")
    sys.exit(0)


if __name__ == "__main__":
    main()
