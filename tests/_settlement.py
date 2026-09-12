"""Synthetic, internally consistent evidence for settlement regression tests.

The source is built by the producer and the snapshot by the consumer. Times
are relative to the test clock, so a positive fixture never silently becomes
a future resolution or a prediction of an already known outcome.
"""
from datetime import datetime, timedelta, timezone

from tests._candidate import raw_market, valid_record
from alpha_consumer import SpoolConsumer
from alpha_service import source_binding_for


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
    return record, snapshot, prediction, settlement
