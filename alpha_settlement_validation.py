"""Pure, shared qualification of retained research and settlement evidence.

SHADOW ONLY. Integrity and an operator-qualified authority policy are separate:
matching hashes do not authenticate an exchange, and a recorded trust flag does
not substitute for checking the authority against the recorded allow-list.
"""

import json
from datetime import datetime, timezone

from alpha_snapshot import snapshot_from_dict
from candidate_contract import (FEED_SCHEMA, strict_text, strict_timestamp,
                                validate_record, verify_checksum)


BINDING_CHECKS = {
    "contract_id": "contract_id",
    "market_snapshot_id": "market_snapshot_id",
    "source_record_sha256": "record_sha256",
    "environment": "environment",
    "contract_schema": "contract_schema",
}


def _utc_now():
    return datetime.now(timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _instant(value, field):
    return strict_timestamp(value, field=field)


def _identity(value, field, maximum=300):
    text = strict_text(value, field=field, max_length=maximum)
    if value != text or any(ord(character) < 32 or ord(character) == 127
                            for character in text):
        raise ValueError(field + " is not a canonical evidence identity")
    return text


def _second(value, field):
    return _instant(value, field).isoformat(timespec="seconds")


def validate_source_snapshot(record, snapshot):
    """Check the checksum, source schema, snapshot identity, and economic facts.

    Derivative scheduling policy is already bound by the snapshot's own hash;
    replay does not rebuild its deadline using today's configurable policy.
    Source facts are compared independently, so matching labels or a copied
    snapshot ID cannot conceal different prices, times, rules or provenance.
    """
    try:
        if not isinstance(record, dict):
            raise ValueError("source evidence must be an object")
        verify_checksum(record)
        errors = validate_record(record)
        if errors:
            raise ValueError("source contract invalid: " + "; ".join(errors))
        payload = snapshot.as_dict() if hasattr(snapshot, "as_dict") else snapshot
        observed = snapshot_from_dict(payload).as_dict()
        expected = {
            field: record.get(field, "")
            for field in ("contract_id", "event_id", "question",
                          "resolution_rules", "resolution_source")
        }
        # event_id is explicitly optional in the source contract. The
        # snapshot represents genuine missing/null absence as empty text;
        # required facts and contradictory provided values are never coerced.
        if expected["event_id"] is None:
            expected["event_id"] = ""
        expected.update({field: float(record[field]) for field in
                         ("yes_bid", "yes_ask", "no_bid", "no_ask",
                          "volume", "open_interest")})
        expected.update({
            "spread": round(float(record["yes_ask"]) - float(record["yes_bid"]), 6),
            "snapshot_time_utc": _second(record["emitted_at_utc"], "emitted_at_utc"),
            "market_close_time_utc": _second(record["market_close_time_utc"], "market_close_time_utc"),
            "expected_resolution_time_utc": _second(record["expected_resolution_time_utc"], "expected_resolution_time_utc"),
            "next_known_catalyst": {
                "name": record.get("catalyst_name", "") or "",
                "time_utc": _second(record["catalyst_time_utc"], "catalyst_time_utc")
                if record.get("catalyst_time_utc") else None,
            },
        })
        for field, value in expected.items():
            if _canonical(observed.get(field)) != _canonical(value):
                raise ValueError("source and snapshot disagree on " + field)
    except Exception as exc:
        return False, "source/snapshot qualification failed: " + str(exc)[:500]
    return True, ""


def verify_source_evidence(prediction):
    """Verify the retained preimage and its exact economic snapshot binding."""
    verdict = {"verified": False, "claimed": "", "recomputed": None,
               "reason": ""}
    try:
        if not isinstance(prediction, dict):
            raise ValueError("prediction must be an object")
        binding = prediction.get("source_binding")
        if not isinstance(binding, dict):
            raise ValueError("prediction carries no source evidence: source binding must be an object")
        claimed = binding.get("record_sha256")
        verdict["claimed"] = claimed
        evidence = binding.get("source_evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("prediction carries no source evidence: retained preimage is absent")
        record = {**evidence, "record_sha256": claimed}
        verdict["recomputed"] = verify_checksum(record)
        qualified, reason = validate_source_snapshot(record, prediction.get("snapshot"))
        if not qualified:
            raise ValueError(reason)
        snapshot = prediction["snapshot"]
        for key in ("contract_id", "market_snapshot_id"):
            known = strict_text(snapshot.get(key), field=key, max_length=300)
            if binding.get(key) != known or prediction.get(key) != known:
                raise ValueError("prediction/source/snapshot disagree on " + key)
        if binding.get("contract_schema") != FEED_SCHEMA:
            raise ValueError("source binding contract schema is unsupported")
        strict_text(binding.get("environment"), field="environment", max_length=100)
        verdict["verified"] = True
    except Exception as exc:
        verdict["reason"] = str(exc)[:700]
    return verdict


def settlement_qualification(prediction, resolution, *, now=None):
    """One semantic gate used both before append and during historical replay.

    No stored flag can establish this verdict. Historical malformed rows remain
    available in the audit view but never enter calibration or learning.
    """
    try:
        if not isinstance(prediction, dict) or not isinstance(resolution, dict):
            raise ValueError("prediction and resolution must be objects")
        for field in ("binding_verified", "source_trusted", "source_evidence_verified"):
            if resolution.get(field) is not True:
                raise ValueError(field + " must be the boolean true")
        if resolution.get("quarantined", False) is not False:
            raise ValueError("quarantined settlement cannot qualify")
        if prediction.get("schema") != "atlas-alpha-ledger-v1" or \
                resolution.get("schema") != "atlas-alpha-ledger-v1":
            raise ValueError("unsupported prediction or resolution ledger schema")
        if prediction.get("kind") != "PREDICTION" or resolution.get("kind") != "RESOLUTION":
            raise ValueError("unexpected prediction or resolution row kind")
        pid = _identity(prediction.get("prediction_id"), "prediction_id", 200)
        if resolution.get("prediction_id") != pid:
            raise ValueError("resolution prediction identity differs")
        if type(resolution.get("actual_outcome")) is not int or resolution["actual_outcome"] not in (0, 1):
            raise ValueError("resolution outcome must be integer zero or one")
        evidence = verify_source_evidence(prediction)
        if not evidence["verified"]:
            raise ValueError("source evidence unverified: " + evidence["reason"])
        # V4 normalization could discard unsupported source structure before
        # hashing it. Such history remains readable, but a rendered label
        # cannot retroactively prove the raw source identity that was lost.
        from source_identity import verify_settlement_source_evidence
        source_identity = verify_settlement_source_evidence(
            prediction["source_binding"]["source_evidence"])
        if not source_identity["verified"]:
            raise ValueError("settlement-source preimage unverified: " +
                             source_identity["reason"])
        supplied = resolution.get("settlement_binding")
        if not isinstance(supplied, dict):
            raise ValueError("settlement binding must be an object")
        for field, binding_key in BINDING_CHECKS.items():
            value = strict_text(supplied.get(field), field=field, max_length=300)
            if value != supplied[field] or value != prediction["source_binding"].get(binding_key):
                raise ValueError("settlement binding differs on " + field)
        authority = _identity(resolution.get("resolution_source"), "resolution_source")
        allowed = resolution.get("trusted_sources")
        if not isinstance(allowed, list) or not allowed or any(
                not isinstance(item, str) or not item.strip() or item != item.strip()
                for item in allowed):
            raise ValueError("recorded settlement authority allow-list is missing or malformed")
        if authority != resolution["resolution_source"] or authority not in allowed:
            raise ValueError("settlement authority is not in the recorded allow-list")
        _identity(resolution.get("settlement_evidence_id"), "settlement_evidence_id")
        predicted_at = _instant(prediction.get("prediction_time"), "prediction_time")
        resolved_at = _instant(resolution.get("resolved_at"), "resolved_at")
        observed_at = _instant(prediction["snapshot"]["snapshot_time_utc"], "snapshot_time_utc")
        current = _utc_now() if now is None else now
        if current.tzinfo is None:
            raise ValueError("qualification clock must be timezone aware")
        if predicted_at < observed_at:
            raise ValueError("prediction precedes its source observation")
        if resolved_at <= predicted_at:
            raise ValueError("resolution must strictly follow prediction")
        if resolved_at > current or predicted_at > current:
            raise ValueError("prediction or resolution is in the future")
    except Exception as exc:
        return False, str(exc)[:900]
    return True, ""
