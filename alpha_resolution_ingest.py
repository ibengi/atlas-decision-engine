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

    RE-AUDIT: "NOT SUPPLIED" IS NOT "NOT CHECKED"
        The previous rule checked every binding field the settlement supplied
        and left the rest alone. Written out, that means a settlement
        supplying NO binding at all passed every check there was -- and a
        `prediction_id` is an opaque token, so matching it proves somebody
        quoted a token, not that this settlement describes that market.

        `REQUIRED_BINDING` is therefore required. A settlement missing any of
        it is QUARANTINED, with the missing names reported, and no resolution
        is written. Partial agreement is not partial proof; it is no proof
        with some corroboration attached.

    RE-AUDIT: AN UNQUALIFIED AUTHORITY IS REFUSED, NOT TRUSTED
        `trusted_sources` used to be off by default, and off meant accept
        anything. The reason given for it being off -- no settlement
        authority has been qualified for this deployment -- is an argument
        for the opposite behaviour. The default is now REFUSE, and an
        operator names the feed they actually verified.

    What this module still cannot do is prove the settlement came from the
    exchange. It proves the settlement is consistent with the prediction it
    names and that its source is one an operator named. Source authority
    remains an external blocker (see `settlement_authority` in the report).
"""

from alpha_ledger import verify_source_evidence
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

#: The binding a settlement MUST carry. Each one answers a different question,
#: and dropping any of them leaves a join that cannot be defended:
#:
#:   contract_id           which market this outcome is about
#:   market_snapshot_id    which observation of it the prediction was made on
#:   source_record_sha256  which exact evidence bytes that observation was
#:
#: RA-11 -- AND THE OTHER TWO, AND THE TWO THAT WERE NOT IN THE BINDING.
#:
#: `environment` and `contract_schema` were left optional with the reasoning
#: that they "narrow a match when present and their absence does not make the
#: join ambiguous". Written out, that means:
#:
#:   environment       a settlement from DEMO could be attached to a
#:                     prediction made in PROD, or the reverse, and nothing in
#:                     the chain would notice. The environment is not a
#:                     narrowing detail; it is WHICH market this outcome is
#:                     about.
#:   contract_schema   a settlement could be attached across a CONTRACT
#:                     VERSION boundary. That field is in the binding
#:                     precisely because v2 and v3 records make different
#:                     claims about the same field names -- which is why v2
#:                     records are refused rather than migrated.
REQUIRED_BINDING = ("contract_id", "market_snapshot_id",
                    "source_record_sha256", "environment",
                    "contract_schema")

#: Settlement fields that are not part of the binding and were required by
#: nothing at all (RA-11):
#:
#:   resolved_at              absent, `AlphaLedger.resolve` filled in
#:                            `_now_iso()`. The stored "resolution time" was
#:                            then the INGESTION time, so every time-ordered
#:                            calibration statistic computed from it measured
#:                            when somebody ran a script.
#:   settlement_evidence_id   absent, the resolution recorded no external
#:                            identity, so the outcome could never be traced
#:                            back to the document that established it.
#:
#: Missing or null is a QUARANTINE, not a rejection: the row is not malformed,
#: it simply cannot be tied to the prediction it names.
REQUIRED_SETTLEMENT_FIELDS = ("resolved_at", "settlement_evidence_id")


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

    # RA-11: ABSENT and MALFORMED are different outcomes for these two, and
    # a blank string means absent -- exactly as it already does for every
    # binding key below. An absent one falls through to the QUARANTINE in
    # `ingest_settlements`, where an operator sees "this settlement cannot be
    # tied to anything"; a PRESENT one that will not parse is a malformed row
    # and is REJECTED here, which is what AA-15 asked for.
    resolved_at = row.get("resolved_at")
    if isinstance(resolved_at, str) and not resolved_at.strip():
        resolved_at = None
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
    if isinstance(evidence_id, str) and not evidence_id.strip():
        evidence_id = None
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


def _missing_settlement_fields(row: dict) -> list:
    """Required non-binding settlement fields that are absent or null (RA-11)."""
    return sorted(field for field in REQUIRED_SETTLEMENT_FIELDS
                  if not str(row.get(field) or "").strip())


def _missing_binding(supplied: dict, committed: dict) -> list:
    """Required binding fields absent from EITHER side.

    A field the prediction does not carry is just as disqualifying as one the
    settlement omits: there is nothing to corroborate against, and "nothing
    disagreed" is not the same as "they agreed".
    """
    return sorted(field for field in REQUIRED_BINDING
                  if not str(supplied.get(field) or "").strip()
                  or not str(committed.get(BINDING_CHECKS[field]) or "").strip())


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

    `trusted_sources` is the allow-list of settlement source names. It is
    REQUIRED: with none supplied, no source has been qualified for this
    deployment and every row is refused. Passing it is how an operator states
    which feed they have actually verified, and the statement is preserved
    into the resolution row so a calibration number can be traced back to the
    authority that produced its outcomes.
    """
    allowed = {str(s).strip() for s in trusted_sources} if trusted_sources \
        else set()
    result = {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "received": 0,
        "appended": 0,
        "idempotent": 0,
        "rejected": [],
        "conflicts": [],
        "binding_mismatches": [],
        # Re-audit: incomplete binding is its own outcome. It is not a
        # malformed row and it is not a disagreement -- it is a settlement
        # that cannot be tied to the prediction it names, and an operator
        # needs to see those separately from rows that actively conflict.
        "quarantined": [],
        # RA-12: settlements refused because the prediction's own retained
        # evidence did not recompute. Its own bucket, because it is a
        # statement about the LEDGER rather than about the feed.
        "evidence_unverified": [],
        "resolved_prediction_ids": [],
        "trusted_sources_enforced": True,
        "trusted_sources": sorted(allowed),
    }

    for index, raw in enumerate(settlements, start=1):
        result["received"] += 1
        try:
            row = _normalise(raw, index)
        except (TypeError, ValueError) as exc:
            result["rejected"].append({"row": index, "reason": str(exc)})
            continue

        prediction_id = row["prediction_id"]
        if not allowed:
            # Re-audit: no allow-list means no authority has been qualified,
            # which is a reason to accept NOTHING rather than everything.
            result["rejected"].append({
                "row": index, "prediction_id": prediction_id,
                "reason": "no settlement authority has been qualified for "
                          "this deployment; pass trusted_sources naming the "
                          "feed you have verified"})
            continue
        if row["source"] not in allowed:
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
        missing = _missing_binding(row["supplied_binding"], committed)
        absent = _missing_settlement_fields(row)
        if missing or absent:
            detail = {"row": index, "prediction_id": prediction_id,
                      "missing_binding": missing,
                      "missing_fields": absent,
                      "reason": "settlement binding is incomplete; a "
                                "prediction_id alone does not identify the "
                                "market an outcome is about, and an outcome "
                                "with no resolution instant or evidence "
                                "identity cannot be traced to anything"}
            result["quarantined"].append(detail)
            result["rejected"].append(dict(detail))
            continue
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

        # RA-12 -- RECOMPUTE THE EVIDENCE, DO NOT COMPARE TWO COPIES OF THE
        # CLAIM ABOUT IT.
        #
        # The check above compares the settlement's `source_record_sha256`
        # against the prediction's `record_sha256`. Those are two copies of
        # the SAME claim: agreement says the settlement quoted the digest
        # correctly, and says nothing about whether that digest describes the
        # evidence the prediction actually carries -- which is the only
        # question the retained copy exists to answer, and the reason
        # `verify_source_evidence` was written in v3.
        #
        # It was never called from here, so a prediction whose evidence had
        # been dropped, or edited after the fact, was settled and QUALIFIED
        # exactly like one whose evidence recomputes. The recomputation is a
        # precondition now, and its verdict travels into the resolution row.
        evidence = verify_source_evidence(prediction)
        if not evidence["verified"]:
            detail = {"row": index, "prediction_id": prediction_id,
                      "source_evidence": evidence,
                      "reason": f"the prediction's retained source evidence "
                                f"does not independently recompute to the "
                                f"digest it claims, so this settlement "
                                f"cannot be qualified: {evidence['reason']}"}
            result["evidence_unverified"].append(detail)
            result["quarantined"].append(dict(detail))
            result["rejected"].append(dict(detail))
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
                    # Every REQUIRED field was present on both sides and every
                    # supplied field agreed. That is what this flag now means;
                    # previously it meant "some binding was supplied".
                    "binding_verified": True,
                    # RA-12: the independent recomputation, recorded with the
                    # outcome so a calibration number can be defended
                    # without re-deriving it.
                    "source_evidence_verified": True,
                    "source_record_sha256_recomputed": evidence["recomputed"],
                    # Re-audit: the trust decision travels WITH the outcome.
                    # A calibration number computed from these rows can then
                    # be traced back to the authority an operator named,
                    # rather than to an unqualified string.
                    "source_trusted": True,
                    "trusted_sources": sorted(allowed),
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
