"""Atlas Intelligence Network — shadow-only research layer.

This package is deliberately separated from RiskManager, PositionSizer,
ExecutionEngine and every broker-write surface. Provider output is research
evidence only; it is never an executable trading instruction.
"""

from .budget import DailyBudgetManager
from .cache import IntelligenceCache
from .evidence import IntelligenceEvidenceStore
from .router import IntelligenceRouter, ProviderAdapter, ShadowOnlyPolicy
from .schemas import IntelligenceObservation, MarketCandidate

__all__ = [
    "DailyBudgetManager",
    "IntelligenceCache",
    "IntelligenceEvidenceStore",
    "IntelligenceObservation",
    "IntelligenceRouter",
    "MarketCandidate",
    "ProviderAdapter",
    "ShadowOnlyPolicy",
]
