"""
ExecutionEngine — Moteur d'execution principal (orchestrateur du pipeline integre).
Extrait de kalshi_alpha_bot.py (P3.15).
"""
import logging
import os
import time
from typing import Optional

from btc_strategy import BtcStrategy, BTC_AVAILABLE, get_btc_context
import account_binding
from equity_ledger import EquityLedger, ACCOUNTING_GUARDS
from config import (CFG, prod_is_read_only, GATE_PARSE_WARNINGS, _env_b, _p, contract_cap_config,
                    daily_oracle_approved, daily_quarantine_blocks,
                    ticker_is_wellformed)
from fee_model import FeeModel
from kalshi_client import KalshiAPIError, KalshiClient, pick, pick_int
from market_validator import MarketValidator
from order_manager import OrderManager
from persistence import JsonStore, PersistenceSentinel
from position_manager import PositionManager
from research_feed import ResearchFeed
from position_sizer import PositionSizer
from risk_manager import RiskManager
from stats_engine import StatsEngine
from trade_logger import TradeLogger, now_iso
from decision_tracer import DecisionTracer
from health_monitor import HEALTH
from timing import timed

ENGINE_VERSION = "v11.4-audit-fixed-2026-07-28"


def _json_shadow(counters: dict) -> str:
    """Compact one-line render of the T7-K shadow counters (logging only)."""
    import json as _j
    return _j.dumps(counters, ensure_ascii=False, sort_keys=True)

#: market_type du BTC quotidien (serie KXBTCD).
DAILY_MARKET_TYPE = "btc_above_strike_daily"

#: LISTE BLANCHE des market_type autorises a atteindre le chemin argent.
#: Une liste blanche, pas une liste noire : `Decision.market_type` vaut None
#: par defaut, donc une Decision mal formee, un alias de strategie futur ou
#: un market_type "unknown" traverseraient une liste noire sans etre vus.
#: `btc_above_strike_daily` en est volontairement ABSENT tant que l'oracle de
#: reglement quotidien n'est pas approuve (voir daily_oracle_approved).
#: Tout le reste conserve exactement son comportement : chaque type ici
#: reste soumis a toutes les portes globales existantes.
EXECUTABLE_MARKET_TYPES = frozenset({
    "btc_15m_above_strike",
    "sports_moneyline", "sports_spread", "sports_total",
    "election_winner",
})


def executable_market_types() -> frozenset:
    """La liste blanche effective. `btc_above_strike_daily` n'y entre QUE si
    l'oracle de reglement quotidien est approuve, pour qu'il n'existe qu'UN
    seul interrupteur : approuver l'oracle suffit, et editer la liste sans
    approuver l'oracle ne suffit pas (la garde quotidienne refuse d'abord).
    """
    if daily_oracle_approved():
        return EXECUTABLE_MARKET_TYPES | {DAILY_MARKET_TYPE}
    return EXECUTABLE_MARKET_TYPES

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


#: P0 observability. `RiskManager.can_trade()` returns one prose string for
#: three different guards. Atlas needs a stable identifier, so the prose is
#: mapped onto one — and onto "risk_can_trade_unclassified" when the wording
#: matches none of them, because guessing would be worse than admitting it.
def _classify_can_trade_guard(why: str) -> str:
    w = str(why or "")
    if "STOP JOURNALIER" in w:
        return "daily_loss_stop"
    if "pertes consecutives" in w or "demi-ouvert" in w:
        return "consecutive_loss_breaker"
    if "trades/cycle" in w:
        return "max_trades_cycle"
    if "budget de risque" in w:
        return "open_risk_budget"
    return "risk_can_trade_unclassified"


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
    log.info(f"daily_execution_enabled={str(daily_oracle_approved()).lower()}")
    log.info(f"executable_market_types="
             f"{','.join(sorted(executable_market_types()))}")
    # Une porte fermee par malentendu doit etre visible au demarrage.
    for _name, _raw in GATE_PARSE_WARNINGS:
        log.error(f"[CONFIG_GATE_INVALID] {_name}={_raw!r} illisible -- "
                  f"lu comme FALSE (fail-closed).")
    if client.env == "demo":
        log.info("NOTE: ordres reels sur l'API DEMO — fonds DEMO uniquement, "
                 "aucun argent reel.")

#: The ONE global guard that PRODUCTION READ_ONLY observation may continue
#: through. Deliberately a single name, not a set: `equity_drawdown` is a
#: CAPITAL guard (it protects money that READ_ONLY cannot move), so stopping
#: the scanner on it destroys model evidence without adding money-path
#: safety. Every other guard, in every mode, and this guard in CAPITAL and
#: DEMO, keep their fail-closed verdict.
OBSERVATION_ONLY_GUARD = "equity_drawdown"
#: F2: the four accounting guards are CAPITAL guards of the same nature
#: (they protect money READ_ONLY cannot move) and are observed through the
#: same way, each recorded by name as `would_block_capital`. This is the
#: complete allow-list; every other guard stops the scan in every mode.
OBSERVATION_ONLY_GUARDS = frozenset((OBSERVATION_ONLY_GUARD, *ACCOUNTING_GUARDS))


def _broker_open_order_ids(client, orders):
    """(ids, error). A read that fails yields (None, reason): unknown is
    never an empty set."""
    from order_manager import OrderManager
    terminal = {str(s).lower() for s in getattr(orders, "TERMINAL", OrderManager.TERMINAL)}
    try:
        rows = client.list_orders()
    except Exception as e:                                # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    ids = []
    for r in rows or []:
        status = str((r or {}).get("status") or "").lower()
        remaining = int((r or {}).get("remaining_count") or 0)
        if status not in terminal or remaining > 0:
            ids.append(str(r.get("order_id") or r.get("id") or "?"))
    return sorted(ids), None


def equity_rebase_context(client, orders, posmgr, risk, equity=None,
                          quiescent: bool = True) -> dict:
    """The authoritative context for an equity rebase (F2 §6, audit A03).

    Orders come from the persisted OrderManager state (`open_orders`,
    `pending_intents`, `resolution_halt`) AND a fresh read-only broker
    listing; positions from the persisted PositionManager state AND a fresh
    broker verification. A query that fails leaves the broker side `None`
    (unknown), which the ledger treats as a refusal; a disagreement between
    local and broker order sets is a refusal too. Nothing here writes to the
    broker.

    Two GETs are not a transaction. Astra demonstrated an order appearing
    between the order query and the position query, invisible to both. The
    broker listing is therefore read TWICE, bracketing the position read: an
    exposure that appears anywhere inside the bracket shows up in the second
    listing and marks the evidence unstable. That does not make the pair
    atomic -- the broker offers no such primitive here -- and it is not
    claimed to: the bracket narrows the window to zero *observed* orders on
    both sides of the position read, and anything else refuses. The local
    files are fingerprinted on both sides of the whole collection for the
    same reason, and re-checked by the ledger at commit.
    """
    bound_before = equity.bound_state() if equity is not None else None
    local_open = sorted(str(k) for k in (getattr(orders, "open_orders", None) or {}))
    pending = sorted(str(k) for k in (getattr(orders, "pending_intents", None) or {}))
    halt = getattr(orders, "resolution_halt", None)
    broker_ids, broker_error = _broker_open_order_ids(client, orders)
    reconcile, reconcile_detail = "UNKNOWN", None
    verify = getattr(posmgr, "verify_against_broker", None)
    if callable(verify):
        try:
            report = verify() or {}
            reconcile = str(report.get("status") or "UNKNOWN")
            reconcile_detail = report.get("detail")
        except Exception as e:                            # noqa: BLE001
            reconcile = f"UNKNOWN ({type(e).__name__})"
    elif getattr(posmgr, "reconcile_halt", None) is None:
        reconcile = "MATCH"
    broker_ids_after, broker_error_after = _broker_open_order_ids(client, orders)
    unstable = None
    if broker_ids is None or broker_ids_after is None:
        unstable = (f"broker order listing unreadable "
                    f"({broker_error or broker_error_after})")
    elif broker_ids != broker_ids_after:
        unstable = (f"the broker order set changed across the position read: "
                    f"{broker_ids} -> {broker_ids_after}")
    local_after = sorted(str(k) for k in (getattr(orders, "open_orders", None) or {}))
    pending_after = sorted(str(k) for k in (getattr(orders, "pending_intents", None) or {}))
    if (local_after, pending_after) != (local_open, pending):
        unstable = "local order/intent state changed during collection"
    if equity is not None:
        drift = equity._bound_state_drift(bound_before)
        if drift:
            unstable = f"local evidence files changed during collection: {drift}"
    drawdown_firing = False
    try:
        drawdown_firing = float(risk.rolling_drawdown_pct()) >= CFG.MAX_EQUITY_DRAWDOWN_PCT
    except Exception:                                     # noqa: BLE001
        pass
    broker_open = None if broker_ids is None else len(broker_ids)
    disagreement = broker_ids is not None and set(local_open) != set(broker_ids)
    ctx = {"drawdown_firing": drawdown_firing,
           "reconcile_status": reconcile,
           "reconcile_detail": reconcile_detail,
           "open_positions": int(posmgr.open_count()),
           "in_flight_orders": len(local_open) + len(pending) + (broker_open or 0),
           "quiescent": bool(quiescent),
           "evidence_unstable": unstable,
           "bound_state": bound_before,
           "orders": {"local_open": local_open, "pending_intents": pending,
                      "resolution_halt": bool(halt), "broker_open": broker_open,
                      "broker_open_ids": broker_ids or [],
                      "broker_error": broker_error or broker_error_after,
                      "disagreement": disagreement}}
    if equity is not None:
        ctx["revalidate"] = lambda: equity_rebase_context(
            client, orders, posmgr, risk, equity=equity, quiescent=quiescent)
    return ctx


def _equity_of(engine):
    """The engine's EquityLedger, or None. Module-level so that harnesses
    which borrow engine methods onto stub objects keep working."""
    return getattr(engine, "equity", None)


def _equity_status_of(engine):
    equity = _equity_of(engine)
    return equity.derive_status() if equity is not None else None


def _equity_dashboard_fields(engine) -> dict:
    equity = _equity_of(engine)
    if equity is None:
        return {}
    s = equity.snapshot()
    return {"risk_equity_status": s["risk_equity_status"],
            "strategy_equity": s["strategy_equity"],
            "risk_equity_reference": s["risk_equity_reference"],
            "strategy_drawdown_pct": s["drawdown_pct"],
            "external_flows_cum": s["external_flows_cum"],
            "capital_guards": s["capital_guards"],
            "capital_hold": s["capital_hold"],
            "capital_eligible": (s["capital_eligible"]
                                 and not getattr(engine, "_capital_blocking_guard", None))}


class ExecutionEngine:
    """Cycle normal = PIPELINE INTEGRE :
    scanner -> ranker -> routeur -> portes edge/EV -> risque -> execution
    -> verification fills -> reconciliation. Plus de dependance exclusive
    a KXBTC15M ; parcours multi-candidats ; carnet relu juste avant l'ordre."""

    #: Per-cycle: the CAPITAL guard that fired but was REPORTED rather than
    #: obeyed (PROD READ_ONLY only). Reset at the start of every cycle path,
    #: at the start of every gate evaluation and in a `finally` at the end of
    #: every finalization, so it can never survive an exception into the
    #: next cycle.
    _capital_blocking_guard = None

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
        # Corrections broker-authoritative du journal (append-only, avant
        # que quiconque ne lise les agregats). Preconditions strictes par
        # correction: no-op partout sauf sur le ledger exact vise.
        from ledger_corrections import apply_ledger_corrections
        apply_ledger_corrections(self.tlog)
        self.posmgr   = PositionManager(client, self.tlog)
        self.orders   = OrderManager(client)
        self.risk     = RiskManager(self.tlog, self.posmgr, capital)
        # F2 risk-equity accounting: strategy equity, high-water mark,
        # external flows and baseline provenance, persisted under DATA_DIR.
        # Deposits raise affordability (self.capital) and never touch it.
        self.equity   = EquityLedger(self.tlog, self.posmgr, env=client.env,
                                 binding=account_binding.from_client(client))
        self.risk.equity = self.equity
        self._current_cycle = 0
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
        # T7-I: the BTC daily strategy has never produced a settled
        # prediction, because _shadow_observer admits only btc15m*. Its
        # evidence goes to a SEPARATE append-only store rather than into the
        # shadow store: backtest_btc15m.py consumes that file with no strategy
        # filter and 15-minute time buckets, so daily rows would silently
        # contaminate a 15-minute analysis. Nothing here can affect an order.
        from btc_daily_evidence import BtcDailyEvidenceStore
        self.btc_daily_evidence = BtcDailyEvidenceStore(CFG.DATA_DIR)
        #: Producer side of the research boundary. Plain JSON to its own
        #: spool directory; no consumer is reachable from here.
        self.research_feed = ResearchFeed()
        # T7-K: evidence-only observation of markets the scanner rejects for
        # no_liquidity. OFF unless BTC_DAILY_SHADOW_ENABLED is set. It holds
        # no risk/order/position component and produces no Decision, so a
        # market observed here cannot become an order candidate.
        from btc_daily_shadow import BtcDailyShadowEvaluator
        self.btc_daily_shadow = BtcDailyShadowEvaluator(
            self.router, self.btc_daily_evidence)
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
        # P0 observability: one durable row per cycle, written on EVERY exit
        # path including the early returns at the global guards. Shares the
        # pipeline's rotating-jsonl writer so retention behaves identically.
        from opportunity_pipeline import RotatingJsonl as _RotatingJsonl
        self.cycles_jsonl = _RotatingJsonl(_p("cycles.jsonl"))
        HEALTH.set_client(client)
        assert_real_demo_integrity(client, CFG.SHADOW_MODE)
        log_execution_banner(client)
        self._probability_engine_report()
        # Recovery apres crash + broker source de verite
        self.orders.reconcile_startup(self.tlog, self.posmgr)
        self.posmgr.reconcile_startup()
        self.posmgr.reconcile_with_broker()
        # F2: declarative operator actions (seed, classification, rebase,
        # hold release, attestation) are applied once, here, after the
        # broker truth is established; every refusal is a logged no-op.
        self._apply_equity_operator_actions()
        log.warning(self.equity.banner_line())
        HEALTH.extra["risk_equity"] = self.equity.snapshot()
        # Reconciliation periodique : le passage de demarrage vient d'avoir
        # lieu, le premier passage periodique attend un intervalle complet.
        self._last_broker_verify = time.monotonic()
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
        """Journalise chaque candidat BTC evalue (accepte ou rejete).

        btc15m* -> shadow store (inchange, consomme par backtest_btc15m.py).
        btc_daily* -> store d'evidence dedie (T7-I). La docstring precedente
        annoncait 'CHAQUE candidat BTC' alors que le filtre n'admettait que
        btc15m : le modele daily n'a jamais produit une seule prediction
        reglee. Aucun des deux chemins ne peut modifier une decision : dec est
        deja fige, et l'appelant absorbe toute exception.
        """
        # Automatic research candidate feed (Alpha Gateway phase 2). This is
        # a WRITE TO A SPOOL DIRECTORY and nothing else: the engine gains no
        # dependency on the Alpha subsystem, learns nothing back from it, and
        # cannot be delayed or failed by it (`emit_candidate` never raises,
        # never trips the persistence sentinel, and is off by default). It
        # lives here because `dec` is already frozen and the caller already
        # absorbs exceptions -- this hook is the engine's existing
        # record-never-decide boundary, not a new one.
        # V4-RA-03 -- NEITHER THE NORMALIZATION NOR ITS FAILURE MAY LOG HERE.
        #
        # This block used to call `candidate_from_market(...)` -- research
        # NORMALIZATION, on the decision cycle's own thread -- and catch its
        # exceptions with `log.debug(f"research feed: {e}")`. That `except`
        # was the hole: `logging.Handler.handle` takes the handler's lock and
        # emits synchronously, so a handler held by another thread (a file
        # handler on a stalled volume is the ordinary case) blocked the CYCLE
        # here. A research subsystem whose ERROR path can stall the money path
        # has become part of the money path, which is the whole of AA-10.
        #
        # `observe_market` is total and reports through the writer's bounded
        # queue, so there is nothing left for this side to catch. The `try`
        # below is kept anyway, because the engine's guarantee must not depend
        # on a promise made in another module -- and its body says something
        # the same non-blocking way instead of touching a device.
        try:
            raw_market = getattr(snapshot, "raw_market", None) or {}
            # AA-01. `raw_market` is the exchange's own observation; `book` is
            # the EXECUTION-normalized structure, in which a missing side may
            # already have been derived (`no_bid = 100 - yes_ask`, or a bare
            # 50). Both are passed so the producer can tell an unobserved
            # quote from a computed one -- and REFUSE the computed one. The
            # order path keeps using `book` exactly as before; nothing about
            # execution changes here.
            self.research_feed.observe_market(
                raw_market, book, raw_book=raw_market,
                cycle_id=(dec.decision_id or "").split("-", 1)[0] or "")
        except Exception as e:                                # noqa: BLE001
            # The fallback, and its three properties:
            #
            #   NON-BLOCKING  no `log` call. The report goes to the research
            #                 writer's bounded queue and is emitted on the
            #                 writer's thread, where a stalled handler costs
            #                 research latency and nothing else.
            #   BOUNDED       `type(e).__name__` reads a slot on the class.
            #                 `str(e)` can run an arbitrary `__str__` and
            #                 return an arbitrary number of bytes, and this
            #                 exception may well be carrying a value the
            #                 exchange sent.
            #   TOTAL         written inline, with its own `except`, so the
            #                 whole block is total for ANY `self` -- a
            #                 failure to say something is never allowed to
            #                 become a failure of the thing trying to speak.
            try:
                self.research_feed.note_observer_failure(type(e).__name__)
            except Exception:                                 # noqa: BLE001
                pass
        try:
            if (dec.strategy or "").startswith("btc_daily"):
                self.btc_daily_evidence.record(
                    decision=dec.to_dict(),
                    model_output=getattr(dec, "model_output", None),
                    market=getattr(snapshot, "raw_market", None) or {},
                    book=book,
                    minutes_remaining=getattr(snapshot, "minutes_remaining",
                                              None),
                    cycle_id=(dec.decision_id or "").split("-", 1)[0] or None)
                return
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
            self._observe_equity(bal)
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

    # ── F2 risk-equity accounting hooks ──────────────────────────────────
    def _observe_equity(self, bal) -> None:
        """One ledger observation per balance read. Never raises into the
        cycle: accounting evidence must not be able to stop observation."""
        equity = getattr(self, "equity", None)
        if equity is None:
            return
        try:
            orders = getattr(self, "orders", None)
            in_flight = bool(getattr(orders, "pending_intents", None))
            halt = getattr(getattr(self, "posmgr", None), "reconcile_halt", None)
            equity.observe(bal, cycle_n=getattr(self, "_current_cycle", 0),
                           quiet=(not in_flight and halt is None))
            HEALTH.extra["risk_equity"] = equity.snapshot()
        except Exception as e:                            # noqa: BLE001
            log_rsk.error(f"[EQUITY] observation failed: {e}")

    def _apply_equity_operator_actions(self) -> None:
        equity = getattr(self, "equity", None)
        if equity is None:
            return
        cash = None
        if os.getenv("EQUITY_LEDGER_SEED_PRE_FLOW_CASH"):
            # only a seed proposal needs today's cash; nothing else reads
            # the broker here (one balance GET per cycle stays the rule)
            try:
                cash = self.client.get_balance()
            except Exception as e:                        # noqa: BLE001
                log_rsk.warning(f"[EQUITY] balance unavailable for the seed proposal: {e}")
        if os.getenv("EQUITY_LEDGER_REBASE_TOKEN"):
            # a rebase needs the authoritative order and position truth,
            # including a fresh read-only broker query; anything unknown
            # refuses. Other actions never need it.
            # Boot-time only: the cycle loop has not started, so execution
            # is quiescent by construction (nothing can submit while this
            # runs). The ledger re-verifies at commit anyway.
            ctx = equity_rebase_context(self.client, self.orders, self.posmgr,
                                        self.risk, equity=equity, quiescent=True)
        else:
            halt = getattr(self.posmgr, "reconcile_halt", None)
            ctx = {"drawdown_firing": False,
                   "reconcile_status": "MATCH" if halt is None else str(halt.get("status")),
                   "open_positions": self.posmgr.open_count(),
                   "in_flight_orders": len(getattr(self.orders, "pending_intents", {}) or {})}
        done = equity.apply_operator_actions(os.environ, cash, ctx)
        if done:
            log.warning(f"[EQUITY] operator actions: {done}")

    def _is_prod_read_only(self) -> bool:
        """"Not demo" and read-only dominance -- the write boundary's own
        formulation (`_assert_broker_write_allowed`), never "is prod"."""
        client = getattr(self, "client", None)
        return getattr(client, "env", None) != "demo" and prod_is_read_only()

    def _post_balance_gates(self) -> tuple:
        """Portes de risque globales APRES le solde. Retourne (ok, guard).

        Every guard is evaluated by `_evaluate_global_guards`, unchanged.
        Exactly one verdict is then re-read: `equity_drawdown` in PRODUCTION
        READ_ONLY. There, no broker mutation exists to protect, so the guard
        is RECORDED for this cycle (`_capital_blocking_guard`, surfaced as
        `would_block_capital` in the durable row, `capital_blocking_guard` in
        the cycle report and the dashboard) and observation continues:
        scanner, model, shadow evidence and sizing all run; the write layer
        is unreachable regardless. CAPITAL and DEMO keep the block. Every
        other guard keeps its verdict in every mode.

        The flag is reset FIRST so nothing an earlier cycle left behind can
        be mistaken for this cycle's verdict.
        """
        self._capital_blocking_guard = None
        ok, guard = self._evaluate_global_guards()
        if ok:
            return ok, guard
        if guard in OBSERVATION_ONLY_GUARDS and self._is_prod_read_only():
            self._capital_blocking_guard = guard
            log_rsk.warning(
                f"[READ_ONLY_OBSERVATION] capital_guard={guard} "
                f"scanner_continues=true broker_writes=false",
                extra={"event": "read_only_observation",
                       "would_block_capital": guard})
            return True, None
        return ok, guard

    def _evaluate_global_guards(self) -> tuple:
        """Portes de risque globales APRES le solde (capital effectif a
        jour). Retourne (ok, guard) ; ok=False si le cycle doit s'arreter.

        P0: `guard` NOMME la porte qui bloque. L'ORDRE D'EVALUATION ET LES
        VALEURS DE RETOUR SONT INCHANGES — seul le nom du bloqueur, jusqu'ici
        perdu, est desormais rendu au cycle pour etre journalise.
        """
        # Portes fail-closed structurelles AVANT les portes de risque :
        # une panne de persistance critique ou une divergence broker/local
        # non resolue interdit toute nouvelle soumission.
        if not PersistenceSentinel.healthy():
            f = PersistenceSentinel.failure() or {}
            log_rsk.error(f"Trading bloque: panne de persistance critique "
                          f"({f.get('path')}: {f.get('reason')})",
                          extra={"event": "trading_blocked",
                                 "reason": "persistence_failure"})
            return False, "persistence_failure"
        cap, cap_err = contract_cap_config()
        if cap is None:
            log_rsk.error(f"Trading bloque: {cap_err}",
                          extra={"event": "trading_blocked",
                                 "reason": "contract_cap_invalid"})
            return False, "contract_cap_invalid"
        halt = getattr(self.posmgr, "reconcile_halt", None)
        if halt:
            # Le nom de la porte reflete la NATURE du verrou (mismatch /
            # broker_unavailable / unknown) : chacun bloque fail-closed,
            # seul un MATCH ulterieur leve le verrou.
            guard = ("reconciliation_"
                     + str(halt.get("status", "mismatch")).lower())
            log_rsk.error(f"Trading bloque: reconciliation broker non "
                          f"concluante ({halt.get('status')}) depuis "
                          f"{halt.get('at')}",
                          extra={"event": "trading_blocked",
                                 "reason": guard})
            return False, guard
        ok, why = self.risk.can_trade(cycle_trades=0)
        if not ok:
            log_rsk.warning(f"Trading bloque: {why}",
                            extra={"event": "trading_blocked", "reason": why})
            return False, _classify_can_trade_guard(why)
        if self.posmgr.open_count() >= CFG.MAX_OPEN_POSITIONS:
            log_rsk.warning(f"MAX_OPEN_POSITIONS={CFG.MAX_OPEN_POSITIONS} atteint.",
                            extra={"event": "max_open_positions",
                                   "open": self.posmgr.open_count(),
                                   "limit": CFG.MAX_OPEN_POSITIONS})
            return False, "max_open_positions"
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
            return False, "equity_drawdown"
        # F2 accounting guards (CAPITAL only; DEMO reports, never blocks).
        # Order and names: equity_ledger.ACCOUNTING_GUARDS.
        equity = getattr(self, "equity", None)
        if equity is not None and getattr(getattr(self, "client", None), "env", None) != "demo":
            for guard in equity.guards():
                log_rsk.warning(f"Trading bloque: {guard} "
                                f"(risk_equity_status={equity.derive_status()})",
                                extra={"event": "trading_blocked", "reason": guard})
                return False, guard
        return True, None

    #: P0 observability. Funnel stage names carried from the pipeline report
    #: into the cycle evidence row, in funnel order.
    _FUNNEL_KEYS = ("scanned_raw", "after_status", "after_time_window",
                    "after_liquidity", "after_classification", "scanner_kept",
                    "supported", "model_evaluated", "positive_edge",
                    "positive_net_ev", "risk_passed", "orders_submitted",
                    "fills", "accepted")

    def _record_cycle_evidence(self, n: int, execution_path: str,
                               blocking_global_guard=None, detail: str = "",
                               pipeline: dict = None,
                               would_block_capital=None) -> dict:
        """Write exactly one durable evidence row per cycle. Observability only.

        `would_block_capital` defaults to this cycle's recorded CAPITAL guard
        (PROD READ_ONLY observation, see `_post_balance_gates`); an explicit
        value from the caller wins.

        Before P0 a cycle that returned early at a global guard wrote nothing
        at all, so "no decisions this cycle" had three indistinguishable
        causes: a guard blocked it, the scan found nothing, or the engine was
        not running. This row separates them.

        Funnel counters are emitted ONLY when the pipeline actually ran. When
        a guard fires before the scan, the counters are genuinely unknown and
        are therefore ABSENT — never zero. A zero here would assert that the
        scanner looked and found nothing, which would be a fabricated fact.

        Never raises: evidence must not be able to break a trading cycle.
        """
        if would_block_capital is None:
            # getattr: harnesses borrow this method onto stub objects.
            would_block_capital = getattr(self, "_capital_blocking_guard", None)
        row = {
            "cycle": n,
            "occurred_at": now_iso(),
            "engine_version": ENGINE_VERSION,
            "execution_path": execution_path,
            "blocking_global_guard": blocking_global_guard,
            "scan_executed": pipeline is not None,
            # Names a CAPITAL guard that fired but was REPORTED rather than
            # obeyed, which only PRODUCTION READ_ONLY observation may do.
            # None on every other cycle. A row with scan_executed=true and
            # a value here is a shadow cycle that CAPITAL would have refused.
            "would_block_capital": would_block_capital,
            "risk_equity_status": _equity_status_of(self),
        }
        if detail:
            row["blocking_detail"] = str(detail)[:300]
        if pipeline is not None:
            report = pipeline.get("report") or {}
            row["cycle_id"] = report.get("cycle_id")
            row["pipeline_version"] = report.get("pipeline_version")
            row["cycle_duration_ms"] = report.get("cycle_duration_ms")
            row["funnel"] = {k: report.get(k) for k in self._FUNNEL_KEYS
                             if report.get(k) is not None}
            row["rejections_by_reason"] = report.get("rejections_by_reason") or {}
            row["model_rejections_detailed"] = \
                report.get("model_rejections_detailed") or {}
        try:
            self.cycles_jsonl.write(row)
        except Exception as e:                              # noqa: BLE001
            log.warning(f"[CYCLE_EVIDENCE] write failed: {e}")
        log.info(f"[CYCLE_EVIDENCE] cycle={n} path={execution_path} "
                 f"blocking_global_guard={blocking_global_guard} "
                 f"scan_executed={row['scan_executed']} "
                 f"would_block_capital={would_block_capital}",
                 extra={"event": "cycle_evidence", "cycle": n,
                        "execution_path": execution_path,
                        "blocking_global_guard": blocking_global_guard,
                        "scan_executed": row["scan_executed"],
                        "would_block_capital": would_block_capital})
        return row

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

        # T7-I: settle matured BTC daily predictions. Only markets whose close
        # time has already passed are polled, so an unsettled backlog costs
        # zero API calls; the store swallows its own errors and can only ever
        # return a count. No trading state is read or written here.
        try:
            n_daily = self.btc_daily_evidence.settle(self.client.get_market)
            if n_daily:
                cov = self.btc_daily_evidence.coverage()
                log.info(f"[BTC_DAILY_EVIDENCE] {n_daily} prediction(s) "
                         f"settled (predictions={cov['predictions']} "
                         f"settled={cov['settled']})")
        except Exception as e:                            # noqa: BLE001
            log.warning(f"[BTC_DAILY_EVIDENCE] reglement: {e}")

        # 0b) Verification broker periodique (non destructrice) : le
        # broker reste la source de verite pendant toute la vie du
        # processus, pas seulement au demarrage. Une divergence arme
        # posmgr.reconcile_halt, lu par les portes ci-dessous ; l'etat
        # local n'est jamais « corrige » automatiquement en vol.
        if (CFG.RECONCILE_INTERVAL_S > 0 and
                time.monotonic() - self._last_broker_verify
                >= CFG.RECONCILE_INTERVAL_S):
            self._last_broker_verify = time.monotonic()
            try:
                self.posmgr.verify_against_broker()
            except Exception as e:                        # noqa: BLE001
                log_pos.warning(f"[RECONCILE_VERIFY] echec du passage: {e}")
            # Une intention d'envoi ambigue peut se resoudre TARD: un ordre
            # accepte mais invisible au premier passage peut apparaitre
            # ensuite. On rejoue donc la resolution tant qu'elle n'a pas
            # conclu, sans attendre un redemarrage.
            if getattr(self.orders, "pending_intents", None):
                try:
                    self.orders.resolve_pending_intents()
                except Exception as e:                    # noqa: BLE001
                    log_pos.warning(
                        f"[AMBIGUOUS_RESOLUTION] passage periodique echoue: {e}")
            # Supervision: une intention ambigue bloque un ticker
            # fail-closed; elle ne doit jamais rester silencieuse. Purement
            # observatoire (aucune soumission, aucune cloture).
            try:
                self.orders.evaluate_intent_alerts()
            except Exception as e:                        # noqa: BLE001
                log_pos.warning(f"[INTENT_ALERT] evaluation echouee: {e}")

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
        self._capital_blocking_guard = None
        self._current_cycle = n
        # 2) Kill switch (seule porte qui ne depend pas du capital effectif)
        #    et 3-4) portes de risque globales — mesurees ensemble : ce sont
        #    les controles risque du cycle (P4.4).
        with timed("risk_check"):
            if CFG.KILL_SWITCH:
                log_rsk.warning("KILL_SWITCH actif -- aucun ordre ce cycle.",
                                extra={"event": "kill_switch"})
                self._record_cycle_evidence(n, "sequential", "kill_switch")
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
                self._record_cycle_evidence(n, "sequential", "balance_gate",
                                            detail=why)
                return 0

            # 4) Portes de risque globales (dependent desormais du capital a jour)
            gates_ok, guard = self._post_balance_gates()
            if not gates_ok:
                self._record_cycle_evidence(n, "sequential", guard)
                self.stats.log_summary(); return 0

        # 5) PIPELINE integre (multi-candidats, jamais bloque sur un ticker)
        res = self.pipeline.run_cycle(
            max_accepted=CFG.MAX_TRADES_CYCLE,
            skip_ticker_fn=(lambda tk: CFG.ONE_TRADE_PER_MKT and
                            (tk in self.posmgr.tickers_open()
                             or self.tlog.has_open_on(tk))))
        return self._finish_cycle(n, res, "sequential")

    def _cycle_parallel(self, n: int) -> int:
        """P8 : solde + checks de sante executes en PARALLELE du scan.
        Le scan reste sur le thread principal (tracing/timing intacts) ;
        le fetch du solde et la sonde de sante tournent en arriere-plan
        (ThreadPoolExecutor, stdlib) et sont joints avant les portes de
        risque. Resultats strictement identiques au sequentiel : solde et
        scan sont independants (le scan ne lit ni n'ecrit self.capital /
        self.risk — portes inchangees, executees apres la jointure)."""
        self._capital_blocking_guard = None
        self._current_cycle = n
        # 2) Kill switch d'abord : aucun scan lance inutilement.
        if CFG.KILL_SWITCH:
            log_rsk.warning("KILL_SWITCH actif -- aucun ordre ce cycle.",
                            extra={"event": "kill_switch"})
            self._record_cycle_evidence(n, "parallel", "kill_switch")
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
                self._record_cycle_evidence(n, "parallel", "balance_gate",
                                            detail=why, pipeline=res)
                return 0
            gates_ok, guard = self._post_balance_gates()
            if not gates_ok:
                self._record_cycle_evidence(n, "parallel", guard,
                                            pipeline=res)
                self.stats.log_summary(); return 0
        return self._finish_cycle(n, res, "parallel")

    def _finish_cycle(self, n: int, res: dict,
                      execution_path: str = "sequential") -> int:
        """Execution des candidats acceptes + rapports de fin de cycle
        (partage sequentiel/parallele).

        Carries this cycle's recorded CAPITAL guard (PROD READ_ONLY
        observation) into the cycle report before it is persisted, and
        clears the flag in `finally` whether finalization returns or raises.
        """
        guard = getattr(self, "_capital_blocking_guard", None)
        try:
            report = res.get("report") if isinstance(res, dict) else None
            if guard and isinstance(report, dict):
                report["capital_blocking_guard"] = guard
                report["capital_eligible"] = False
            if isinstance(report, dict):
                report.update(_equity_dashboard_fields(self))
            return self._finalize_cycle(n, res, execution_path)
        finally:
            self._capital_blocking_guard = None

    def _finalize_cycle(self, n: int, res: dict,
                        execution_path: str = "sequential") -> int:
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
            # `stage_n`, never `n`: `n` is this method's cycle number and
            # is still needed below. Rebinding it here made every completed
            # cycle record the last stage's count (fills, so 0) as its
            # cycle number in the durable evidence, the cycle report and the
            # dashboard -- while blocked cycles, which return before this
            # loop, numbered correctly.
            stage_n = int(report.get(name) or 0)
            conv[name] = {"n": stage_n,
                          "pct_of_prev": round(100.0 * stage_n / prev, 2)
                          if prev else (100.0 if stage_n else 0.0)}
            prev = stage_n if stage_n else prev
        report["funnel_conversion"] = conv
        report["fills_confirmed"] = placed
        report["orders"] = report.get("orders_submitted", 0)
        # T7-K: evidence-only observation, run AFTER every order decision is
        # final so it cannot influence one. Its counters live under their own
        # key — no production funnel counter is reused or touched. A failure
        # here is logged and discarded; it can never create an order.
        try:
            shadow = self.btc_daily_shadow.run(
                self.scanner.shadow_population(), report.get("cycle_id"))
            if shadow.get("shadow_daily_predicted"):
                log.info(f"[SHADOW_DAILY] {_json_shadow(shadow)}")
            report["shadow_daily"] = shadow
        except Exception as e:                            # noqa: BLE001
            log.warning(f"[SHADOW_DAILY] {e}")
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
                # PROD READ_ONLY observation: the CAPITAL guard this cycle
                # observed through, None on every other cycle.
                "capital_blocking_guard":
                    getattr(self, "_capital_blocking_guard", None),
                **_equity_dashboard_fields(self),
                **({"read_only": True, "capital_eligible": False}
                   if getattr(self, "_capital_blocking_guard", None) else {}),
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
        # P0: the cycle ran end to end. blocking_global_guard is None here,
        # which is itself the evidence that no global guard fired — the fact
        # that distinguishes "no opportunity" from "blocked before looking".
        self._record_cycle_evidence(n, execution_path, None, pipeline=res)
        if placed == 0:
            self.stats.log_summary()
        return placed

    def _execute_decision(self, dec, report) -> int:
        ticker = dec.ticker
        # 5.0) QUARANTAINE QUOTIDIENNE + LISTE BLANCHE.
        #
        # Place AVANT tout : aucun carnet, aucun budget de risque, aucun
        # dimensionnement, aucune reservation de disjoncteur, aucun compteur
        # de soumission. Retourner 0 ici ne consomme pas non plus le budget
        # MAX_TRADES_CYCLE, donc un candidat quotidien refuse ne peut pas
        # affamer un candidat 15m du meme cycle.
        mtype = getattr(dec, "market_type", None)
        # Un ticker non classable (None, blancs, objet non textuel,
        # caractere invisible) ne peut PAS etre soumis a la quarantaine de
        # maniere fiable : on refuse ici, avant toute lecture de carnet, au
        # lieu de laisser la seule garde du gestionnaire d'ordres decider.
        if not ticker_is_wellformed(ticker):
            report["rejections"]["ticker_malformed"] = \
                report["rejections"].get("ticker_malformed", 0) + 1
            log_trd.info(
                f"[TICKER_MALFORMED] {ticker!r}: forme non reconnue -- non "
                f"classable, aucun carnet, aucun ordre.",
                extra={"ticker": str(ticker), "strategy": dec.strategy,
                       "market_type": mtype})
            return 0
        if daily_quarantine_blocks(ticker) or mtype == DAILY_MARKET_TYPE:
            if not daily_oracle_approved():
                report["rejections"]["daily_oracle_unapproved"] = \
                    report["rejections"].get("daily_oracle_unapproved", 0) + 1
                log_trd.info(
                    f"[DAILY_QUARANTINE] {ticker}: candidat quotidien NON "
                    f"executable (oracle de reglement non approuve) -- aucun "
                    f"carnet, aucun risque, aucun ordre.",
                    extra={"ticker": ticker, "strategy": dec.strategy,
                           "market_type": mtype})
                return 0
        # LISTE BLANCHE, jamais une liste noire : Decision.market_type vaut
        # None par defaut, et un market_type absent, inconnu ou nouvellement
        # ajoute doit etre refuse tant que personne ne l'a inscrit ici
        # deliberement. Une liste noire laisserait passer None.
        if mtype not in executable_market_types():
            report["rejections"]["market_type_not_executable"] = \
                report["rejections"].get("market_type_not_executable", 0) + 1
            log_trd.info(
                f"[EXECUTION_ALLOWLIST] {ticker}: market_type={mtype!r} "
                f"absent de la liste blanche -- aucun ordre.",
                extra={"ticker": ticker, "strategy": dec.strategy,
                       "market_type": mtype})
            return 0
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
                side=getattr(dec, "side", "yes"),
                # F2: the throttle reads the strategy-equity drawdown
                # percentage, never dollars over cash
                drawdown_pct=(self.risk.rolling_drawdown_pct()
                              if hasattr(self.risk, "rolling_drawdown_pct") else None))
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

        # 5d-bis) LECTURE SEULE PRODUCTION : la decision est COMPLETE (modele,
        # probabilite marche, edge, EV, portes de risque, taille) et le
        # resultat est journalise comme WOULD_SUBMIT. Le chemin d'ecriture
        # n'est PAS emprunte : on n'appelle pas place_and_track pour se faire
        # refuser par le garde client. Ce garde est une defense en
        # profondeur, pas le chemin d'execution normal du shadow -- un shadow
        # qui "essaie puis echoue" laisse une tentative reelle a un bug pres
        # du reseau, et pollue les compteurs de rejet avec des refus qui ne
        # sont pas des decisions.
        if self.client.env != "demo" and prod_is_read_only():
            report["would_submit"] = report.get("would_submit", 0) + 1
            report["rejections"]["prod_read_only"] = \
                report["rejections"].get("prod_read_only", 0) + 1
            log_trd.info(
                f"[WOULD_SUBMIT] {ticker} {dec.side.upper()} x{count} @ "
                f"{entry}c -- PROD_ACCESS_MODE=READ_ONLY: decision complete, "
                f"AUCUN appel au chemin d'ecriture.",
                extra={"ticker": ticker, "side": dec.side, "size": count,
                       "price": entry, "would_submit": True,
                       "model_probability": dec.model_probability,
                       "market_probability": dec.market_probability,
                       "edge_net": dec.net_edge, "ev_net": dec.net_ev,
                       "est_fee_total": est_fee_total,
                       "strategy": dec.strategy,
                       "market_type": getattr(dec, "market_type", None),
                       "rejection_reason": "prod_read_only",
                       "environment": self.client.env})
            return 0

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
