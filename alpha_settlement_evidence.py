"""Retained settlement receipt integrity. SHADOW_ONLY, no external I/O.

The caller installs an independently qualified authority policy. Hashes and
policy membership prove the binding under that policy, not exchange origin.
No real settlement authority or credential is installed by this module.
"""

import base64
import hashlib
import json
import re

from candidate_contract import strict_text, strict_timestamp


EVIDENCE_SCHEMA = "atlas-alpha-settlement-evidence-v1"
RESPONSE_SCHEMA = "atlas-alpha-settlement-response-v1"
POLICY_SCHEMA = "atlas-alpha-settlement-authority-policy-v1"
MAX_PREIMAGE_BYTES = 65536
BINDING_FIELDS = (
    "prediction_id", "contract_id", "market_snapshot_id",
    "source_record_sha256", "environment", "contract_schema_version",
)
EVIDENCE_FIELDS = {
    "schema", "binding", "settlement_authority",
    "settlement_response_sha256", "settlement_evidence_id",
}
RESPONSE_FIELDS = {
    "schema", "contract_id", "environment", "contract_schema_version",
    "settlement_authority", "outcome", "resolved_at", "authority_record_id",
}
RETAINED_FIELDS = (
    "contract_schema_version", "settlement_authority", "settlement_evidence",
    "settlement_response_sha256", "settlement_response_preimage",
    "settlement_response_bytes_base64", "settlement_authority_policy",
)
INPUT_FIELDS = {
    "prediction_id", "outcome", "source", "resolved_at", "contract_id",
    "market_snapshot_id", "source_record_sha256", "environment",
    "contract_schema", "contract_schema_version", "settlement_authority",
    "settlement_evidence_id", "settlement_evidence",
    "settlement_response_sha256", "settlement_response_preimage",
    "settlement_response_bytes_base64", "binding_verified", "source_trusted",
    "source_evidence_verified", "quarantined",
}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value, field, maximum=300):
    parsed = strict_text(value, field=field, max_length=maximum)
    if value != parsed or any(ord(c) < 32 or ord(c) == 127 for c in parsed):
        raise ValueError(field + " is not a canonical identity")
    return parsed


def _digest(value, field):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(field + " must be 64 lowercase hexadecimal characters")
    return value


def _object(value, fields, field):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(field + " has missing or unsupported fields")


def authority_policy(authorities):
    """Snapshot the caller's policy, never a policy supplied by the feed row."""
    if not isinstance(authorities, (list, tuple, set, frozenset)) or not authorities:
        raise ValueError("no settlement authority policy is installed")
    names = sorted({_text(name, "settlement authority") for name in authorities})
    body = {"schema": POLICY_SCHEMA, "authorities": names}
    return {**body, "policy_id": "sha256:" + _hash(body)}


def evidence_identity(evidence):
    """Content identity binds the response digest, authority and full join."""
    material = {key: value for key, value in evidence.items()
                if key != "settlement_evidence_id"}
    return "sha256:" + _hash(material)


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate response field: " + key)
        obj[key] = value
    return obj


def _nonfinite(value):
    raise ValueError("non-finite response number: " + value)


def verify_settlement_evidence(prediction, resolution):
    """Raise unless the entire retained evidence chain independently verifies.

    Called only by the shared settlement_qualification gate, both at intake
    and replay. All facts here are retained; no digest is treated as proof of
    unavailable bytes and no remote request is made during replay.
    """
    source = prediction["source_binding"]
    binding = resolution["settlement_binding"]
    _object(binding, {"contract_id", "market_snapshot_id", "source_record_sha256",
                      "environment", "contract_schema", "contract_schema_version"},
            "settlement binding")
    expected = {
        "prediction_id": prediction["prediction_id"],
        "contract_id": source["contract_id"],
        "market_snapshot_id": source["market_snapshot_id"],
        "source_record_sha256": source["record_sha256"],
        "environment": source["environment"],
        "contract_schema_version": source["contract_schema"],
    }
    for key, value in expected.items():
        _text(value, key)
    _digest(expected["source_record_sha256"], "source_record_sha256")
    version = _text(resolution.get("contract_schema_version"), "contract_schema_version")
    if version != expected["contract_schema_version"] or binding.get("contract_schema") != version:
        raise ValueError("contract schema version disagrees with the prediction")

    evidence = resolution.get("settlement_evidence")
    _object(evidence, EVIDENCE_FIELDS, "settlement evidence")
    if evidence["schema"] != EVIDENCE_SCHEMA:
        raise ValueError("unsupported settlement evidence schema")
    _object(evidence["binding"], BINDING_FIELDS, "settlement evidence binding")
    for key, value in expected.items():
        if _text(evidence["binding"][key], key) != value:
            raise ValueError("settlement evidence binding differs on " + key)

    authority = _text(resolution.get("settlement_authority"), "settlement_authority")
    if authority != resolution.get("resolution_source") or \
            _text(evidence["settlement_authority"], "evidence authority") != authority:
        raise ValueError("settlement authority binding disagrees")
    policy = resolution.get("settlement_authority_policy")
    _object(policy, {"schema", "authorities", "policy_id"}, "settlement authority policy")
    recorded = resolution.get("trusted_sources")
    if type(recorded) is not list or recorded != sorted(set(recorded)):
        raise ValueError("authority policy must have a canonical authority list")
    if policy != authority_policy(recorded) or authority not in policy["authorities"]:
        raise ValueError("settlement authority is outside the recorded qualified policy")

    preimage = resolution.get("settlement_response_preimage")
    if type(preimage) is not str:
        raise ValueError("settlement response preimage must be retained canonical JSON text")
    raw = preimage.encode("utf-8")
    if not raw or len(raw) > MAX_PREIMAGE_BYTES:
        raise ValueError("settlement response preimage is absent or too large")
    response = json.loads(preimage, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    _object(response, RESPONSE_FIELDS, "settlement response")
    if canonical_json(response) != preimage:
        raise ValueError("settlement response is not canonical JSON")
    if response["schema"] != RESPONSE_SCHEMA:
        raise ValueError("unsupported settlement response schema")
    _text(response["authority_record_id"], "authority_record_id")
    if "settlement_response_bytes_base64" in resolution:
        encoded = resolution["settlement_response_bytes_base64"]
        if type(encoded) is not str or len(encoded) > 4 * ((MAX_PREIMAGE_BYTES + 2) // 3):
            raise ValueError("invalid encoded settlement response preimage")
        decoded = base64.b64decode(encoded, validate=True)
        if decoded != raw or base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError("encoded settlement response preimage disagrees")

    supplied_digest = _digest(resolution.get("settlement_response_sha256"), "settlement_response_sha256")
    inner_digest = _digest(evidence["settlement_response_sha256"], "evidence response digest")
    # Independent byte recomputation is mandatory; matching two claims is not proof.
    recomputed = hashlib.sha256(raw).hexdigest()
    if supplied_digest != recomputed or inner_digest != recomputed:
        raise ValueError("settlement response digest does not match retained preimage")
    expected_id = evidence_identity(evidence)
    if _text(resolution.get("settlement_evidence_id"), "settlement_evidence_id") != expected_id or \
            _text(evidence["settlement_evidence_id"], "evidence identity") != expected_id:
        raise ValueError("settlement evidence identity does not match the complete evidence object")

    for key in ("contract_id", "environment", "contract_schema_version"):
        if _text(response[key], "response " + key) != expected[key]:
            raise ValueError("settlement response disagrees on " + key)
    if _text(response["settlement_authority"], "response authority") != authority:
        raise ValueError("settlement response authority disagrees")
    if type(response["outcome"]) is not int or response["outcome"] not in (0, 1) or \
            response["outcome"] != resolution["actual_outcome"]:
        raise ValueError("settlement outcome is not supported by retained response")
    resolved_at = _text(response["resolved_at"], "response resolution timestamp")
    strict_timestamp(resolved_at, field="response resolution timestamp")
    if resolved_at != resolution["resolved_at"]:
        raise ValueError("resolution timestamp is not supported by retained response")
