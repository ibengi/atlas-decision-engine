"""Fail-closed readiness check for wiring a read-only source into Alpha.

SHADOW ONLY. This module performs no I/O beyond data passed by the caller and
imports no execution/broker code. Its purpose is to prevent an integration
from inventing immutable snapshot facts that the source did not actually
record at decision time.

A source is READY only when every field required by ``alpha_snapshot`` is
available directly. We deliberately do not derive a missing order-book side
from ``entry_ask``/``spread`` and do not derive an expiry from
``recorded_at + minutes_remaining``. Those transformations would create facts
that were never persisted by the producer and would make later calibration
look more certain than the evidence permits.
"""

from __future__ import annotations

from typing import Any


# Target snapshot field -> literal source paths that can supply it without
# inference. Aliases are renames only; no arithmetic or market reconstruction.
DIRECT_PATHS = {
    "contract_id": ("contract_id", "ticker", "decision.contract_id", "decision.ticker"),
    "event_id": ("event_id", "decision.event_id"),
    "question": ("question", "title", "decision.question", "decision.title"),
    "resolution_rules": ("resolution_rules", "decision.resolution_rules"),
    "resolution_source": ("resolution_source", "decision.resolution_source"),
    "snapshot_time_utc": ("snapshot_time_utc", "emitted_at_utc", "recorded_at"),
    "yes_bid": ("yes_bid", "decision.yes_bid"),
    "yes_ask": ("yes_ask", "decision.yes_ask"),
    "no_bid": ("no_bid", "decision.no_bid"),
    "no_ask": ("no_ask", "decision.no_ask"),
    "volume": ("volume", "decision.volume"),
    "open_interest": ("open_interest", "decision.open_interest"),
    "market_close_time_utc": (
        "market_close_time_utc", "close_time", "decision.market_close_time_utc",
        "decision.close_time",
    ),
    "expected_resolution_time_utc": (
        "expected_resolution_time_utc", "expiration_time",
        "decision.expected_resolution_time_utc", "decision.expiration_time",
    ),
}


def _get_path(row: dict, path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _present(value: Any) -> bool:
    return value is not None and value != ""


def assess_record(row: dict) -> dict:
    """Return a machine-readable, fail-closed source-compatibility verdict."""
    if not isinstance(row, dict):
        return {
            "ready": False,
            "direct_fields": {},
            "missing_fields": sorted(DIRECT_PATHS),
            "prohibited_inferences": [],
            "reason": "record is not an object",
        }

    direct = {}
    missing = []
    for target, paths in DIRECT_PATHS.items():
        hit = next(((path, _get_path(row, path)) for path in paths
                    if _present(_get_path(row, path))), None)
        if hit is None:
            missing.append(target)
        else:
            direct[target] = {"source_path": hit[0], "value_present": True}

    prohibited = []
    # Common temptation in the currently deployed /decisions surface:
    # reconstruct a book from one chosen-side ask plus spread. Refuse it.
    if any(_present(_get_path(row, p)) for p in
           ("entry_ask", "decision.entry_ask", "spread", "decision.spread")):
        absent_book = [f for f in ("yes_bid", "yes_ask", "no_bid", "no_ask")
                       if f in missing]
        if absent_book:
            prohibited.append({
                "would_infer": absent_book,
                "from": ["entry_ask", "spread"],
                "reason": "one chosen-side quote cannot prove the full contemporaneous book",
            })

    # Another tempting reconstruction: recorded_at + model minutes_remaining.
    if "expected_resolution_time_utc" in missing and (
            _present(_get_path(row, "recorded_at")) and
            _present(_get_path(row, "decision.model_output.features.minutes_remaining"))):
        prohibited.append({
            "would_infer": ["expected_resolution_time_utc"],
            "from": ["recorded_at", "decision.model_output.features.minutes_remaining"],
            "reason": "derived expiry is not producer-persisted resolution evidence",
        })

    return {
        "ready": not missing,
        "direct_fields": direct,
        "missing_fields": sorted(missing),
        "prohibited_inferences": prohibited,
        "reason": "ready" if not missing else "source lacks immutable snapshot facts",
    }


def assess_records(rows: list[dict]) -> dict:
    """Aggregate readiness without allowing one complete row to mask gaps."""
    results = [assess_record(row) for row in rows]
    missing_counts = {field: 0 for field in DIRECT_PATHS}
    for result in results:
        for field in result["missing_fields"]:
            missing_counts[field] += 1
    return {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "records": len(results),
        "ready_records": sum(1 for r in results if r["ready"]),
        "all_records_ready": bool(results) and all(r["ready"] for r in results),
        "missing_counts": {k: v for k, v in missing_counts.items() if v},
        "results": results,
    }
