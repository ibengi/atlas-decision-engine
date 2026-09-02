"""
PositionManager — Gestion du cycle de vie des positions Kalshi.
Extrait de kalshi_alpha_bot.py (P3.6).
"""

import logging
import math
import time
from datetime import datetime, timezone

from config import CFG, _p
from kalshi_client import KalshiClient, pick, pick_int
from persistence import JsonStore
from trade_logger import TradeLogger, now_iso

# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log_pos = logging.getLogger("POSITION")


#: SETTLEMENT CONFIRMATION PROTOCOL (money path).
#:
#: Measured on 2026-09-02 in the daily evidence journal: KXBTCD markets
#: polled within minutes of close returned result="no" before the exchange
#: had determined them, and that first answer was frozen. The same
#: get_market().result read settles POSITIONS here, so the same protocol
#: applies: a yes/no/void is booked only under a finalized market status,
#: or when the identical value is observed twice at least
#: SETTLE_CONFIRM_MIN_S apart with the first observation at least
#: SETTLE_MIN_LAG_S after close. Until then the position stays open and the
#: pending observation is persisted with it, so a restart resumes the
#: window instead of restarting it.
SETTLE_FINALIZED_STATUSES = ("settled", "finalized")
SETTLE_MIN_LAG_S = 1800.0
SETTLE_CONFIRM_MIN_S = 1800.0


def _parse_iso_dt(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


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

    def _audit(self, event: str, p: dict, **fields):
        """Explicit settlement audit trail. The event name is in the message
        (so a log filter finds it) AND in `extra` (so a JSON sink keys it)."""
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        log_pos.info(f"[{event}] {p.get('ticker')} trade={p.get('trade_id')} "
                     f"{detail}", extra={"event": event, "ticker": p.get("ticker"),
                                         "trade_id": p.get("trade_id"), **fields})

    def _settlement_confirmed(self, p: dict, m: dict, result: str,
                              status: str, now_dt: datetime) -> bool:
        """The protocol above. True only when `result` may be booked NOW.
        Every other path leaves the position open and returns False."""
        if status in SETTLE_FINALIZED_STATUSES:
            self._audit("SETTLEMENT_CONFIRMED", p, result=result,
                        status=status, method="status")
            return True
        close = _parse_iso_dt(pick(m, "close_time", default=None))
        lag = (now_dt - close).total_seconds() if close else None
        now_iso = now_dt.isoformat(timespec="seconds")
        if lag is not None and lag < SETTLE_MIN_LAG_S:
            # Exactly the failure mode: an answer minutes after close.
            self._audit("SETTLEMENT_OBSERVED", p, result=result,
                        status=status or "-", lag_s=round(lag),
                        note="too_early_to_count")
            self._audit("SETTLEMENT_PENDING_CONFIRMATION", p,
                        reason="observed_before_min_lag")
            return False
        prior = p.get("settle_obs")
        if prior and prior.get("result") != result:
            self._audit("SETTLEMENT_REJECTED_INCONSISTENT", p,
                        prior=prior.get("result"), now=result,
                        prior_at=prior.get("observed_at"))
            prior = None                     # the clock restarts below
        if prior:
            prior_at = _parse_iso_dt(prior.get("observed_at"))
            prior_lag = prior.get("lag_s")
            if (prior_at is not None and prior_lag is not None
                    and (now_dt - prior_at).total_seconds()
                    >= SETTLE_CONFIRM_MIN_S):
                self._audit("SETTLEMENT_CONFIRMED", p, result=result,
                            status=status or "-", method="repeat",
                            first_seen=prior.get("observed_at"))
                return True
            self._audit("SETTLEMENT_PENDING_CONFIRMATION", p,
                        reason="confirmation_window_open",
                        first_seen=prior.get("observed_at"))
            return False
        p["settle_obs"] = {"result": result, "status": status or None,
                           "observed_at": now_iso,
                           "close_time": close.isoformat(timespec="seconds")
                           if close else None,
                           "lag_s": round(lag, 1) if lag is not None else None}
        self.flush()                          # survives a restart
        self._audit("SETTLEMENT_OBSERVED", p, result=result,
                    status=status or "-",
                    lag_s=round(lag) if lag is not None else "unknown")
        self._audit("SETTLEMENT_PENDING_CONFIRMATION", p,
                    reason="first_observation"
                    if lag is not None else "close_time_unknown")
        return False

    def check_settlements(self, now=None) -> list:
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
        now_dt = now or datetime.now(timezone.utc)
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

            # ── confirmation protocol: a readable result is booked only
            # ── once confirmed; until then the position stays open ─────
            if result in ("yes", "no", "void") and \
                    not self._settlement_confirmed(p, m, result, status, now_dt):
                continue

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

    #: Champs de quantite acceptes d'une ligne /portfolio/positions. L'API
    #: DEMO reelle n'expose que position_fp (chaine a virgule fixe, signee).
    QTY_FIELDS = ("position", "position_fp", "quantity", "count")

    @staticmethod
    def parse_broker_qty(bp: dict):
        """Quantite nette signee d'une ligne broker -> (int, None) ou
        (None, raison).

        Un echec de lecture n'est JAMAIS converti en 0 : le 2026-08-31,
        position_fp="-6.00" lu comme 0 a fait juger le broker "a plat" et le
        demarrage a detruit les trois positions restaurees. Les champs
        multiples doivent CONCORDER (aucune precedence documentee par l'API)
        et les quantites fractionnaires sont refusees tant que la
        specification broker ne les atteste pas.
        """
        seen = []
        for f in PositionManager.QTY_FIELDS:
            v = bp.get(f)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return None, f"{f}={v!r} illisible"
            if not math.isfinite(fv):
                return None, f"{f}={v!r} non fini (NaN/inf)"
            if fv != int(fv):
                return None, (f"{f}={v!r} non entier -- contrats "
                              f"fractionnaires non attestes par l'API")
            seen.append((f, int(fv)))
        if not seen:
            return None, ("aucun champ de quantite reconnu (attendus: "
                          + ", ".join(PositionManager.QTY_FIELDS) + ")")
        if len({q for _, q in seen}) > 1:
            return None, f"champs de quantite contradictoires: {seen}"
        return seen[0][1], None

    def _broker_net_positions(self, broker):
        """(dict ticker->net signe, None) ou (None, raison UNKNOWN)."""
        net = {}
        for bp in broker:
            if not isinstance(bp, dict):
                return None, f"ligne broker inexploitable: {bp!r}"
            tk = bp.get("ticker")
            if not tk:
                return None, f"ligne broker sans ticker: {bp!r}"
            qty, err = self.parse_broker_qty(bp)
            if err:
                return None, f"{tk}: {err}"
            net[tk] = net.get(tk, 0) + qty
        return {tk: q for tk, q in net.items() if q != 0}, None

    def _local_net_positions(self):
        net = {}
        for p in self._active_positions():
            sign = 1 if p.get("side") == "yes" else -1
            net[p["ticker"]] = net.get(p["ticker"], 0) \
                + sign * int(p.get("count", 0))
        return {tk: q for tk, q in net.items() if q != 0}

    @staticmethod
    def _classify_mismatch(b, l) -> str:
        if l is None:
            return "broker_only"
        if b is None:
            return "local_only"
        if (b > 0) != (l > 0):
            return "side_mismatch"
        return "quantity_mismatch"

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
                              -> SEUL statut qui leve le verrou
          MISMATCH            au moins une divergence (manquant d'un cote,
                              ou quantite/sens differents) -> verrou
          BROKER_UNAVAILABLE  la reconciliation etait DUE et la verite n'a
                              pas pu etre etablie -> verrou (fail-closed) ;
                              un verrou anterieur plus specifique
                              (MISMATCH/UNKNOWN) est conserve tel quel
          UNKNOWN             reponse broker inexploitable -> verrou

        Les positions financieres existantes ne sont JAMAIS detruites ni
        modifiees parce que le broker est indisponible ; les reglements et
        la recuperation en lecture seule continuent par ailleurs.
        """
        report = {"status": "MATCH", "mismatches": [], "detail": ""}
        def _unavailable(detail):
            report["status"] = "BROKER_UNAVAILABLE"
            report["detail"] = detail
            # Fail-closed : la reconciliation etait due et la verite n'a
            # pas pu etre etablie -> les NOUVELLES soumissions sont
            # bloquees. Un verrou anterieur plus specifique (MISMATCH/
            # UNKNOWN) est conserve : seul un MATCH digne de confiance
            # leve un verrou, jamais un echec de verification.
            if self.reconcile_halt is None:
                self.reconcile_halt = {"status": "BROKER_UNAVAILABLE",
                                       "detail": detail, "at": now_iso()}
            log_pos.error(f"[RECONCILE_VERIFY] broker indisponible "
                          f"({detail}) -- verite non etablie, soumissions "
                          f"bloquees fail-closed jusqu'a un MATCH.")
            return report

        try:
            broker = self.client.get_positions()
        except Exception as e:                                # noqa: BLE001
            return _unavailable(str(e))
        if broker is None:
            return _unavailable("get_positions() -> None")

        # Broker : quantite nette signee par ticker (yes>0, no<0) via le
        # parseur PARTAGE avec la reconciliation de demarrage.
        try:
            broker_net, err = self._broker_net_positions(broker)
        except (TypeError, AttributeError) as e:
            broker_net, err = None, str(e)
        if err is not None:
            report["status"] = "UNKNOWN"
            report["detail"] = f"reponse broker inexploitable: {err}"
            self.reconcile_halt = {"status": "UNKNOWN",
                                   "detail": report["detail"],
                                   "at": now_iso()}
            log_pos.error("[RECONCILE_VERIFY] reponse broker inexploitable "
                          f"({err}) -- soumissions bloquees fail-closed.")
            return report

        local_net = self._local_net_positions()

        for tk in sorted(set(broker_net) | set(local_net)):
            b, l = broker_net.get(tk), local_net.get(tk)
            if b == l:
                continue
            kind = self._classify_mismatch(b, l)
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
        """Verification broker de DEMARRAGE, meme loi que le verificateur
        periodique : strictement NON destructrice.

        Ce passage a longtemps ete l'inverse : il RECONSTRUISAIT les
        positions broker absentes localement (prix d'entree inventes) et
        SUPPRIMAIT comme « fantomes » les positions locales qu'il ne
        retrouvait pas chez le broker. Le 2026-08-31, un simple changement
        de champ dans la reponse /portfolio/positions (position_fp) a fait
        lire chaque quantite comme 0 : le broker a ete juge a plat et les
        trois positions tout juste restaurees de la migration ont ete
        detruites localement -- sans reglement, sans trace journal --
        pendant que la meme reponse broker les listait.

        Aucune divergence broker/local ne justifie une correction
        automatique d'etat financier. Toute divergence (ou impossibilite
        d'etablir la verite) arme self.reconcile_halt, que les portes du
        moteur lisent pour bloquer les soumissions fail-closed ; seul un
        MATCH digne de confiance laisse le demarrage sans verrou.
        """
        report = {"status": "MATCH", "mismatches": [], "matched": [],
                  "detail": ""}

        def _halt(status, detail):
            report["status"] = status
            report["detail"] = detail
            self.reconcile_halt = {"status": status, "detail": detail,
                                   "at": now_iso()}
            if report["mismatches"]:
                self.reconcile_halt["mismatches"] = report["mismatches"]
            log_pos.error(
                f"[RECONCILE_STARTUP] {status} ({detail}) -- soumissions "
                f"bloquees fail-closed ; AUCUNE position locale supprimee, "
                f"AUCUNE position broker adoptee, journal INTACT.")
            JsonStore.save(_p("reconciliation_report.json"), report)
            return report

        MAX_RETRIES = 3
        RETRY_BACKOFF_SECONDS = 2.0
        broker = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                broker = self.client.get_positions()
            except Exception as e:                            # noqa: BLE001
                return _halt("BROKER_UNAVAILABLE", str(e))
            if broker is not None:
                break
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                log_pos.warning(
                    f"get_positions() returned None (attempt "
                    f"{attempt}/{MAX_RETRIES}), retrying in {wait:.0f}s...")
                time.sleep(wait)
        if broker is None:
            return _halt("BROKER_UNAVAILABLE",
                         f"get_positions() -> None apres {MAX_RETRIES} "
                         f"tentatives")

        try:
            broker_net, err = self._broker_net_positions(broker)
        except (TypeError, AttributeError) as e:
            broker_net, err = None, str(e)
        if err is not None:
            return _halt("UNKNOWN", f"reponse broker inexploitable: {err}")

        local_net = self._local_net_positions()
        for tk in sorted(set(broker_net) | set(local_net)):
            b, l = broker_net.get(tk), local_net.get(tk)
            if b == l:
                report["matched"].append(tk)
                continue
            report["mismatches"].append(
                {"ticker": tk, "kind": self._classify_mismatch(b, l),
                 "broker": b, "local": l})
        if report["mismatches"]:
            return _halt("MISMATCH",
                         f"{len(report['mismatches'])} divergence(s) "
                         f"broker/local: {report['mismatches']}")

        JsonStore.save(_p("reconciliation_report.json"), report)
        log_pos.info(f"[RECONCILE_STARTUP] MATCH (tickers "
                     f"broker={len(broker_net)} local={len(local_net)})")
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
            # positions = l'etat local a ete PERDU, pas simplement absent —
            # et reconcile_with_broker armera alors un verrou MISMATCH
            # (broker_only) qui bloque toute soumission jusqu'a decision
            # de l'operateur : plus aucune reconstruction automatique.
            # Un journal ne contenant qu'une correction de ledger n'est PAS
            # la preuve qu'un moteur a deja trade.
            from trade_logger import is_correction
            if not [t for t in self.tlog.trades if not is_correction(t)]:
                log_pos.warning(
                    "[STATE_EMPTY] aucune position locale ET journal de "
                    "trades vide au demarrage. Si le broker detient des "
                    "positions, l'etat local a ete perdu (disque ephemere ?) "
                    ": la reconciliation de demarrage bloquera les "
                    "soumissions (broker_only) au lieu de reconstruire.",
                    extra={"event": "state_empty_at_startup"})

