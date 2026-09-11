import os
import tempfile
import unittest

from alpha_ledger import AlphaLedger, ROW_PREDICTION, ROW_RESOLUTION
from alpha_resolution_ingest import ingest_settlements


class AlphaResolutionIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = AlphaLedger(
            path=os.path.join(self.tmp.name, "alpha.jsonl"),
            cost_path=os.path.join(self.tmp.name, "cost.jsonl"),
        )
        self.ledger.record_prediction({"prediction_id": "p1"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_appends_one_resolution_without_rewriting_prediction(self):
        result = ingest_settlements(self.ledger, [{
            "prediction_id": "p1", "outcome": 1,
            "source": "trusted-settlement-feed",
        }])
        self.assertEqual(result["appended"], 1)
        self.assertFalse(result["broker_authority"])
        rows = self.ledger.rows()
        self.assertEqual(sum(r.get("kind") == ROW_PREDICTION for r in rows), 1)
        self.assertEqual(sum(r.get("kind") == ROW_RESOLUTION for r in rows), 1)
        self.assertIsNone(self.ledger.find_prediction("p1").get("actual_outcome"))

    def test_duplicate_matching_resolution_is_idempotent(self):
        feed = [{"prediction_id": "p1", "outcome": 0, "source": "feed"}]
        self.assertEqual(ingest_settlements(self.ledger, feed)["appended"], 1)
        again = ingest_settlements(self.ledger, feed)
        self.assertEqual(again["idempotent"], 1)
        self.assertEqual(len(self.ledger.resolutions()), 1)

    def test_conflicting_resolution_is_not_written(self):
        ingest_settlements(self.ledger, [
            {"prediction_id": "p1", "outcome": 1, "source": "feed-a"}
        ])
        conflict = ingest_settlements(self.ledger, [
            {"prediction_id": "p1", "outcome": 0, "source": "feed-b"}
        ])
        self.assertEqual(len(conflict["conflicts"]), 1)
        self.assertEqual(self.ledger.find_resolution("p1")["actual_outcome"], 1)

    def test_unknown_and_malformed_rows_are_rejected(self):
        result = ingest_settlements(self.ledger, [
            {"prediction_id": "missing", "outcome": 1, "source": "feed"},
            {"prediction_id": "p1", "outcome": 2, "source": "feed"},
            {"prediction_id": "p1", "outcome": 1, "source": ""},
        ])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["rejected"]), 3)
        self.assertIsNone(self.ledger.find_resolution("p1"))


if __name__ == "__main__":
    unittest.main()
