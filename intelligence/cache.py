"""Small TTL cache for provider opinions prepared ahead of trade time."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Callable

from .schemas import IntelligenceObservation


@dataclass(frozen=True)
class CacheEntry:
    observation: IntelligenceObservation
    stored_at: float
    ttl_seconds: float


class IntelligenceCache:
    """Thread-safe cache keyed by provider and contract.

    Only fresh observations are returned by ``get``.  Stale data can be read
    explicitly through ``peek`` for telemetry, never accidentally promoted.
    """

    def __init__(self, now_fn: Callable[[], float] | None = None) -> None:
        self._now_fn = now_fn or monotonic
        self._entries: dict[tuple[str, str], CacheEntry] = {}
        self._lock = Lock()

    @staticmethod
    def _key(provider: str, contract_id: str) -> tuple[str, str]:
        return str(provider), str(contract_id)

    def put(self, observation: IntelligenceObservation, ttl_seconds: float) -> None:
        ttl = float(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be > 0")
        entry = CacheEntry(observation, self._now_fn(), ttl)
        with self._lock:
            self._entries[self._key(observation.provider, observation.contract_id)] = entry

    def get(self, provider: str, contract_id: str) -> IntelligenceObservation | None:
        key = self._key(provider, contract_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            age = self._now_fn() - entry.stored_at
            if age > entry.ttl_seconds:
                return None
            return entry.observation

    def peek(self, provider: str, contract_id: str) -> dict | None:
        key = self._key(provider, contract_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            age = max(0.0, self._now_fn() - entry.stored_at)
            return {
                "observation": entry.observation,
                "age_seconds": age,
                "ttl_seconds": entry.ttl_seconds,
                "fresh": age <= entry.ttl_seconds,
            }

    def prune(self) -> int:
        now = self._now_fn()
        with self._lock:
            stale = [
                key
                for key, entry in self._entries.items()
                if now - entry.stored_at > entry.ttl_seconds
            ]
            for key in stale:
                del self._entries[key]
            return len(stale)
