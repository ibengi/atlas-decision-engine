"""
TradeLogger — Journal des trades reels Kalshi.
Extrait de kalshi_alpha_bot.py (PR #16, P3.5+).

Le journal contient DEUX types d'evenements, distingues par ``event_type``:

  ``trade``             un trade independant (defaut ; les lignes ecrites
                        avant l'introduction du champ n'en portent pas et
                        sont donc des trades)
  ``ledger_correction`` une correction broker-authoritative portant sur UN
                        trade existant (``corrects_trade_id``). Ce n'est PAS
                        un trade : c'est le delta economique d'un fill que
                        le moteur n'avait pas observe.

REGLE DE COMPTAGE (unique et explicite) : une correction n'est JAMAIS une
ligne des surfaces qui comptent des trades ; son economie est REPLIEE dans
le trade d'origine par ``fold_corrections``. Le nombre de trades, le
win/loss, la serie de pertes et la recence de reglement restent donc ceux
du trading reel, tandis que PnL, frais et quantite refletent la verite
broker. ``settled_trades``/``open_trades`` servent cette vue repliee ;
``trades`` reste le journal brut (audit), ``trade_rows``/``correction_rows``
en donnent les deux moities.
"""

import logging
import uuid
from datetime import datetime, timezone

from config import CFG, _p
from persistence import JsonStore, file_fingerprint, PersistenceSentinel
from strict_data import finite_number, integer, validate_tree
from journal_transaction import journal_transaction

# Module-level logger (meme format que dans kalshi_alpha_bot.py)
log_trd = logging.getLogger("TRADE")

#: Valeurs de ``event_type``. L'absence du champ signifie EVENT_TRADE
#: (compatibilite avec tout le journal historique).
EVENT_TRADE = "trade"
EVENT_LEDGER_CORRECTION = "ledger_correction"

#: Champs economiques additifs replies d'une correction vers sa cible.
_FOLDED_FIELDS = ("gross_pnl", "net_pnl", "fees", "filled_count")


def now_iso() -> str:
    """Horodatage UTC ISO 8601 a la seconde pres."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_correction(row: dict) -> bool:
    return row.get("event_type") == EVENT_LEDGER_CORRECTION


def fold_corrections(rows: list) -> list:
    """Vue ECONOMIQUE du journal: les trades independants seuls, chacun
    portant la somme de ses corrections broker-authoritative.

    Entree = journal brut (liste de dicts, corrections comprises).
    Sortie = liste des seuls trades, meme ordre, valeurs corrigees. Une
    ligne corrigee est une COPIE marquee ``corrected``/``correction_ids``;
    une ligne sans correction est renvoyee telle quelle (identite
    preservee). Une correction orpheline (cible absente) n'est repliee
    nulle part et ne peut donc pas creer de PnL fantome.
    """
    by_target = {}
    for row in rows:
        if is_correction(row):
            by_target.setdefault(row.get("corrects_trade_id"), []).append(row)
    out = []
    for t in rows:
        if is_correction(t):
            continue
        corrs = by_target.get(t.get("trade_id"))
        if not corrs:
            out.append(t)
            continue
        eff = dict(t)
        for c in corrs:
            for field in _FOLDED_FIELDS:
                delta = c.get(field)
                if delta is None:
                    continue
                base = eff.get(field)
                if base is None:
                    # Cible sans valeur (trade encore ouvert): on ne
                    # fabrique pas une economie a partir de rien.
                    continue
                eff[field] = (base + delta if isinstance(base, int)
                              and isinstance(delta, int)
                              else round(float(base) + float(delta), 6))
        eff["corrected"] = True
        eff["correction_ids"] = [c.get("correction_id") for c in corrs]
        out.append(eff)
    return out


class TradeLogger:
    """Journal des trades. REGLE ABSOLUE : on n'enregistre un trade que si
    filled_count > 0 (execution verifiee). Les anciens enregistrements
    'dry_run' sont archives a part, jamais melanges aux vrais."""

    SCHEMA = "v11"

    def __init__(self):
        self.path = _p(CFG.TRADES_FILE)
        raw = JsonStore.load(self.path, [])
        self._fingerprint = file_fingerprint(self.path)
        self.integrity_error = None
        if not isinstance(raw, list) or any(not isinstance(t, dict) for t in raw):
            self.integrity_error = "journal has malformed rows"
            raw = []
        legacy = [t for t in raw if t.get("schema") != self.SCHEMA]
        if legacy:
            self.integrity_error = "unknown/legacy journal schema: explicit migration required"
        try:
            self.validate_rows(raw)
        except (ValueError, TypeError) as exc:
            self.integrity_error = str(exc)
        if any(t.get("orphan") for t in raw):
            self.integrity_error = "orphan execution evidence requires reconstruction"
        self.trades = raw
        if self.integrity_error:
            PersistenceSentinel.record_failure(self.path, self.integrity_error)
        #: A04: economic identity, computed on load and maintained on every
        #: append. A journal holding the same trade twice reports a smaller
        #: loss while its count, its digest and its totals all stay
        #: self-consistent -- Astra replayed one profitable row and cut the
        #: drawdown from 30.8% to 7.7% without a single mismatch.
        self.duplicate_ids = self._scan_duplicate_ids(self.trades)
        if self.duplicate_ids:
            log_trd.critical(
                f"[JOURNAL_IDENTITY] {len(self.duplicate_ids)} identite(s) "
                f"economique(s) en DOUBLE dans le journal: "
                f"{sorted(self.duplicate_ids)[:8]} -- l'historique n'est plus "
                f"une suite d'evenements distincts; le ledger refuse "
                f"d'etendre son watermark et bloque CAPITAL.")
    @staticmethod
    def validate_rows(rows):
        validate_tree(rows)
        for row in rows:
            for field in ("gross_pnl", "net_pnl", "roi", "fees", "avg_fill_price", "requested_price"):
                if row.get(field) is not None:
                    finite_number(row[field], field, minimum=0 if not is_correction(row) and field in (
                        "fees", "avg_fill_price", "requested_price") else None)
            for field in ("filled_count", "requested_count"):
                if row.get(field) is not None:
                    integer(row[field], field, minimum=0 if not is_correction(row) else None)
            if row.get("state") not in ("open", "settled", "expired"):
                raise ValueError("unknown journal economic state")

    @staticmethod
    def event_keys(row: dict) -> list:
        """The identities that make a row a DISTINCT economic event."""
        keys = []
        if not is_correction(row):
            order_id = row.get("order_id")
            if order_id:
                keys.append(("broker_order_id", str(order_id)))
        else:
            # A correction is a revision of a target, not a second trade.
            broker_digest = row.get("evidence_sha256") or row.get("broker_evidence_sha256")
            if isinstance(row.get("broker_evidence"), dict) and row["broker_evidence"]:
                import hashlib
                from strict_data import dumps
                broker_digest = hashlib.sha256(dumps(row["broker_evidence"],
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if broker_digest:
                keys.append(("correction_evidence", str(row.get("corrects_trade_id")) + ":" + str(broker_digest)))
        tid = row.get("trade_id")
        if tid:
            keys.append(("trade_id", str(tid)))
        sid = row.get("settlement_id")
        if sid:
            keys.append(("settlement_id", str(sid)))
        cid = row.get("correction_id")
        if cid:
            keys.append(("correction_id", str(cid)))
        return keys

    @classmethod
    def _scan_duplicate_ids(cls, rows) -> set:
        seen, dupes = set(), set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in cls.event_keys(row):
                if key in seen:
                    dupes.add("%s=%s" % key)
                else:
                    seen.add(key)
        return dupes

    def _reject_duplicate(self, rec: dict) -> bool:
        """True when `rec` would re-introduce an identity already present.

        A correction or a reversal is a DIFFERENT event with its own
        `correction_id` that points at the row it corrects: that is how the
        journal expresses "this economics changed". Re-appending a row that
        carries an identity already in the journal is not a correction, it
        is a duplicate, and it is refused.
        """
        existing = set()
        for row in self.trades:
            existing.update(self.event_keys(row))
        clash = [k for k in self.event_keys(rec) if k in existing]
        if not clash:
            return False
        log_trd.critical(
            f"[JOURNAL_IDENTITY] ecriture REFUSEE: {clash} existe deja dans "
            f"le journal. Un evenement economique ne peut pas etre "
            f"enregistre deux fois; une economie qui change s'exprime par "
            f"une correction portant son propre correction_id.")
        return True

    @journal_transaction
    def open_trade(self, *, ticker, market_title, side, req_price, avg_price,
                   req_count, filled_count, spread, fees, edge, ev, confidence,
                   grade, reason, analysis, order_id, order_status,
                   decision_id=None) -> dict:
        # decision_id: the originating Decision's identifier, attached at the
        # moment the trade row is born and never rewritten afterwards
        # (settle_trade updates only settlement fields). It is the canonical
        # Decision -> Trade -> Settlement lifecycle key; None means the trade
        # did not originate from a traceable decision (e.g. crash recovery)
        # and stays honestly unjoinable.
        for label, value in (("price", avg_price), ("requested price", req_price),
                             ("fees", fees)):
            finite_number(value, label, minimum=0)
        integer(filled_count, "filled count", minimum=1)
        integer(req_count, "requested count", minimum=filled_count)
        if side not in ("yes", "no") or not isinstance(order_id, str) or not order_id:
            raise ValueError("verified broker order identity and side required")
        rec = {
            "schema": self.SCHEMA, "trade_id": uuid.uuid4().hex[:12],
            "decision_id": decision_id,
            "timestamp": now_iso(), "ticker": ticker, "market": market_title,
            "side": side, "requested_price": req_price, "avg_fill_price": avg_price,
            "requested_count": req_count, "filled_count": filled_count,
            "spread": spread, "fees": round(fees, 2), "edge": edge, "ev": ev,
            "confidence": confidence, "grade": grade, "reason": reason,
            "analysis": analysis, "order_id": order_id, "order_status": order_status,
            "state": "open",       # open -> settled | expired
            "result": None, "won": None,
            "gross_pnl": None, "net_pnl": None, "roi": None,
            "holding_seconds": None, "settled_at": None,
        }
        if self._reject_duplicate(rec):
            raise ValueError(f"duplicate economic identity for {ticker}")
        self.trades.append(rec)
        self.flush()
        log_trd.info(f"OUVERT {ticker} {side.upper()} {filled_count}/{req_count} "
                     f"@ {avg_price}c (frais {fees:.2f}$) ordre={order_id}")
        return rec

    @journal_transaction
    def settle_trade(self, trade_id: str, result: str, won: bool,
                     gross_pnl: float, net_pnl: float):
        finite_number(gross_pnl, "gross PnL")
        finite_number(net_pnl, "net PnL")
        for t in self.trades:
            if t["trade_id"] == trade_id:
                if t.get("state") == "settled":
                    if (t.get("result"), t.get("won"), t.get("gross_pnl"), t.get("net_pnl")) == (
                            result, won, round(gross_pnl, 6), round(net_pnl, 6)):
                        return t
                    raise ValueError("conflicting settlement requires explicit correction")
                opened = datetime.fromisoformat(t["timestamp"])
                t.update({
                    "state": "settled", "result": result, "won": won,
                    "gross_pnl": round(gross_pnl, 6), "net_pnl": round(net_pnl, 6),
                    "roi": round(net_pnl / max(0.01, t["avg_fill_price"] / 100.0
                                               * t["filled_count"]), 4),
                    "settled_at": now_iso(),
                    "holding_seconds": int((datetime.now(timezone.utc) - opened)
                                           .total_seconds()),
                })
                self.flush()
                log_trd.info(f"REGLE  {t['ticker']} -> {result.upper()} | "
                             f"{'GAGNE' if won else 'PERDU'} | net {net_pnl:+.2f}$")
                return t
        log_trd.error(f"settle_trade: trade_id {trade_id} introuvable.")
        return None

    @journal_transaction
    def settle_orphan(self, position: dict, result: str, won: bool,
                      gross_pnl: float, net_pnl: float) -> dict:
        """Regle une position dont le trade d'origine est absent du journal.

        Cas reel : une position reconstruite par reconcile_with_broker apres
        un redemarrage (id ``brk-...``) n'a jamais eu de ligne de trade — le
        journal vivait sur le disque ephemere du conteneur precedent. Pour
        elle, settle_trade echoue a CHAQUE cycle et echouera toujours :
        « position kept for retry » etait un mensonge, le retry ne pouvait
        jamais aboutir et le slot restait occupe jusqu'a l'echappatoire
        MAX_POSITION_AGE_DAYS (30 j).

        On ecrit donc une ligne de reglement synthetisee depuis la position
        elle-meme, marquee ``orphan`` pour que les statistiques puissent
        l'exclure ou l'auditer : ses champs d'entree (prix moyen, frais)
        viennent de la reconstruction broker, pas d'un fill observe.
        """
        self.integrity_error = "orphan execution evidence requires reconstruction"
        rec = {
            "schema": self.SCHEMA,
            "trade_id": position.get("trade_id") or f"orphan-{uuid.uuid4().hex[:8]}",
            "decision_id": None,
            "timestamp": position.get("opened_at") or now_iso(),
            "ticker": position.get("ticker"), "market": None,
            "side": position.get("side"),
            "requested_price": None,
            "avg_fill_price": position.get("avg_price"),
            "requested_count": position.get("count_initial"),
            "filled_count": position.get("count"),
            "spread": None, "fees": round(position.get("fees", 0.0), 2),
            "edge": None, "ev": None, "confidence": None, "grade": None,
            "reason": "orphan_settlement",
            "analysis": ("reglement orphelin : position reconstruite depuis "
                         "le broker apres redemarrage, trade d'origine absent "
                         "du journal (disque ephemere)"),
            "order_id": None, "order_status": None,
            "state": "settled", "result": result, "won": won,
            "gross_pnl": round(gross_pnl, 6), "net_pnl": round(net_pnl, 6),
            "roi": None, "holding_seconds": None, "settled_at": now_iso(),
            "orphan": True,
        }
        if self._reject_duplicate(rec):
            # A04: an orphan settlement carries the reconstructed position's
            # trade_id. Replaying the same one would credit the same
            # settlement twice.
            return None
        self.trades.append(rec)
        self.flush()
        log_trd.warning(
            f"REGLE (orphelin) {rec['ticker']} -> {str(result).upper()} | "
            f"{'GAGNE' if won else 'PERDU'} | net {net_pnl:+.2f}$ | "
            f"trade d'origine absent du journal — reglement synthetise "
            f"depuis la position reconstruite")
        return rec

    # -- vues du journal -------------------------------------------------
    # trades              : journal BRUT (audit, ecriture) -- corrections
    #                       comprises, jamais filtre
    # trade_rows          : trades independants, valeurs D'ORIGINE
    # correction_rows     : evenements correctifs seuls
    # effective_trades    : trades independants, economie CORRIGEE
    # settled/open_trades : la vue effective, filtree par etat

    def trade_rows(self) -> list:
        """Trades independants (les corrections n'en sont pas)."""
        return [t for t in self.trades if not is_correction(t)]

    def correction_rows(self) -> list:
        return [t for t in self.trades if is_correction(t)]

    def effective_trades(self) -> list:
        """Trades independants, corrections broker repliees dedans."""
        return fold_corrections(self.trades)

    def has_open_on(self, ticker: str) -> bool:
        return any(t["ticker"] == ticker and t["state"] == "open"
                   for t in self.trade_rows())

    def open_trades(self) -> list:
        return [t for t in self.effective_trades() if t["state"] == "open"]

    def settled_trades(self) -> list:
        return [t for t in self.effective_trades() if t["state"] == "settled"]

    @journal_transaction
    def append_correction(self, row):
        if self._reject_duplicate(row):
            raise ValueError("duplicate correction")
        self.trades.append(row)
        self.flush()
        return row

    def flush(self):
        self.validate_rows(self.trades)
        self.duplicate_ids = self._scan_duplicate_ids(self.trades)
        if not JsonStore.save(self.path, self.trades, expect_fingerprint=self._fingerprint):
            raise RuntimeError("journal not durably committed")
        self._fingerprint = file_fingerprint(self.path)
        return True
