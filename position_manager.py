"""
PositionManager — Gestion du cycle de vie des positions Kalshi.
Extrait de kalshi_alpha_bot.py (P3.6).
"""

import logging
import time
from datetime import datetime, timezone

import accounting
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
        # AIR-001 W3: set on any reconcile pass that meets broker rows the
        # typed contract cannot interpret — consumed by the risk validator
        # as a fail-closed trading block (EXCHANGE_SCHEMA_INCOMPATIBLE).
        self.exchange_schema_incompatible = False

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

    def unresolved_settlement_count(self) -> int:
        """Positions parked as settlement_unknown (AIR-001 W7): the
        realized PnL of these is UNKNOWN, so risk aggregates built on
        realized PnL are unreliable until an operator resolves them."""
        return sum(1 for p in self.positions.values()
                   if p.get("state") == "settlement_unknown")

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

    @staticmethod
    def correlation_group(ticker: str, category: str = "Other") -> str:
        """Stable broad-underlying bucket used for portfolio concentration."""
        t = str(ticker or "").upper()
        for prefix, group in (("KXBTC", "BTC"), ("KXETH", "ETH"),
                              ("KXXRP", "XRP")):
            if t.startswith(prefix):
                return group
        return str(category or "Other").strip().upper() or "OTHER"

    def open_risk_by_group(self) -> dict:
        out = {}
        for p in self._active_positions():
            group = self.correlation_group(p.get("ticker"), p.get("category"))
            out[group] = out.get(group, 0.0) + p["count"] * p["avg_price"] / 100.0
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
            # ATLAS-TOTAL-AUDIT-001 : une position reconstruite (brk-*)
            # n'a pas de ligne de registre — son reglement echouait
            # 'settle_trade introuvable' en boucle infinie. VERITE
            # BROKER > CACHE LOCAL : adoption d'une ligne a provenance
            # explicite AVANT tout reglement (idempotent). Un orphelin
            # NON brk-* reste une erreur bruyante (bug distinct).
            if str(p.get("trade_id", "")).startswith("brk-") and \
                    not any(t["trade_id"] == p["trade_id"]
                            for t in self.tlog.trades):
                self.tlog.adopt_reconstructed(p)
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
                    # AIR-001 W7: conservative = WORST case (full cost +
                    # fees, once). The old code recorded only -fees as
                    # gross and subtracted fees AGAIN in net.
                    acct = accounting.settle_forced_conservative(
                        count=p["count"], avg_price_cents=p["avg_price"],
                        fees_dollars=p["fees"])
                    gross, net = acct["gross"], acct["net"]
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
                acct = accounting.settle_void(fees_dollars=p["fees"])
                gross, net = acct["gross"], acct["net"]
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
                    # AIR-001 W7: UNKNOWN stays UNKNOWN. The old code
                    # CONVERTED an unreadable result into an invented
                    # fee-loss settlement (double-counting the fees).
                    # The position is now PARKED, loudly, with no PnL
                    # fabricated; the RiskProof blocks new orders while
                    # any settlement is unresolved.
                    if p.get("state") != "settlement_unknown":
                        p["state"] = "settlement_unknown"
                        p["settlement_unknown_since"] = now_iso()
                        p["settlement_raw_result"] = repr(
                            pick(m, "result", default=None))
                        self.flush()
                    log_pos.error(
                        f"[SETTLEMENT_UNKNOWN] {p['ticker']}: statut "
                        f"'{status}' mais result illisible (raw="
                        f"{repr(pick(m, 'result', default=None))}) -- "
                        "position PARQUEE, PnL NON fabrique, resolution "
                        "operateur requise")
                    continue
                else:
                    # market still open or unknown → keep position
                    continue

            # ── happy path: yes / no ──────────────────────────────────
            acct = accounting.settle_yes_no(
                side=p["side"], result=result, count=p["count"],
                avg_price_cents=p["avg_price"], fees_dollars=p["fees"])
            won, gross, net = acct["won"], acct["gross"], acct["net"]
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
        # Every exit path (including broker-unreachable) carries the full
        # observability shape. NOTE (residual risk, AIR-001 Wave 3):
        # broker-unreachable currently reports trading_blocked=False for
        # backward compatibility; the fail-closed broker-freshness block
        # belongs to the Wave 6 RiskProof validator.
        report = {"rebuilt": [], "ghost": [], "matched": [],
                  "contradicted": [], "classification": {},
                  "unknown_schema": 0, "trading_blocked": False,
                  "blocked_reason": None}
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
        # AIR-001 Wave 3 (DE-P0-004): broker rows go through the ONE typed
        # Kalshi contract boundary. Reproduced defect: a current
        # fixed-point row ({"position_fp": "3", ...}) parsed to qty 0 via
        # the tolerant fallbacks, was silently skipped, and the matching
        # LOCAL position was ghosted AND DELETED — broker exposure made
        # invisible (open_risk 0). Now: no-guess parsing; UNKNOWN_SCHEMA
        # rows BLOCK trading (EXCHANGE_SCHEMA_INCOMPATIBLE) and disable
        # every ghost/delete authority for the pass; no invented cost
        # basis (the old code defaulted to 50c and misread dollar
        # exposure as a cent price).
        from decimal import Decimal
        from exchange.kalshi_contracts import classify_positions
        classification = classify_positions(
            broker, list(self.positions.values()))
        report["classification"] = classification["counts"]
        report["unknown_schema"] = classification["unknown_schema"]
        report["trading_blocked"] = classification["trading_blocked"]
        report["blocked_reason"] = classification["blocked_reason"]
        self.exchange_schema_incompatible = \
            classification["trading_blocked"]
        if classification["trading_blocked"]:
            log_pos.error(classification["blocked_reason"])

        seen_tickers = set()
        for entry in classification["entries"]:
            kind = entry["classification"]
            if kind == "UNKNOWN_SCHEMA":
                continue                 # blocked above; nothing touched
            tk, side = entry.get("ticker"), entry.get("side")
            if kind in ("MATCHED", "CONTRADICTED", "BROKER_ONLY"):
                seen_tickers.add(tk)
            if kind == "MATCHED":
                report["matched"].append(tk)
                continue
            if kind == "CONTRADICTED":
                # Broker is authoritative on QUANTITY, but silently
                # overwriting the local count would hide the disagreement:
                # record it loudly; resolution belongs to the startup
                # reconciliation drill, never to a quiet mutation.
                report.setdefault("contradicted", []).append(entry)
                log_pos.error(f"[RECON_CONTRADICTED] {tk} {side}: "
                              f"broker={entry['broker_count']} "
                              f"local={entry['local_count']}")
                continue
            if kind == "BROKER_ONLY":
                tid = f"brk-{tk}-{side}"         # ID STABLE = idempotent
                if tid in self.positions:
                    continue
                count = int(Decimal(entry["count"]))
                exposure = Decimal(entry["exposure_dollars"])
                # Cost basis from the broker's own exposure figure when
                # present; else the labeled conservative 99c bound —
                # exposure is never understated, never invented at 50c.
                avg = int(min(Decimal(99), max(Decimal(1), (
                    exposure * 100 / count).to_integral_value())))
                self.positions[tid] = {
                    "trade_id": tid, "ticker": tk, "side": side,
                    "count_initial": count, "count": count,
                    "avg_price": avg, "fees": 0.0, "opened_at": now_iso(),
                    "order_ids": [], "fill_ids": [], "state": "open",
                    "strategy": "reconciled", "market_score": None,
                    "entry_edge": None, "entry_ev": None,
                    "cost_basis_source": entry["exposure_basis"],
                }
                report["rebuilt"].append(tk)

        ghost_removed = []
        if not classification["trading_blocked"]:
            for tid, p in list(self.positions.items()):
                if p["ticker"] not in seen_tickers \
                        and not tid.startswith("mig-"):
                    p["state"] = "ghost_local_only"
                    report["ghost"].append(p["ticker"])
            # Clean up ghost positions that the broker doesn't know about
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

