"""What the out-of-sample Brier check must refuse to do.

The rule in MODEL_VALIDATION_GUIDE.md is out-of-sample for one reason: a
model that has seen the data always looks good on it. These tests pin that
the tool cannot be talked into a favourable answer — not by a flattering
full-sample score, not by an undersized sample, and not by scoring the two
sides on different rows.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import brier_oos as bo


def row(ts, p, yes_ask, result, settled_at="2026-09-01T12:00:00+00:00"):
    return {"ts": ts, "ticker": "KXBTC15M-X", "probability_yes": p,
            "yes_ask": yes_ask, "result": result, "settled_at": settled_at,
            "features": {"model_version": "btc15m-v1.0-ref"}}


def series(n, p, yes_ask, result, day="01", start_hour=0):
    """n rows an hour apart, all identical apart from their timestamp."""
    return [row(f"2026-09-{day}T{(start_hour + i) % 24:02d}:"
                f"{(i // 24):02d}:00+00:00", p, yes_ask, result,
                settled_at=f"2026-09-{day}T23:00:00+00:00")
            for i in range(n)]


class UsableRowsTest(unittest.TestCase):
    def test_an_unsettled_row_is_not_an_observation(self):
        self.assertEqual(bo.usable_rows([row("2026-09-01T00:00:00+00:00",
                                             0.5, 50, None)]), [])

    def test_a_row_without_a_market_ask_is_dropped_from_BOTH_series(self):
        # The guide compares model and market on the same events. Keeping a
        # row the market never priced would score the model on data the
        # baseline never saw.
        rows = [row("2026-09-01T00:00:00+00:00", 0.5, None, "yes"),
                row("2026-09-01T01:00:00+00:00", 0.5, 50, "yes")]
        kept = bo.usable_rows(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(bo._pairs(kept, "model")),
                         len(bo._pairs(kept, "market")))

    def test_a_row_without_a_model_probability_is_dropped_too(self):
        self.assertEqual(bo.usable_rows([row("2026-09-01T00:00:00+00:00",
                                             None, 50, "yes")]), [])

    def test_an_out_of_range_probability_is_refused_not_clamped(self):
        self.assertEqual(bo.usable_rows([row("2026-09-01T00:00:00+00:00",
                                             1.4, 50, "yes")]), [])

    def test_a_non_numeric_probability_is_refused(self):
        self.assertEqual(bo.usable_rows([row("2026-09-01T00:00:00+00:00",
                                             "high", 50, "yes")]), [])

    def test_a_non_dict_entry_cannot_crash_the_run(self):
        self.assertEqual(bo.usable_rows(["nope", None, 42]), [])


class BrierTest(unittest.TestCase):
    def test_an_empty_sample_scores_None_not_zero(self):
        # 0.0 is a perfect Brier score. An absent sample must never read as
        # one.
        self.assertIsNone(bo.brier([]))

    def test_a_perfect_forecast_scores_zero(self):
        self.assertEqual(bo.brier([(1.0, 1), (0.0, 0)]), 0.0)


class SplitTest(unittest.TestCase):
    def test_the_split_is_chronological_regardless_of_input_order(self):
        rows = list(reversed(series(10, 0.5, 50, "yes")))
        train, val, test = bo.split_chronological(rows)
        self.assertEqual([bo._ts(r) for r in train],
                         sorted(bo._ts(r) for r in train))
        self.assertLess(max(bo._ts(r) for r in train),
                        min(bo._ts(r) for r in test))

    def test_no_test_row_precedes_a_train_row(self):
        train, _, test = bo.split_chronological(series(50, 0.5, 50, "yes"))
        self.assertTrue(all(bo._ts(a) < bo._ts(b)
                            for a in train for b in test))


class VerdictTest(unittest.TestCase):
    def test_a_model_that_wins_in_sample_and_loses_out_of_sample_FAILS(self):
        # The exact trap the out-of-sample rule exists to catch: the model
        # is near-perfect on the first 80% and wrong on the last 20%.
        rows = (series(320, 0.99, 50, "yes", day="01")
                + series(80, 0.99, 50, "no", day="02"))
        rep = bo.analyse(rows, "sha", min_settled=1)
        self.assertFalse(rep["test"]["model_beats_market"])
        self.assertTrue(rep["context_only_not_decisive"]
                           ["full_sample"]["model_beats_market"])
        self.assertEqual(rep["verdict"], "FAIL")

    def test_a_favourable_full_sample_score_never_decides(self):
        rows = (series(320, 0.99, 50, "yes", day="01")
                + series(80, 0.99, 50, "no", day="02"))
        rep = bo.analyse(rows, "sha", min_settled=1)
        self.assertEqual(rep["decisive_slice"], "test")
        self.assertIn("model_brier_beats_market_baseline_out_of_sample",
                      rep["failed_gates"])

    def test_too_few_settled_predictions_FAILS_even_when_the_model_wins(self):
        rows = series(50, 0.99, 50, "yes")
        rep = bo.analyse(rows, "sha")           # default gate is 300
        self.assertTrue(rep["test"]["model_beats_market"])
        self.assertIn("settled_predictions", rep["failed_gates"])
        self.assertEqual(rep["verdict"], "FAIL")

    def test_everything_defined_passing_still_is_not_a_PASS(self):
        # The minimum number of independent settlement dates is recorded in
        # T7-I sect. 9 as requiring operator approval. While it is
        # undefined, the tool must not promote itself to PASS.
        rows = series(400, 0.99, 50, "yes")
        rep = bo.analyse(rows, "sha", min_settled=1)
        self.assertEqual(rep["failed_gates"], [])
        self.assertEqual(rep["undefined_gates"],
                         ["independent_settlement_dates"])
        self.assertEqual(rep["verdict"], "INDETERMINATE")

    def test_no_input_can_produce_a_PASS_while_a_gate_is_undefined(self):
        for n, p, ask, res in ((400, 0.99, 50, "yes"), (900, 0.5, 50, "no"),
                               (301, 0.01, 99, "no")):
            rep = bo.analyse(series(n, p, ask, res), "sha", min_settled=1)
            self.assertNotEqual(rep["verdict"], "PASS")

    def test_rows_settling_on_one_day_count_as_one_settlement_date(self):
        # 400 strikes resolving from a single price path are not 400
        # independent observations, and the report must say so.
        rep = bo.analyse(series(400, 0.9, 50, "yes", day="01"), "sha",
                         min_settled=1)
        self.assertEqual(rep["n_settlement_dates"], 1)

    def test_the_dataset_sha_is_carried_into_the_report(self):
        rep = bo.analyse(series(10, 0.5, 50, "yes"), "deadbeef",
                         min_settled=1)
        self.assertEqual(rep["dataset_sha256"], "deadbeef")

    def test_an_empty_dataset_cannot_pass(self):
        rep = bo.analyse([], "sha", min_settled=0)
        self.assertIn("test_slice_non_empty", rep["failed_gates"])
        self.assertNotEqual(rep["verdict"], "PASS")


class CliTest(unittest.TestCase):
    def test_a_failing_verdict_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "shadow.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(series(10, 0.5, 50, "yes"), f)
            out = os.path.join(d, "report.json")
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = bo.main([path, "--out", out])
            self.assertEqual(code, 1)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
