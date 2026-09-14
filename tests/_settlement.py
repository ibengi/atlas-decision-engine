"""Synthetic, internally consistent evidence for settlement regression tests.

The source is built by the producer and the snapshot by the consumer. Times
are relative to the test clock, so a positive fixture never silently becomes
a future resolution or a prediction of an already known outcome.
"""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

from tests._candidate import raw_market, valid_record
from alpha_consumer import SpoolConsumer
from alpha_service import source_binding_for


def _canonical(value):
    """Independent test encoding: do not import the validator under test."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def seal_settlement(settlement):
    """Return a fully bound SYNTHETIC response, with no real authority claim.

    Seal positive fixtures before applying an invalid mutation. Deliberately
    reseal only when a test needs a second genuinely consistent synthetic
    outcome (for example the append-only conflicting-outcome test).
    """
    row = copy.deepcopy(settlement)
    row["contract_schema_version"] = row["contract_schema"]
    row["settlement_authority"] = row["source"]
    response = {
        "schema": "atlas-alpha-settlement-response-v1",
        "contract_id": row["contract_id"],
        "environment": row["environment"],
        "contract_schema_version": row["contract_schema_version"],
        "settlement_authority": row["settlement_authority"],
        "outcome": row["outcome"],
        "resolved_at": row["resolved_at"],
        "authority_record_id": "SYNTHETIC-outcome-" + row["prediction_id"],
    }
    row["settlement_response_preimage"] = _canonical(response)
    row["settlement_response_sha256"] = hashlib.sha256(
        row["settlement_response_preimage"].encode("utf-8")).hexdigest()
    evidence = {
        "schema": "atlas-alpha-settlement-evidence-v1",
        "binding": {key: row[key] for key in (
            "prediction_id", "contract_id", "market_snapshot_id",
            "source_record_sha256", "environment", "contract_schema_version")},
        "settlement_authority": row["settlement_authority"],
        "settlement_response_sha256": row["settlement_response_sha256"],
    }
    row["settlement_evidence_id"] = "sha256:" + hashlib.sha256(
        _canonical(evidence).encode("utf-8")).hexdigest()
    evidence["settlement_evidence_id"] = row["settlement_evidence_id"]
    row["settlement_evidence"] = evidence
    return row


def synthetic_authority_policy(authorities):
    """Only for disposable replay fixtures; never qualify a live authority."""
    policy = {"schema": "atlas-alpha-settlement-authority-policy-v1",
              "authorities": sorted(authorities)}
    policy["policy_id"] = "sha256:" + hashlib.sha256(
        _canonical(policy).encode("utf-8")).hexdigest()
    return policy


def qualified_fixture(*, prediction_id="p1", contract_id="KX-FIXTURE",
                      source="trusted-settlement-feed", record=None,
                      now=None, environment=None, **prediction_fields):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    observed = now - timedelta(seconds=3)
    if record is None:
        record = valid_record(raw_market(ticker=contract_id),
                              observed_at_utc=observed.isoformat())
    snapshot = SpoolConsumer.mint(None, record)
    binding = source_binding_for(
        record, contract_id=snapshot.contract_id,
        market_snapshot_id=snapshot.market_snapshot_id, digest_verified=True)
    if environment is not None:
        binding["environment"] = environment
    prediction = {
        "prediction_id": prediction_id,
        "contract_id": snapshot.contract_id,
        "market_snapshot_id": snapshot.market_snapshot_id,
        "snapshot": snapshot.as_dict(),
        "source_binding": binding,
        "prediction_time": (now - timedelta(seconds=2)).isoformat(),
    }
    prediction.update(prediction_fields)
    settlement = {
        "prediction_id": prediction_id, "outcome": 1, "source": source,
        "contract_id": binding["contract_id"],
        "market_snapshot_id": binding["market_snapshot_id"],
        "source_record_sha256": binding["record_sha256"],
        "environment": binding["environment"],
        "contract_schema": binding["contract_schema"],
        "resolved_at": (now - timedelta(seconds=1)).isoformat(),
        "settlement_evidence_id": "synthetic-settlement-" + prediction_id,
    }
    return record, snapshot, prediction, seal_settlement(settlement)
