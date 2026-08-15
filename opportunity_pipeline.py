"""
opportunity_pipeline.py — v2 (2026-07-24)
Chaine normalisation -> classification -> routage -> modele -> prix
executable -> edge net -> decision, avec observabilite structuree.

OBSERVABILITE (exigence G) :
  - UN SEUL resume structure par cycle (dict "summary") :
      scanned_raw, open_cached, liquid, supported, model_evaluated,
      positive_edge, positive_net_ev, risk_passed, orders_submitted,
      fills, rejections_by_reason, cycle_duration_ms, cycle_id ;
  - AUCUN rejet individuel dans le journal principal (DEBUG seulement) ;
  - details par decision (decision_id) dans un JSONL ROTATIF
    (decisions.jsonl, 5 Mo x 3) ;
  - cache de rejets avec TTL (permanent/temporaire) pour ne pas re-evaluer
    en boucle les memes marches condamnes.

PRIX : ask a l'achat (bid a la vente), JAMAIS last_price (applique dans
strategy_router.price_and_gate). Probabilite non calibree => rejet.
"""

import os
import time
import json
import uuid
import logging

from market_classifier import classify
from strategy_router import (Decision, GateConfig, price_and_gate,
                             minutes_to_close)
from decision_tracer import current_tracer
from timing import timed

log = logging.getLogger("PIPELINE")

PIPELINE_VERSION = "pipeline-v2-2026-07-24"

_JSONL_MAX_BYTES = 5 * 1024 * 1024
_JSONL_KEEP = 3


def _env_i(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


_LINEAGE_CACHE = {}


def _lineage() -> dict:
    """The 008E lineage stamp, computed once and never allowed to raise.

    Observability must not be able to break trading. If the export module is
    missing or malformed, the stamp is empty and the cycle proceeds exactly as
    it did before this program.
    """
    if "v" not in _LINEAGE_CACHE:
        try:
            import research_export
            _LINEAGE_CACHE["v"] = research_export.lineage()
        except Exception:                                # noqa: BLE001
            _LINEAGE_CACHE["v"] = {}
    return _LINEAGE_CACHE["v"]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class RotatingJsonl:
    def __init__(self, path):
        self.path = path

    def write(self, obj: dict):
        try:
            if os.path.exists(self.path) and \
                    os.path.getsize(self.path) > _JSONL_MAX_BYTES:
                for i in range(_JSONL_KEEP - 1, 0, -1):
                    src = f"{self.path}.{i}"
                    if os.path.exists(src):
                        os.replace(src, f"{self.path}.{i + 1}")
                os.replace(self.path, f"{self.path}.1")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError as e:
            log.debug(f"jsonl: {e}")


class MarketOpportunityPipeline:
    """client/router/gates injectables ; fresh_book_fn(ticker) ->
    (market, book) ; scanner optionnel (sinon un MarketScanner par defaut
    est construit). observer(snapshot, book, decision) optionnel."""

    def __init__(self, client, router, gates: GateConfig = None,
                 fresh_book_fn=None, observer=None, scanner=None,
                 data_dir: str = "."):
        from market_scanner import MarketScanner
        self.client = client
        self.router = router
        self.gates = gates or GateConfig()
        self.fresh_book_fn = fresh_book_fn
        self.observer = observer
        self.scanner = scanner or MarketScanner(client, router=router,
                                                data_dir=data_dir)
        self.jsonl = RotatingJsonl(os.path.join(data_dir, "decisions.jsonl"))
        # P0: scanner-stage rejections have their own sink. They are kept out
        # of decisions.jsonl on purpose — a decision row means the model was
        # given a market and produced a verdict, and a market the scanner
        # dropped never got that far. Merging the two would make the decision
        # dataset mean two different things.
        self.funnel_jsonl = RotatingJsonl(
            os.path.join(data_dir, "funnel_rejections.jsonl"))
        self.max_deep = _env_i("SCAN_MAX_DEEP_ANALYSES_PER_CYCLE", 50)
        self.ttl_perm = _env_i("PIPELINE_REJECT_TTL_PERMANENT_S", 3600)
        self.ttl_temp = _env_i("PIPELINE_REJECT_TTL_TEMPORARY_S", 120)
        self._reject_cache = {}          # ticker -> (expiry_ts, reason)

    # rejets structurels (ne changeront pas dans l'heure) vs temporaires
    _PERMANENT = ("no_compatible_strategy", "unknown_market_type",
                  "horizon_hors_perimetre")

    def _cache_reject(self, ticker, reason):
        base = reason.split(":", 1)[0]
        ttl = self.ttl_perm if base in self._PERMANENT else self.ttl_temp
        self._reject_cache[ticker] = (time.time() + ttl, reason)

    def _cached_reject(self, ticker):
        e = self._reject_cache.get(ticker)
        if not e:
            return None
        if time.time() > e[0]:
            self._reject_cache.pop(ticker, None)
            return None
        return e[1]

    def _write_funnel_rejections(self, cycle_id: str, observed_at: str,
                                 srep: dict) -> int:
        """Persist one row per scanner-eliminated market. Observability only.

        Never raises: evidence capture must not be able to break a trading
        cycle. A write that fails is logged and the cycle continues exactly
        as it would have before this method existed.
        """
        rows = srep.get("rejections_detail") or []
        written = 0
        try:
            for row in rows:
                self.funnel_jsonl.write({
                    "cycle_id": cycle_id,
                    "observed_at": observed_at,
                    "scanner_version": srep.get("scanner_version"),
                    "rejection": row,
                })
                written += 1
        except Exception as e:                              # noqa: BLE001
            log.warning(f"[FUNNEL_EVIDENCE] write failed: {e}")
        return written

    def run_cycle(self, max_accepted: int = 3, skip_ticker_fn=None) -> dict:
        t0 = time.time()
        cycle_id = uuid.uuid4().hex[:10]
        S = {"cycle_id": cycle_id, "pipeline_version": PIPELINE_VERSION,
             "occurred_at": _now_iso(),
             "scanned_raw": 0, "open_cached": 0, "liquid": 0, "supported": 0,
             # P0: the scanner's intermediate stages were collapsed into
             # open_cached/liquid, so after_status and after_classification
             # had no representation outside the scanner's in-memory report.
             # They are carried explicitly now.
             "after_status": 0, "after_time_window": 0, "after_liquidity": 0,
             "after_classification": 0, "scanner_kept": 0,
             "model_evaluated": 0, "positive_edge": 0, "positive_net_ev": 0,
             "risk_passed": 0, "orders_submitted": 0, "fills": 0,
             "rejections_by_reason": {}, "cycle_duration_ms": None}

        def _rej(reason, ticker=None, price=None):
            base = reason.split(":", 1)[0]
            S["rejections_by_reason"][base] = \
                S["rejections_by_reason"].get(base, 0) + 1
            if tracer and ticker:
                tracer.market(ticker, "REJECTED", reason=base, price=price)

        with timed("api_fetch"):
            scan = self.scanner.scan_cycle()
        srep = scan["report"]
        S["scanned_raw"] = srep["funnel"].get("scanned_raw", 0)
        self._trace_market_count = len(scan.get("markets") or [])
        S["open_cached"] = srep["funnel"].get("after_time_window", 0)
        S["liquid"] = srep["funnel"].get("after_liquidity", 0)
        for _k in ("after_status", "after_time_window", "after_liquidity",
                   "after_classification"):
            S[_k] = srep["funnel"].get(_k, 0)
        S["scanner_kept"] = srep["funnel"].get("kept", 0)
        # P0: scanner-stage eliminations become durable per-market evidence.
        # Markets dropped by the scanner never reach the evaluation loop
        # below, so no decision row is ever written for them. This write is
        # what makes "the universe was eliminated upstream" provable instead
        # of inferred from an absence of rows.
        self._write_funnel_rejections(cycle_id, S["occurred_at"], srep)
        for r, n in srep["excluded_by_reason"].items():
            S["rejections_by_reason"][r] = \
                S["rejections_by_reason"].get(r, 0) + n

        tracer = current_tracer()
        self._trace_market_count = 0
        accepted, deep = [], 0
        S["model_rejections_detailed"] = {}
        S["model_executed"] = {}
        _mt_logged = [0]
        _mt_max = int(os.getenv("MODEL_LOG_MAX", "10"))
        _mt_all = os.getenv("MODEL_DEBUG", "0") == "1"

        def _model_trace(ticker, mtype, strat_name, model):
            """Exigence audit Probability Engine : pour CHAQUE marche
            supporte, strategie choisie, modele, execute ou non, et raison
            precise. Plafond MODEL_LOG_MAX/cycle (MODEL_DEBUG=1 = tout) ;
            l'agregat complet part dans [MODEL-SUMMARY] + le rapport."""
            executed = bool(model.valid)
            mdl = (model.features or {}).get("model_version")                 if executed else "-"
            reason = "ok" if executed else (model.reason or "inconnue")
            key = reason if not executed else "executed"
            if executed:
                S["model_executed"][mdl] = S["model_executed"].get(mdl, 0) + 1
            else:
                S["model_rejections_detailed"][reason] =                     S["model_rejections_detailed"].get(reason, 0) + 1
            if _mt_all or _mt_logged[0] < _mt_max:
                log.info("[MODEL_TRACE] "
                         f"ticker={ticker} market_type={mtype} "
                         f"strategy={strat_name} model={mdl} "
                         f"executed={str(executed).lower()} reason={reason}",
                         extra={"ticker": ticker, "market_type": mtype,
                                "strategy": strat_name, "model": mdl,
                                "executed": executed, "reason": reason})
                _mt_logged[0] += 1
        with timed("market_evaluation"):
            for m in scan["markets"]:
                ticker = m.get("ticker") or ""
                self._trace_market_count += 1
                if tracer:
                    tracer.market(ticker, "SCANNED")
                cls = m.get("_classification") or classify(m).to_dict()
                mtype, cat = cls["market_type"], cls["category"]

                if skip_ticker_fn and skip_ticker_fn(ticker):
                    _rej("already_positioned", ticker); continue
                cached = self._cached_reject(ticker)
                if cached:
                    _rej("cached:" + cached, ticker); continue

                strat = self.router.route(mtype)
                if strat is None:
                    _rej("no_compatible_strategy", ticker)
                    self._cache_reject(ticker, "no_compatible_strategy")
                    log.debug(f"[REJECT] {ticker} no_compatible_strategy "
                              f"(market_type={mtype})")
                    continue
                S["supported"] += 1

                if deep >= self.max_deep:
                    _rej("deep_analysis_cap", ticker); continue
                deep += 1

                # carnet FRAIS (ou celui du snapshot si pas de fetch dedie)
                if self.fresh_book_fn:
                    m2, book = self.fresh_book_fn(ticker)
                    m = m2 or m
                else:
                    from kalshi_alpha_bot import MarketValidator
                    book = MarketValidator.normalize_book(m)
                if not book:
                    _rej("no_liquidity", ticker)
                    self._cache_reject(ticker, "no_liquidity")
                    continue

                mins = m.get("_minutes_remaining") or minutes_to_close(m)
                dec = Decision(ticker=ticker, strategy=strat.name,
                               market_type=mtype, category=cat,
                               decision_id=f"{cycle_id}-{uuid.uuid4().hex[:8]}")
                model = strat.evaluate(m, book, mins)
                _model_trace(ticker, mtype, strat.name, model)
                if model.valid:
                    S["model_evaluated"] += 1
                with timed("signal_check"):
                    dec = price_and_gate(dec, model, book, self.gates,
                                         market=m)
                if tracer:
                    tracer.market(ticker, "EVALUATED", reason=(dec.rejection_reason if not dec.accepted else "accepted"),
                                  edge=dec.net_edge, price=dec.entry_ask)
                if dec.gross_edge is not None and dec.gross_edge > 0:
                    S["positive_edge"] += 1
                if dec.net_ev is not None and dec.net_ev > 0:
                    S["positive_net_ev"] += 1

                if self.observer:
                    try:
                        self.observer(_Snap(m, mins), book, dec)
                    except Exception as e:            # noqa: BLE001
                        log.debug(f"observer: {e}")

                d = dec.to_dict()
                d["final_decision"] = "Accepted" if dec.accepted else \
                    f"Rejected: {(dec.rejection_reason or 'unspecified').split(':', 1)[0]}"
                # Program 008E: lineage stamp. Purely additive metadata on a
                # record that is written to a log and read back by nothing in
                # this process. `dec` is not touched, so no downstream value
                # can change. _lineage() is cached after its first call and
                # cannot raise.
                self.jsonl.write({"cycle_id": cycle_id, "decision": d,
                                  "run_id": tracer.run_id if tracer else None,
                                  "recorded_at": _now_iso(),
                                  "lineage": _lineage()})
                if dec.accepted:
                    log.info(f"[CANDIDAT] {ticker} strat={dec.strategy} "
                             f"{(dec.side or '?').upper()} @ {dec.entry_ask}c "
                             f"edge_net={dec.net_edge:+.3f} "
                             f"ev_net={dec.net_ev:+.3f} conf={dec.confidence}",
                             extra={"ticker": ticker, "strategy": dec.strategy,
                                    "side": dec.side, "price": dec.entry_ask,
                                    "edge_net": dec.net_edge, "ev_net": dec.net_ev,
                                    "confidence": dec.confidence})
                    accepted.append(dec)
                    if len(accepted) >= max_accepted:
                        break
                else:
                    _rej(dec.rejection_reason or "unspecified", ticker, dec.entry_ask)
                    self._cache_reject(ticker, dec.rejection_reason or "unspecified")
                    log.debug(f"[REJECT] {ticker} {dec.rejection_reason}",
                              extra={"ticker": ticker,
                                     "reason": dec.rejection_reason})

        S["cycle_duration_ms"] = int((time.time() - t0) * 1000)
        S["accepted"] = len(accepted)
        # cles heritees lues par ExecutionEngine (compat)
        S.update({"scanned": S["scanned_raw"],
                  "scanner_included": srep["funnel"].get("kept", 0),
                  "valid": srep["funnel"].get("kept", 0),
                  "eligible": srep["funnel"].get("kept", 0),
                  "ranker_eligible": srep["funnel"].get("kept", 0),
                  "strategy_supported": S["supported"],
                  "model_probability": S["model_evaluated"],
                  "rejections": S["rejections_by_reason"],
                  "scanner_report": srep})
        chain = [("scanned_raw", S["scanned_raw"]),
                 ("open_cached", S["open_cached"]),
                 ("liquid", S["liquid"]),
                 ("supported", S["supported"]),
                 ("model_evaluated", S["model_evaluated"]),
                 ("positive_edge", S["positive_edge"]),
                 ("positive_net_ev", S["positive_net_ev"]),
                 ("risk_passed", S["risk_passed"]),
                 ("orders_submitted", S["orders_submitted"]),
                 ("fills", S["fills"])]
        conv, prev = {}, None
        for name, n in chain:
            conv[name] = {"n": n,
                          "pct_of_prev": round(100.0 * n / prev, 2)
                          if prev else (100.0 if n else 0.0)}
            prev = n if n else prev
        S["funnel_conversion"] = conv
        if S["model_rejections_detailed"] or S["model_executed"]:
            log.info("[MODEL-SUMMARY] " + json.dumps(
                {"executed": S["model_executed"],
                 "rejections": S["model_rejections_detailed"]},
                ensure_ascii=False, sort_keys=True))
        return {"report": S, "accepted": accepted}


class _Snap:
    """Snapshot minimal passe a l'observer (compat shadow store)."""
    def __init__(self, market, minutes_remaining):
        self.raw_market = market
        self.minutes_remaining = minutes_remaining
        self.quality = None
