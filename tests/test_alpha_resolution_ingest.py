import os
import tempfile
import unittest

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

from _candidate import valid_record                           # noqa: E402
from alpha_ledger import (AlphaLedger, ROW_PREDICTION,        # noqa: E402
                          ROW_RESOLUTION)
from alpha_resolution_ingest import ingest_settlements        # noqa: E402
from candidate_contract import FEED_SCHEMA, canonical_content  # noqa: E402
from _settlement import qualified_fixture                     # noqa: E402


#: The binding a real prediction is committed with. The fixtures carry it
#: because production does: since the AA-15 re-audit a settlement must
#: identify the market, the observation and the evidence bytes, not just
#: quote a prediction_id, and a fixture without it would be testing an
#: incomplete settlement rather than the behaviour each test names.
#:
#: RA-11 widened that identity to the full VERSIONED one -- environment and
#: contract version as well -- and RA-12 made settlement qualification
#: recompute the digest from the RETAINED evidence rather than compare two
#: copies of the claim about it. So the fixture carries a real record: a
#: synthetic `"f" * 64` cannot be re-derived from anything, and a prediction
#: whose evidence does not recompute is quarantined rather than settled.
RECORD, SNAPSHOT, PREDICTION, SETTLEMENT = qualified_fixture()
BINDING = PREDICTION["source_binding"]

#: The operator's statement of which settlement feed they verified. Required
#: since the re-audit: with no allow-list no authority has been qualified and
#: every row is refused.
TRUSTED = ["trusted-settlement-feed", "feed", "feed-a", "feed-b"]


def settlement(source="trusted-settlement-feed", **over):
    row = {"prediction_id": "p1", "outcome": 1, "source": source,
           "contract_id": BINDING["contract_id"],
           "market_snapshot_id": BINDING["market_snapshot_id"],
           "source_record_sha256": BINDING["record_sha256"],
           "environment": BINDING["environment"],
           "contract_schema": BINDING["contract_schema"],
           # RA-11: the ACTUAL resolution instant and the evidence identity.
           # Absent, `resolve()` used to fill in the ingestion time, so every
           # time-ordered statistic measured when a script ran.
           "resolved_at": SETTLEMENT["resolved_at"],
           "settlement_evidence_id": "fixture-settlement-0001"}
    row.update(over)
    return row


class AlphaResolutionIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = AlphaLedger(
            path=os.path.join(self.tmp.name, "alpha.jsonl"),
            cost_path=os.path.join(self.tmp.name, "cost.jsonl"),
        )
        self.ledger.record_prediction(dict(PREDICTION))

    def ingest(self, rows, **kw):
        kw.setdefault("trusted_sources", TRUSTED)
        return ingest_settlements(self.ledger, rows, **kw)

    def tearDown(self):
        self.tmp.cleanup()

    def test_appends_one_resolution_without_rewriting_prediction(self):
        result = self.ingest([settlement()])
        self.assertEqual(result["appended"], 1)
        self.assertFalse(result["broker_authority"])
        rows = self.ledger.rows()
        self.assertEqual(sum(r.get("kind") == ROW_PREDICTION for r in rows), 1)
        self.assertEqual(sum(r.get("kind") == ROW_RESOLUTION for r in rows), 1)
        self.assertIsNone(self.ledger.find_prediction("p1").get("actual_outcome"))

    def test_duplicate_matching_resolution_is_idempotent(self):
        feed = [settlement(source="feed", outcome=0)]
        self.assertEqual(self.ingest(feed)["appended"], 1)
        again = self.ingest(feed)
        self.assertEqual(again["idempotent"], 1)
        self.assertEqual(len(self.ledger.resolutions()), 1)

    def test_conflicting_resolution_is_not_written(self):
        self.ingest([settlement(source="feed-a", outcome=1)])
        conflict = self.ingest([settlement(source="feed-b", outcome=0)])
        self.assertEqual(len(conflict["conflicts"]), 1)
        self.assertEqual(self.ledger.find_resolution("p1")["actual_outcome"], 1)

    def test_unknown_and_malformed_rows_are_rejected(self):
        result = self.ingest([
            settlement(source="feed", prediction_id="missing"),
            settlement(source="feed", outcome=2),
            settlement(source=""),
        ])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["rejected"]), 3)
        self.assertIsNone(self.ledger.find_resolution("p1"))


    def test_an_unqualified_source_is_refused_without_an_allow_list(self):
        """Re-audit: the default is REFUSE. Asserted here as well as in the
        v3 suite, because this file is what a reader consults for the
        module's everyday contract."""
        result = ingest_settlements(self.ledger, [settlement()])
        self.assertEqual(result["appended"], 0)
        self.assertIn("qualified", result["rejected"][0]["reason"])

    def test_an_incomplete_binding_is_quarantined(self):
        row = settlement()
        row.pop("source_record_sha256")
        result = self.ingest([row])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(result["quarantined"][0]["missing_binding"],
                         ["source_record_sha256"])


if __name__ == "__main__":
    unittest.main()
