"""StatsEngine — Statistiques completes + rapports periodiques. Extrait de kalshi_alpha_bot.py (P3.10)."""
import json
import logging
import math
import os
from datetime import date, datetime, timezone

from config import CFG, _p
from persistence import JsonStore
from trade_logger import TradeLogger, now_iso

# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log_sts = logging.getLogger("STATS")


class StatsEngine:
    def __init__(self, tlog: TradeLogger):
        self.tlog = tlog
        self._last_report_day = None

    def compute(self) -> dict:
        settled = self.tlog.settled_trades()
        if not settled:
            return {"n": 0}
        rets   = [t["net_pnl"] for t in settled]
        wins   = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        n      = len(rets)
        wr     = len(wins) / n
        avg_w  = sum(wins) / len(wins) if wins else 0.0
        avg_l  = sum(losses) / len(losses) if losses else 0.0
        mean   = sum(rets) / n
        var    = sum((r - mean) ** 2 for r in rets) / n if n > 1 else 0.0
        std    = math.sqrt(var)
        downs  = [min(0.0, r) for r in rets]
        dvar   = sum(d ** 2 for d in downs) / n
        dstd   = math.sqrt(dvar)
        # Sharpe/Sortino PAR TRADE (pas annualises -- interpretation relative
        # uniquement ; l'annualisation sur du 15-min serait trompeuse).
        sharpe  = mean / std  if std  > 0 else None
        sortino = mean / dstd if dstd > 0 else None
        gp, gl  = sum(wins), -sum(losses)
        # Kelly (formule classique ; valable seulement si WR/gains stables)
        kelly = None
        if avg_w > 0 and avg_l < 0:
            b = avg_w / abs(avg_l)
            kelly = round(wr - (1 - wr) / b, 4)
        curve, peak, maxdd = 0.0, 0.0, 0.0
        curve_points = []
        for t in settled:
            curve += t["net_pnl"]; peak = max(peak, curve)
            maxdd = max(maxdd, peak - curve)
            curve_points.append({"t": t["settled_at"], "equity": round(curve, 2)})
        JsonStore.save(_p(CFG.CURVE_FILE), curve_points)
        durations = [t["holding_seconds"] for t in settled if t.get("holding_seconds")]
        return {
            "n": n, "win_rate": round(wr, 4),
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "expectancy": round(mean, 3),
            "sharpe_per_trade": round(sharpe, 3) if sharpe is not None else None,
            "sortino_per_trade": round(sortino, 3) if sortino is not None else None,
            "kelly_fraction": kelly,
            "average_win": round(avg_w, 2), "average_loss": round(avg_l, 2),
            "largest_win": round(max(rets), 2), "largest_loss": round(min(rets), 2),
            "max_drawdown": round(maxdd, 2),
            "net_pnl": round(sum(rets), 2),
            "avg_duration_s": round(sum(durations) / len(durations)) if durations else None,
        }

    def _period_report(self, label: str, day_filter) -> dict:
        settled = [t for t in self.tlog.settled_trades()
                   if day_filter((t.get("settled_at") or "")[:10])]
        pnl = sum(t["net_pnl"] for t in settled)
        wins = sum(1 for t in settled if t["won"])
        return {"period": label, "trades": len(settled), "wins": wins,
                "net_pnl": round(pnl, 2),
                "win_rate": round(wins / len(settled), 4) if settled else 0.0}

    def maybe_daily_report(self):
        """A chaque changement de jour UTC : rapport quotidien (+ hebdo le
        lundi, + mensuel le 1er), ecrit dans reports/ et logge."""
        today = datetime.now(timezone.utc).date()
        if self._last_report_day == today:
            return
        prev = self._last_report_day
        self._last_report_day = today
        if prev is None:
            return
        os.makedirs(_p(CFG.REPORT_DIR), exist_ok=True)
        day = prev.isoformat()
        rep = {"generated": now_iso(),
               "daily":  self._period_report(day, lambda d: d == day),
               "global": self.compute()}
        if today.weekday() == 0:
            week_start = date.fromordinal(today.toordinal() - 7).isoformat()
            rep["weekly"] = self._period_report(
                "7 derniers jours", lambda d: d >= week_start)
        if today.day == 1:
            rep["monthly"] = self._period_report(
                prev.strftime("%Y-%m"), lambda d: d.startswith(prev.strftime("%Y-%m")))
        JsonStore.save(os.path.join(_p(CFG.REPORT_DIR), f"report_{day}.json"), rep)
        log_sts.info(f"Rapport {day}: {json.dumps(rep['daily'], ensure_ascii=False)}")

    def log_summary(self):
        s = self.compute()
        if s.get("n"):
            log_sts.info(f"BILAN: n={s['n']} WR={s['win_rate']:.1%} "
                         f"PnL={s['net_pnl']:+.2f}$ PF={s['profit_factor']} "
                         f"Exp={s['expectancy']}$ MaxDD={s['max_drawdown']}$")
