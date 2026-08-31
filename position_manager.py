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
        # Verrou de reconciliation periodique (voir verify_against_broker) :
        # None = pas de divergence connue ; sinon dict {status, detail, at}.
        # Volontairement NON persiste : chaque redemarrage re-verifie contre
        # le broker au lieu d'heriter d'un verdict peut-etre perime.
        self.reconcile_halt = None

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

    def _settle_and_release(self, tid: str, p: dict, result: str,
                            won: bool, gross: float, net: float):
        """Regle le trade et libere le slot ; retourne la ligne reglee.

        settle_trade ne retourne None que dans UN cas : le trade_id est absent
        du journal (« introuvable »). Ce n'est pas une erreur transitoire — un
        id inconnu du journal ne le deviendra jamais — donc « garder la
        position pour reessayer » garantissait un slot occupe a vie. Le cas
        concret est une position ``brk-...`` reconstruite depuis le broker
        apres un redemarrage : le journal des trades vivait sur le disque
        ephemere du conteneur precedent.

        Pour ces positions, le reglement est ecrit comme ligne orpheline
        (auditee, marquee ``orphan``) et le slot est libere quand meme : le
        broker a publie un resultat, la position n'existe plus chez lui, la
        garder localement ne protege rien et bloque MAX_OPEN_POSITIONS.
        """
        t = self.tlog.settle_trade(p["trade_id"], result, won, gross, net)
        if t is None:
            log_pos.warning(
                f"{p['ticker']}: trade {p['trade_id']} absent du journal "
                f"(position reconstruite apres redemarrage ?) — reglement "
                f"orphelin, slot libere quand meme.")
            t = self.tlog.settle_orphan(p, result, won, gross, net)
        self.positions.pop(tid, None)
        self.flush()
        return t

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
                    t = self._settle_and_release(tid, p, "expired_stale", False, gross, net)
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
                t = self._settle_and_release(tid, p, "void", False, gross, net)
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
                    t = self._settle_and_release(tid, p, "void_unreadable", False, gross, net)
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
            t = self._settle_and_release(tid, p, result, won, gross, net)
            if t: realized.append(t)
        return realized

    def verify_against_broker(self) -> dict:
        """Verification broker PERIODIQUE, strictement NON destructrice.

        A la difference de reconcile_with_broker (demarrage), ce passage ne
        reconstruit rien, ne supprime rien et ne cree evidemment aucun
        trade : le broker reste la source de verite, mais un etat financier
        incertain n'est jamais « corrige » automatiquement en cours de vol.
        Une divergence arme self.reconcile_halt, que les portes du moteur
        lisent pour bloquer toute NOUVELLE soumission fail-closed ; un
        passage ulterieur entierement MATCH le desarme (retablissement).

        Statuts retournes :
          MATCH               broker et local concordent ticker par ticker
          MISMATCH            au moins une divergence (manquant d'un cote,
                              ou quantite/sens differents)
          BROKER_UNAVAILABLE  API indisponible — AUCUNE conclusion, le
                              verrou existant n'est ni pose ni leve
          UNKNOWN             reponse broker inexploitable — traite comme
                              une divergence (fail-closed)
        """
        report = {"status": "MATCH", "mismatches": [], "detail": ""}
        try:
            broker = self.client.get_positions()
        except Exception as e:                                # noqa: BLE001
            report["status"] = "BROKER_UNAVAILABLE"
            report["detail"] = str(e)
            log_pos.warning(f"[RECONCILE_VERIFY] broker indisponible ({e}) "
                            "-- verdict inchange (absence de preuve).")
            return report
        if broker is None:
            report["status"] = "BROKER_UNAVAILABLE"
            report["detail"] = "get_positions() -> None"
            log_pos.warning("[RECONCILE_VERIFY] get_positions() -> None "
                            "-- verdict inchange (absence de preuve).")
            return report

        # Broker : quantite nette signee par ticker (yes>0, no<0).
        broker_net = {}
        try:
            for bp in broker:
                tk = bp.get("ticker")
                if not tk:
                    continue
                qty = pick_int(bp, "position", "quantity", "count", default=0)
                broker_net[tk] = broker_net.get(tk, 0) + qty
        except (TypeError, AttributeError) as e:
            report["status"] = "UNKNOWN"
            report["detail"] = f"reponse broker inexploitable: {e}"
            self.reconcile_halt = {"status": "UNKNOWN",
                                   "detail": report["detail"],
                                   "at": now_iso()}
            log_pos.error("[RECONCILE_VERIFY] reponse broker inexploitable "
                          f"({e}) -- soumissions bloquees fail-closed.")
            return report
        broker_net = {tk: q for tk, q in broker_net.items() if q != 0}

        local_net = {}
        for p in self._active_positions():
            sign = 1 if p.get("side") == "yes" else -1
            local_net[p["ticker"]] = (local_net.get(p["ticker"], 0)
                                      + sign * int(p.get("count", 0)))
        local_net = {tk: q for tk, q in local_net.items() if q != 0}

        for tk in sorted(set(broker_net) | set(local_net)):
            b, l = broker_net.get(tk), local_net.get(tk)
            if b == l:
                continue
            if l is None:
                kind = "broker_only"
            elif b is None:
                kind = "local_only"
            else:
                kind = "quantity_mismatch"
            report["mismatches"].append(
                {"ticker": tk, "kind": kind, "broker": b, "local": l})

        if report["mismatches"]:
            report["status"] = "MISMATCH"
            self.reconcile_halt = {"status": "MISMATCH",
                                   "detail": report["mismatches"],
                                   "at": now_iso()}
            log_pos.error(f"[RECONCILE_VERIFY] MISMATCH -- soumissions "
                          f"bloquees fail-closed, etat local INTACT "
                          f"(aucune correction automatique): "
                          f"{report['mismatches']}")
        else:
            if self.reconcile_halt is not None:
                log_pos.warning("[RECONCILE_VERIFY] retablissement: broker "
                                "et local de nouveau concordants -- verrou "
                                "de reconciliation leve.")
            self.reconcile_halt = None
            log_pos.info(f"[RECONCILE_VERIFY] MATCH "
                         f"(tickers broker={len(broker_net)} "
                         f"local={len(local_net)})")
        return report

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
            # Le broker connait l'EXISTENCE, le sens et la quantite. Il ne
            # rend pas le prix d'entree reellement paye : ni les frais, ni la
            # strategie, ni l'horodatage d'ouverture. `default=50` n'est donc
            # pas une mesure mais un remplissage — 50c au hasard sur une
            # position reellement entree a 3c fausse open_risk, le PnL au
            # reglement et toute statistique qui les agrege.
            #
            # Le chiffre est conserve (le retirer casserait l'arithmetique en
            # aval) mais il est desormais ETIQUETE : `avg_price_estimated`
            # dit qu'aucun fill ne l'atteste, et `opened_at_estimated` dit
            # que l'horodatage est celui de la reconstruction, pas de
            # l'ouverture — ce qui compte pour l'echappatoire d'age.
            measured_avg = pick_int(bp, "avg_price", default=0)
            avg = measured_avg or (pick_int(bp, "market_exposure", default=50) or 50)
            estimated = measured_avg <= 0
            self.positions[tid] = {
                "trade_id": tid, "ticker": tk, "side": side,
                "count_initial": abs(qty), "count": abs(qty),
                "avg_price": avg, "fees": 0.0, "opened_at": now_iso(),
                "order_ids": [], "fill_ids": [], "state": "open",
                "strategy": "reconciled", "market_score": None,
                "entry_edge": None, "entry_ev": None,
                "avg_price_estimated": estimated,
                "fees_estimated": True,
                "opened_at_estimated": True,
            }
            if estimated:
                log_pos.warning(
                    f"{tk}: position reconstruite depuis le broker sans prix "
                    f"d'entree atteste -- avg_price={avg}c ESTIME, frais "
                    f"inconnus (0.0$), strategie perdue. Le PnL de reglement "
                    f"de cette position sera approximatif.")
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
        else:
            # Un demarrage sans AUCUNE position locale est normal apres un
            # arret propre a plat — et c'est aussi exactement ce que l'on
            # observe quand le disque a disparu. Les deux se distinguent en
            # regardant le journal des trades : un moteur qui a deja
            # travaille en a forcement un. Vide + un broker qui detient des
            # positions = l'etat local a ete PERDU, pas simplement absent.
            #
            # Le dire fort au demarrage est la seule occasion de le voir :
            # au cycle suivant, reconcile_with_broker aura reconstruit les
            # positions et tout aura l'air normal, avec des prix d'entree
            # inventes et un historique de risque remis a zero.
            if not self.tlog.trades:
                log_pos.warning(
                    "[STATE_EMPTY] aucune position locale ET journal de "
                    "trades vide au demarrage. Si le broker detient des "
                    "positions, l'etat local a ete perdu (disque ephemere ?) "
                    ": les prix d'entree seront estimes et l'historique de "
                    "risque (drawdown, pertes consecutives, PnL du jour) "
                    "repart de zero.",
                    extra={"event": "state_empty_at_startup"})

