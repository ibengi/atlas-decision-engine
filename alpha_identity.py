"""Replayable provider transport observations. SHADOW ONLY; no authority.

This is controlled HTTPS capture evidence, NOT a provider signature. Local
digests detect inconsistency; they cannot authenticate an arbitrary supplied
receipt. Only reviewed adapter origins and exact model mappings can qualify
an identity. No Astra authority/model mapping has been selected in this code.
"""
import base64
from contextvars import ContextVar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

SCHEMA = "atlas-provider-identity-receipt-v1"
POLICY_VERSION = "atlas-provider-identity-policy-v1-unconfigured"
TRUST_BASIS = "controlled_verified_https_capture_not_remote_signature"
MAX_BODY_BYTES = 262144
APPROVED_ENDPOINTS = frozenset({
    ("openai", "https://api.openai.com/v1/responses"),
    ("grok", "https://api.x.ai/v1/responses"),
})
# A change here requires an independently reviewed authority decision.
# Entries: (provider, endpoint, requested_model, resolved_model, role, policy_id).
# A configured model string, substring or generated answer never adds an entry.
REVIEWED_MODEL_MAPPINGS = frozenset()
_CAPTURE_INVOCATION = ContextVar("alpha_identity_capture_invocation", default=None)


def begin_capture():
    invocation = object()
    return invocation, _CAPTURE_INVOCATION.set(invocation)


def end_capture(token):
    _CAPTURE_INVOCATION.reset(token)


def current_capture():
    return _CAPTURE_INVOCATION.get()


def model_key(provider, model):
    return provider + "/" + model

_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
_SECRET_KEYS = frozenset({"authorization", "api_key", "apikey", "api-key",
                          "x-api-key", "x-goog-api-key", "access_token",
                          "refresh_token", "password", "secret", "cookie"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def strict_json(text):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate response key")
            out[key] = value
        return out
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def secret_free(value, *, redactor=None, depth=0):
    if depth > 40:
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and k.lower() not in _SECRET_KEYS
                   and secret_free(v, redactor=redactor, depth=depth+1)
                   for k, v in value.items())
    if isinstance(value, list):
        return all(secret_free(v, redactor=redactor, depth=depth+1) for v in value)
    if isinstance(value, str):
        if "bearer " in value.lower() or (redactor is not None and redactor(value) != value):
            return False
        # Forecast JSON is nested inside envelope text. Screen the decoded
        # forecast too, otherwise Unicode escapes can conceal a key echo.
        if value.lstrip().startswith(("{", "[")):
            try:
                embedded = strict_json(value)
            except (ValueError, TypeError, RecursionError):
                pass
            else:
                return secret_free(embedded, redactor=redactor, depth=depth+1)
    return True


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


@dataclass(frozen=True)
class TransportObservation:
    """Immutable metadata created by the adapter, outside generated JSON."""
    encoded: str
    capture_token: object = None

    def as_dict(self):
        return strict_json(self.encoded)


class ObservedResponse(dict):
    """Preserves the existing parser surface without trusting body metadata."""
    def __init__(self, body, observation):
        super().__init__(body)
        self.observation = observation


def make_receipt(response, *, provider, requested_model, snapshot,
                 environment, output):
    observation = getattr(response, "observation", None)
    if not isinstance(observation, TransportObservation):
        return None
    data = observation.as_dict()
    body = strict_json(base64.b64decode(data["response_body_b64"], validate=True))
    receipt = {
        "schema": SCHEMA, "trust_basis": TRUST_BASIS,
        "policy_version": POLICY_VERSION, "provider": provider,
        "requested_model": requested_model,
        "resolved_model": body.get("model", body.get("modelVersion")),
        "response_id": body.get("id", body.get("responseId")),
        "contract_id": snapshot.contract_id,
        "market_snapshot_id": snapshot.market_snapshot_id,
        "snapshot_sha256": digest(snapshot.as_dict()),
        "environment": environment,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "transport": data,
    }
    receipt["receipt_sha256"] = digest(receipt)
    return receipt


def verify_receipt(receipt, *, snapshot, environment, provider=None,
                   requested_model=None, output=None):
    """Recompute all bindings; never trust persisted qualification booleans."""
    refusal = {"valid": False, "qualified": False, "role": None,
               "reason": "IDENTITY_UNVERIFIED"}
    try:
        if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
            return refusal
        expected_keys = {"schema", "trust_basis", "policy_version", "provider",
                         "requested_model", "resolved_model", "response_id", "contract_id",
                         "market_snapshot_id", "snapshot_sha256", "environment", "output_sha256",
                         "transport", "receipt_sha256"}
        if set(receipt) != expected_keys or receipt["trust_basis"] != TRUST_BASIS:
            return refusal
        content = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        if digest(content) != receipt["receipt_sha256"]:
            return refusal
        snap = snapshot.as_dict() if hasattr(snapshot, "as_dict") else snapshot
        from alpha_snapshot import snapshot_from_dict
        restored = snapshot_from_dict(snap)
        if (receipt["snapshot_sha256"] != digest(snap)
                or receipt["market_snapshot_id"] != restored.market_snapshot_id
                or receipt["contract_id"] != restored.contract_id
                or receipt["environment"] != environment
                or environment not in ("prod", "demo")
                or (provider is not None and receipt["provider"] != provider)
                or (requested_model is not None and receipt["requested_model"] != requested_model)):
            return refusal
        t = receipt["transport"]
        if not isinstance(t, dict) or set(t) != {
                "request_id", "request_started_at", "received_at", "endpoint", "final_url",
                "http_status", "verified_tls", "redirected", "transport_request_id",
                "request_schema", "request_body", "request_sha256", "response_body_b64",
                "response_body_sha256", "wire_body_available"}:
            return refusal
        if (not valid_id(t["request_id"]) or type(t["http_status"]) is not int
                or t["http_status"] != 200 or t["verified_tls"] is not True
                or t["redirected"] is not False or t["wire_body_available"] is not True
                or t["endpoint"] != t["final_url"]
                or (receipt["provider"], t["endpoint"]) not in APPROVED_ENDPOINTS
                or not valid_id(receipt["response_id"]) or not valid_id(receipt["resolved_model"])
                or not valid_id(receipt["requested_model"])
                or (t["transport_request_id"] is not None and not valid_id(t["transport_request_id"]))):
            return refusal
        url = urlsplit(t["endpoint"])
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            return refusal
        from alpha_snapshot import parse_utc
        started, received = (parse_utc(t[k]) for k in ("request_started_at", "received_at"))
        if started > received or started < restored.snapshot_time or received > datetime.now(timezone.utc):
            return refusal
        if t["request_schema"] != "provider-json-request-v1" or digest(t["request_body"]) != t["request_sha256"]:
            return refusal
        from alpha_providers import build_prompt
        request = t["request_body"]
        if (request.get("input") != build_prompt(restored)
                or request.get("model") != receipt["requested_model"]):
            return refusal
        raw = base64.b64decode(t["response_body_b64"], validate=True)
        if len(raw) > MAX_BODY_BYTES or hashlib.sha256(raw).hexdigest() != t["response_body_sha256"]:
            return refusal
        body = strict_json(raw)
        if not isinstance(body, dict) or not secret_free(body) or not secret_free(request):
            return refusal
        if (body.get("status") != "completed" or body.get("id") != receipt["response_id"]
                or body.get("model") != receipt["resolved_model"]):
            return refusal
        from alpha_providers import _ResponsesAPI
        text, _ = _ResponsesAPI._extract(object.__new__(_ResponsesAPI), body)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt["output_sha256"]:
            return refusal
        if output is not None and text != output:
            return refusal
        # Independently replay schema, snapshot and forecast chronology too.
        from alpha_schema import validate_signal
        signal = validate_signal(text, restored, provider=receipt["provider"],
                                 model=receipt["resolved_model"], received_at=received)
        if not signal.valid or parse_utc(signal.generated_at_utc) > received:
            return refusal
        mappings = [entry for entry in REVIEWED_MODEL_MAPPINGS
                    if len(entry) == 6 and tuple(entry[:4]) == (
                        receipt["provider"], t["endpoint"], receipt["requested_model"],
                        receipt["resolved_model"])
                    and entry[5] == receipt["policy_version"]]
        qualified = len(mappings) == 1
        return {"valid": True, "qualified": qualified,
                "role": mappings[0][4] if qualified else None,
                "reason": "QUALIFIED" if qualified else "MODEL_MAPPING_UNREVIEWED",
                "model": receipt["resolved_model"], "provider": receipt["provider"],
                "request_id": t["request_id"], "response_id": receipt["response_id"],
                "p_yes": signal.p_yes,
                "forecast": {"p_yes": signal.p_yes, "low": signal.probability_low,
                             "high": signal.probability_high, "confidence": signal.confidence,
                             "provider": receipt["provider"]},
                "model_key": model_key(receipt["provider"], receipt["resolved_model"])}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, RuntimeError):
        return refusal


def forecast_matches(signal, forecast):
    if not isinstance(signal, dict) or not isinstance(forecast, dict) or not forecast:
        return False
    for field, expected in forecast.items():
        actual = signal.get(field)
        if field == "provider":
            if type(actual) is not str or actual != expected:
                return False
        elif type(actual) not in (int, float) or actual != expected:
            return False
    return True


def prediction_binding_valid(row, signal, receipt):
    try:
        if not isinstance(row, dict) or not isinstance(signal, dict) or not isinstance(receipt, dict):
            return False
        binding = signal.get("provider_prediction_binding")
        if (not valid_id(row.get("prediction_id")) or not isinstance(binding, dict)
                or set(binding) != {"prediction_id", "receipt_sha256"}
                or binding["prediction_id"] != row["prediction_id"]
                or binding["receipt_sha256"] != receipt.get("receipt_sha256")
                or not isinstance(row.get("prediction_time"), str)):
            return False
        from alpha_snapshot import parse_utc
        prediction_time = parse_utc(row["prediction_time"])
        received = parse_utc(receipt["transport"]["received_at"])
        return received <= prediction_time <= datetime.now(timezone.utc)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def qualified_prediction_signal(row, key, signal, *, role="astra"):
    """Restart verification; old rows without receipts remain immutable/unqualified."""
    if not isinstance(row, dict) or not isinstance(signal, dict):
        return False
    receipt = signal.get("provider_identity_receipt")
    if not prediction_binding_valid(row, signal, receipt):
        return False
    result = verify_receipt(receipt, snapshot=row.get("snapshot"),
                            environment=(row.get("source_binding") or {}).get("environment"))
    return (result["qualified"] is True and (role is None or result["role"] == role)
            and result.get("model_key") == key
            and forecast_matches(signal, result.get("forecast")))
