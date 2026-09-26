"""What the shadow census must not let a reader believe.

The census exists to explain a collapse: thousands of settled rows, a
hundred scorable ones. Its only value is that the explanation is exact, so
these tests pin the two ways it could mislead — counting a row under the
wrong reason, and disagreeing with the tool whose sample it claims to
describe.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import brier_oos as bo
from tools import shadow_census as sc


def row(**kw):
    """A usable row by default; each test spoils exactly one field."""
    r = {"ts": "2026-09-01T00:00:00+00:00", "ticker": "KXBTC15M-26SEP01-T1",
         "probability_yes": 0.5, "yes_ask": 50, "result": "yes",
         "settled_at": "2026-09-01T23:00:00+00:00",
         "features": {"model_version": "btc15m-baseline-0.1"}}
    r.update(kw)
    return r


class DropReasonTest(unittest.TestCase):
    def test_the_control_row_is_usable(self):
        # Anti-vacuity: without this, every assertion below would also hold
        # for a tool that rejected everything.
        self.assertIsNone(sc.drop_reason(row()))

    def test_an_unsettled_row_is_unsettled_not_missing_data(self):
        self.assertEqual(sc.drop_reason(row(result=None)), "unsettled")

    def test_a_missing_probability_is_not_reported_as_a_missing_ask(self):
        self.assertEqual(sc.drop_reason(row(probability_yes=None)),
                         "no_model_probability")

    def test_a_missing_ask_is_named_as_such(self):
        # The decisive case: instrumentation, not model quality.
        self.assertEqual(sc.drop_reason(row(yes_ask=None)), "no_market_ask")

    def test_an_unparseable_number_is_not_silently_out_of_range(self):
        self.assertEqual(sc.drop_reason(row(yes_ask="n/a")),
                         "unparseable_number")

    def test_an_impossible_probability_is_out_of_range(self):
        self.assertEqual(sc.drop_reason(row(probability_yes=1.5)),
                         "probability_out_of_range")

    def test_every_reason_the_tool_can_emit_is_declared(self):
        # A reason absent from DROP_REASONS would vanish from the report's
        # by_reason breakdown and the attrition would not add up.
        bad = [row(result=None), row(probability_yes=None), row(yes_ask=None),
               row(yes_ask="n/a"), row(probability_yes=1.5),
               row(yes_ask=101), "not a dict"]
        for r in bad:
            self.assertIn(sc.drop_reason(r), sc.DROP_REASONS)


class AgreesWithTheToolItDescribesTest(unittest.TestCase):
    def test_survivors_match_brier_oos_exactly(self):
        records = [row(), row(result="no"), row(result=None),
                   row(yes_ask=None), row(probability_yes=None),
                   row(yes_ask="x"), row(probability_yes=-0.1), "junk"]
        rep = sc.census(records, "sha")
        self.assertEqual(rep["n_usable"], len(bo.usable_rows(records)))

    def test_the_attrition_accounts_for_every_record(self):
        records = [row(), row(result=None), row(yes_ask=None), "junk"]
        rep = sc.census(records, "sha")
        self.assertEqual(rep["n_usable"] + rep["attrition"]["dropped"],
                         rep["n_records_total"])
        self.assertEqual(sum(rep["attrition"]["by_reason"].values()),
                         rep["attrition"]["dropped"])


class SeriesTest(unittest.TestCase):
    def test_a_quarantined_series_is_visible_as_its_own_line(self):
        records = [row(ticker="KXBTCD-26SEP01-T68000"),
                   row(ticker="KXBTC15M-26SEP01-T1")]
        names = [s["series"] for s in sc.census(records, "sha")["by_series"]]
        self.assertIn("KXBTCD", names)
        self.assertIn("KXBTC15M", names)

    def test_a_row_without_a_ticker_is_not_folded_into_a_real_series(self):
        rep = sc.census([row(ticker=None)], "sha")
        self.assertEqual(rep["by_series"][0]["series"], "<no_ticker>")


class ConcentrationTest(unittest.TestCase):
    def test_rows_sharing_a_settlement_date_are_counted_as_one_date(self):
        records = [row(), row(), row()]
        rep = sc.census(records, "sha")
        self.assertEqual(rep["settlement_dates"]["n_distinct"], 1)
        self.assertEqual(rep["settlement_dates"]["max_rows_on_one_date"], 3)


class CliTest(unittest.TestCase):
    def test_it_emits_json_carrying_the_dataset_hash(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            out = os.path.join(d, "r.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump([row()], f)
            self.assertEqual(sc.main([p, "--out", out]), 0)
            with open(out, encoding="utf-8") as f:
                rep = json.load(f)
            self.assertEqual(len(rep["dataset_sha256"]), 64)
            self.assertEqual(rep["n_usable"], 1)


if __name__ == "__main__":
    unittest.main()


class DataQualityTest(unittest.TestCase):
    """The census must not let a degraded-input row pass as a sound one."""

    def q(self, score):
        r = row()
        r["features"] = dict(r["features"], data_quality=score)
        return r

    def test_the_control_row_lands_in_a_band(self):
        # Anti-vacuity: without this, a tool that banded nothing would pass
        # every assertion below.
        self.assertEqual(sc.quality_band(self.q(95)), "90-100")

    def test_a_score_under_the_router_floor_is_named_as_refused(self):
        # confidence_from_quality rejects below 60; that row never reached a
        # decision and must not be averaged in with ones that did.
        self.assertEqual(sc.quality_band(self.q(59.9)),
                         "refused_below_router_floor")

    def test_the_floor_itself_is_not_refused(self):
        self.assertEqual(sc.quality_band(self.q(60)), "60-75")

    def test_an_unrecorded_quality_is_not_guessed(self):
        r = row()
        r["features"] = {}
        self.assertIsNone(sc.quality_band(r))

    def test_an_unparseable_quality_is_not_guessed_either(self):
        self.assertIsNone(sc.quality_band(self.q("n/a")))

    def test_an_impossible_score_is_flagged_rather_than_binned(self):
        self.assertEqual(sc.quality_band(self.q(140)), "out_of_range")

    def test_rows_without_a_recorded_quality_are_counted_not_dropped(self):
        r = row()
        r["features"] = {}
        rep = sc.census([r, self.q(95)], "sha")
        self.assertEqual(rep["n_without_recorded_data_quality"], 1)
        self.assertEqual(sum(b["n"] for b in rep["by_data_quality"]), 1)
        self.assertEqual(rep["n_usable"], 2)

    def test_each_band_is_scored_against_the_market_separately(self):
        lo = [dict(self.q(65), result="no") for _ in range(4)]
        hi = [self.q(95) for _ in range(4)]
        rep = sc.census(lo + hi, "sha")
        got = {b["band"]: b["realised_yes_rate"] for b in rep["by_data_quality"]}
        self.assertEqual(got["60-75"], 0.0)
        self.assertEqual(got["90-100"], 1.0)
