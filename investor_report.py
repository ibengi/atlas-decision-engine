# -*- coding: utf-8 -*-
"""ATLAS-TOTAL-AUDIT-001 — registre de performance investisseur.

UNE source autoritaire (TradeLogger, schema v11) -> classes de
provenance -> politique d'inclusion PREDECLAREE -> metriques avec
gardes d'echantillon. Principes :

- VERITE BROKER > CACHE LOCAL : les lignes broker_reconstructed sont
  comptabilisees (le cash a bouge) mais JAMAIS comptees comme alpha de
  strategie ;
- pas de double comptage (trade_id unique impose) ;
- pas de metrique statistiquement trompeuse sur petit echantillon :
  n < 10 -> INSUFFICIENT_SAMPLE, n < 30 -> LOW_SAMPLE (Sharpe/Sortino
  omis sous 30 reglements) ;
- chaque valeur est etiquetee MEASURED / DERIVED / UNVERIFIED.

Ce module ne soumet RIEN, ne modifie RIEN : lecture pure du registre.
"""
import math
import os
import statistics
from datetime import datetime, timezone

#: Politique d'inclusion PREDECLAREE (Domain 5) — versionnee ; toute
#: modification exige une nouvelle cohorte T0.
INCLUSION_POLICY = {
    "version": "IP-1",
    "rule": "settled AND decision_id present AND provenance absente "
            "(ligne née d'une Decision tracee du moteur) — tout le "
            "reste est exclu des metriques investisseur",
    "included_classes": ["A_STRATEGY"],
    "excluded_classes": ["B_MANUAL", "C_PROBE", "D_DEBUG",
                         "E_HISTORICAL_DUPLICATE", "F_RECONSTRUCTED",
                         "G_UNKNOWN_PROVENANCE"],
}

MIN_N_FULL_STATS = 30
MIN_N_BASIC_STATS = 10


def classify_trade(t: dict) -> str:
    """Classe de provenance (Domain 5). Les fills de sonde/manuels/
    doublons historiques n'entrent au registre QUE via la reconstruction
    broker -> ils portent provenance=broker_reconstructed (F). Une ligne
    nee d'une Decision tracee du moteur porte decision_id (A). Le reste
    est honnetement G (jamais promu en A par defaut)."""
    if t.get("provenance") == "broker_reconstructed":
        return "F_RECONSTRUCTED"
    if t.get("decision_id"):
        return "A_STRATEGY"
    return "G_UNKNOWN_PROVENANCE"


def build_sha() -> str:
    for var in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "BUILD_SHA"):
        v = os.getenv(var, "").strip()
        if v:
            return v
    return "UNVERIFIED"


def market_family(ticker: str) -> str:
    t = str(ticker or "").upper()
    for prefix, fam in (("KXBTC15M", "BTC_15M"), ("KXBTCD", "BTC_DAILY"),
                        ("KXBTC", "BTC_OTHER"), ("KXETH", "ETH")):
        if t.startswith(prefix):
            return fam
    return "OTHER"


def build_ledger(trades: list, sha: str = None) -> list:
    """Registre trace : une ligne par trade, classe + drapeau
    d'inclusion. Leve sur trade_id duplique (double comptage interdit)."""
    sha = sha or build_sha()
    seen, rows = set(), []
    for t in trades or []:
        tid = t.get("trade_id")
        if tid in seen:
            raise ValueError(f"double comptage: trade_id {tid} duplique")
        seen.add(tid)
        cls = classify_trade(t)
        rows.append({
            "trade_id": tid,
            "decision_id": t.get("decision_id"),
            "provenance_class": cls,
            "investor_included": (cls in INCLUSION_POLICY[
                "included_classes"] and t.get("state") == "settled"),
            "strategy": (t.get("analysis") or {}).get("strategy")
            if isinstance(t.get("analysis"), dict) else None,
            "market_family": market_family(t.get("ticker")),
            "ticker": t.get("ticker"),
            "side": t.get("side"),
            "decision_ts": t.get("timestamp"),
            "requested_count": t.get("requested_count"),
            "filled_count": t.get("filled_count"),
            "avg_fill_price_cents": t.get("avg_fill_price"),
            "fees": t.get("fees"),
            "state": t.get("state"),
            "result": t.get("result"),
            "won": t.get("won"),
            "gross_pnl": t.get("gross_pnl"),
            "net_pnl": t.get("net_pnl"),
            "holding_seconds": t.get("holding_seconds"),
            "order_id": t.get("order_id"),
            "edge": t.get("edge"),
            "ev": t.get("ev"),
            "confidence": t.get("confidence"),
            "cost_basis_source": t.get("cost_basis_source"),
            "build_sha": sha,
        })
    return rows


def sample_grade(n: int) -> str:
    if n >= MIN_N_FULL_STATS:
        return "OK"
    if n >= MIN_N_BASIC_STATS:
        return "LOW_SAMPLE"
    return "INSUFFICIENT_SAMPLE"


def compute_metrics(ledger_rows: list, starting_equity: float = None
                    ) -> dict:
    """Metriques investisseur sur les SEULES lignes investor_included.
    Aucune annualisation pretendue ; Sharpe/Sortino par-trade et
    UNIQUEMENT si n >= 30 (sinon omis avec raison)."""
    inc = [r for r in ledger_rows if r["investor_included"]]
    nets = [float(r["net_pnl"]) for r in inc if r["net_pnl"] is not None]
    n = len(nets)
    grade = sample_grade(n)
    m = {"policy_version": INCLUSION_POLICY["version"],
         "sample_grade": grade,
         "settled_trades": n,
         "excluded_rows": len(ledger_rows) - len(inc),
         "labels": {"pnl": "MEASURED(ledger)",
                    "equity_curve": "DERIVED(trade-sequence)",
                    "ratios": "DERIVED(per-trade, non annualise)"}}
    if n == 0:
        m["note"] = ("INSUFFICIENT_SAMPLE: aucun trade eligible — aucune "
                     "metrique de performance publiable")
        return m
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross = [float(r["gross_pnl"]) for r in inc
             if r["gross_pnl"] is not None]
    fees = [float(r["fees"] or 0.0) for r in inc]
    m.update({
        "net_pnl": round(sum(nets), 2),
        "gross_pnl": round(sum(gross), 2),
        "total_fees": round(sum(fees), 2),
        "wins": len(wins), "losses": len(losses),
        "flat": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 4),
        "average_win": round(statistics.mean(wins), 4) if wins else None,
        "average_loss": round(statistics.mean(losses), 4)
        if losses else None,
        "largest_win": round(max(nets), 2),
        "largest_loss": round(min(nets), 2),
        "expectancy_per_trade": round(statistics.mean(nets), 4),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 4)
                          if losses else "NO_LOSSES(n/a)"),
    })
    holds = [r["holding_seconds"] for r in inc
             if r["holding_seconds"] is not None]
    m["average_holding_seconds"] = (round(statistics.mean(holds))
                                    if holds else None)
    costs = [(r["filled_count"] or 0) * (r["avg_fill_price_cents"] or 0)
             / 100.0 for r in inc]
    m["turnover_dollars"] = round(sum(costs), 2)
    # courbe d'equity par sequence de trades (DERIVED)
    eq = float(starting_equity) if starting_equity is not None else 0.0
    curve, peak, max_dd = [eq], eq, 0.0
    for x in nets:
        eq += x
        curve.append(eq)
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    m.update({"starting_equity": (round(float(starting_equity), 2)
                                  if starting_equity is not None
                                  else "UNVERIFIED"),
              "ending_equity": round(curve[-1], 2),
              "peak_equity": round(peak, 2),
              "minimum_equity": round(min(curve), 2),
              "max_drawdown": round(max_dd, 2)})
    if starting_equity:
        m["return_pct"] = round(sum(nets) / float(starting_equity) * 100,
                                2)
    if grade == "OK" and n >= 2:
        sd = statistics.pstdev(nets)
        m["sharpe_like_per_trade"] = (round(statistics.mean(nets) / sd, 4)
                                      if sd > 0 else None)
        downside = [x for x in nets if x < 0]
        dsd = statistics.pstdev(downside) if len(downside) >= 2 else None
        m["sortino_like_per_trade"] = (round(
            statistics.mean(nets) / dsd, 4) if dsd else None)
    else:
        m["sharpe_like_per_trade"] = f"OMIS({grade})"
        m["sortino_like_per_trade"] = f"OMIS({grade})"
    # ventilations
    for key, label in (("strategy", "by_strategy"),
                       ("market_family", "by_market_family")):
        agg = {}
        for r in inc:
            k = r.get(key) or "unknown"
            a = agg.setdefault(k, {"n": 0, "net_pnl": 0.0})
            a["n"] += 1
            if r["net_pnl"] is not None:
                a["net_pnl"] = round(a["net_pnl"] + float(r["net_pnl"]), 2)
        m[label] = agg
    return m


def provenance_summary(ledger_rows: list) -> dict:
    out = {}
    for r in ledger_rows:
        out[r["provenance_class"]] = out.get(r["provenance_class"], 0) + 1
    return out


def render_report(trades: list, starting_equity: float = None,
                  sha: str = None) -> dict:
    """Rapport investisseur reproductible : memes entrees -> meme
    sortie. Chaque section porte son etiquette d'evidence."""
    ledger = build_ledger(trades, sha=sha)
    metrics = compute_metrics(ledger, starting_equity=starting_equity)
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "build_sha": ledger[0]["build_sha"] if ledger else build_sha(),
        "inclusion_policy": INCLUSION_POLICY,
        "provenance_counts": provenance_summary(ledger),
        "performance": metrics,
        "evidence_labels": {
            "ledger_rows": "MEASURED (TradeLogger v11)",
            "provenance": "DERIVED (regles IP-1, jamais promu en A "
                          "par defaut)",
            "starting_equity": ("MEASURED" if starting_equity is not None
                                else "UNVERIFIED"),
            "unrealized_pnl": "NON INCLUS (positions ouvertes exclues "
                              "des metriques realisees)",
        },
        "known_limitations": [
            "metriques par-trade, aucune annualisation",
            "drawdown calcule sur la sequence de trades regles, pas "
            "sur une courbe temporelle intraday",
            "les lignes broker_reconstructed sont comptabilisees mais "
            "exclues de l'alpha (provenance incomplete, marquee)",
        ],
        "ledger": ledger,
    }
