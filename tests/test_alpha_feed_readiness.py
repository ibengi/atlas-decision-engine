import unittest

from alpha_feed_readiness import assess_record, assess_records


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
        result = assess_record(self.complete())
        self.assertTrue(result["ready"])
        self.assertEqual(result["missing_fields"], [])

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
        for field in ("event_id", "question", "resolution_rules",
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
        result = assess_records([self.complete(), {"contract_id": "C-2"}])
        self.assertFalse(result["all_records_ready"])
        self.assertEqual(result["ready_records"], 1)


if __name__ == "__main__":
    unittest.main()
