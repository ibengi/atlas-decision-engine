import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _candidate import valid_record                            # noqa: E402

from alpha_feed_readiness import assess_record, assess_records  # noqa: E402


class AlphaFeedReadinessTests(unittest.TestCase):
    def complete(self):
        return {
            "contract_id": "C-1",
            "event_id": "E-1",
            "question": "Will the event occur?",
            "resolution_rules": "Resolve from the named official source.",
            "resolution_source": "official source",
            "snapshot_time_utc": "2026-09-11T11:00:00+00:00",
            "yes_bid": 0.44, "yes_ask": 0.46,
            "no_bid": 0.54, "no_ask": 0.56,
            "volume": 100, "open_interest": 50,
            "market_close_time_utc": "2026-09-11T12:00:00+00:00",
            "expected_resolution_time_utc": "2026-09-11T12:05:00+00:00",
        }

    def shadow_prediction(self):
        return {
            "ts": "2026-09-11T13:51:14+00:00",
            "ticker": "KXBTC15M-26SEP111000-00",
            "strike": 77740.1,
            "minutes_remaining": 8.757207983333332,
            "yes_bid": 0.97,
            "yes_ask": 0.97,
            "no_bid": 0.03,
            "no_ask": 0.03,
            "spread": 0,
            "ranker_score": None,
            "features": {"minutes_remaining": 8.757207983333332,
                         "strike": 77740.1},
            "settled_at": "2026-09-11T14:00:40+00:00",
        }

    def test_complete_record_is_ready(self):
        """READY means the WHOLE contract, built by the real producer."""
        result = assess_record(valid_record())
        self.assertTrue(result["ready"], result["reason"])
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["contract_errors"], [])

    def test_a_shape_complete_row_without_the_contract_is_not_ready(self):
        """AA-07. This is the regression the finding names.

        `self.complete()` carries every required field NAME and nothing else:
        no schema, no checksum, no provenance, no per-quote observation. The
        previous readiness gate called that READY, which is precisely what a
        substituted default passes -- `resolution_source: "kalshi"` and
        `volume: 0.0` are syntactically present too. A shape can no longer
        produce a READY verdict on its own.
        """
        result = assess_record(self.complete())
        self.assertFalse(result["ready"])
        self.assertTrue(result["contract_errors"])
        self.assertIn("does not carry", result["reason"])
        # ...and the shape analysis still says the names were all there, so
        # the refusal is demonstrably about the CONTRACT, not about a missing
        # field. Without this the test could pass for the wrong reason.
        self.assertEqual(result["missing_fields"], [])

    def test_a_legacy_schema_row_is_refused_by_name(self):
        """A v1/v2 record cannot be re-labelled truthful; it must be
        re-observed. Reported as a legacy refusal rather than as a generic
        contract failure, so the operator knows which it is."""
        row = dict(valid_record(), schema="atlas-research-candidate-v2")
        result = assess_record(row)
        self.assertFalse(result["ready"])
        self.assertIn("legacy schema", result["reason"])

    def test_partial_record_fails_closed(self):
        result = assess_record({
            "recorded_at": "2026-09-11T11:00:00+00:00",
            "decision": {"ticker": "C-2"},
        })
        self.assertFalse(result["ready"])
        self.assertIn("yes_bid", result["missing_fields"])
        self.assertIn("expected_resolution_time_utc", result["missing_fields"])

    def test_shadow_timestamp_is_a_direct_alias(self):
        result = assess_record(self.shadow_prediction())
        self.assertEqual(
            result["direct_fields"]["snapshot_time_utc"]["source_path"], "ts")

    def test_shadow_prediction_still_fails_without_contract_metadata(self):
        result = assess_record(self.shadow_prediction())
        self.assertFalse(result["ready"])
        # AA-08: `event_id` is OPTIONAL in the contract, in the producer and in
        # the consumer, so readiness no longer counts its absence as a gap.
        # Listing it here was the three-component disagreement the finding
        # names -- readiness refused a source the other two would have taken.
        self.assertNotIn("event_id", result["missing_fields"])
        for field in ("question", "resolution_rules",
                      "resolution_source", "volume", "open_interest",
                      "market_close_time_utc", "expected_resolution_time_utc"):
            self.assertIn(field, result["missing_fields"])

    def test_shadow_prediction_does_not_turn_settlement_into_deadline(self):
        result = assess_record(self.shadow_prediction())
        reasons = {item["reason"] for item in result["prohibited_inferences"]}
        self.assertIn(
            "post-outcome settlement evidence cannot define a pre-outcome snapshot deadline",
            reasons)
        self.assertIn(
            "derived market timing is not producer-persisted decision-time evidence",
            reasons)

    def test_shadow_prediction_does_not_parse_ticker_into_question(self):
        result = assess_record(self.shadow_prediction())
        reasons = {item["reason"] for item in result["prohibited_inferences"]}
        self.assertIn(
            "contract text must come from the producer/exchange, not ticker parsing",
            reasons)

    def test_liquidity_score_is_not_volume_or_open_interest(self):
        row = self.shadow_prediction()
        row["liquidity"] = 0.7
        result = assess_record(row)
        reasons = {item["reason"] for item in result["prohibited_inferences"]}
        self.assertIn(
            "a generic liquidity metric is not observed exchange volume/open interest",
            reasons)

    def test_empty_batch_is_not_ready(self):
        result = assess_records([])
        self.assertFalse(result["all_records_ready"])
        self.assertFalse(result["broker_authority"])
        self.assertEqual(result["mode"], "SHADOW_ONLY")

    def test_incomplete_row_cannot_be_masked(self):
        result = assess_records([valid_record(), {"contract_id": "C-2"}])
        self.assertFalse(result["all_records_ready"])
        self.assertEqual(result["ready_records"], 1)
        self.assertEqual(result["contract_violations"], 1)


if __name__ == "__main__":
    unittest.main()
