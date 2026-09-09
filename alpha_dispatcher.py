"""Parallel provider dispatch with per-provider isolation. SHADOW ONLY.

Alpha Gateway v1, sections 4, 5, 17, 18.

THE PROPERTY THIS MODULE OWES THE REST OF THE SYSTEM
    One slow or broken provider must not delay, degrade or cancel the
    others. Four adapters are submitted at once and each is collected under
    its own deadline; a provider that misses it is STALE and excluded, and
    the cycle completes with whatever arrived in time.

    That is not the same as "run them in a loop with a timeout each". A
    sequential loop with a 45 s budget per provider spends up to 180 s
    before the ensemble sees anything, by which time a FAST market has
    already moved. The wall-clock cost of the whole dispatch here is the
    ANALYSIS BUDGET, not the sum of the providers and not even the slowest
    one: collection stops at the deadline and anything still running is
    excluded as ANALYSIS_TIMEOUT.

    The deadline is enforced twice on purpose. Each adapter is handed the
    remaining budget as its own request timeout, and the dispatcher stops
    collecting at that deadline regardless. Relying on the adapter alone
    would mean any provider that forgets a timeout -- or an in-process
    estimator with no transport at all -- can hang the cycle.

WHY THREADS
    The rest of this engine is synchronous and uses `ThreadPoolExecutor`
    (`market_scanner`, `execution_engine`). Provider calls are I/O-bound
    HTTP, which threads parallelise perfectly well, and introducing an
    asyncio loop into a synchronous cycle would mean either an event loop
    per cycle or a colour change that reaches the scanner. Same
    concurrency, no new failure mode.

MARKET MOVEMENT (section 17)
    Prices are read at dispatch (T0) and again at completion (T1) through a
    caller-supplied quote function. If the apparent alpha existed at T0 and
    is gone at T1, the opportunity is `MARKET_MOVED` -- which is how latency
    decay gets measured rather than assumed.

NO EXECUTION PATH
    This module imports adapters, a schema and a clock. It does not import
    `order_manager`, `execution_engine` or `kalshi_client`.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone

from alpha_schema import rejected, validate_signal
from alpha_snapshot import MarketSnapshot, redacted

log = logging.getLogger("ALPHA")


class DispatchResult:
    """Signals plus the observable facts about how they were obtained."""

    def __init__(self, signals, dispatched_at, completed_at,
                 quote_at_dispatch=None, quote_at_completion=None):
        self.signals = list(signals)
        self.dispatched_at = dispatched_at
        self.completed_at = completed_at
        self.quote_at_dispatch = quote_at_dispatch
        self.quote_at_completion = quote_at_completion

    @property
    def valid(self) -> list:
        return [s for s in self.signals if s.valid]

    @property
    def excluded(self) -> list:
        return [s for s in self.signals if not s.valid]

    @property
    def wall_ms(self) -> int:
        return int(round((self.completed_at - self.dispatched_at)
                         .total_seconds() * 1000))

    def as_dict(self) -> dict:
        return {
            "dispatched_at_utc": self.dispatched_at.isoformat(timespec="seconds"),
            "completed_at_utc": self.completed_at.isoformat(timespec="seconds"),
            "wall_ms": self.wall_ms,
            "valid_models": [s.model for s in self.valid],
            "excluded": [{"provider": s.provider, "model": s.model,
                          "reason": s.rejected_reason,
                          "detail": s.rejected_detail,
                          "latency_ms": s.analysis_latency_ms}
                         for s in self.excluded],
            "quote_at_dispatch": self.quote_at_dispatch,
            "quote_at_completion": self.quote_at_completion,
        }


def _remaining_budget(snapshot: MarketSnapshot, now: datetime) -> float:
    """Seconds left before the analysis deadline. Never negative."""
    return max(0.0, (snapshot.analysis_deadline - now).total_seconds())


def dispatch(snapshot: MarketSnapshot, providers, *, quote_fn=None,
             now_fn=None, max_workers: int = None) -> DispatchResult:
    """Ask every provider at once; collect what arrives before the deadline.

    `quote_fn()` returns the live top-of-book as a dict (section 17). It is
    a READ. Anything it returns is recorded, never acted on.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    providers = list(providers)
    dispatched_at = now_fn()
    budget = _remaining_budget(snapshot, dispatched_at)

    quote_t0 = _safe_quote(quote_fn, "dispatch")
    if budget <= 0.0:
        # The deadline had already passed when the cycle reached us. Every
        # provider is excluded for the same honest reason, and no vendor is
        # billed for an answer nobody could have used.
        log.warning(f"[ALPHA_DISPATCH] deadline already passed for "
                    f"{redacted(snapshot)} -- no provider called")
        signals = [rejected(p.name, p.model, "analysis_timeout",
                            "analysis deadline had already passed at dispatch",
                            snapshot_id=snapshot.market_snapshot_id,
                            contract_id=snapshot.contract_id)
                   for p in providers]
        return DispatchResult(signals, dispatched_at, now_fn(),
                              quote_t0, _safe_quote(quote_fn, "completion"))

    signals = []
    workers = max_workers or max(1, len(providers))
    started = time.monotonic()
    # NOT a `with` block. `ThreadPoolExecutor.__exit__` calls
    # `shutdown(wait=True)`, which would make the whole dispatch wait for the
    # slowest worker no matter what deadline we set -- exactly the property
    # this module exists to prevent. The pool is shut down explicitly below
    # WITHOUT waiting, so the cycle returns at the deadline.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="alpha")
    futures = {}
    try:
        futures = {pool.submit(_run_one, provider, snapshot, budget, now_fn):
                   provider for provider in providers}
        try:
            for future in as_completed(futures, timeout=budget):
                provider = futures[future]
                try:
                    signals.append(future.result())
                except Exception as e:                        # noqa: BLE001
                    # Belt and braces: `_run_one` already catches everything,
                    # so reaching here means the worker itself died. One dead
                    # worker is one excluded provider, never a dead cycle.
                    log.error(f"[ALPHA_DISPATCH] worker for {provider.name} "
                              f"died: {type(e).__name__}: {e}")
                    signals.append(rejected(
                        provider.name, provider.model, "provider_crash",
                        f"{type(e).__name__}: {e}",
                        snapshot_id=snapshot.market_snapshot_id,
                        contract_id=snapshot.contract_id))
        except FuturesTimeout:
            # The analysis deadline arrived. Everything still running is
            # STALE by definition: section 5 says a model arriving after the
            # deadline is excluded, and waiting for it would be the same
            # mistake as merging it.
            pass
        for future, provider in futures.items():
            if future.done() and not future.cancelled():
                continue
            future.cancel()
            log.warning(f"[ALPHA_DISPATCH] {provider.name} did not answer "
                        f"within {budget:.3f}s -- excluded as ANALYSIS_TIMEOUT")
            signals.append(rejected(
                provider.name, provider.model, "analysis_timeout",
                f"no answer within the {budget:.3f}s analysis budget",
                snapshot_id=snapshot.market_snapshot_id,
                contract_id=snapshot.contract_id,
                latency_ms=int(round(budget * 1000))))
    finally:
        # `cancel_futures` drops work that never started; a worker already
        # inside a provider call cannot be killed, and is not waited for.
        # Those threads end when their own HTTP timeout fires -- which is why
        # every adapter is handed the remaining budget as its timeout, and
        # why an adapter must never issue an untimed request.
        pool.shutdown(wait=False, cancel_futures=True)
    completed_at = now_fn()
    quote_t1 = _safe_quote(quote_fn, "completion")
    order = {p.name: i for i, p in enumerate(providers)}
    signals.sort(key=lambda s: order.get(s.provider, len(order)))
    log.info(f"[ALPHA_DISPATCH] {snapshot.contract_id} "
             f"valid={sum(1 for s in signals if s.valid)}/{len(signals)} "
             f"wall_ms={int(round((time.monotonic() - started) * 1000))}")
    return DispatchResult(signals, dispatched_at, completed_at,
                          quote_t0, quote_t1)


def _run_one(provider, snapshot: MarketSnapshot, budget: float, now_fn):
    """One provider, start to validated signal. Never raises."""
    try:
        raw, meta = provider.analyze(snapshot, budget)
    except Exception as e:                                    # noqa: BLE001
        return rejected(provider.name, provider.model, "provider_exception",
                        f"{type(e).__name__}: {e}",
                        snapshot_id=snapshot.market_snapshot_id,
                        contract_id=snapshot.contract_id)
    received_at = now_fn()
    cost = meta.get("cost") or {}
    latency = int(meta.get("latency_ms") or 0)
    if meta.get("error"):
        reason = "provider_timeout" if _looks_like_timeout(meta["error"]) \
            else "provider_failure"
        return rejected(provider.name, provider.model, reason, meta["error"],
                        snapshot_id=snapshot.market_snapshot_id,
                        contract_id=snapshot.contract_id,
                        latency_ms=latency, cost=cost)
    if raw is None:
        return rejected(provider.name, provider.model, "provider_failure",
                        "provider returned nothing",
                        snapshot_id=snapshot.market_snapshot_id,
                        contract_id=snapshot.contract_id,
                        latency_ms=latency, cost=cost)
    # The deadline is judged on ARRIVAL, inside the validator, so a provider
    # that answers one millisecond late is STALE rather than merged.
    signal = validate_signal(raw, snapshot, provider=provider.name,
                             model=provider.model, latency_ms=latency,
                             received_at=received_at, cost=cost)
    if not signal.valid:
        log.warning(f"[ALPHA_SIGNAL_EXCLUDED] provider={provider.name} "
                    f"reason={signal.rejected_reason} "
                    f"detail={signal.rejected_detail[:120]}")
    return signal


def _looks_like_timeout(message: str) -> bool:
    lowered = str(message).lower()
    return any(marker in lowered for marker in
               ("timeout", "timed out", "deadline", "read timed"))


def _safe_quote(quote_fn, when: str):
    """Read the book without letting a quote failure end the cycle.

    A missing quote is recorded as None, which downstream reads as "market
    movement could not be measured" -- never as "the market did not move".
    """
    if quote_fn is None:
        return None
    try:
        quote = quote_fn()
    except Exception as e:                                    # noqa: BLE001
        log.warning(f"[ALPHA_QUOTE] {when} quote unavailable: "
                    f"{type(e).__name__}: {e}")
        return None
    return dict(quote) if isinstance(quote, dict) else None


def market_moved(snapshot: MarketSnapshot, result: DispatchResult,
                 *, side: str, threshold: float = None) -> bool:
    """Did the price the edge depended on move against us during analysis?

    Compared on the ASK of the side under consideration, because that is
    what a hypothetical taker would have paid. Returns False when either
    quote is missing: an unmeasured move is not a measured one, and
    `quote_movement()` carries the `measured` flag that says which.
    """
    t0, t1 = result.quote_at_dispatch, result.quote_at_completion
    if not t0 or not t1:
        return False
    key = "yes_ask" if side == "yes" else "no_ask"
    before, after = t0.get(key), t1.get(key)
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return False
    threshold = float(CFG_MOVE_EPS if threshold is None else threshold)
    return (float(after) - float(before)) > threshold


#: A move smaller than this is book noise, not information.
CFG_MOVE_EPS = 0.005


def quote_movement(result: DispatchResult) -> dict:
    """What the book did during the analysis, for the ledger."""
    t0, t1 = result.quote_at_dispatch, result.quote_at_completion
    out = {"measured": bool(t0 and t1)}
    for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        before = (t0 or {}).get(key)
        after = (t1 or {}).get(key)
        out[f"dispatch_{key}"] = before
        out[f"completion_{key}"] = after
        out[f"delta_{key}"] = (round(float(after) - float(before), 6)
                               if isinstance(before, (int, float))
                               and isinstance(after, (int, float)) else None)
    return out
