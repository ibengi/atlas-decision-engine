"""
ExecutionEngine — Moteur d'execution principal (orchestrateur du pipeline integre).
Extrait de kalshi_alpha_bot.py (P3.15).
"""
import logging
import os
import time
from typing import Optional

from btc_strategy import BtcStrategy, BTC_AVAILABLE, get_btc_context
from config import CFG, _env_b, _p
from fee_model import FeeModel
from kalshi_client import KalshiAPIError, KalshiClient, pick, pick_int
from market_validator import MarketValidator
from order_manager import OrderManager
from persistence import JsonStore
from position_manager import PositionManager
from position_sizer import PositionSizer
from risk_manager import RiskManager
from stats_engine import StatsEngine
from trade_logger import TradeLogger, now_iso
from decision_tracer import DecisionTracer
from health_monitor import HEALTH
from timing import timed

ENGINE_VERSION = "v11.4-audit-fixed-2026-07-28"

# Module-level loggers (memes canaux que dans kalshi_alpha_bot.py)
log     = logging.getLogger("BOT")
log_rsk = logging.getLogger("RISK")
log_trd = logging.getLogger("TRADE")
log_pos = logging.getLogger("POSITION")

def _client_is_genuine(client) -> bool:
    """Un client authentique est une instance de KalshiClient de CE module,
    avec une base_url Kalshi reelle. Tout double de test (Fake/Mock/patch)
    echoue ce test."""
    return (type(client) is KalshiClient
            and str(getattr(client, "base_url", ""))
            .startswith("https://") and "kalshi.co" in
            str(getattr(client, "base_url", "")))


def assert_real_demo_integrity(client, shadow_mode: bool):
    """Exigence 1 : en EXECUTION_MODE=real_demo, aucun mock, aucun dry-run,
    aucune simulation ne peut remplacer l'appel API reel. Arret FATAL."""
    if CFG.EXECUTION_MODE != "real_demo":
        return
    problems = []
    if not _client_is_genuine(client):
        problems.append(f"client non authentique: {type(client).__name__} "
                        f"({type(client).__module__})")
    if CFG.DRY_RUN:
        problems.append("DRY_RUN=true")
    if shadow_mode:
        problems.append("SHADOW_MODE actif (= simulation)")
    if not CFG.ALLOW_ORDER_SUBMISSION:
        problems.append("ALLOW_ORDER_SUBMISSION=false")
    if problems:
        log.critical("[FATAL] Mock or simulation detected in REAL_DEMO mode: "
                     + "; ".join(problems))
        raise SystemExit(3)


def log_execution_banner(client):
    """Exigence 2 : etat d'execution explicite au demarrage. Les fonds sont
    des fonds DEMO — jamais annonce comme argent reel."""
    genuine = _client_is_genuine(client)
    log.info("[EXECUTION]")
    log.info(f"environment={'DEMO' if client.env == 'demo' else 'PROD'}")
    log.info(f"api_base_url={getattr(client, 'base_url', '?')}")
    log.info(f"execution_mode={CFG.EXECUTION_MODE.upper()}")
    if client.env == "demo" and CFG.EXECUTION_MODE != "real_demo":
        log.info("conseil: definir EXECUTION_MODE=real_demo (Railway) pour "
                 "activer le garde anti-mock ; les chemins d'execution sont "
                 "IDENTIQUES (verifie par test), seule la protection change.")
    log.info(f"dry_run={str(CFG.DRY_RUN or CFG.SHADOW_MODE).lower()}")
    log.info(f"mock_enabled={str(not genuine).lower()}")
    log.info(f"order_submission_enabled="
             f"{str(CFG.ALLOW_ORDER_SUBMISSION and not CFG.SHADOW_MODE).lower()}")
    if client.env == "demo":
        log.info("NOTE: ordres reels sur l'API DEMO — fonds DEMO uniquement, "
                 "aucun argent reel.")

class ExecutionEngine:
    """Cycle normal = PIPELINE INTEGRE :
    scanner -> ranker -> routeur -> portes edge/EV -> risque -> execution
    -> verification fills -> reconciliation. Plus de dependance exclusive
    a KXBTC15M ; parcours multi-candidats ; carnet relu juste avant l'ordre."""

    def __init__(self, client: KalshiClient, capital: float):
        from strategy_router import (GateConfig, build_default_registry,
                                     RegistryValidationError)
        from opportunity_pipeline import MarketOpportunityPipeline
        from market_scanner import MarketScanner
        from shadow_prediction_store import ShadowPredictionStore
        self.client   = client
        self.configured_capital = capital           # PLAFOND, pas la verite
        self.capital  = capital                     # effectif (maj par solde)
        self.tlog     = TradeLogger()
        self.posmgr   = PositionManager(client, self.tlog)
        self.orders   = OrderManager(client)
        self.risk     = RiskManager(self.tlog, self.posmgr, capital)
        self.stats    = StatsEngine(self.tlog)
        self.strategy = BtcStrategy(client)          # analyse crypto existante
        # REGISTRE CANONIQUE indexe par market_type (correctif cause racine :
        # la v1 n'enregistrait que la serie KXBTC15M, absente de l'univers,
        # d'ou strategy_supported=0 et no_compatible_strategy partout).
        # ECHEC DE DEMARRAGE si le registre est vide ou incomplet.
        btc_enabled = _env_b("BTC_STRATEGY_ENABLED", default=True)
        try:
            self.router = build_default_registry(
                btc_context_provider=(get_btc_context if BTC_AVAILABLE
                                      else None),
                btc_enabled=btc_enabled)
        except RegistryValidationError as e:
            log.error(f"REGISTRE DE STRATEGIES INVALIDE: {e} -- ARRET.")
            raise SystemExit(2)
        self.scanner = MarketScanner(client, router=self.router,
                                     data_dir=CFG.DATA_DIR)
        self.shadow_store = ShadowPredictionStore(_p("shadow_predictions.json"))
        self.gates = GateConfig(
            MIN_MODEL_CONFIDENCE=CFG.MIN_MODEL_CONFIDENCE,
            MIN_GROSS_EDGE=CFG.MIN_GROSS_EDGE, MIN_NET_EDGE=CFG.MIN_NET_EDGE,
            MIN_NET_EV=CFG.MIN_NET_EV,
            MAX_ACCEPTABLE_SPREAD=CFG.MAX_ACCEPTABLE_SPREAD,
            MIN_MARKET_SCORE=CFG.MIN_MARKET_SCORE,
            MIN_FILL_PROXY=CFG.MIN_FILL_PROXY,
            SLIPPAGE_BUFFER_CENTS=CFG.SLIPPAGE_BUFFER_CENTS,
            FEE_RATE=CFG.FEE_RATE)
        self.pipeline = MarketOpportunityPipeline(
            client, self.router, gates=self.gates,
            fresh_book_fn=self.fresh_book,
            observer=self._shadow_observer,
            scanner=self.scanner, data_dir=CFG.DATA_DIR)
        self.tracer = DecisionTracer(log)
        HEALTH.set_client(client)
        assert_real_demo_integrity(client, CFG.SHADOW_MODE)
        log_execution_banner(client)
        self._probability_engine_report()
        # Recovery apres crash + broker source de verite
        self.orders.reconcile_startup(self.tlog, self.posmgr)
        self.posmgr.reconcile_startup()
        self.posmgr.reconcile_with_broker()
        # ── P8 : parallelisme solde+sante vs scan (desactive par defaut).
        # Executor paresseux a l'usage : aucun thread tant qu'un cycle
        # parallele n'est execute.
        self._parallel_enabled = bool(CFG.API_PARALLEL_ENABLED)
        self._executor = None
        if self._parallel_enabled:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, CFG.API_PARALLEL_WORKERS),
                thread_name_prefix="atlas-p8")

    def fresh_book(self, ticker: str):
        """Relecture du carnet JUSTE avant decision puis avant ordre."""
        m = self.client.get_market(ticker) or {}
        return m, MarketValidator.normalize_book(m)

    def _shadow_observer(self, snapshot, book, dec):
        """Journalise CHAQUE candidat BTC evalue (accepte ou rejete) dans le
        shadow store — base de la calibration et du backtest."""
        try:
            if not (dec.strategy or "").startswith("btc15m"):
                return
            mo = getattr(dec, "model_output", None) or {}
            feats = mo.get("features", {})
            self.shadow_store.record(
                ticker=dec.ticker, cycle_ts_iso=now_iso(),
                market=snapshot.raw_market or {},
                strike=feats.get("strike"), spot=feats.get("spot"),
                minutes_remaining=snapshot.minutes_remaining,
                yes_bid=(book or {}).get("yes_bid"),
                yes_ask=(book or {}).get("yes_ask"),
                no_bid=(book or {}).get("no_bid"),
                no_ask=(book or {}).get("no_ask"),
                spread=(book or {}).get("spread"),
                ranker_score=getattr(getattr(snapshot, "quality", None),
                                     "total_score", None),
                features=feats,
                probability_yes=mo.get("probability_yes"),
                probability_no=mo.get("probability_no"),
                confidence=mo.get("confidence"),
                estimated_fee=dec.estimated_fees,
                estimated_slippage=dec.expected_slippage,
                gross_edge=dec.gross_edge, net_edge=dec.net_edge,
                net_ev=dec.net_ev,
                shadow_decision=(dec.side if dec.accepted else "none"),
                decision_reason=(dec.rejection_reason or "accepted"))
        except Exception as e:
            log.warning(f"[SHADOW] enregistrement: {e}")

    def _balance_gate(self, bal: Optional[float] = None):
        """Solde reel a chaque cycle. effective_capital = min(plafond,
        solde broker). Prod sans solde = blocage ; demo : secours possible
        via ALLOW_FALLBACK_CAPITAL=1, clairement journalise.
        P8 : `bal` peut etre le solde PRE-FETCHE par le thread parallele
        (aucun second appel HTTP dans ce cas)."""
        if bal is None:
            bal = self.client.get_balance()
        self.last_balance = bal
        if bal is not None:
            self.capital = min(self.configured_capital, bal) \
                if self.configured_capital else bal
            self.risk.capital = self.capital
            return True, f"solde={bal:.2f}$ capital_effectif={self.capital:.2f}$"
        if self.client.env != "demo":
            return False, "solde broker INDISPONIBLE en production -- aucun trade"
        if CFG.ALLOW_FALLBACK_CAPITAL:
            log_rsk.warning(f"DEMO: solde indisponible, capital de secours "
                            f"{self.configured_capital:.2f}$ (ALLOW_FALLBACK_CAPITAL=1)")
            self.capital = self.configured_capital
            return True, "capital de secours (demo, journalise)"
        return False, ("solde indisponible; en demo, ALLOW_FALLBACK_CAPITAL=1 "
                       "requis pour un secours explicite")

    def _background_balance_health(self):
        """Fetch du solde + checks de sante, execute dans le thread P8 en
        PARALLELE du scan. Ne leve JAMAIS : un solde indisponible est un
        simple None (traite par la porte de solde comme en sequentiel)."""
        bal = self.client.get_balance()
        try:
            health = HEALTH.run_all()
        except Exception as e:                              # noqa: BLE001
            log.debug(f"health run_all: {e}")
            health = None
        self._last_health = health
        return bal, health

    def _post_balance_gates(self) -> bool:
        """Portes de risque globales APRES le solde (capital effectif a
        jour). Retourne False si le cycle doit s'arreter (le code retour
        du chemin sequentiel est strictement identique)."""
        ok, why = self.risk.can_trade(cycle_trades=0)
        if not ok:
            log_rsk.warning(f"Trading bloque: {why}",
                            extra={"event": "trading_blocked", "reason": why})
            return False
        if self.posmgr.open_count() >= CFG.MAX_OPEN_POSITIONS:
            log_rsk.warning(f"MAX_OPEN_POSITIONS={CFG.MAX_OPEN_POSITIONS} atteint.",
                            extra={"event": "max_open_positions",
                                   "open": self.posmgr.open_count(),
                                   "limit": CFG.MAX_OPEN_POSITIONS})
            return False
        dd_pct = self.risk.rolling_drawdown_pct()
        if dd_pct >= CFG.MAX_EQUITY_DRAWDOWN_PCT:
            log_rsk.warning(
                f"Drawdown {dd_pct:.1f}% "
                f"({self.risk.rolling_drawdown():.2f}$) >= "
                f"{CFG.MAX_EQUITY_DRAWDOWN_PCT:g}% -- trading coupe.",
                extra={"event": "drawdown_limit",
                       "drawdown_pct": dd_pct,
                       "drawdown_amount": self.risk.rolling_drawdown(),
                       "limit_pct": CFG.MAX_EQUITY_DRAWDOWN_PCT})
            return False
        return True

    def _probability_engine_report(self):
        """Audit du Probability Engine au demarrage : pour chaque strategie,
        sa source de probabilite ; puis une sonde REELLE du fournisseur BTC
        (valid/reason/spot/qualite/sources) pour reveler immediatement dans
        les logs Railway pourquoi un modele ne produirait rien.
        PROBE_PROVIDERS_ON_START=0 pour desactiver (tests hors-ligne)."""
        log.info("[PROBABILITY_ENGINE]")
        for mt, st in sorted(self.router._by_type.items()):
            src = "OUI" if st.has_probability_source() else "NON"
            log.info(f"  {mt:24} strategie={st.name:24} "
                     f"source_probabilite={src} ({st.provider_desc()})")
        if os.getenv("PROBE_PROVIDERS_ON_START", "1") != "1":
            return
        if not BTC_AVAILABLE:
            log.warning("  sonde BTC: btc_context ABSENT (module non "
                        "importable) -> tout candidat crypto sera rejete "
                        "no_model_probability:btc_context_absent")
            return
        try:
            ctx = get_btc_context()
            n_src = getattr(ctx, "n_valid_sources", "?")
            log.info(f"  sonde BTC: valid={getattr(ctx, 'valid', False)} "
                     f"reason='{getattr(ctx, 'reason', '')}' "
                     f"spot={getattr(ctx, 'spot', None)} "
                     f"sources_valides={n_src} "
                     f"qualite={getattr(ctx, 'data_quality_score', None)} "
                     f"vol_1m={getattr(ctx, 'realized_vol_1m', None)} "
                     f"flags={getattr(ctx, 'quality_flags', [])}")
            if not getattr(ctx, "valid", False):
                log.warning("  sonde BTC INVALIDE: chaque marche BTC sera "
                            "rejete no_model_probability tant que le "
                            "fournisseur de donnees ne repond pas — "
                            "verifier l'acces sortant de Railway vers "
                            "les APIs spot (Coinbase/Kraken/Bitstamp) et "
                            "klines (Binance).")
        except Exception as e:                              # noqa: BLE001
            log.warning(f"  sonde BTC: EXCEPTION {type(e).__name__}: {e}")

    def cycle(self, n: int) -> int:
        """Run one iteration inside a fresh end-to-end decision trace."""
        with self.tracer.trace_run() as trace:
            trace.event("RUN_STARTED", cycle=n, market_count=0)
            try:
                with timed("full_cycle"):
                    result = self._cycle(n)
                HEALTH.record_run(ok=True, run_id=trace.run_id, cycle=n)
                return result
            except Exception as e:                        # noqa: BLE001
                HEALTH.record_run(ok=False, run_id=trace.run_id, cycle=n,
                                  error=f"{type(e).__name__}: {e}")
                raise
            finally:
                trace.event("RUN_ENDED", cycle=n)
                # run_id injecte par la record factory (P4.2) — pas via extra.
                log.info("[DECISION_TRACE] run complete",
                         extra={"event_type": "RUN_ENDED",
                                "cycle": n, "market_count": getattr(self, "_trace_market_count", 0),
                                "duration_ms": trace.elapsed_ms(),
                                "timestamp_ms": trace.elapsed_ms()})

    def _cycle(self, n: int) -> int:
        log.info(f"── CYCLE #{n} ─────────────────────────────────────────────")
        self.stats.maybe_daily_report()

        # P8 : caches par cycle — purge AVANT tout fetch (aucune donnee
        # d'un cycle precedent ne peut servir au cycle courant).
        if getattr(self.client, "cache_enabled", False):
            self.client.clear_caches()
        if CFG.BTC_CONTEXT_CYCLE_CACHE:
            try:
                from btc_context import begin_cycle
                begin_cycle()
            except ImportError:
                pass

        # 0) Reglement des predictions shadow (journal de recherche)
        try:
            n_shadow = self.shadow_store.settle_pending(self.client.get_market)
            self.shadow_settled_total = len(self.shadow_store.settled())
            if n_shadow:
                log.info(f"[SHADOW] {n_shadow} prediction(s) reglee(s) "
                         f"(total regle: {len(self.shadow_store.settled())})")
        except Exception as e:
            log.warning(f"[SHADOW] reglement: {e}")

        # 1) Reglements d'abord : le PnL realise conditionne les portes
        for _t in self.posmgr.check_settlements():
            log_rsk.info(f"PnL jour realise: {self.risk.daily_realized_pnl():+.2f}$ "
                         f"/ limite -{CFG.MAX_DAILY_LOSS:.2f}$")

        if self._parallel_enabled:
            return self._cycle_parallel(n)
        return self._cycle_sequential(n)

    def _cycle_sequential(self, n: int) -> int:
        """Chemin par defaut (P8 desactive) : kill switch, solde, portes de
        risque globales, PUIS scan — ordre historique inchange."""
        # 2) Kill switch (seule porte qui ne depend pas du capital effectif)
        #    et 3-4) portes de risque globales — mesurees ensemble : ce sont
        #    les controles risque du cycle (P4.4).
        with timed("risk_check"):
            if CFG.KILL_SWITCH:
                log_rsk.warning("KILL_SWITCH actif -- aucun ordre ce cycle.",
                                extra={"event": "kill_switch"})
                return 0

            # 3) Solde reel du broker -- CORRECTIF AUDIT : deplace AVANT les
            # portes de risque (stop %, drawdown %, budget ouvert). Avant ce
            # correctif, can_trade()/rolling_drawdown_pct() lisaient
            # self.risk.capital, qui n'etait mis a jour QU'APRES ces controles
            # (par le present appel) : les portes utilisaient donc le capital
            # effectif du cycle PRECEDENT, pas le solde courant. En cas de
            # variation de solde (depot/retrait/PnL importants), les limites
            # de risque en % pouvaient etre evaluees sur un capital perime.
            ok, why = self._balance_gate()
            log_rsk.info(f"[CAPITAL] {why}")
            if not ok:
                return 0

            # 4) Portes de risque globales (dependent desormais du capital a jour)
            if not self._post_balance_gates():
                self.stats.log_summary(); return 0

        # 5) PIPELINE integre (multi-candidats, jamais bloque sur un ticker)
        res = self.pipeline.run_cycle(
            max_accepted=CFG.MAX_TRADES_CYCLE,
            skip_ticker_fn=(lambda tk: CFG.ONE_TRADE_PER_MKT and
                            (tk in self.posmgr.tickers_open()
                             or self.tlog.has_open_on(tk))))
        return self._finish_cycle(n, res)

    def _cycle_parallel(self, n: int) -> int:
        """P8 : solde + checks de sante executes en PARALLELE du scan.
        Le scan reste sur le thread principal (tracing/timing intacts) ;
        le fetch du solde et la sonde de sante tournent en arriere-plan
        (ThreadPoolExecutor, stdlib) et sont joints avant les portes de
        risque. Resultats strictement identiques au sequentiel : solde et
        scan sont independants (le scan ne lit ni n'ecrit self.capital /
        self.risk — portes inchangees, executees apres la jointure)."""
        # 2) Kill switch d'abord : aucun scan lance inutilement.
        if CFG.KILL_SWITCH:
            log_rsk.warning("KILL_SWITCH actif -- aucun ordre ce cycle.",
                            extra={"event": "kill_switch"})
            return 0
        bal_future = self._executor.submit(self._background_balance_health)
        try:
            # 5) PIPELINE integre — chevauche le fetch du solde.
            res = self.pipeline.run_cycle(
                max_accepted=CFG.MAX_TRADES_CYCLE,
                skip_ticker_fn=(lambda tk: CFG.ONE_TRADE_PER_MKT and
                                (tk in self.posmgr.tickers_open()
                                 or self.tlog.has_open_on(tk))))
        except Exception:
            bal_future.result()          # join avant propagation (thread ok)
            raise
        bal, _health = bal_future.result()
        # 3-4) Portes de risque globales (solde pre-fetche : aucun 2e appel)
        with timed("risk_check"):
            ok, why = self._balance_gate(bal)
            log_rsk.info(f"[CAPITAL] {why}")
            if not ok:
                return 0
            if not self._post_balance_gates():
                self.stats.log_summary(); return 0
        return self._finish_cycle(n, res)

    def _finish_cycle(self, n: int, res: dict) -> int:
        """Execution des candidats acceptes + rapports de fin de cycle
        (partage sequentiel/parallele)."""
        report = res["report"]
        placed = 0
        for dec in res["accepted"]:
            if placed >= CFG.MAX_TRADES_CYCLE:
                break
            placed += self._execute_decision(dec, report)
        report["fills"] = placed
        # Tache 8 : pourcentages de conversion recalcules sur les compteurs
        # FINAUX (risk/ordres/fills sont incrementes pendant l'execution).
        conv = report.get("funnel_conversion") or {}
        prev = None
        for name in ("scanned_raw", "open_cached", "liquid", "supported",
                     "model_evaluated", "positive_edge", "positive_net_ev",
                     "risk_passed", "orders_submitted", "fills"):
            n = int(report.get(name) or 0)
            conv[name] = {"n": n,
                          "pct_of_prev": round(100.0 * n / prev, 2)
                          if prev else (100.0 if n else 0.0)}
            prev = n if n else prev
        report["funnel_conversion"] = conv
        report["fills_confirmed"] = placed
        report["orders"] = report.get("orders_submitted", 0)
        # UN SEUL resume structure par cycle (exigence G). Les details par
        # decision sont dans decisions.jsonl (rotatif), pas ici.
        summary = {k: report.get(k) for k in
                   ("cycle_id", "scanned_raw", "open_cached", "liquid",
                    "supported", "model_evaluated", "positive_edge",
                    "positive_net_ev", "risk_passed", "orders_submitted",
                    "fills", "rejections_by_reason", "cycle_duration_ms")}
        summary["cycle"] = n
        import json as _json
        log.info(f"[CYCLE-SUMMARY] {_json.dumps(summary, ensure_ascii=False)}")
        JsonStore.save(_p("cycle_report.json"), {"cycle": n, **report})
        # Etat dashboard : UNIQUEMENT des donnees reelles du cycle (les
        # champs non disponibles restent absents -- l'UI affiche « — »).
        try:
            cands = []
            for d in (res.get("accepted") or []):
                cands.append({
                    "ticker": getattr(d, "ticker", None),
                    "strategy": getattr(d, "strategy", None),
                    "side": getattr(d, "side", None),
                    "model_probability": getattr(d, "model_probability", None),
                    "market_probability": getattr(d, "market_probability", None),
                    "edge_net": getattr(d, "edge_net", None),
                    "ev_net": getattr(d, "ev_net", None),
                    "confidence": getattr(d, "confidence", None),
                    "status": "submitted" if placed else "evalue",
                })
            JsonStore.save(_p("dashboard_state.json"), {
                "ts": now_iso(), "version": ENGINE_VERSION,
                "env": getattr(self.client, "env", "demo"),
                "cycle": n, "balance": getattr(self, "last_balance", None),
                "capital": self.capital,
                "configured_capital": self.configured_capital,
                "shadow_settled": getattr(self, "shadow_settled_total", None),
                "exchange_paused": time.time() <
                getattr(self.orders, "exchange_pause_until", 0.0),
                "candidates": cands,
            })
        except Exception as e:
            log.warning(f"dashboard_state: {e}")
        JsonStore.save(_p("pipeline_stats.json"), {
            "cycle": n,
            "scanned": report["scanned"],
            "valid": report.get("scanner_included"),
            "eligible": report["ranker_eligible"],
            "strategy_supported": report.get("strategy_supported"),
            "model_probability": report.get("model_probability"),
            "positive_edge": report.get("positive_edge"),
            "positive_net_ev": report.get("positive_net_ev"),
            "risk_passed": report.get("risk_passed", 0),
            "accepted": report["accepted"],
            "orders": report.get("orders_submitted", 0),
        })
        JsonStore.save(_p("reject_reasons.json"),
                       {"cycle": n, "reject_reasons": report["rejections"]})
        if placed == 0:
            self.stats.log_summary()
        return placed

    def _execute_decision(self, dec, report) -> int:
        ticker = dec.ticker
        # 5a) carnet FRAIS une DERNIERE fois, juste avant l'ordre (TEST L)
        with timed("api_fetch"):
            m, book = self.fresh_book(ticker)
        if not book:
            log.info(f"CARNET DISPARU avant execution sur {ticker} -- annule.")
            report["rejections"]["stale_book"] = \
                report["rejections"].get("stale_book", 0) + 1
            return 0
        with timed("signal_check"):
            ask = book.get("yes_ask") if dec.side == "yes" else book.get("no_ask")
            if ask is None or not (1 <= int(ask) <= 99):
                report["rejections"]["no_executable_ask"] = \
                    report["rejections"].get("no_executable_ask", 0) + 1
                return 0
            entry = int(ask)

        # 5b) budgets risque categorie / marche + taille (sur capital effectif)
        with timed("risk_check"):
            cat = getattr(dec, "category", None) or "Other"
            # Emergency stop is checked before any sizing; concentration is
            # checked again below with the actual proposed allocation.
            ok, why = self.risk.portfolio_check(ticker, cat, 0.0)
            if not ok:
                report["rejections"]["portfolio_risk"] = report["rejections"].get("portfolio_risk", 0) + 1
                log_rsk.info(f"[REJECT] {ticker}: {why}")
                return 0
            cat_risk = self.posmgr.open_risk_by_category().get(cat, 0.0)
            if cat_risk >= self.capital * CFG.MAX_CATEGORY_RISK_PCT / 100.0:
                report["rejections"]["category_budget"] = \
                    report["rejections"].get("category_budget", 0) + 1
                return 0
            if self.posmgr.open_risk_on(ticker) >= \
                    self.capital * CFG.MAX_SINGLE_MARKET_RISK_PCT / 100.0:
                report["rejections"]["risk_blocked"] = \
                    report["rejections"].get("risk_blocked", 0) + 1
                return 0

            # 5c) taille sur capital EFFECTIF (solde reel plafonne) (TEST K)
            count = PositionSizer.contracts(
                self.capital, entry, dec.taille, dec.confidence,
                self.risk.rolling_drawdown(), self.posmgr.open_risk(),
                probability=getattr(dec, "model_probability", None),
                side=getattr(dec, "side", "yes"))
            count = int(count * self.risk.drawdown_size_factor())
            proposed_risk = count * entry / 100.0
            ok, why = self.risk.portfolio_check(ticker, cat, proposed_risk)
            if not ok:
                report["rejections"]["portfolio_risk"] = report["rejections"].get("portfolio_risk", 0) + 1
                log_rsk.info(f"[REJECT] {ticker}: {why}")
                return 0
            if count <= 0:
                log_rsk.info(f"[REJECT] {ticker}: risk_blocked (taille=0)")
                report["rejections"]["risk_blocked"] = \
                    report["rejections"].get("risk_blocked", 0) + 1
                return 0
            report["risk_passed"] = report.get("risk_passed", 0) + 1
            log_rsk.info(f"[RISK] {ticker}: portes de risque PASSEES "
                         f"(taille={count}, capital={self.capital:.2f}$)",
                         extra={"ticker": ticker, "size": count,
                                "capital": self.capital})

        est_fee_total = FeeModel.trading_fee(count, entry)
        log_trd.info(f"[SIGNAL VALIDE] {ticker} {dec.side.upper()} x{count} "
                     f"@ {entry}c | modele={dec.model_probability:.1%} "
                     f"marche={dec.market_probability:.1%} "
                     f"edge_net={dec.net_edge:+.3f} ev_net={dec.net_ev:+.3f} "
                     f"strat={dec.strategy}",
                     extra={"ticker": ticker, "side": dec.side, "size": count,
                            "price": entry,
                            "model_probability": dec.model_probability,
                            "market_probability": dec.market_probability,
                            "edge_net": dec.net_edge, "ev_net": dec.net_ev,
                            "strategy": dec.strategy})

        # 5d) SHADOW : decision complete journalisee, AUCUN ordre
        if CFG.SHADOW_MODE:
            log_trd.info("[SHADOW] ordre NON envoye (mode shadow).")
            report["rejections"]["shadow_mode"] = \
                report["rejections"].get("shadow_mode", 0) + 1
            return 0

        # Circuit breaker demi-ouvert : la reservation se fait au dernier
        # moment, apres toutes les autres portes, juste avant la soumission.
        # Ainsi un cycle sans candidat ne consomme jamais l'unique essai.
        claimed, claim_reason = self.risk.claim_half_open_attempt(ticker)
        if not claimed:
            log_rsk.warning(f"[REJECT] {ticker}: {claim_reason}")
            report["rejections"]["half_open_already_claimed"] = (
                report["rejections"].get("half_open_already_claimed", 0) + 1)
            return 0

        report["orders_submitted"] = report.get("orders_submitted", 0) + 1
        log_trd.info(f"[EXECUTION] {ticker} {dec.side.upper()} x{count} "
                     f"@ {entry}c -> envoi de l'ordre",
                     extra={"ticker": ticker, "side": dec.side, "size": count,
                            "price": entry, "edge": dec.net_edge})
        with timed("order_placement"):
            exec_res = self.orders.place_and_track(ticker, dec.side, count,
                                                   entry)
        from decision_tracer import current_tracer
        tracer = current_tracer()
        if tracer:
            tracer.market(ticker, "EXECUTED", edge=dec.net_edge, price=entry,
                          reason="order_submitted")
        if exec_res.filled <= 0:
            # Ne pas consumer l'unique essai si aucune soumission n'a ete
            # acceptee, ou si l'annulation sans fill est explicitement confirmee.
            # Un order_id avec etat incertain reste verrouille par prudence.
            if exec_res.order_id is None:
                self.risk.release_half_open_attempt(ticker,
                    f"soumission non acceptee: {exec_res.status}")
            elif exec_res.state == "cancelled" and exec_res.status not in ("unverified", "unknown"):
                self.risk.release_half_open_attempt(ticker,
                    f"ordre confirme sans fill: {exec_res.status}")
            log_trd.warning(f"NON EXECUTE ({exec_res.state}: {exec_res.status}) "
                            f"-- AUCUN trade enregistre.",
                            extra={"ticker": ticker, "side": dec.side,
                                   "size": count, "price": entry,
                                   "order_state": exec_res.state,
                                   "order_status": exec_res.status,
                                   "filled": exec_res.filled})
            return 0

        # 5e) frais REELS d'abord (reponse d'ordre puis fills) (TEST M)
        try:
            fills = self.client.get_fills(exec_res.order_id) \
                if exec_res.order_id else []
        except KalshiAPIError:
            fills = []
        fee_amt, fee_src = FeeModel.from_api({}, fills,
                                             exec_res.filled, exec_res.avg_price)
        trade = self.tlog.open_trade(
            ticker=ticker, market_title=m.get("title", ""),
            side=dec.side, req_price=entry,
            avg_price=exec_res.avg_price, req_count=count,
            filled_count=exec_res.filled, spread=book["spread"], fees=fee_amt,
            edge=dec.net_edge, ev=dec.net_ev, confidence=dec.confidence,
            grade="B", reason=dec.reason,
            analysis={"market_prob": dec.market_probability,
                      "model_prob": dec.model_probability,
                      "gross_edge": dec.gross_edge,
                      "fee_source": fee_src,
                      "estimated_fee_before_order": est_fee_total,
                      "actual_fee_after_fill": fee_amt,
                      "strategy": dec.strategy},
            order_id=exec_res.order_id, order_status=exec_res.status,
            decision_id=dec.decision_id)
        self.posmgr.open_position(trade, extra={
            "strategy": dec.strategy, "category": cat, "market_score": None,
            "entry_edge": dec.net_edge, "entry_ev": dec.net_ev,
            "fill_ids": [f.get("fill_id") or f.get("id")
                         for f in fills if f.get("fill_id") or f.get("id")]})
        # Exigence 7 : la position n'est declaree OUVERTE qu'apres avoir ete
        # RETROUVEE dans /portfolio/positions. Sinon on le dit honnetement.
        try:
            brk = self.client.get_positions()
        except KalshiAPIError as e:
            brk = None
            log_pos.warning(f"[POSITION_VERIFY] lecture impossible: {e}")
        found = None
        for p in (brk or []):
            if str(pick(p, "ticker", "market_ticker", default="")) == dec.ticker:
                found = p
                break
        if found is not None:
            log_pos.info("[POSITION_VERIFY] "
                         f"ticker={dec.ticker} position_found=true "
                         f"net_position={pick_int(found, 'position', 'total_traded', default=exec_res.filled)} "
                         f"market_exposure={found.get('market_exposure', '-')} "
                         f"realized_pnl={found.get('realized_pnl', '-')} "
                         f"fees_paid={found.get('fees_paid', '-')}")
            log_pos.info(f"[POSITION_OPENED] {dec.ticker} confirme par l'API",
                         extra={"ticker": dec.ticker})
        elif brk is not None:
            log_pos.warning("[POSITION_VERIFY] "
                            f"ticker={dec.ticker} position_found=false — "
                            "fill confirme par /fills mais position pas "
                            "encore visible dans /positions (position "
                            "enregistree localement, statut POSITION_OPENED "
                            "NON emis)")
        snap = self.risk.snapshot()
        log_rsk.info(f"risque_ouvert={snap['open_risk']}$ "
                     f"pnl_jour={snap['daily_realized_pnl']}$ "
                     f"frais_cumules={snap['fees_paid']}$ (source={fee_src})")
        return 1
