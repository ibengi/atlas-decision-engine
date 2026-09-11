"""Append-only settlement ingestion for Atlas Alpha Learning. SHADOW ONLY.

This module accepts externally supplied settlement facts and attaches them to
existing immutable Alpha predictions. It has no broker imports, no execution
authority, and never edits a prediction row in place.

The caller is responsible for supplying a trusted read-only settlement feed.
This module is the integrity boundary between that feed and AlphaLedger:
unknown predictions are rejected, duplicate matching resolutions are
idempotent, and conflicting outcomes are surfaced without overwriting history.
"""


def _normalise(row, index):
    if not isinstance(row, dict):
        raise ValueError(f"row {index}: settlement must be an object")
    prediction_id = str(row.get("prediction_id") or "").strip()
    if not prediction_id:
        raise ValueError(f"row {index}: prediction_id is required")
    outcome = row.get("outcome")
    if outcome not in (0, 1, False, True):
        raise ValueError(f"row {index}: outcome must be 0 or 1")
    source = str(row.get("source") or "").strip()
    if not source:
        raise ValueError(f"row {index}: source is required")
    resolved_at = row.get("resolved_at")
    if resolved_at is not None:
        resolved_at = str(resolved_at).strip() or None
    return {
        "prediction_id": prediction_id,
        "outcome": int(bool(outcome)),
        "source": source[:300],
        "resolved_at": resolved_at,
    }


def ingest_settlements(ledger, settlements) -> dict:
    """Append trusted settlement facts to an AlphaLedger.

    Returns an auditable summary. No exception from a malformed or conflicting
    feed row can cause an existing resolution to be changed: conflicts and
    rejects are reported and left unwritten.
    """
    result = {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "received": 0,
        "appended": 0,
        "idempotent": 0,
        "rejected": [],
        "conflicts": [],
        "resolved_prediction_ids": [],
    }

    for index, raw in enumerate(settlements, start=1):
        result["received"] += 1
        try:
            row = _normalise(raw, index)
        except (TypeError, ValueError) as exc:
            result["rejected"].append({"row": index, "reason": str(exc)})
            continue

        prediction_id = row["prediction_id"]
        try:
            prediction = ledger.find_prediction(prediction_id)
        except Exception as exc:  # read failure must not become a write
            result["rejected"].append({
                "row": index,
                "prediction_id": prediction_id,
                "reason": f"ledger read failed: {type(exc).__name__}: {exc}",
            })
            continue
        if prediction is None:
            result["rejected"].append({
                "row": index,
                "prediction_id": prediction_id,
                "reason": "unknown prediction_id",
            })
            continue

        existing = ledger.find_resolution(prediction_id)
        if existing is not None:
            existing_outcome = int(existing.get("actual_outcome"))
            if existing_outcome == row["outcome"]:
                result["idempotent"] += 1
                continue
            result["conflicts"].append({
                "row": index,
                "prediction_id": prediction_id,
                "existing_outcome": existing_outcome,
                "incoming_outcome": row["outcome"],
            })
            continue

        try:
            ledger.resolve(
                prediction_id,
                row["outcome"],
                resolved_at=row["resolved_at"],
                source=row["source"],
            )
        except Exception as exc:
            result["rejected"].append({
                "row": index,
                "prediction_id": prediction_id,
                "reason": f"append failed: {type(exc).__name__}: {exc}",
            })
            continue
        result["appended"] += 1
        result["resolved_prediction_ids"].append(prediction_id)

    return result
