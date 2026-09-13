"""Synthetic transport evidence only; this never requests a provider."""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from alpha_snapshot import build_snapshot, SCHEMA_VERSION
from alpha_identity import (TransportObservation, ObservedResponse, canonical,
                            digest, make_receipt)
from alpha_providers import build_prompt

MODEL = "gpt-astra-pro-max"
MODEL_KEY = "openai/" + MODEL
POLICY = "synthetic-test-authority-v1"
ENDPOINT = "https://api.openai.com/v1/responses"
MAPPINGS = frozenset({("openai", ENDPOINT, "synthetic-requested", MODEL, "astra", POLICY)})


def evidence_fixture(*, p=.6, contract="SYNTHETIC-LI05", request="synthetic-request-1",
                     response_id="resp-synthetic-1", snapshot=None):
    now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=2)
    snapshot = snapshot or build_snapshot(
        contract_id=contract, event_id="SYNTHETIC-EVENT", question="Synthetic forecast?",
        resolution_rules="Synthetic rules", resolution_source="Synthetic public source",
        yes_bid=.44, yes_ask=.46, no_bid=.54, no_ask=.56, volume=100, open_interest=200,
        snapshot_time_utc=now.isoformat(),
        market_close_time_utc=(now + timedelta(hours=3)).isoformat(),
        expected_resolution_time_utc=(now + timedelta(hours=4)).isoformat())
    now = snapshot.snapshot_time + timedelta(seconds=1)
    output = json.dumps({"schema_version": SCHEMA_VERSION,
        "market_snapshot_id": snapshot.market_snapshot_id,
        "contract_id": snapshot.contract_id, "model": "untrusted-astra-self-claim",
        "model_version": "untrusted-version", "generated_at_utc": now.isoformat(),
        "p_yes": p, "probability_low": max(0., p - .03),
        "probability_high": min(1., p + .03), "confidence": .7,
        "evidence_quality": .8, "data_completeness": .9, "analysis_latency_ms": 1,
        "valid_until_utc": snapshot.analysis_deadline_utc, "status": "VALID"})
    body = {"id": response_id, "model": MODEL, "status": "completed",
            "output_text": output, "usage": {"input_tokens": 1, "output_tokens": 1}}
    raw = canonical(body).encode("utf-8")
    payload = {"model": "synthetic-requested", "input": build_prompt(snapshot),
               "max_output_tokens": 100}
    observation = {"request_id": request, "request_started_at": now.isoformat(),
                   "received_at": now.isoformat(), "endpoint": ENDPOINT, "final_url": ENDPOINT,
                   "http_status": 200, "verified_tls": True, "redirected": False,
                   "transport_request_id": "req-synthetic-server", "request_schema": "provider-json-request-v1",
                   "request_body": payload, "request_sha256": digest(payload),
                   "response_body_b64": base64.b64encode(raw).decode("ascii"),
                   "response_body_sha256": hashlib.sha256(raw).hexdigest(), "wire_body_available": True}
    response = ObservedResponse(body, TransportObservation(canonical(observation)))
    receipt = make_receipt(response, provider="openai", requested_model="synthetic-requested",
                           snapshot=snapshot, environment="demo", output=output)
    return snapshot, output, body, response, receipt


def attributed_row(row, p):
    """Add synthetic complete identity evidence to arithmetic-only fixtures."""
    snapshot, _, _, _, receipt = evidence_fixture(p=p, contract=row["contract_id"],
        request="request-" + row["prediction_id"], response_id="response-" + row["prediction_id"])
    row["snapshot"] = snapshot.as_dict()
    row["market_snapshot_id"] = snapshot.market_snapshot_id
    row["prediction_time"] = datetime.now(timezone.utc).isoformat()
    row["source_binding"] = {"environment": "demo"}
    row["per_model"][MODEL_KEY] = row["per_model"].pop(MODEL)
    row["per_model"][MODEL_KEY].update({"low":max(0.,p-.03), "high":min(1.,p+.03),
        "confidence":.7, "provider":"openai", "provider_identity_receipt": receipt,
        "provider_prediction_binding": {"prediction_id": row["prediction_id"],
                                         "receipt_sha256": receipt["receipt_sha256"]}})
    return row
