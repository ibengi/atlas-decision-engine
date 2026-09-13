"""Combined source-v4/provider identity boundaries, offline synthetic only.

The v4 fixture retains archived public market bytes; all metadata, provider
responses, authority mappings, predictions and outcomes are synthetic. These
tests do not establish a real provider or settlement authority.
"""
import base64
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests import _gates  # noqa: F401
from tests._candidate import valid_record
from tests._alpha_identity import evidence_fixture, POLICY, MAPPINGS, MODEL_KEY
from tests.test_li02_source_contract_v4 import bundle_fixture, rewrite_capture, incoming
from candidate_contract import canonical_content, compute_checksum
from research_source_contract_v4 import build_record
from alpha_consumer import SpoolConsumer
from alpha_dispatcher import DispatchResult
from alpha_gateway import AlphaGateway
from alpha_identity import canonical, digest, qualified_prediction_signal
from alpha_ledger import AlphaLedger
from alpha_learning import learning_report
from alpha_meta import ensemble
from alpha_providers import AtlasQuantProvider
from alpha_resolution_ingest import ingest_settlements
from alpha_schema import validate_signal
from alpha_settlement_validation import verify_source_evidence
from config import CFG


CAPTURED = datetime(2026, 9, 13, 20, 10, 11, 100000, tzinfo=timezone.utc)
RECEIVED = CAPTURED.replace(microsecond=500000)
PREDICTED = RECEIVED + timedelta(microseconds=1)
REVIEW_CLOCK = datetime(2026, 9, 13, 20, 30, tzinfo=timezone.utc)


class ReviewClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return REVIEW_CLOCK if tz is not None else REVIEW_CLOCK.replace(tzinfo=None)


class ShadowIntegrationBindings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpha-joint-binding-")
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch("alpha_identity.POLICY_VERSION", POLICY))
        self.enterContext(patch("alpha_identity.REVIEWED_MODEL_MAPPINGS", MAPPINGS))
        self.enterContext(patch("alpha_identity.datetime", ReviewClock))
        self.enterContext(patch("alpha_settlement_validation._utc_now", return_value=REVIEW_CLOCK))
        self.enterContext(patch.object(CFG, "ALPHA_ENVIRONMENT", "prod"))

    def record(self, schema="v4"):
        if schema == "v3":
            return valid_record(observed_at_utc="2026-09-13T20:10:09+00:00")
        bundle = bundle_fixture()
        capture = bundle["series_capture"]
        capture["emitted_at_utc"] = CAPTURED.isoformat()
        capture["record_sha256"] = compute_checksum(capture)
        return build_record(bundle)

    @staticmethod
    def source_binding(record, snapshot):
        return {"record_sha256": record["record_sha256"],
                "source_evidence": canonical_content(record), "digest_verified": True,
                "contract_id": snapshot.contract_id, "market_snapshot_id": snapshot.market_snapshot_id,
                "contract_schema": record["schema"], "environment": "prod"}

    def ledger(self):
        return AlphaLedger(str(Path(self.tmp.name)/"predictions.jsonl"),
                           str(Path(self.tmp.name)/"cost.jsonl"))

    def receipt_signal(self, snapshot, received=RECEIVED):
        _, output, body, _, receipt = evidence_fixture(snapshot=snapshot)
        receipt["environment"] = "prod"
        receipt["transport"]["received_at"] = received.isoformat()
        body["output_text"] = output
        raw = canonical(body).encode()
        receipt["transport"]["response_body_b64"] = base64.b64encode(raw).decode()
        receipt["transport"]["response_body_sha256"] = hashlib.sha256(raw).hexdigest()
        receipt["output_sha256"] = hashlib.sha256(output.encode()).hexdigest()
        receipt["receipt_sha256"] = digest({k:v for k,v in receipt.items() if k != "receipt_sha256"})
        signal = validate_signal(output, snapshot, provider="openai", model="synthetic-requested",
                                 received_at=received, provider_identity_receipt=receipt)
        self.assertTrue(signal.valid, signal.rejected_reason)
        self.assertTrue(signal.provider_identity_qualified)
        return signal

    def observed(self, record, *, provider_receipt=True, predicted=PREDICTED):
        snapshot = SpoolConsumer.mint(None, record)
        signals = [self.receipt_signal(snapshot)] if provider_receipt else []
        result = DispatchResult(signals, RECEIVED, RECEIVED)
        meta = ensemble(signals, snapshot, RECEIVED)
        row = AlphaGateway._record(None, snapshot, result, meta, {}, {},
            "NO_EDGE", "SYNTHETIC combined binding", 0., predicted,
            source_binding=self.source_binding(record, snapshot))
        return row

    def assert_provider(self, row, expected=True):
        self.assertEqual(qualified_prediction_signal(row, MODEL_KEY,
            row["per_model"][MODEL_KEY]), expected)

    def test_v4_same_second_source_survives_atlasquant_safe_terminal(self):
        record = self.record()
        snapshot = SpoolConsumer.mint(None, record)
        gateway = AlphaGateway(providers=[AtlasQuantProvider()], ledger=self.ledger(),
                               now_fn=lambda:PREDICTED)
        row = gateway.analyze(snapshot, source_binding=self.source_binding(record, snapshot))
        self.assertEqual(row["state"], "INSUFFICIENT_DATA")
        self.assertTrue(row["persisted"])
        self.assertEqual(row["prediction_time"], PREDICTED.isoformat(timespec="microseconds"))
        self.assertTrue(verify_source_evidence(row)["verified"])
        self.assertFalse(row["executed"])
        self.assertFalse(row["execution_authorized"])

    def test_v3_same_second_provider_receipt_keeps_precision_and_both_bindings(self):
        row = self.observed(self.record("v3"))
        self.assertEqual(row["prediction_time"], PREDICTED.isoformat(timespec="microseconds"))
        self.assertTrue(verify_source_evidence(row)["verified"])
        self.assert_provider(row)
        self.assertEqual(row["per_model"][MODEL_KEY]["provider_prediction_binding"]["prediction_id"],
                         row["prediction_id"])

    def test_v4_same_second_source_and_provider_receipt_both_qualify(self):
        row = self.observed(self.record())
        self.assertEqual(row["prediction_time"], PREDICTED.isoformat(timespec="microseconds"))
        self.assertTrue(verify_source_evidence(row)["verified"])
        self.assert_provider(row)

    def test_v4_prediction_before_metadata_capture_refused_without_provider(self):
        row = self.observed(self.record(), provider_receipt=False,
                            predicted=CAPTURED-timedelta(microseconds=1))
        self.assertFalse(verify_source_evidence(row)["verified"])

    def test_v3_prediction_before_provider_capture_refused(self):
        row = self.observed(self.record("v3"), predicted=RECEIVED-timedelta(microseconds=1))
        self.assertTrue(verify_source_evidence(row)["verified"])
        self.assert_provider(row, False)

    def test_v4_prediction_after_source_but_before_provider_refused(self):
        row = self.observed(self.record(), predicted=RECEIVED-timedelta(microseconds=1))
        self.assertTrue(verify_source_evidence(row)["verified"])
        self.assert_provider(row, False)

    def test_v4_prediction_before_both_captures_refuses_both(self):
        row = self.observed(self.record(), predicted=CAPTURED-timedelta(microseconds=1))
        self.assertFalse(verify_source_evidence(row)["verified"])
        self.assert_provider(row, False)

    def test_combined_receipts_restart_preserves_bytes_and_joint_learning(self):
        record = self.record()
        row = self.observed(record)
        ledger = self.ledger()
        stored = ledger.record_prediction(row)
        result = ingest_settlements(ledger, [incoming(stored)],
                                    trusted_sources=[record["resolution_source"]])
        self.assertEqual(result["appended"], 1)
        before = Path(ledger.log.path).read_bytes()
        restarted = self.ledger()
        self.assertEqual(learning_report(restarted)["astra"]["samples"], 1)
        recovered = restarted.find_prediction(stored["prediction_id"])
        self.assertTrue(verify_source_evidence(recovered)["verified"])
        self.assert_provider(recovered)
        self.assertEqual(Path(ledger.log.path).read_bytes(), before)

    def test_source_a_snapshot_b_with_valid_provider_receipt_cannot_learn(self):
        source_a = self.record()
        bundle_b = bundle_fixture()
        rewrite_capture(bundle_b, "market", lambda body:body["markets"][0].update(yes_bid_dollars="0.4000"))
        source_b = build_record(bundle_b)
        row = self.observed(source_b)
        row["source_binding"] = self.source_binding(source_a,
            SpoolConsumer.mint(None, source_b))
        self.assertFalse(verify_source_evidence(row)["verified"])
        self.assert_provider(row)
        ledger = self.ledger()
        stored = ledger.record_prediction(row)
        ingest_settlements(ledger, [incoming(stored)],
                            trusted_sources=[source_a["resolution_source"]])
        self.assertEqual(learning_report(ledger)["astra"]["samples"], 0)

    def test_legacy_v3_without_provider_receipt_keeps_prior_timestamp_format(self):
        row = self.observed(self.record("v3"), provider_receipt=False)
        self.assertEqual(row["prediction_time"], PREDICTED.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
