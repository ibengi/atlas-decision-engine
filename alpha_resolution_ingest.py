"""Append-only settlement ingestion for Atlas Alpha Learning. SHADOW ONLY.

This module accepts externally supplied settlement facts and attaches them to
existing immutable Alpha predictions. It has no broker imports, no execution
authority, and never edits a prediction row in place.

AA-15 -- R4 IS A VERIFIED JOIN, NOT A prediction_id LOOKUP
    The previous version joined a settlement to a prediction on
    `prediction_id` alone, and accepted any non-empty `source` string and any
    `resolved_at` at all -- `str(value).strip()`, which turns
    `"not a date"` into a stored resolution timestamp. A prediction_id is a
    20-hex-character opaque token; matching it proves that somebody quoted a
    token, not that this settlement describes the market that prediction was
    about.

    The chain is now:

        SOURCE RECORD  ->  VERIFIED PREDICTION  ->  TRUSTED RESOLUTION
        (record_sha256      (binding copied        (binding re-checked
         verified by the     into the prediction    against the prediction;
         consumer)          row at analysis)       mismatch = REJECT)

    Any binding field the settlement supplies is CHECKED against the
    prediction's own binding. A disagreement is a rejection, and the
    conflicting values are reported rather than discarded -- an operator
    needs to see what disagreed with what.

    Fields the settlement does NOT supply are simply not checked. That is a
    deliberate limit and it is stated plainly: this module can prove the
    settlement is consistent with the prediction it names, and it cannot prove
    the settlement came from the exchange. Source authority remains an
    external blocker (see `settlement_authority` in the report).
"""

from candidate_contract import ContractError, strict_text, strict_timestamp

#: Binding fields a settlement may carry. Each one, WHEN SUPPLIED, must agree
#: with the prediction. Mapping is settlement key -> prediction binding key.
BINDING_CHECKS = {
    "contract_id": "contract_id",
    "market_snapshot_id": "market_snapshot_id",
    "source_record_sha256": "record_sha256",
    "environment": "environment",
    "contract_schema": "contract_schema",
}


def _prediction_binding(prediction: dict) -> dict:
    """The identity a prediction was committed with.

    Falls back to the prediction's own top-level fields for rows written
    before `source_binding` existed, so an older prediction can still be
    settled on the fields it does carry rather than being unresolvable.
    """
    binding = dict(prediction.get("source_binding") or {})
    binding.setdefault("contract_id", prediction.get("contract_id"))
    binding.setdefault("market_snapshot_id",
                       prediction.get("market_snapshot_id"))
    return {k: v for k, v in binding.items() if v not in (None, "")}


def _normalise(row, index):
    """One settlement row, strictly typed, or raise (AA-02 types, AA-15)."""
    if not isinstance(row, dict):
        raise ValueError(f"row {index}: settlement must be an object")
    try:
        prediction_id = strict_text(row.get("prediction_id"),
                                    field="prediction_id", max_length=200)
    except ContractError as exc:
        raise ValueError(f"row {index}: {exc}")

    outcome = row.get("outcome")
    # `True`/`False` are accepted as YES/NO, but nothing else is coerced: a
    # string "1" is not an outcome, and `outcome in (0, 1)` would have let
    # `True` and `1.0` through as the same fact by accident.
    if not isinstance(outcome, (bool, int)) or isinstance(outcome, float) \
            or int(outcome) not in (0, 1):
        raise ValueError(f"row {index}: outcome must be 0 or 1, got "
                         f"{outcome!r}")

    try:
        source = strict_text(row.get("source"), field="source", max_length=300)
    except ContractError as exc:
        raise ValueError(f"row {index}: {exc}")

    resolved_at = row.get("resolved_at")
    if resolved_at is not None:
        # AA-15: "Do not accept malformed resolved_at." Previously any string
        # survived; a settlement timestamp that is not a timestamp makes every
        # time-ordered calibration statistic computed from it meaningless.
        try:
            resolved_at = strict_timestamp(resolved_at,
                                           field="resolved_at").isoformat()
        except ContractError as exc:
            raise ValueError(f"row {index}: {exc}")

    evidence_id = row.get("settlement_evidence_id")
    if evidence_id is not None:
        try:
            evidence_id = strict_text(evidence_id,
                                      field="settlement_evidence_id",
                                      max_length=300)
        except ContractError as exc:
            raise ValueError(f"row {index}: {exc}")

    supplied = {}
    for key in BINDING_CHECKS:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            supplied[key] = strict_text(value, field=key, max_length=300)
        except ContractError as exc:
            raise ValueError(f"row {index}: {exc}")

    return {
        "prediction_id": prediction_id,
        "outcome": int(bool(outcome)),
        "source": source,
        "resolved_at": resolved_at,
        "settlement_evidence_id": evidence_id,
        "supplied_binding": supplied,
    }


def _binding_mismatches(supplied: dict, committed: dict) -> list:
    """Every binding field the settlement and the prediction disagree on."""
    out = []
    for settlement_key, binding_key in BINDING_CHECKS.items():
        claimed = supplied.get(settlement_key)
        if claimed in (None, ""):
            continue
        known = committed.get(binding_key)
        if known in (None, ""):
            out.append({"field": settlement_key, "settlement_value": claimed,
                        "prediction_value": None,
                        "reason": "the prediction carries no such binding, so "
                                  "the claim cannot be corroborated"})
            continue
        if str(claimed) != str(known):
            out.append({"field": settlement_key, "settlement_value": claimed,
                        "prediction_value": str(known),
                        "reason": "settlement and prediction disagree"})
    return out


def ingest_settlements(ledger, settlements, *, trusted_sources=None) -> dict:
    """Append trusted settlement facts to an AlphaLedger.

    Returns an auditable summary. No exception from a malformed or conflicting
    feed row can cause an existing resolution to be changed: conflicts and
    rejects are reported and left unwritten.

    `trusted_sources`, when given, is an allow-list of settlement source
    names. It is OFF by default because no settlement authority has been
    qualified for this deployment yet; passing it is how an operator states
    which feed they have actually verified.
    """
    allowed = {str(s).strip() for s in trusted_sources} if trusted_sources \
        else None
    result = {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "received": 0,
        "appended": 0,
        "idempotent": 0,
        "rejected": [],
        "conflicts": [],
        "binding_mismatches": [],
        "resolved_prediction_ids": [],
        "trusted_sources_enforced": allowed is not None,
    }

    for index, raw in enumerate(settlements, start=1):
        result["received"] += 1
        try:
            row = _normalise(raw, index)
        except (TypeError, ValueError) as exc:
            result["rejected"].append({"row": index, "reason": str(exc)})
            continue

        prediction_id = row["prediction_id"]
        if allowed is not None and row["source"] not in allowed:
            result["rejected"].append({
                "row": index, "prediction_id": prediction_id,
                "reason": f"source {row['source']!r} is not in the trusted "
                          f"settlement source allow-list"})
            continue
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

        # AA-15. The join is verified here, not assumed from the id.
        committed = _prediction_binding(prediction)
        mismatches = _binding_mismatches(row["supplied_binding"], committed)
        if mismatches:
            detail = {"row": index, "prediction_id": prediction_id,
                      "mismatches": mismatches}
            result["binding_mismatches"].append(detail)
            result["rejected"].append({
                "row": index, "prediction_id": prediction_id,
                "reason": "settlement binding does not match the prediction",
                "mismatches": mismatches})
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
                binding={
                    "settlement_binding": row["supplied_binding"],
                    "settlement_evidence_id": row["settlement_evidence_id"],
                    "binding_verified": bool(row["supplied_binding"]),
                },
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
