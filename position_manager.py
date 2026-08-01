"""
PositionManager — Gestion du cycle de vie des positions Kalshi.
Extrait de kalshi_alpha_bot.py (P3.6).
"""

import logging
import time
from datetime import datetime, timezone

from config import CFG, _p
from kalshi_client import KalshiClient, pick, pick_int
from persistence import JsonStore
from trade_logger import TradeLogger, now_iso

# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log_pos = logging.getLogger("POSITION")


class PositionManager:
    """Positions indexees par trade_id (plusieurs lots possibles par ticker
    si ONE_TRADE_PER_MARKET est desactive). Migration automatique de
    l'ancienne structure ticker->pos. Reconciliation broker idempotente."""

    def __init__(self, client: KalshiClient, trade_log: TradeLogger):
        self.client, self.tlog = client, trade_log
        raw = JsonStore.load(_p(CFG.POSITIONS_FILE), {})
        self.positions = self._migrate(raw)          # trade_id -> pos
        self.seen_fill_ids = set(
            JsonStore.load(_p("seen_fill_ids.json"), []))

    @staticmethod
    def _migrate(raw: dict) -> dict:
        out = {}
        for k, p in (raw or {}).items():
            if "ticker" in p:                        # nouveau format
                out[k] = p
            else:                                    # ancien: cle = ticker
                tid = p.get("trade_id") or f"mig-{k}"
                out[tid] = {**p, "ticker": k}
        return out

    def flush(self):
        JsonStore.save(_p(CFG.POSITIONS_FILE), self.positions)
        JsonStore.save(_p("seen_fill_ids.json"),
                       sorted(self.seen_fill_ids)[-5000:])

    def open_position(self, trade: dict, extra: dict = None):
        pos = {
            "trade_id": trade["trade_id"], "ticker": trade["ticker"],
            "side": trade["side"],
            "count_initial": trade["filled_count"],
            "count": trade["filled_count"],
            "avg_price": trade["avg_fill_price"],
            "fees": trade["fees"], "opened_at": trade["timestamp"],
            "order_ids": [trade.get("order_id")],
            "fill_ids": (extra or {}).get("fill_ids", []),
            "state": "open",
            "strategy": (extra or {}).get("strategy"),
            "category": (extra or {}).get("category", "Other"),
            "market_score": (extra or {}).get("market_score"),
            "entry_edge": (extra or {}).get("entry_edge"),
            "entry_ev": (extra or {}).get("entry_ev"),
        }
        self.positions[trade["trade_id"]] = pos
        for fid in pos["fill_ids"]:
            self.seen_fill_ids.add(fid)
        self.flush()
        log_pos.info(f"{trade['ticker']}: {trade['side'].upper()} "
                     f"x{trade['filled_count']} @ {trade['avg_fill_price']}c")

    def _active_positions(self):
        return (p for p in self.positions.values() if p.get("state", "open") == "open")

    def tickers_open(self) -> set:
        return {p["ticker"] for p in self._active_positions()}

    def open_count(self) -> int:
        return sum(1 for _ in self._active_positions())

    def open_risk(self) -> float:
        """Capital en risque = cout total des positions effectivement ouvertes."""
        return sum(p["count"] * p["avg_price"] / 100.0
                   for p in self._active_positions())

    def open_risk_by_category(self) -> dict:
        out = {}
        for p in self._active_positions():
            cat = p.get("category", "Other")
            out[cat] = out.get(cat, 0.0) + p["count"] * p["avg_price"] / 100.0
        return out

    def open_risk_on(self, ticker: str) -> float:
        return sum(p["count"] * p["avg_price"] / 100.0
                   for p in self._active_positions() if p["ticker"] == ticker)

    def unrealized_pnl(self, mid_price_lookup=None) -> float:
        """PnL latent estime au prix mid courant (0 si donnee indisponible)."""
        total = 0.0
        for p in self._active_positions():
            if not mid_price_lookup: continue
            mid = mid_price_lookup(p["ticker"], p["side"])
            if mid is None: continue
            total += p["count"] * (mid - p["avg_price"]) / 100.0
        return total

    def check_settlements(self) -> list:
        """Interroge l'API pour les marches regles ; realise le PnL.
        Ecriture du reglement AVANT retrait de la position : un crash entre
        les deux laisse au pire un doublon detecte (trade deja settled),
        jamais un trade zombie.

        Changements P2.1 (2026-07-31) :
        - result "void" reconnu comme reglement valide (perte limitee aux frais)
        - statut settled/finalized avec result illisible => void_unreadable
        - max-age escape hatch : positions de plus de MAX_POSITION_AGE_DAYS
          sur des marches non "open" nettoyees comme expired_stale
        - echec get_market() : log WARNING + cleanup si position trop vieille
        """
        realized = []
        now_dt = datetime.now(timezone.utc)
        for tid, p in list(self.positions.items()):
            if p.get("state", "open") != "open":
                continue
            m = self.client.get_market(p["ticker"])

            # ── max-age escape hatch ──────────────────────────────────
            opened_str = p.get("opened_at", "")
            if opened_str:
                try:
                    opened = datetime.fromisoformat(opened_str)
                    age_days = (now_dt - opened).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    age_days = None
            else:
                age_days = None

            if age_days is not None and age_days > CFG.MAX_POSITION_AGE_DAYS:
                if not m or str(pick(m, "status", default="") or "").lower() != "open":
                    gross = -p["fees"]   # conservative: lose fees on stale position
                    net = gross - p["fees"]
                    t = self.tlog.settle_trade(p["trade_id"], "expired_stale", False, gross, net)
                    if t is None:
                        log_pos.error(f"settle_trade failed for {p['trade_id']} — position kept for retry")
                        continue
                    self.positions.pop(tid, None)
                    self.flush()
                    if t:
                        realized.append(t)
                    log_pos.warning(
                        f"{p['ticker']}: position agee de {age_days:.0f}j > "
                        f"{CFG.MAX_POSITION_AGE_DAYS}j, statut marche="
                        f"{str(pick(m, 'status', default='N/A') or 'N/A').lower() if m else 'inaccessible'}"
                        f" -- nettoyee comme expired_stale (gross={gross:+.2f}$)")
                    continue

            # ── API failure (m is None / empty) ───────────────────────
            if not m:
                if age_days is not None and age_days <= CFG.MAX_POSITION_AGE_DAYS:
                    log_pos.warning(f"{p['ticker']}: get_market() a echoue -- "
                                    f"position fraiche ({age_days:.0f}j), conservee.")
                else:
                    log_pos.warning(f"{p['ticker']}: get_market() a echoue, "
                                    f"age={age_days}j -- conservee.")
                continue

            result = str(pick(m, "result", default="") or "").lower()
            status = str(pick(m, "status", default="") or "").lower()

            # ── void (legitimate settlement) ──────────────────────────
            if result == "void":
                gross = 0.0   # return of premium, net loss = fees only
                net = gross - p["fees"]
                t = self.tlog.settle_trade(p["trade_id"], "void", False, gross, net)
                if t is None:
                    log_pos.error(f"settle_trade failed for {p['trade_id']} — position kept for retry")
                    continue
                self.positions.pop(tid, None)
                self.flush()
                if t:
                    realized.append(t)
                log_pos.info(f"{p['ticker']}: reglement VOID (remboursement premium, "
                             f"perte={-net:.2f}$ frais)")
                continue

            # ── settled/finalized with unreadable result ──────────────
            if result not in ("yes", "no"):
                if status in ("settled", "finalized"):
                    gross = -p["fees"]   # conservative: assume loss
                    net = gross - p["fees"]
                    t = self.tlog.settle_trade(p["trade_id"], "void_unreadable", False, gross, net)
                    if t is None:
                        log_pos.error(f"settle_trade failed for {p['trade_id']} — position kept for retry")
                        continue
                    self.positions.pop(tid, None)
                    self.flush()
                    if t:
                        realized.append(t)
                    log_pos.warning(
                        f"{p['ticker']}: statut '{status}' mais result illisible "
                        f"(raw={repr(pick(m, 'result', default=None))}) -- "
                        f"traite comme void_unreadable (gross={gross:+.2f}$)")
                    continue
                else:
                    # market still open or unknown → keep position
                    continue

            # ── happy path: yes / no ──────────────────────────────────
            won  = (result == p["side"])
            cost = p["count"] * p["avg_price"] / 100.0
            gross = (p["count"] * 1.0 - cost) if won else -cost
            net   = gross - p["fees"]
            t = self.tlog.settle_trade(p["trade_id"], result, won, gross, net)
            if t is None:
                log_pos.error(f"settle_trade failed for {p['trade_id']} — position kept for retry")
                continue
            self.positions.pop(tid, None)
            self.flush()
            if t: realized.append(t)
        return realized

    def reconcile_with_broker(self) -> dict:
        """Broker = source de verite. Reconstruit les positions presentes
        chez Kalshi mais absentes localement (id stable => idempotent),
        marque 'ghost' les positions locales absentes du broker."""
        report = {"rebuilt": [], "ghost": [], "matched": []}
        MAX_RETRIES = 3
        RETRY_BACKOFF_SECONDS = 2.0
        broker = None
        for attempt in range(1, MAX_RETRIES + 1):
            broker = self.client.get_positions()
            if broker is not None:
                break
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log_pos.warning(
                    f"get_positions() returned None (attempt {attempt}/{MAX_RETRIES}), "
                    f"retrying in {wait:.0f}s..."
                )
                time.sleep(wait)
        if broker is None:
            log_pos.error(
                f"get_positions() failed after {MAX_RETRIES} attempts — reconciliation skipped"
            )
            return report
        seen_tickers = set()
        for bp in broker:
            tk = bp.get("ticker")
            if not tk:
                continue
            qty = pick_int(bp, "position", "quantity", "count", default=0)
            if qty == 0:
                continue
            side = "yes" if qty > 0 else "no"
            seen_tickers.add(tk)
            local = [p for p in self._active_positions() if p["ticker"] == tk]
            if local:
                report["matched"].append(tk)
                continue
            tid = f"brk-{tk}-{side}"                 # ID STABLE = idempotent
            if tid in self.positions:
                continue
            avg = pick_int(bp, "avg_price", "market_exposure", default=50) or 50
            self.positions[tid] = {
                "trade_id": tid, "ticker": tk, "side": side,
                "count_initial": abs(qty), "count": abs(qty),
                "avg_price": avg, "fees": 0.0, "opened_at": now_iso(),
                "order_ids": [], "fill_ids": [], "state": "open",
                "strategy": "reconciled", "market_score": None,
                "entry_edge": None, "entry_ev": None,
            }
            report["rebuilt"].append(tk)
        for tid, p in list(self.positions.items()):
            if p["ticker"] not in seen_tickers and not tid.startswith("mig-"):
                p["state"] = "ghost_local_only"
                report["ghost"].append(p["ticker"])
        # Clean up ghost positions that the broker doesn't know about
        ghost_removed = []
        for tid in list(self.positions.keys()):
            if self.positions[tid].get("state") == "ghost_local_only":
                self.positions.pop(tid, None)
                ghost_removed.append(tid)
        if ghost_removed:
            self.flush()
            log_pos.info(f"Ghost cleanup: removed {len(ghost_removed)} stale position(s): {ghost_removed}")
        if report["rebuilt"] or report["ghost"]:
            self.flush()
            JsonStore.save(_p("reconciliation_report.json"), report)
            log_pos.warning(f"Reconciliation broker: reconstruites="
                            f"{report['rebuilt']} fantomes={report['ghost']}")
        return report

    def reconcile_startup(self):
        """Apres crash/redemarrage : les positions persistees restent valides
        (elles vivent chez le broker) ; on les re-verifie au prochain cycle."""
        if self.positions:
            log_pos.info(f"Recovery: {len(self.positions)} position(s) ouverte(s) "
                         f"rechargee(s): {', '.join(self.tickers_open())}")

