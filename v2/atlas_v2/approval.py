"""External signature and exact artifact binding, not a research-controlled flag.

No trusted approver key or financial permission ships with V2. This verifier can
authenticate a separately supplied review artifact; live capability stays absent.
The operator must authenticate the issuer/key outside the research process.
"""
import base64
from .domain import Refused, canonical, hash_id, utc


def verify_review(envelope, trusted_public_key, expected_bindings, at):
    required = {"source", "model", "features", "thresholds", "config", "dataset",
                "lock", "validation", "independent_review", "cost_receipts"}
    if not trusted_public_key or set(expected_bindings) != required:
        raise Refused("external review authority/binding absent")
    for value in expected_bindings.values():
        hash_id(value)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise Refused("review envelope")
    payload = envelope["payload"]
    if (not isinstance(payload, dict) or set(payload) != {"purpose", "bindings", "issued_at", "expires_at"}
            or payload["purpose"] != "ATLAS_V2_INDEPENDENT_RESEARCH_REVIEW"
            or payload["bindings"] != expected_bindings
            or not utc(payload["issued_at"]) <= utc(at) < utc(payload["expires_at"])):
        raise Refused("review binding/purpose/validity mismatch")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        signature = base64.b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(signature, canonical(payload))
    except Exception as exc:
        raise Refused("review signature unavailable/invalid") from exc
    return {"review_authenticated": True, "financial_authority": False, "capital": "OFF"}

