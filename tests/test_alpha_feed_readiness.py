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
