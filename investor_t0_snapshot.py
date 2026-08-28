#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
investor_t0_snapshot.py — Instantane T0 pre-enregistre (LECTURE SEULE).
(ATLAS-TOTAL-AUDIT-001, Domain 14)

Fige, AVANT le demarrage du chronometre officiel de performance :
date/heure UTC, SHA de build, hachages de configuration (strategie +
risque, config_identity), limites de risque effectives, familles de
marches incluses/exclues, fournisseurs de donnees, politique
d'inclusion IP-1, solde broker de depart (global + par exchange_index),
positions ouvertes, ordres ouverts — puis un SHA-256 du paquet
canonique. Toute modification ulterieure de code/config INVALIDE la
cohorte (nouveau T0 obligatoire).

GET uniquement. Aucun ordre, aucun transfert, jamais LIVE. Le
chronometre n'est PAS demarre par ce script : l'operateur archive le
hachage (exit 0) puis demarre la cohorte explicitement.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from kalshi_client import KalshiClient, KalshiAPIError
from config import CFG, DEMO_URLS_ALLOWED
from config_identity import strategy_config_hash, risk_config_hash
from investor_report import INCLUSION_POLICY, build_sha


def fatal(msg):
    print(f"[FATAL] {msg}")
    sys.exit(2)


def _read(fn, default):
    try:
        return fn()
    except (KalshiAPIError, Exception) as e:      # noqa: BLE001
        return {"UNVERIFIED": f"{type(e).__name__}: {str(e)[:120]}"} \
            if default is None else default


def build_snapshot(client) -> dict:
    snap = {
        "t0_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "build_sha": build_sha(),
        "strategy_config_hash": strategy_config_hash(),
        "risk_config_hash": risk_config_hash(),
        "environment": "DEMO",
        "base_url": client.base_url,
        "risk_limits": {
            "capital_ceiling": getattr(CFG, "CAPITAL_MAX", None)
            or getattr(CFG, "MAX_CAPITAL", None) or "voir risk_config_hash",
            "max_daily_loss_pct": CFG.MAX_DAILY_LOSS_PCT,
            "max_daily_loss_abs": CFG.MAX_DAILY_LOSS,
            "max_position_pct": CFG.MAX_POS_PCT,
            "risk_budget_pct": CFG.RISK_BUDGET_PCT,
            "max_consecutive_losses": CFG.MAX_CONSECUTIVE_LOSSES,
            "max_open_positions": CFG.MAX_OPEN_POSITIONS,
        },
        "market_families_included": ["KXBTCD", "KXBTC15M"],
        "market_families_excluded": ["tout le reste"],
        "data_providers": {
            "spot": ["coinbase", "kraken", "bitstamp"],
            "klines": ["binance", "kraken", "coinbase"],
        },
        "inclusion_policy": INCLUSION_POLICY,
        "starting_balance": _read(client.get_balance, None),
        "balances_by_exchange_index": _read(
            client.get_subaccounts_balances, None),
        "open_positions": _read(client.get_positions, None),
        "open_orders": _read(client.get_orders, None),
        "clock_started": False,
    }
    canonical = json.dumps(snap, sort_keys=True, default=str)
    snap["package_sha256"] = hashlib.sha256(
        canonical.encode("utf-8")).hexdigest()
    return snap


def main():
    for var in ("LIVE_TRADING", "KALSHI_ENV_CONFIRM"):
        if os.getenv(var, "").upper() in ("1", "LIVE"):
            fatal(f"{var} indique un contexte LIVE — T0 DEMO uniquement.")
    client = KalshiClient("demo")
    if client.base_url.rstrip("/") not in DEMO_URLS_ALLOWED:
        fatal(f"URL inattendue: {client.base_url}")
    snap = build_snapshot(client)
    out = os.path.join(os.getenv("DATA_DIR", "."), "t0_snapshot.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, default=str)
    print(f"[T0_SNAPSHOT] file={out} package_sha256="
          f"{snap['package_sha256']} clock_started=false")
    print("[T0_SNAPSHOT] archiver ce hachage HORS du conteneur "
          "(immuabilite) ; tout changement code/config apres T0 exige "
          "une NOUVELLE cohorte.")
    sys.exit(0)


if __name__ == "__main__":
    main()
