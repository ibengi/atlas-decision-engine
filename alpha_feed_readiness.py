"""Fail-closed readiness check for wiring a read-only source into Alpha.

SHADOW ONLY. This module performs no I/O beyond data passed by the caller and
imports no execution/broker code. Its purpose is to prevent an integration
from inventing immutable snapshot facts that the source did not actually
record at decision time.

AA-07: READY NOW MEANS THE WHOLE CONTRACT, NOT A SHAPE
    The previous version reported ``ready: true`` for any row in which the
    required field NAMES were present. That is a shape check, and a shape
    check is exactly what a substituted default passes: ``resolution_source:
    "kalshi"``, ``volume: 0.0`` and a NO quote derived from the YES quote are
    all syntactically present. A row is now READY only when
    ``candidate_contract.validate_record`` accepts it in full --
    schema, recomputed checksum, strict field types, provenance container,
    semantic source binding, per-quote observed/derived verdict,
    ``unavailable_fields`` container and valid timestamps. There is no
    fallback path that can report readiness on less than that.

    The shape and prohibited-inference diagnostics below are KEPT, because
    they answer the operator's real question -- "what is this candidate
    source still missing, and what would I be tempted to invent" -- but they
    can no longer produce a READY verdict on their own.

A source is READY only when every field required by ``alpha_snapshot`` is
available directly. We deliberately do not derive a missing order-book side
from ``entry_ask``/``spread`` and do not derive an expiry from a timestamp plus
``minutes_remaining``. We also do not turn a ticker/strike into a question or
rename a generic liquidity score into exchange volume/open interest. Those
transformations would create facts that were never persisted by the producer
and would make later calibration look more certain than the evidence permits.
"""

from __future__ import annotations

from typing import Any

from candidate_contract import (FEED_SCHEMA, LEGACY_FEED_SCHEMAS,
                                      OPTIONAL_FIELDS, validate_record)

#: AA-08. `event_id` is OPTIONAL in the contract and in the producer, so it is
#: optional here too. Previously readiness counted it as missing and therefore
#: refused a source the producer and the consumer would both have accepted --
#: three components, two different answers to "is this record complete".
#: Absence is fine; a SUPPLIED value must still validate and carry provenance,
#: which `validate_record` enforces.
READINESS_OPTIONAL = frozenset(OPTIONAL_FIELDS)


# Target snapshot field -> literal source paths that can supply it without
# inference. Aliases are renames only; no arithmetic or market reconstruction.
DIRECT_PATHS = {
    "contract_id": ("contract_id", "ticker", "decision.contract_id", "decision.ticker"),
    "event_id": ("event_id", "decision.event_id"),
    "question": ("question", "title", "decision.question", "decision.title"),
    "resolution_rules": ("resolution_rules", "decision.resolution_rules"),
    "resolution_source": ("resolution_source", "decision.resolution_source"),
    "snapshot_time_utc": (
        "snapshot_time_utc", "emitted_at_utc", "recorded_at", "ts",
    ),
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


def _has_any(row: dict, paths: tuple[str, ...]) -> bool:
    return any(_present(_get_path(row, path)) for path in paths)


def assess_record(row: dict) -> dict:
    """Return a machine-readable, fail-closed source-compatibility verdict."""
    if not isinstance(row, dict):
        return {
            "ready": False,
            "direct_fields": {},
            "missing_fields": sorted(set(DIRECT_PATHS) - READINESS_OPTIONAL),
            "prohibited_inferences": [],
            "contract_errors": ["record is not an object"],
            "reason": "record is not an object",
        }

    direct = {}
    missing = []
    for target, paths in DIRECT_PATHS.items():
        hit = next(((path, _get_path(row, path)) for path in paths
                    if _present(_get_path(row, path))), None)
        if hit is None:
            if target not in READINESS_OPTIONAL:      # AA-08
                missing.append(target)
        else:
            direct[target] = {"source_path": hit[0], "value_present": True}

    prohibited = []

    # A record from the neutral research producer carries its own provenance:
    # which source key each fact was read from, and which facts the source
    # genuinely did not record. When it is present it OVERRIDES the path
    # search above, because a value being syntactically present is not the
    # same as it having been observed -- that gap is precisely how a
    # substituted default ("kalshi", 0.0, the ticker) passes a shape check.
    provenance = row.get("field_provenance")
    unavailable = row.get("unavailable_fields")
    if isinstance(provenance, dict) and isinstance(unavailable, list):
        unattributed = []
        for target in DIRECT_PATHS:
            if target == "snapshot_time_utc":
                continue          # supplied by the producer's emission clock
            if target in READINESS_OPTIONAL and not _present(row.get(target)):
                continue          # AA-08: absent optional field is not a gap
            attributed = str(provenance.get(target) or "").strip()
            if target in unavailable or not attributed:
                unattributed.append(target)
                direct.pop(target, None)
                if target not in missing:
                    missing.append(target)
            else:
                direct.setdefault(target, {})["provenance"] = attributed
        if unattributed:
            prohibited.append({
                "would_infer": sorted(unattributed),
                "from": ["producer default / unrecorded source fact"],
                "reason": "the producer did not attribute these to an observed source key",
            })

    # Common temptation in the currently deployed /decisions surface:
    # reconstruct a book from one chosen-side ask plus spread. Refuse it.
    if _has_any(row, ("entry_ask", "decision.entry_ask", "spread", "decision.spread")):
        absent_book = [f for f in ("yes_bid", "yes_ask", "no_bid", "no_ask")
                       if f in missing]
        if absent_book:
            prohibited.append({
                "would_infer": absent_book,
                "from": ["entry_ask", "spread"],
                "reason": "one chosen-side quote cannot prove the full contemporaneous book",
            })

    # A relative horizon is useful model evidence, but it is not the
    # producer-persisted absolute close/resolution timestamp the immutable
    # snapshot binds. This covers both /decisions and shadow_predictions.
    has_relative_horizon = _has_any(row, (
        "minutes_remaining",
        "features.minutes_remaining",
        "model_output.features.minutes_remaining",
        "decision.model_output.features.minutes_remaining",
    ))
    has_observation_time = _has_any(row, (
        "recorded_at", "ts", "snapshot_time_utc", "emitted_at_utc",
    ))
    if has_relative_horizon and has_observation_time:
        absent_times = [f for f in (
            "market_close_time_utc", "expected_resolution_time_utc") if f in missing]
        if absent_times:
            prohibited.append({
                "would_infer": absent_times,
                "from": ["observation timestamp", "minutes_remaining"],
                "reason": "derived market timing is not producer-persisted decision-time evidence",
            })

    # Historical settlement time is known only after the outcome. It cannot
    # be repurposed as the decision-time expected resolution timestamp.
    if _present(_get_path(row, "settled_at")) and (
            "market_close_time_utc" in missing or
            "expected_resolution_time_utc" in missing):
        prohibited.append({
            "would_infer": [f for f in (
                "market_close_time_utc", "expected_resolution_time_utc") if f in missing],
            "from": ["settled_at"],
            "reason": "post-outcome settlement evidence cannot define a pre-outcome snapshot deadline",
        })

    # Ticker/strike may let a human describe a contract, but parsing them into
    # a canonical question or event id would be reconstruction, not a rename.
    if "question" in missing and _has_any(row, (
            "ticker", "contract_id", "strike", "features.strike")):
        prohibited.append({
            "would_infer": ["question"],
            "from": ["ticker", "strike"],
            "reason": "contract text must come from the producer/exchange, not ticker parsing",
        })

    # A ranker/liquidity score is not exchange volume or open interest.
    if _has_any(row, ("liquidity", "decision.liquidity", "ranker_score")):
        absent_sizes = [f for f in ("volume", "open_interest") if f in missing]
        if absent_sizes:
            prohibited.append({
                "would_infer": absent_sizes,
                "from": ["liquidity/ranker score"],
                "reason": "a generic liquidity metric is not observed exchange volume/open interest",
            })

    # AA-07. The verdict. `ready` is decided by the SHARED contract and by
    # nothing else; the shape analysis above only explains WHY.
    contract_errors = validate_record(row)
    schema = row.get("schema")
    if schema in LEGACY_FEED_SCHEMAS:
        contract_reason = (f"{schema!r} is a refused legacy schema; its facts "
                           f"may be substituted and must be re-observed")
    elif schema != FEED_SCHEMA:
        contract_reason = (f"record does not carry the {FEED_SCHEMA!r} "
                           f"contract (schema, recomputed checksum, "
                           f"provenance and per-quote observation are all "
                           f"required before Alpha may ingest it)")
    elif contract_errors:
        contract_reason = "; ".join(contract_errors)
    else:
        contract_reason = ""

    ready = not missing and not contract_errors
    if ready:
        reason = "ready"
    elif contract_errors:
        reason = contract_reason
    else:
        reason = "source lacks immutable snapshot facts"
    return {
        "ready": ready,
        "direct_fields": direct,
        "missing_fields": sorted(missing),
        "prohibited_inferences": prohibited,
        "contract_errors": contract_errors,
        "reason": reason,
    }


def assess_records(rows: list[dict]) -> dict:
    """Aggregate readiness without allowing one complete row to mask gaps."""
    results = [assess_record(row) for row in rows]
    missing_counts = {field: 0 for field in DIRECT_PATHS}
    for result in results:
        for field in result["missing_fields"]:
            missing_counts[field] = missing_counts.get(field, 0) + 1
    return {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "records": len(results),
        "ready_records": sum(1 for r in results if r["ready"]),
        "all_records_ready": bool(results) and all(r["ready"] for r in results),
        "missing_counts": {k: v for k, v in missing_counts.items() if v},
        # AA-07: a contract violation anywhere is visible in the aggregate,
        # so a caller reading only the summary cannot miss it.
        "contract_violations": sum(1 for r in results
                                   if r.get("contract_errors")),
        "results": results,
    }
