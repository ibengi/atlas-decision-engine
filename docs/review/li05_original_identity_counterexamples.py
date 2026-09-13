"""Current LI-05 identity limitations, synthetic local witnesses only.

Run using the repository's offline isolation wrapper. No API key is read,
no provider request is sent, and no source or runtime state is modified.
These tests PASS when the documented limitation is reproducible; they are
not assertions that Astra identity is qualified.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from alpha_dispatcher import _run_one
from alpha_learning import score_model
from alpha_meta import ensemble
from alpha_providers import OpenAIProvider
from alpha_schema import SCHEMA_VERSION, validate_signal
from alpha_snapshot import build_snapshot


class IdentityLimitations(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.snapshot = build_snapshot(
            contract_id="SYNTHETIC-LI05", event_id="SYNTHETIC-EVENT",
            question="Synthetic identity fixture?", resolution_rules="synthetic rules",
            resolution_source="synthetic source", yes_bid=.44, yes_ask=.46,
            no_bid=.54, no_ask=.56, volume=100, open_interest=200,
            snapshot_time_utc=self.now.isoformat(),
            market_close_time_utc=(self.now + timedelta(hours=3)).isoformat(),
            expected_resolution_time_utc=(self.now + timedelta(hours=4)).isoformat())
        self.output = {
            "schema_version": SCHEMA_VERSION,
            "market_snapshot_id": self.snapshot.market_snapshot_id,
            "contract_id": self.snapshot.contract_id,
            "model": "synthetic-astra-self-claim", "model_version": "self-claimed-version",
            "generated_at_utc": self.now.isoformat(), "p_yes": .6,
            "probability_low": .55, "probability_high": .65,
            "confidence": .7, "evidence_quality": .8, "data_completeness": .9,
            "analysis_latency_ms": 1,
            "valid_until_utc": self.snapshot.analysis_deadline_utc,
            "status": "VALID",
        }
        self.response = {
            "id": "resp-synthetic-independent-envelope",
            "model": "synthetic-envelope-model-different-from-output",
            "status": "completed", "output_text": json.dumps(self.output),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        self.provider = OpenAIProvider(model="synthetic-configured-model")

    def test_w01_post_discards_authenticated_response_headers(self):
        class Response:
            status_code = 200
            headers = {"x-request-id": "req-synthetic-transport-id"}
            def json(inner):
                return self.response
        class Session:
            def post(inner, *args, **kwargs):
                return Response()
        self.provider.session = Session()
        returned = self.provider._post("https://synthetic.invalid/responses",
                                       headers={}, payload={}, timeout=1)
        self.assertEqual(returned, self.response)
        self.assertNotIn("x-request-id", returned)

    def test_w02_extractor_discards_envelope_id_and_model(self):
        output, usage = self.provider._extract(self.response)
        self.assertEqual(json.loads(output)["model"], "synthetic-astra-self-claim")
        self.assertNotIn("model", usage)
        self.assertNotIn("id", usage)

    def test_w03_analysis_metadata_uses_configured_label(self):
        with patch.object(self.provider, "configured", return_value=True), \
                patch.object(self.provider, "_call", return_value=self.response):
            output, metadata = self.provider.analyze(self.snapshot, 1)
        self.assertIsNotNone(output)
        self.assertEqual(metadata["model"], "synthetic-configured-model")
        self.assertNotIn("response_id", metadata)
        self.assertNotIn("request_id", metadata)
        self.assertNotIn("provider_identity_receipt", metadata)

    def test_w04_valid_signal_accepts_self_reported_model_identity(self):
        signal = validate_signal(self.output, self.snapshot, provider="openai",
                                 model="synthetic-configured-model", received_at=self.now)
        self.assertTrue(signal.valid)
        self.assertEqual(signal.model, "synthetic-astra-self-claim")
        self.assertEqual(signal.model_version, "self-claimed-version")

    def test_w05_dispatch_drops_unrecognized_identity_receipt_metadata(self):
        metadata = {"cost": {}, "latency_ms": 1, "error": None,
                    "provider_identity_receipt": {"response_id": "resp-synthetic",
                                                   "request_id": "req-synthetic"}}
        with patch.object(self.provider, "analyze", return_value=(self.output, metadata)):
            signal = _run_one(self.provider, self.snapshot, 1, lambda: self.now)
        self.assertTrue(signal.valid)
        self.assertNotIn("provider_identity_receipt", signal.as_dict())

    def test_w06_astra_selector_classifies_self_reported_label(self):
        signal = validate_signal(self.output, self.snapshot, provider="openai",
                                 model="synthetic-configured-model", received_at=self.now)
        result = ensemble([signal], self.snapshot, self.now)
        # This bypasses settlement ingestion deliberately to test only the
        # model selector, not qualification of any historical settlement.
        rows = [{"per_model": result["per_model"], "actual_outcome": 1}]
        report = score_model(rows, "astra")
        self.assertEqual(report["samples"], 1)
        self.assertEqual(report["models_seen"], ["synthetic-astra-self-claim"])

    def test_w07_existing_wrong_snapshot_refusal_remains_intact(self):
        output = dict(self.output, market_snapshot_id="other-snapshot")
        signal = validate_signal(output, self.snapshot, provider="openai",
                                 model="synthetic-configured-model", received_at=self.now)
        self.assertFalse(signal.valid)
        self.assertEqual(signal.rejected_reason, "wrong_snapshot_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
