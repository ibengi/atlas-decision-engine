"""Release-pinned provider evidence, never an operator scope override.

Enrollment requires review of the original redacted provider artifact and its
authenticated origin. Pin the canonical manifest only AFTER that review; see
SPORTS_SCOPE_EVIDENCE.md. No real provider artifact has yet been enrolled.
"""
import hashlib
import hmac
import re

from .domain import Refused, canonical, strict_json, utc

ENV_NAME = "ATLAS_V2_SPORTS_SCOPE_EVIDENCE"
MAX_BYTES = 8192
MAX_VALIDITY_SECONDS = 86400
# Code-reviewed trust roots. Never load this allowlist from environment/volume.
REVIEWED_EVIDENCE_SHA256 = frozenset()
FIELDS = {"schema", "provider", "source_url", "artifact_sha256",
          "key_id_sha256", "scopes", "write_allowed", "trade_allowed",
          "transfer_allowed", "observed_at", "expires_at", "origin_receipt_sha256"}
SOURCES = {"https://kalshi.com/account/profile",
           "https://external-api.kalshi.com/trade-api/v2/api_keys"}


def require(condition, code):
    if not condition:
        raise Refused(code)


def key_fingerprint(key_id):
    return hashlib.sha256(("atlas-sports-key-id-v1:" + key_id).encode()).hexdigest()


def verify_provider_evidence(encoded, key_id, at):
    require(isinstance(encoded, str) and 0 < len(encoded.encode()) <= MAX_BYTES,
            "SCOPE_EVIDENCE_MISSING_OR_OVERSIZE")
    try:
        body = strict_json(encoded.encode())
        require(isinstance(body, dict) and set(body) == FIELDS, "SCOPE_EVIDENCE_SCHEMA")
        require(body["schema"] == "atlas-sports-provider-scope/1", "SCOPE_EVIDENCE_SCHEMA")
        require(body["provider"] == "Kalshi" and body["source_url"] in SOURCES,
                "SCOPE_EVIDENCE_SOURCE")
        for field in ("artifact_sha256", "key_id_sha256", "origin_receipt_sha256"):
            require(isinstance(body[field], str) and re.fullmatch(r"[0-9a-f]{64}", body[field]),
                    "SCOPE_EVIDENCE_HASH")
        require(body["scopes"] == ["read"] and body["write_allowed"] is False
                and body["trade_allowed"] is False and body["transfer_allowed"] is False,
                "SCOPE_EVIDENCE_PERMISSIONS")
        require(hmac.compare_digest(body["key_id_sha256"], key_fingerprint(key_id)),
                "SCOPE_EVIDENCE_KEY_MISMATCH")
        observed, expires, current = utc(body["observed_at"]), utc(body["expires_at"]), utc(at)
        require(observed <= current < expires
                and 0 < (expires-observed).total_seconds() <= MAX_VALIDITY_SECONDS,
                "SCOPE_EVIDENCE_TIME")
        manifest_hash = hashlib.sha256(canonical(body)).hexdigest()
        require(manifest_hash in REVIEWED_EVIDENCE_SHA256, "SCOPE_EVIDENCE_UNTRUSTED")
    except Refused:
        raise
    except Exception:
        raise Refused("SCOPE_EVIDENCE_SCHEMA") from None
    # Never persist supplied JSON, key identifiers, raw artifacts or signatures.
    return {"matching_key_found": True, "scopes": ["read"], "key_id_redacted": True,
            "authority": "release_pinned_provider_evidence", "provider": "Kalshi",
            "source_url": body["source_url"], "key_fingerprint": body["key_id_sha256"],
            "evidence_manifest_sha256": manifest_hash, "artifact_sha256": body["artifact_sha256"],
            "origin_receipt_sha256": body["origin_receipt_sha256"],
            "observed_at": body["observed_at"], "expires_at": body["expires_at"]}
