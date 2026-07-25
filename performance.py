# -*- coding: utf-8 -*-
"""
performance.py — Statistiques de trading (Tache 9) calculees UNIQUEMENT
depuis les trades REGLES par l'API (TradeLogger) et le dernier rapport de
cycle. Aucune metrique n'est estimee : si l'echantillon est trop petit pour
qu'une statistique soit significative (Sharpe, drawdown), elle est renvoyee
avec un drapeau `low_sample` explicite plutot que presentee comme fiable.
"""
import json
import math
import os
from datetime import datetime

MIN_SAMPLE_FOR_RATIOS = 30   # en-dessous : ratios marques low_sample


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def compute_stats(settled: list, capital: float = None) -> dict:
    """settled : liste de trades regles (schema TradeLogger v11+)."""
    n = len(settled)
    out = {"trades_executed": n, "low_sample": n < MIN_SAMPLE_FOR_RATIOS}
    if n == 0:
        return out

    pnls = [float(t.get("net_pnl") or 0.0) for t in settled]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    stakes = [float(t.get("filled_count") or 0)
              * float(t.get("avg_fill_price") or 0) / 100.0 for t in settled]
    total_stake = sum(stakes)
    total_pnl = sum(pnls)

    out["win_rate"] = round(len(wins) / n, 4)
    out["pnl"] = round(total_pnl, 2)
    out["roi_on_stakes"] = round(total_pnl / total_stake, 4) \
        if total_stake > 0 else None
    if capital:
        out["roi_on_capital"] = round(total_pnl / capital, 4)
    gp, gl = sum(wins), -sum(losses)
    out["profit_factor"] = round(gp / gl, 3) if gl > 0 else \
        (float("inf") if gp > 0 else None)
    out["expectancy_per_trade"] = round(total_pnl / n, 4)
    edges = [t.get("edge") for t in settled if t.get("edge") is not None]
    out["average_edge"] = round(sum(edges) / len(edges), 4) if edges else None

    holds = []
    for t in settled:
        a, b = _parse_ts(t.get("timestamp")), _parse_ts(t.get("settled_at"))
        if a and b:
            holds.append((b - a).total_seconds() / 60.0)
    out["average_holding_minutes"] = round(sum(holds) / len(holds), 1) \
        if holds else None

    # Sharpe par trade (rendement = pnl/mise). PAS annualise : sans
    # frequence de trading stable, l'annualisation serait une invention.
    rets = [p / s for p, s in zip(pnls, stakes) if s > 0]
    if len(rets) >= 2:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        out["sharpe_per_trade"] = round(mu / sd, 3) if sd > 0 else None
    else:
        out["sharpe_per_trade"] = None

    # Max drawdown sur le PnL cumule (ordre chronologique de reglement)
    ordered = sorted(settled, key=lambda t: str(t.get("settled_at") or ""))
    cum = peak = 0.0
    mdd = 0.0
    for t in ordered:
        cum += float(t.get("net_pnl") or 0.0)
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    out["max_drawdown"] = round(mdd, 2)
    if capital:
        out["max_drawdown_pct_of_capital"] = round(-mdd / capital * 100, 2)
    return out


def load_report(data_dir: str, capital: float = None) -> dict:
    """Assemble stats de trades + rejets du dernier cycle."""
    def _load(name, default):
        p = os.path.join(data_dir, name)
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default
    trades = _load("kalshi_trades.json", [])
    settled = [t for t in trades if t.get("state") == "settled"
               or t.get("settled_at")]
    rep = _load("cycle_report.json", {})
    out = compute_stats(settled, capital=capital)
    out["open_trades"] = len(trades) - len(settled)
    out["last_cycle"] = {
        "cycle_id": rep.get("cycle_id"),
        "rejections_by_reason": rep.get("rejections_by_reason") or {},
        "funnel_conversion": rep.get("funnel_conversion") or {},
    }
    return out


def format_report(stats: dict) -> str:
    L = ["=== Performance (trades regles par l'API uniquement) ==="]
    if stats.get("low_sample"):
        L.append(f"  ⚠ echantillon {stats.get('trades_executed', 0)} < "
                 f"{MIN_SAMPLE_FOR_RATIOS} : ratios NON significatifs")
    order = ("trades_executed", "win_rate", "pnl", "roi_on_stakes",
             "roi_on_capital", "profit_factor", "expectancy_per_trade",
             "average_edge", "average_holding_minutes", "sharpe_per_trade",
             "max_drawdown", "max_drawdown_pct_of_capital", "open_trades")
    for k in order:
        if k in stats:
            L.append(f"  {k:28} {stats[k]}")
    lc = stats.get("last_cycle") or {}
    if lc.get("rejections_by_reason"):
        L.append("  rejets (dernier cycle) : "
                 + json.dumps(lc["rejections_by_reason"], ensure_ascii=False))
    return "\n".join(L)
