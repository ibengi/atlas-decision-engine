"""Normalized, non-executable data contracts for AI research providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


_FORBIDDEN_EXECUTION_KEYS = {
    "action",
    "trade_action",
    "order",
    "order_payload",
    "quantity",
    "size",
    "position_size",
    "submit_order",
    "cancel_order",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_unit_interval(name: str, value: float) -> float:
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value}")
    return value


def _find_forbidden_key(value: Any, path: str = "root") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_s = str(key)
            if key_s.lower() in _FORBIDDEN_EXECUTION_KEYS:
                return f"{path}.{key_s}"
            found = _find_forbidden_key(child, f"{path}.{key_s}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            found = _find_forbidden_key(child, f"{path}[{i}]")
            if found:
                return found
    return None


@dataclass(frozen=True)
class MarketCandidate:
    """Market information exposed to research providers.

    This object intentionally contains no broker client, order manager, account
    balance, write token or callable that could mutate trading state.
    """

    contract_id: str
    market_probability: float
    title: str = ""
    rules: str = ""
    category: str = ""
    observed_at: str = field(default_factory=_utc_now_iso)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise ValueError("contract_id is required")
        object.__setattr__(
            self,
            "market_probability",
            _validate_unit_interval("market_probability", self.market_probability),
        )
        forbidden = _find_forbidden_key(self.context, "context")
        if forbidden:
            raise ValueError(f"execution-shaped field forbidden in candidate: {forbidden}")


@dataclass(frozen=True)
class IntelligenceObservation:
    """One provider's shadow opinion about one contract.

    The schema has no action, side effect, position size or order payload.  It
    can be persisted, scored and compared with outcomes, but cannot itself be
    executed.
    """

    contract_id: str
    provider: str
    model: str
    probability: float | None
    confidence: float
    abstain: bool = False
    ambiguity: str = "UNKNOWN"
    evidence_freshness: str = "UNKNOWN"
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    observed_at: str = field(default_factory=_utc_now_iso)
    rationale_summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise ValueError("contract_id is required")
        if not self.provider.strip():
            raise ValueError("provider is required")
        if not self.model.strip():
            raise ValueError("model is required")
        if self.probability is not None:
            object.__setattr__(
                self,
                "probability",
                _validate_unit_interval("probability", self.probability),
            )
        object.__setattr__(
            self,
            "confidence",
            _validate_unit_interval("confidence", self.confidence),
        )
        if self.latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        if self.cost_usd < 0:
            raise ValueError("cost_usd cannot be negative")
        if self.abstain and self.probability is not None:
            raise ValueError("an abstaining observation must not publish a probability")
        forbidden = _find_forbidden_key(self.metadata, "metadata")
        if forbidden:
            raise ValueError(f"execution-shaped field forbidden in observation: {forbidden}")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "IntelligenceObservation":
        forbidden = _find_forbidden_key(payload)
        if forbidden:
            raise ValueError(f"provider payload contains executable field: {forbidden}")
        return cls(**dict(payload))

    def as_evidence(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "provider": self.provider,
            "model": self.model,
            "probability": self.probability,
            "confidence": self.confidence,
            "abstain": self.abstain,
            "ambiguity": self.ambiguity,
            "evidence_freshness": self.evidence_freshness,
            "latency_ms": round(float(self.latency_ms), 3),
            "cost_usd": round(float(self.cost_usd), 8),
            "observed_at": self.observed_at,
            "rationale_summary": self.rationale_summary,
            "metadata": dict(self.metadata),
            "shadow_only": True,
        }
