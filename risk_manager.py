"""RiskManager — Gestion du risque (stop loss, drawdown, limites quotidiennes). Extrait de kalshi_alpha_bot.py (P3.9)."""
import logging
from datetime import datetime, timezone
from typing import Optional

from config import CFG, _p
from persistence import JsonStore
from position_manager import PositionManager
from trade_logger import TradeLogger, now_iso

# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log_rsk = logging.getLogger("RISK")


class RiskManager:
    def __init__(self, tlog: TradeLogger, posmgr: PositionManager, capital: float):
        self.tlog, self.posmgr, self.capital = tlog, posmgr, capital
        st = JsonStore.load(_p(CFG.RISK_FILE), {})
        today = datetime.now(timezone.utc).date().isoformat()
        if st.get("date") != today:
            st = {"date": today}
        self.state = st
        self.flush()

    def flush(self):
        JsonStore.save(_p(CFG.RISK_FILE), self.state)

    # -- agregats jour (recalcules depuis le journal : source de verite unique)
    def _today_settled(self) -> list:
        today = datetime.now(timezone.utc).date().isoformat()
        return [t for t in self.tlog.settled_trades()
                if (t.get("settled_at") or "").startswith(today)]

    def daily_realized_pnl(self) -> float:
        return sum(t["net_pnl"] for t in self._today_settled())

    def daily_realized_loss(self) -> float:
        return sum(t["net_pnl"] for t in self._today_settled() if t["net_pnl"] < 0)

    def daily_realized_profit(self) -> float:
        return sum(t["net_pnl"] for t in self._today_settled() if t["net_pnl"] > 0)

    def trades_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(1 for t in self.tlog.trades
                   if t["timestamp"].startswith(today))

    def rolling_drawdown(self) -> float:
        """Drawdown courant en dollars de la courbe de PnL net cumule."""
        curve, peak = 0.0, 0.0
        for t in self.tlog.settled_trades():
            curve += t["net_pnl"]
            peak = max(peak, curve)
        return max(0.0, peak - curve)

    def rolling_drawdown_pct(self) -> float:
        """Drawdown courant en pourcentage du capital effectif.

        Evite l'ancien melange d'un drawdown en dollars avec une limite en %.
        """
        if self.capital <= 0:
            return 0.0
        return 100.0 * self.rolling_drawdown() / self.capital

    def effective_daily_stop(self) -> float:
        """Stop journalier en $ : min(plafond absolu, MAX_DAILY_LOSS_PCT du
        CAPITAL EFFECTIF). Pour 93,26$ : min(50, 4.66) = 4,66$. Le capital
        de reference (500$) ne peut plus influencer un solde inferieur."""
        pct_stop = max(0.0, self.capital) * CFG.MAX_DAILY_LOSS_PCT / 100.0
        return round(min(CFG.MAX_DAILY_LOSS, pct_stop), 2)

    def consecutive_losses(self) -> int:
        """Pertes consecutives en fin de sequence des trades regles."""
        n = 0
        for t in reversed(self.tlog.settled_trades()):
            if t.get("net_pnl") is not None and t["net_pnl"] < 0:
                n += 1
            else:
                break
        return n

    def seconds_since_last_settlement(self) -> Optional[float]:
        """Anciennete (s) du dernier trade REGLE, ou None si aucun trade
        regle n'existe encore. Utilise pour le cooldown du kill-switch."""
        settled = self.tlog.settled_trades()
        if not settled:
            return None
        ts = settled[-1].get("settled_at")
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds()

    def _last_settlement_anchor(self) -> Optional[str]:
        settled = self.tlog.settled_trades()
        return (settled[-1].get("settled_at") if settled else None)

    def half_open_required(self) -> bool:
        """Vrai uniquement quand la serie de pertes depasse le seuil ET que
        le cooldown est ecoule. Le claim est persistant dans risk_state.json."""
        if self.consecutive_losses() < CFG.MAX_CONSECUTIVE_LOSSES:
            return False
        elapsed = self.seconds_since_last_settlement()
        return elapsed is not None and elapsed >= CFG.CONSECUTIVE_LOSS_COOLDOWN_S

    def claim_half_open_attempt(self, ticker: str) -> (bool, str):
        """Reserve atomiquement l'unique essai demi-ouvert.

        L'ancre est le dernier settled_at. Tant qu'aucun nouveau reglement
        n'est intervenu, une seconde soumission est refusee, y compris apres
        redemarrage du processus.
        """
        if not self.half_open_required():
            return True, ""
        anchor = self._last_settlement_anchor()
        if not anchor:
            return False, "demi-ouvert impossible: dernier reglement inconnu"
        if (self.state.get("half_open_anchor") == anchor and
                self.state.get("half_open_claimed")):
            return False, (
                "ARRET: essai demi-ouvert deja consomme depuis le dernier "
                "trade regle; attendre le reglement de cet essai")
        self.state.update({
            "half_open_anchor": anchor,
            "half_open_claimed": True,
            "half_open_claimed_at": now_iso(),
            "half_open_ticker": ticker,
        })
        self.flush()
        log_rsk.warning(
            f"[RISK] essai demi-ouvert RESERVE pour {ticker}; aucune autre "
            "soumission autorisee avant un nouveau reglement.",
            extra={"event": "half_open_reserved", "ticker": ticker})
        return True, ""

    def release_half_open_attempt(self, ticker: str, reason: str) -> bool:
        """Libere un claim demi-ouvert uniquement lorsqu'aucun ordre n'a ete
        accepte par Kalshi ou lorsqu'un ordre est confirme sans aucun fill.
        Un etat incertain avec order_id reste verrouille par securite.
        """
        anchor = self._last_settlement_anchor()
        if not anchor:
            return False
        if not (self.state.get("half_open_anchor") == anchor and
                self.state.get("half_open_claimed") and
                self.state.get("half_open_ticker") == ticker):
            return False
        self.state.update({
            "half_open_claimed": False,
            "half_open_released_at": now_iso(),
            "half_open_release_reason": reason,
        })
        self.flush()
        log_rsk.warning(f"[RISK] essai demi-ouvert LIBERE pour {ticker}: {reason}",
                        extra={"event": "half_open_released",
                               "ticker": ticker, "reason": reason})
        return True

    # -- portes de risque ------------------------------------------------------
    def can_trade(self, cycle_trades: int) -> (bool, str):
        pnl = self.daily_realized_pnl()
        stop = self.effective_daily_stop()
        if pnl <= -stop:
            return False, (f"STOP JOURNALIER: PnL realise {pnl:+.2f}$ <= "
                           f"-{stop:.2f}$ (={CFG.MAX_DAILY_LOSS_PCT:g}% du "
                           f"capital effectif {self.capital:.2f}$)")
        losses = self.consecutive_losses()
        if losses >= CFG.MAX_CONSECUTIVE_LOSSES:
            elapsed = self.seconds_since_last_settlement()
            cooldown = CFG.CONSECUTIVE_LOSS_COOLDOWN_S
            if elapsed is None or elapsed < cooldown:
                remaining = cooldown - (elapsed or 0.0)
                return False, (
                    f"ARRET: {losses} pertes consecutives >= "
                    f"{CFG.MAX_CONSECUTIVE_LOSSES} -- reprise possible dans "
                    f"{max(0.0, remaining):.0f}s (cooldown "
                    f"{cooldown:.0f}s depuis le dernier trade regle)")
            # Cooldown ecoule : verifier qu'aucun essai demi-ouvert n'a
            # deja ete reserve pour le meme dernier reglement.
            anchor = self._last_settlement_anchor()
            if (anchor and self.state.get("half_open_anchor") == anchor and
                    self.state.get("half_open_claimed")):
                return False, (
                    "ARRET: essai demi-ouvert deja consomme depuis le "
                    "dernier trade regle; attendre son reglement")
            log_rsk.warning(
                f"[RISK] cooldown de {cooldown:.0f}s ecoule apres "
                f"{losses} pertes consecutives -- 1 nouvel essai disponible.",
                extra={"event": "consecutive_loss_cooldown_elapsed",
                       "losses": losses, "cooldown_s": cooldown})
        if cycle_trades >= CFG.MAX_TRADES_CYCLE:
            return False, f"max {CFG.MAX_TRADES_CYCLE} trades/cycle atteint"
        open_risk = self.posmgr.open_risk()
        budget    = self.capital * CFG.RISK_BUDGET_PCT / 100.0
        if open_risk >= budget:
            return False, (f"budget de risque ouvert atteint "
                           f"({open_risk:.2f}$ >= {budget:.2f}$)")
        return True, ""

    def snapshot(self) -> dict:
        settled = self.tlog.settled_trades()
        wins    = [t for t in settled if t["won"]]
        losses  = [t for t in settled if not t["won"]]
        gp = sum(t["net_pnl"] for t in wins)
        gl = -sum(t["net_pnl"] for t in losses)
        return {
            "capital_deployed":      round(self.posmgr.open_risk(), 2),
            "open_risk":             round(self.posmgr.open_risk(), 2),
            "realized_pnl":          round(sum(t["net_pnl"] for t in settled), 2),
            "unrealized_pnl":        round(self.posmgr.unrealized_pnl(), 2),
            "daily_realized_pnl":    round(self.daily_realized_pnl(), 2),
            "daily_realized_loss":   round(self.daily_realized_loss(), 2),
            "daily_realized_profit": round(self.daily_realized_profit(), 2),
            "gross_pnl":             round(sum(t["gross_pnl"] for t in settled), 2),
            "net_pnl":               round(sum(t["net_pnl"] for t in settled), 2),
            "fees_paid":             round(sum(t["fees"] for t in self.tlog.trades), 2),
            "win_rate":  round(len(wins) / len(settled), 4) if settled else 0.0,
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "rolling_drawdown": round(self.rolling_drawdown(), 2),
        }
