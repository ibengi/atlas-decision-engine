"""Provider orchestration for Atlas Intelligence Network SHADOW phase."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Protocol

from .budget import DailyBudgetManager
from .cache import IntelligenceCache
from .schemas import IntelligenceObservation, MarketCandidate

log = logging.getLogger(__name__)


_TRUE = {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True)
class ShadowOnlyPolicy:
    """Hard boundary for phase 1: intelligence can observe, never execute."""

    mode: str = "SHADOW"

    @classmethod
    def from_environment(cls) -> "ShadowOnlyPolicy":
        mode = os.getenv("AI_MODE", "SHADOW").strip().upper()
        if mode != "SHADOW":
            raise RuntimeError(
                f"AI Intelligence Network phase 1 supports SHADOW only, got {mode!r}"
            )
        dangerous = {
            name: os.getenv(name, "0").strip().lower()
            for name in (
                "AI_CAN_INFLUENCE_TRADE",
                "AI_CAN_SIZE_POSITION",
                "AI_CAN_SUBMIT_ORDER",
            )
        }
        armed = [name for name, value in dangerous.items() if value in _TRUE]
        if armed:
            raise RuntimeError(
                "shadow-only boundary violated by environment: " + ", ".join(armed)
            )
        return cls()

    @property
    def can_influence_trade(self) -> bool:
        return False

    @property
    def can_size_position(self) -> bool:
        return False

    @property
    def can_submit_order(self) -> bool:
        return False

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "can_influence_trade": False,
            "can_size_position": False,
            "can_submit_order": False,
        }


class ProviderAdapter(Protocol):
    """Minimal contract implemented by Astra/Gemini/Grok adapters later."""

    provider_name: str
    cache_ttl_seconds: float

    def estimate_cost_usd(self, candidate: MarketCandidate) -> float:
        ...

    def analyze(self, candidate: MarketCandidate) -> IntelligenceObservation:
        ...


class IntelligenceRouter:
    """Collect normalized provider opinions without touching trading state.

    The router intentionally exposes only ``collect`` and telemetry snapshots.
    It has no account, risk manager, position sizer, order manager or broker
    dependency.  Provider failure therefore degrades to missing intelligence,
    never a trading-engine failure.
    """

    def __init__(
        self,
        providers: Iterable[ProviderAdapter],
        budget: DailyBudgetManager,
        cache: IntelligenceCache | None = None,
        policy: ShadowOnlyPolicy | None = None,
    ) -> None:
        self.providers = {p.provider_name: p for p in providers}
        if len(self.providers) != len(list(self.providers.values())):
            raise ValueError("provider names must be unique")
        self.budget = budget
        self.cache = cache or IntelligenceCache()
        self.policy = policy or ShadowOnlyPolicy.from_environment()
        self._stats = {
            "cache_hits": 0,
            "provider_calls": 0,
            "provider_errors": 0,
            "budget_skips": 0,
        }

    def collect(
        self,
        candidate: MarketCandidate,
        provider_names: Iterable[str] | None = None,
    ) -> list[IntelligenceObservation]:
        names = list(provider_names) if provider_names is not None else list(self.providers)
        observations: list[IntelligenceObservation] = []

        for name in names:
            provider = self.providers.get(name)
            if provider is None:
                log.warning("[AI_SHADOW] unknown provider=%s", name)
                continue

            cached = self.cache.get(name, candidate.contract_id)
            if cached is not None:
                self._stats["cache_hits"] += 1
                observations.append(cached)
                continue

            estimate = max(0.0, float(provider.estimate_cost_usd(candidate)))
            decision = self.budget.reserve(name, estimate)
            if not decision.allowed:
                self._stats["budget_skips"] += 1
                log.info(
                    "[AI_SHADOW] provider=%s contract=%s skipped=%s estimate_usd=%.6f",
                    name,
                    candidate.contract_id,
                    decision.reason,
                    estimate,
                )
                continue

            try:
                self._stats["provider_calls"] += 1
                observation = provider.analyze(candidate)
                if observation.provider != name:
                    raise ValueError(
                        f"provider identity mismatch: router={name!r} payload={observation.provider!r}"
                    )
                if observation.contract_id != candidate.contract_id:
                    raise ValueError(
                        "provider contract_id does not match requested candidate"
                    )
                self.budget.settle(name, estimate, observation.cost_usd)
                self.cache.put(observation, float(provider.cache_ttl_seconds))
                observations.append(observation)
            except Exception:
                self.budget.release(name, estimate)
                self._stats["provider_errors"] += 1
                log.exception(
                    "[AI_SHADOW] provider=%s contract=%s failed; continuing without opinion",
                    name,
                    candidate.contract_id,
                )

        return observations

    def snapshot(self) -> dict:
        return {
            "policy": self.policy.snapshot(),
            "providers": sorted(self.providers),
            "stats": dict(self._stats),
            "budget": self.budget.snapshot(),
        }
