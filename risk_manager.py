"""RiskManager — Gestion du risque (stop loss, drawdown, limites quotidiennes). Extrait de kalshi_alpha_bot.py (P3.9)."""
import logging
from datetime import datetime, timezone
from typing import Optional

from config import CFG, _p
from persistence import JsonStore, file_fingerprint
from risk_transaction import risk_transaction
from position_manager import PositionManager
from trade_logger import TradeLogger, now_iso

# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log_rsk = logging.getLogger("RISK")


class RiskManager:
    #: F2 risk-equity ledger (equity_ledger.EquityLedger), attached by the
    #: engine. None or unseeded -> the historical cash formulas apply.
    equity = None

    def __init__(self, tlog: TradeLogger, posmgr: PositionManager, capital: float):
        self.tlog, self.posmgr, self.capital = tlog, posmgr, capital
        # F2: the risk-equity ledger (equity_ledger.EquityLedger), attached by
        # the engine. None or unseeded -> the historical cash formulas.
        self.equity = None
        st = JsonStore.load(_p(CFG.RISK_FILE), {})
        self._fingerprint = file_fingerprint(_p(CFG.RISK_FILE))
        today = datetime.now(timezone.utc).date().isoformat()
        if st.get("date") != today:
            st = {**st, "date": today}
        self.state = st
        self.flush()

    def flush(self):
        ok = JsonStore.save(_p(CFG.RISK_FILE), self.state, expect_fingerprint=self._fingerprint)
        if ok:
            self._fingerprint = file_fingerprint(_p(CFG.RISK_FILE))
        return ok

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
        # trade_rows(), pas trades : une correction de ledger n'est pas un
        # trade et ne consomme aucun quota quotidien.
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(1 for t in self.tlog.trade_rows()
                   if t["timestamp"].startswith(today))

    def rolling_drawdown(self) -> float:
        """Drawdown courant en dollars de la courbe de PnL net cumule."""
        curve, peak = 0.0, 0.0
        for t in self.tlog.settled_trades():
            curve += t["net_pnl"]
            peak = max(peak, curve)
        return max(0.0, peak - curve)

    def _strategy_mode(self) -> bool:
        """True when F2 accounting decides percentages (audit finding A05).

        The old test was `mode == "strategy"`, so ANY other string -- a typo,
        an empty variable, a stale value from an older release -- silently
        selected the cash denominator. That is not a neutral fallback: a
        deposit raises cash, so a cash-denominated drawdown SHRINKS when
        money is added, which is exactly the loss-derived protection F2
        exists to preserve. Astra turned a 30% strategy drawdown into 3% that
        way and walked through the global gate.

        So: only a RECOGNIZED mode may choose a denominator at all, and only
        the recognized `cash` rollback -- an explicit, deliberate value --
        gets the historical one. Anything unrecognized keeps the
        loss-preserving computation whenever a seeded ledger can provide it,
        and `equity_ledger.GUARD_ACCOUNTING_MODE` blocks CAPITAL either way.
        """
        if self.equity is None or not getattr(self.equity, "seeded", False):
            return False
        mode = str(getattr(CFG, "RISK_EQUITY_MODE", "strategy") or "").strip().lower()
        if mode == "cash":
            return False              # recognized rollback, CAPITAL blocked
        return True                   # "strategy", or anything unrecognized

    def rolling_drawdown_pct(self) -> float:
        """Drawdown courant en pourcentage.

        F2 (strategy mode): 100 x (HWM - strategy_equity) / HWM from the
        equity ledger -- a deposit cannot lower it, a withdrawal cannot raise
        it. Otherwise: the historical ratio to effective capital (cash).
        """
        if self._strategy_mode():
            pct = self.equity.drawdown_pct()
            if pct is not None:
                return float(pct)
        if self.capital <= 0:
            return 0.0
        return 100.0 * self.rolling_drawdown() / self.capital

    def effective_daily_stop(self) -> float:
        """Stop journalier en $ : min(plafond absolu, MAX_DAILY_LOSS_PCT du
        CAPITAL EFFECTIF). Pour 93,26$ : min(50, 4.66) = 4,66$. Le capital
        de reference (500$) ne peut plus influencer un solde inferieur."""
        pct_stop = max(0.0, self.capital) * CFG.MAX_DAILY_LOSS_PCT / 100.0
        stop = min(CFG.MAX_DAILY_LOSS, pct_stop)
        if self._strategy_mode():
            # F2 §7: the start-of-day strategy equity bounds the stop too, so
            # an intraday deposit cannot widen it; the cash term stays so a
            # withdrawal still tightens it. Never below one cent while the
            # reference is positive: `stop == 0` would read as "disabled".
            sod = self.equity.sod_strategy_equity()
            if sod is None:
                sod = self.equity.strategy_equity()
            if sod is not None:
                stop = min(stop, max(0.0, float(sod)) * CFG.MAX_DAILY_LOSS_PCT / 100.0)
                ref = self.equity.risk_equity_reference()
                if ref is not None and ref > 0:
                    stop = max(stop, 0.01)
        return round(stop, 2)

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

    @risk_transaction
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
        if not self.flush():
            return False, "risk_state_recovery_required"
        log_rsk.warning(
            f"[RISK] essai demi-ouvert RESERVE pour {ticker}; aucune autre "
            "soumission autorisee avant un nouveau reglement.",
            extra={"event": "half_open_reserved", "ticker": ticker})
        return True, ""

    @risk_transaction
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
        if not self.flush():
            return False
        log_rsk.warning(f"[RISK] essai demi-ouvert LIBERE pour {ticker}: {reason}",
                        extra={"event": "half_open_released",
                               "ticker": ticker, "reason": reason})
        return True

    # -- portes de risque ------------------------------------------------------
    @staticmethod
    def _group_for(ticker: str, category: str = "Other") -> str:
        # Keep this independent of a concrete PositionManager for testability.
        return PositionManager.correlation_group(ticker, category)

    def _group_limit_pct(self, group: str) -> float:
        """Group cap as a percent of effective capital; zero disables it."""
        return float(getattr(CFG, "MAX_CORRELATION_GROUP_PCT", 0.0) or 0.0)

    def drawdown_size_factor(self) -> float:
        """Multiplier for new size after portfolio drawdown throttle."""
        threshold = float(getattr(CFG, "PORTFOLIO_DRAWDOWN_THROTTLE_PCT", 0.0) or 0.0)
        if threshold <= 0 or self.rolling_drawdown_pct() < threshold:
            return 1.0
        return max(0.0, min(1.0, float(getattr(CFG, "DRAWDOWN_THROTTLE_FACTOR", 0.5))))

    def portfolio_check(self, ticker: str, category: str = "Other",
                        proposed_risk: float = 0.0) -> (bool, str):
        """Check emergency stop and incremental portfolio concentration."""
        pnl = self.daily_realized_pnl()
        stop = self.effective_daily_stop()
        if stop > 0 and pnl <= -stop:
            return False, f"daily_loss_limit: realized PnL {pnl:+.2f} <= -{stop:.2f}"
        total_limit = float(getattr(CFG, "MAX_PORTFOLIO_RISK_PCT", 0.0) or 0.0)
        projected = self.posmgr.open_risk() + max(0.0, float(proposed_risk or 0.0))
        if total_limit > 0 and projected > self.capital * total_limit / 100.0:
            return False, f"portfolio_limit: {projected:.2f} > {total_limit:g}%"
        group = self._group_for(ticker, category)
        group_limit = self._group_limit_pct(group)
        by_group = self.posmgr.open_risk_by_group()
        group_projected = by_group.get(group, 0.0) + max(0.0, float(proposed_risk or 0.0))
        if group_limit > 0 and group_projected > self.capital * group_limit / 100.0:
            return False, f"correlation_group_limit:{group}: {group_projected:.2f} > {group_limit:g}%"
        return True, ""

    def can_trade(self, cycle_trades: int) -> (bool, str):
        pnl = self.daily_realized_pnl()
        stop = self.effective_daily_stop()
        if stop > 0 and pnl <= -stop:
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
            # frais : vue EFFECTIVE (corrections de frais broker incluses)
            "fees_paid":             round(sum(t["fees"] for t in
                                               self.tlog.effective_trades()), 2),
            "win_rate":  round(len(wins) / len(settled), 4) if settled else 0.0,
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "rolling_drawdown": round(self.rolling_drawdown(), 2),
            "rolling_drawdown_pct": round(self.rolling_drawdown_pct(), 4),
            "risk_equity": (self.equity.snapshot() if self.equity is not None else None),
        }
