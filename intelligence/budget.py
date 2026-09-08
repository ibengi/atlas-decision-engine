"""Deterministic daily AI spend guard for the shadow intelligence layer."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Mapping


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    provider: str
    estimated_cost_usd: float


class DailyBudgetManager:
    """In-memory daily budget ledger with atomic reservations.

    Provider API billing remains authoritative; this guard exists to prevent a
    runaway Atlas process from issuing unbounded research calls.  Reservations
    count immediately and are settled to actual cost after the call.
    """

    def __init__(
        self,
        global_limit_usd: float,
        provider_limits_usd: Mapping[str, float] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if global_limit_usd < 0:
            raise ValueError("global_limit_usd cannot be negative")
        self.global_limit_usd = float(global_limit_usd)
        self.provider_limits_usd = {
            str(k): float(v) for k, v in (provider_limits_usd or {}).items()
        }
        if any(v < 0 for v in self.provider_limits_usd.values()):
            raise ValueError("provider budget limits cannot be negative")
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._day = self._day_key()
        self._spent = defaultdict(float)
        self._lock = Lock()

    def _day_key(self) -> str:
        return self._now_fn().astimezone(timezone.utc).date().isoformat()

    def _roll_day_locked(self) -> None:
        today = self._day_key()
        if today != self._day:
            self._day = today
            self._spent.clear()

    def _global_spend_locked(self) -> float:
        return float(sum(self._spent.values()))

    def reserve(self, provider: str, estimated_cost_usd: float) -> BudgetDecision:
        provider = str(provider)
        cost = float(estimated_cost_usd)
        if cost < 0:
            raise ValueError("estimated_cost_usd cannot be negative")
        with self._lock:
            self._roll_day_locked()
            projected_global = self._global_spend_locked() + cost
            if projected_global > self.global_limit_usd + 1e-12:
                return BudgetDecision(False, "global_daily_budget", provider, cost)
            provider_limit = self.provider_limits_usd.get(provider)
            if provider_limit is not None:
                projected_provider = self._spent[provider] + cost
                if projected_provider > provider_limit + 1e-12:
                    return BudgetDecision(False, "provider_daily_budget", provider, cost)
            self._spent[provider] += cost
            return BudgetDecision(True, "reserved", provider, cost)

    def settle(self, provider: str, reserved_usd: float, actual_usd: float) -> None:
        reserved = float(reserved_usd)
        actual = float(actual_usd)
        if reserved < 0 or actual < 0:
            raise ValueError("costs cannot be negative")
        with self._lock:
            self._roll_day_locked()
            self._spent[str(provider)] = max(
                0.0,
                self._spent[str(provider)] - reserved + actual,
            )

    def release(self, provider: str, reserved_usd: float) -> None:
        self.settle(provider, reserved_usd, 0.0)

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day_locked()
            provider_spend = dict(self._spent)
            return {
                "day_utc": self._day,
                "global_limit_usd": self.global_limit_usd,
                "global_spend_usd": round(self._global_spend_locked(), 8),
                "provider_limits_usd": dict(self.provider_limits_usd),
                "provider_spend_usd": {
                    k: round(v, 8) for k, v in provider_spend.items()
                },
            }
