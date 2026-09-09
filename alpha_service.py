"""Atlas Alpha Shadow Service — the automatic research loop. SHADOW ONLY.

Alpha Gateway phase 2, sections 2, 6, 7, 8, 10, 11.

    scanner candidate → snapshot emitted → service receives snapshot
      → providers dispatched → responses validated → P_META calculated
      → shadow opportunity stored → later outcome attached
      → calibration updated

No execution action occurs at any point, and none can: this module imports
the consumer, the gateway, the ledger, the budget and the telemetry, and
nothing from the money path. `tests/test_alpha_safety_boundary.py` enforces
that structurally, in both directions.

WHY THIS IS A SEPARATE PROCESS
    Section 2 asks for `Atlas Engine Service` and `Atlas Alpha Shadow
    Service` as distinct deployments, and the separation buys three things
    that an in-process thread would not:

      * CREDENTIALS. The Alpha service is given XAI/Gemini/OpenAI keys and
        no broker key. A thread inside the engine would share the engine's
        environment, which holds broker credentials -- so "the Alpha
        subsystem has no broker credential" would stop being checkable.
        `assert_no_broker_credentials()` refuses to start if one is visible.
      * BLAST RADIUS. A hung provider, a memory leak or a crash in research
        cannot touch the process that manages positions.
      * SPEND. The research process can be stopped, restarted or
        rate-limited without interrupting risk management.

    The two processes share only a directory: the engine writes candidate
    records, the service reads them and writes its own ledgers.
"""

import logging
import os
import sys
import signal
import time
from datetime import datetime, timezone

from alpha_consumer import STATUS_ANALYZED, STATUS_DEFERRED, SpoolConsumer
from alpha_cost import REASON_BUDGET, REASON_EXPIRED, BudgetGuard
from alpha_gateway import AlphaGateway
from alpha_ledger import AlphaLedger
from alpha_providers import default_providers, set_pricing_table
from alpha_telemetry import Telemetry
from config import CFG

log = logging.getLogger("ALPHA")

#: A shadow state of its own: every provider was refused before it was
#: called. Not a probability, not a failure of the models -- a spend limit.
STATE_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

#: Environment variables whose presence means this process can move money.
#: Modules that constitute the money path. None may be loaded in this
#: process; `loaded_execution_modules()` checks that at runtime.
BROKER_MODULES = (
    "order_manager", "execution_engine", "kalshi_client", "position_manager",
    "position_sizer", "risk_manager", "equity_ledger", "trade_logger",
    "kalshi_alpha_bot", "state_restore",
)

#: The Alpha service refuses to start while any of them is set.
BROKER_CREDENTIAL_VARS = (
    "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY",
    "KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY",
    "KALSHI_PROD_KEY_ID", "KALSHI_PROD_PRIVATE_KEY",
)
#: ...and boolean gates that would authorize a write if this process ever
#: grew one. `DEMO_TRADING` is here deliberately: a demo write is still a
#: broker write, and the demo credentials are real credentials.
BROKER_AUTHORITY_VARS = (
    "ALLOW_ORDER_SUBMISSION", "LIVE_TRADING", "LIVE_TRADING_CONFIRMED",
    "LIVE_BROKER_WRITES_AUTHORIZED", "KALSHI_ENV_CONFIRM",
    "DEMO_TRADING", "MODEL_APPROVED_FOR_LIVE", "ALLOW_FALLBACK_CAPITAL",
)

#: Variables that carry authority in a VALUE rather than as a boolean. A
#: truthiness test would miss `PROD_ACCESS_MODE=CAPITAL` entirely -- the
#: string is not "1" or "true" -- which is exactly the setting that turns
#: capital on. Matched case-insensitively against the listed values.
BROKER_AUTHORITY_VALUES = {
    "PROD_ACCESS_MODE": ("capital",),
    "EXECUTION_MODE": ("live",),
}

#: Truthy spellings. Kept explicit so a new spelling is a deliberate edit.
_TRUTHY = ("1", "true", "yes", "y", "on", "live", "enabled")


def loaded_execution_modules() -> list:
    """Execution modules actually present in THIS interpreter, by name.

    The AST tests prove no alpha module *imports* one; this is the runtime
    counterpart, and it is measured rather than asserted. A health field
    that always printed 0 would report the property it is supposed to be
    checking, which is worth nothing precisely when it matters -- if
    something ever did pull the execution path into this process, a
    hardcoded zero would hide it.
    """
    return sorted(name for name in BROKER_MODULES if name in sys.modules)


class BrokerCredentialsPresent(RuntimeError):
    """The Alpha service was started in an environment that can trade."""


def assert_no_broker_credentials(env=None) -> list:
    """Refuse to run beside a broker credential (section 2).

    The Alpha service has no code path to a broker, but a credential in its
    environment would mean the deployment does not actually separate the two
    services -- and the whole argument for a separate process is that the
    separation is real. Returns the (empty) list of offending variable NAMES;
    values are never read, logged or returned.
    """
    env = os.environ if env is None else env
    found = [name for name in BROKER_CREDENTIAL_VARS
             if str(env.get(name, "")).strip()]
    authority = [name for name in BROKER_AUTHORITY_VARS
                 if str(env.get(name, "")).strip().lower() in _TRUTHY]
    valued = [name for name, values in BROKER_AUTHORITY_VALUES.items()
              if str(env.get(name, "")).strip().lower() in values]
    offending = found + authority + valued
    if offending and CFG.ALPHA_REFUSE_BROKER_CREDENTIALS:
        raise BrokerCredentialsPresent(
            f"the Alpha Shadow Service must not hold broker credentials or "
            f"write authority; found {offending} in its environment. Deploy "
            f"it as a separate service with only XAI_API_KEY, "
            f"GEMINI_API_KEY and OPENAI_API_KEY. "
            f"(Set ALPHA_REFUSE_BROKER_CREDENTIALS=false only to run both "
            f"in one environment for a local test.)")
    return offending


def observation_intervals() -> list:
    """Configured follow-up sample times, in seconds, ascending."""
    out = []
    for part in str(CFG.ALPHA_OBSERVATION_INTERVALS_S or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            log.warning(f"[ALPHA_SERVICE] ignoring unreadable observation "
                        f"interval {part!r}")
            continue
        if value > 0:
            out.append(value)
    return sorted(set(out))


class AlphaShadowService:
    """The loop. One `cycle()` per poll; `run()` repeats it until stopped."""

    def __init__(self, *, providers=None, ledger=None, consumer=None,
                 budget=None, telemetry=None, quote_fn=None, now_fn=None,
                 session=None, quant_estimator=None):
        self.ledger = ledger if ledger is not None else AlphaLedger()
        self.consumer = consumer if consumer is not None else SpoolConsumer()
        self.budget = budget if budget is not None else BudgetGuard()
        # One pricing table for the whole process: the guard's pre-call
        # estimate and the adapters' post-call actual must be computed from
        # the same rates, or a cap is enforced against prices nobody is
        # billed at.
        set_pricing_table(self.budget.pricing)
        self.telemetry = telemetry if telemetry is not None else Telemetry()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        #: Read-only market data. It is a QUOTE function, not a client: the
        #: service is handed a callable that returns a price dict, so it
        #: never holds anything that could place an order even by mistake.
        self.quote_fn = quote_fn
        self.providers = list(providers) if providers is not None \
            else default_providers(session=session,
                                   quant_estimator=quant_estimator)
        self.gateway = AlphaGateway(providers=self.providers,
                                    ledger=self.ledger, now_fn=self.now_fn)
        self._stop = False
        self._pending_observations = []       # [(due_ts, prediction_id, s)]
        self._last_duplicate_total = 0
        self.health = {}

    # ── startup ─────────────────────────────────────────────────────────
    def startup_report(self) -> dict:
        """Provider health, pricing and budget, logged once at start.

        A provider that fails its check is NOT swapped for another model
        (section 3): it simply will not contribute, and the report says so.
        """
        assert_no_broker_credentials()
        set_pricing_table(self.budget.pricing)
        self.health = {}
        for provider in self.providers:
            try:
                report = provider.health_check()
            except Exception as e:                            # noqa: BLE001
                report = {"provider": provider.name, "model": provider.model,
                          "configured": False, "priced": False,
                          "reachable": False,
                          "detail": f"{type(e).__name__}: {e}"}
            self.health[provider.name] = report
            log.warning(f"[ALPHA_HEALTH] provider={report['provider']} "
                        f"model={report['model']} "
                        f"configured={report['configured']} "
                        f"priced={report['priced']} "
                        f"reachable={report['reachable']} "
                        f"{report['detail']}")
        budget = self.budget.snapshot()
        priced = set(budget.get("priced_models") or ())
        report = {
            "service": "atlas-alpha-shadow",
            "service_mode": "SHADOW_ONLY", "mode": "SHADOW_ONLY",
            # Section 7. Flat booleans as well as the detailed per-provider
            # block, so a health check can be asserted without parsing.
            "grok_configured": self._configured("grok"),
            "gemini_configured": self._configured("gemini"),
            "openai_configured": self._configured("openai"),
            "pricing_valid": bool(priced) and all(
                self._priced(name) for name in ("grok", "gemini", "openai")),
            "budget_available": not bool(budget.get("exhausted")),
            "broker_credentials_present": False,
            "capital_authority": False,
            "execution_imports": len(loaded_execution_modules()),
            "execution_modules_loaded": loaded_execution_modules(),
            "quant_connected": False,
            "broker_credentials": [], "providers": self.health,
            "budget": budget,
            "observation_intervals_s": observation_intervals(),
            "feed": self.consumer.source.describe(),
            "spool": self.consumer.directory}
        log.warning(f"[ALPHA_SERVICE] started SHADOW_ONLY; "
                    f"budget={report['budget'].get('caps')} "
                    f"priced_models={report['budget'].get('priced_models')}")
        return report

    def _configured(self, name: str) -> bool:
        """Whether the provider holds a usable credential. Never its value."""
        return bool((self.health.get(name) or {}).get("configured"))

    def _priced(self, name: str) -> bool:
        return bool((self.health.get(name) or {}).get("priced"))

    # ── one poll ────────────────────────────────────────────────────────
    def cycle(self, limit: int = None) -> dict:
        """Consume, analyse, observe, invalidate. Never raises."""
        self.telemetry.incr("cycles")
        summary = {"analyzed": [], "deferred": [], "invalidated": 0,
                   "observations": 0}
        try:
            pending = self.consumer.pending(limit=limit)
        except Exception as e:                                # noqa: BLE001
            self.telemetry.record_error(f"consume: {type(e).__name__}: {e}")
            log.error(f"[ALPHA_SERVICE] consume failed: {e}")
            pending = []
        self.telemetry.incr("snapshots_received", len(pending))
        # The consumer keeps a running total; telemetry wants the delta since
        # the last cycle. Subtracting the telemetry counter from it would
        # only work while the two are exactly 1:1 forever, which is the kind
        # of coupling that silently breaks the first time anything else
        # touches the counter.
        duplicates = self.consumer.stats["duplicates"]
        self.telemetry.incr("snapshots_deduplicated",
                            max(0, duplicates - self._last_duplicate_total))
        self._last_duplicate_total = duplicates

        for snapshot, record in pending:
            try:
                outcome = self._analyze_one(snapshot, record)
            except Exception as e:                            # noqa: BLE001
                self.telemetry.record_error(f"analyze: {type(e).__name__}: {e}")
                log.error(f"[ALPHA_SERVICE] analysis of "
                          f"{snapshot.contract_id} failed: {e}")
                continue
            (summary["deferred"] if outcome.get("deferred")
             else summary["analyzed"]).append(outcome)

        summary["observations"] = self._take_due_observations()
        summary["invalidated"] = self._sweep_catalysts()
        self.telemetry.flush({"budget": self.budget.snapshot(),
                              "consumer": self.consumer.snapshot_stats(),
                              "providers": self.health})
        return summary

    def _analyze_one(self, snapshot, record) -> dict:
        """One snapshot through the gateway, with the budget gate attached."""
        analysis_spend = {"usd": 0.0}

        # The worst case is priced against the REAL prompt, so the refusal
        # is made on the largest amount this call could actually cost.
        from alpha_providers import build_prompt
        try:
            prompt_chars = len(build_prompt(snapshot))
        except Exception:                                     # noqa: BLE001
            prompt_chars = None

        def gate(provider):
            verdict = self.budget.check(
                provider.name, provider.model,
                analysis_spent_usd=analysis_spend["usd"],
                prompt_chars=prompt_chars)
            if verdict["allowed"]:
                analysis_spend["usd"] += verdict["estimated_cost_usd"]
            else:
                log.warning(f"[ALPHA_BUDGET] {provider.name}/{provider.model} "
                            f"NOT called: {verdict['reason']} "
                            f"-- {verdict['detail']}")
            return verdict

        def on_signal(sig):
            self.telemetry.record_signal(sig)
            cost = sig.cost or {}
            if cost.get("provider"):
                self.budget.record_actual({**cost,
                                           "outcome": "VALID" if sig.valid
                                           else (sig.rejected_reason or "")})

        opportunity = self.gateway.analyze(
            snapshot, quote_fn=self.quote_fn, gate=gate, on_signal=on_signal)

        # Every provider refused on spend is its own terminal state: the
        # models were never asked, so "no valid signal" would be misleading.
        refusals = {e.get("reason") for e in
                    opportunity["dispatch"]["excluded"]}
        if opportunity["p_meta"] is None and refusals and refusals.issubset(
                {REASON_BUDGET, "pricing_unconfigured", REASON_EXPIRED}):
            opportunity["state"] = STATE_BUDGET_EXHAUSTED
            opportunity["state_reason"] = (
                "every provider was refused before being called: "
                + ", ".join(sorted(refusals)))
        self.telemetry.record_state(opportunity["state"])
        if opportunity["p_meta"] is not None:
            self.telemetry.incr("p_meta_generated")

        deferred = opportunity["state"] == STATE_BUDGET_EXHAUSTED
        try:
            self.consumer.store.mark(
                snapshot.market_snapshot_id,
                STATUS_DEFERRED if deferred else STATUS_ANALYZED,
                contract_id=snapshot.contract_id,
                detail=opportunity["state"],
                prediction_id=opportunity["prediction_id"])
        except RuntimeError as e:
            self.telemetry.record_error(str(e))
            log.error(f"[ALPHA_SERVICE] {e}")

        if not deferred:
            self._schedule_observations(opportunity["prediction_id"])
        return {"prediction_id": opportunity["prediction_id"],
                "contract_id": snapshot.contract_id,
                "state": opportunity["state"],
                "p_meta": opportunity["p_meta"],
                "shadow_net_edge": opportunity["shadow_net_edge"],
                "deferred": deferred}

    # ── section 8: follow-up price observations ─────────────────────────
    def _schedule_observations(self, prediction_id: str) -> None:
        now = time.time()
        for interval in observation_intervals():
            self._pending_observations.append(
                (now + interval, prediction_id, interval))

    def _take_due_observations(self) -> int:
        if self.quote_fn is None or not self._pending_observations:
            return 0
        now, taken, still_pending = time.time(), 0, []
        for due, prediction_id, interval in self._pending_observations:
            if due > now:
                still_pending.append((due, prediction_id, interval))
                continue
            try:
                quote = self.quote_fn()
            except Exception as e:                            # noqa: BLE001
                # An unavailable book is not a price of zero. The sample is
                # dropped and the series records one fewer point.
                log.warning(f"[ALPHA_SERVICE] observation quote "
                            f"unavailable: {type(e).__name__}: {e}")
                continue
            try:
                self.ledger.record_observation(
                    prediction_id, interval_s=interval,
                    quote=quote if isinstance(quote, dict) else None)
                taken += 1
            except Exception as e:                            # noqa: BLE001
                self.telemetry.record_error(f"observation: {e}")
        self._pending_observations = still_pending
        self.telemetry.incr("observations_recorded", taken)
        return taken

    # ── section 7: catalyst invalidation ────────────────────────────────
    def _sweep_catalysts(self) -> int:
        try:
            rows = self.ledger.sweep_catalysts(now=self.now_fn())
        except Exception as e:                                # noqa: BLE001
            self.telemetry.record_error(f"catalyst sweep: {e}")
            return 0
        if rows:
            self.telemetry.incr("catalyst_invalidated", len(rows))
            log.warning(f"[ALPHA_SERVICE] {len(rows)} prediction(s) "
                        f"invalidated: their catalyst has occurred")
        return len(rows)

    # ── outcomes ────────────────────────────────────────────────────────
    def attach_outcome(self, prediction_id: str, outcome: int, *,
                       source: str = "") -> dict:
        row = self.ledger.resolve(prediction_id, outcome, source=source)
        self.telemetry.incr("resolved_predictions")
        return row

    # ── run forever ─────────────────────────────────────────────────────
    def stop(self, *_a) -> None:
        self._stop = True

    def run(self, *, max_cycles: int = None, sleep_fn=None) -> dict:
        sleep_fn = sleep_fn or time.sleep
        report = self.startup_report()
        cycles = 0
        while not self._stop and (max_cycles is None or cycles < max_cycles):
            cycles += 1
            summary = self.cycle()
            if summary["analyzed"] or summary["deferred"]:
                log.info(f"[ALPHA_SERVICE] cycle {cycles}: "
                         f"{len(summary['analyzed'])} analysed, "
                         f"{len(summary['deferred'])} deferred")
            if self._stop or (max_cycles is not None and cycles >= max_cycles):
                break
            sleep_fn(float(CFG.ALPHA_SPOOL_POLL_S))
        report["cycles"] = cycles
        report["telemetry"] = self.telemetry.flush()
        return report

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):      # pragma: no cover - non-main
                pass
