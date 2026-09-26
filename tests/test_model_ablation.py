"""What the ablation must not let a reader conclude.

An ablation is a search, and a search that keeps its best result is how a
model gets talked into an edge it does not have. These tests pin the two
things that make this one safe to read: it reproduces the deployed model
exactly before comparing anything to it, and no outcome it can emit reads
as a pass.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_probability_model import (MOMENTUM_CAP, ModelInputError,
                                   probability_yes)
from tools import model_ablation as ab


def row(spot=100.0, strike=100.0, sigma=0.001, t=15.0, ret5=0.0,
        result="yes", yes_ask=50, ts="2026-09-01T00:00:00+00:00", p=None):
    r = {"ts": ts, "ticker": "KXBTC15M-X", "spot": spot, "strike": strike,
         "sigma_1m": sigma, "minutes_remaining": t, "ret_5m": ret5,
         "result": result, "yes_ask": yes_ask,
         "settled_at": "2026-09-01T23:00:00+00:00", "features": {}}
    if p is not None:
        r["probability_yes"] = p
    else:
        # A row whose inputs the model refuses carries no probability, the
        # same as production: nothing is invented to make a fixture work.
        try:
            r["probability_yes"] = probability_yes(spot, strike, sigma, t,
                                                   ret5)
        except ModelInputError:
            r["probability_yes"] = None
    return r


class RecomputeTest(unittest.TestCase):
    def test_the_control_reproduces_the_deployed_model_exactly(self):
        # Anti-vacuity: everything below is worthless if this fails.
        r = row(spot=101.0, ret5=0.002)
        self.assertAlmostEqual(ab.recompute(r), r["probability_yes"],
                               places=12)

    def test_turning_momentum_off_changes_a_row_that_had_momentum(self):
        r = row(spot=100.0, ret5=0.01)
        self.assertNotAlmostEqual(ab.recompute(r),
                                  ab.recompute(r, use_momentum=False),
                                  places=6)

    def test_at_the_money_with_no_momentum_the_model_says_one_half(self):
        self.assertAlmostEqual(ab.recompute(row(ret5=0.0),
                                            use_momentum=False), 0.5,
                               places=12)

    def test_the_cap_bounds_the_momentum_term_at_its_constant(self):
        # A huge ret_5m must move P by exactly Phi(cap) - Phi(0), which is
        # the ~19 points the claimed edges land on.
        r = row(ret5=99.0)
        self.assertAlmostEqual(ab.recompute(r),
                               ab.norm_cdf(MOMENTUM_CAP), places=12)
        self.assertAlmostEqual(ab.norm_cdf(MOMENTUM_CAP) - 0.5,
                               0.191462, places=5)

    def test_a_row_missing_an_input_is_skipped_not_defaulted(self):
        for f in ("spot", "strike", "sigma_1m", "minutes_remaining"):
            r = row()
            r[f] = None
            self.assertIsNone(ab.recompute(r), f"{f} was defaulted")

    def test_a_nonpositive_input_is_skipped(self):
        self.assertIsNone(ab.recompute(row(sigma=0.0)))
        self.assertIsNone(ab.recompute(row(spot=-1.0)))


class ControlTest(unittest.TestCase):
    def test_a_store_written_by_another_model_is_caught(self):
        rows = [row(p=0.123) for _ in range(5)]
        c = ab.control_agreement(rows)
        self.assertFalse(c["model_in_tree_reproduces_the_store"])
        self.assertEqual(c["rows_mismatched"], 5)

    def test_a_faithful_store_passes_the_control(self):
        c = ab.control_agreement([row(spot=100.5) for _ in range(5)])
        self.assertTrue(c["model_in_tree_reproduces_the_store"])

    def test_no_comparable_row_is_not_reported_as_agreement(self):
        # Zero checked rows must not read as "reproduces".
        self.assertFalse(ab.control_agreement(
            [])["model_in_tree_reproduces_the_store"])


class VerdictTest(unittest.TestCase):
    def rows(self, n=60, **kw):
        return [row(ts=f"2026-09-01T{i//60:02d}:{i%60:02d}:00+00:00", **kw)
                for i in range(n)]

    def test_the_fixture_itself_produces_scorable_rows(self):
        # Anti-vacuity for the skip test below: it must be able to fail.
        rep = ab.analyse(self.rows(), "sha")
        v = next(x for x in rep["variants"] if x["variant"] == "as_recorded")
        self.assertEqual(v["n_skipped_missing_inputs"], 0)
        self.assertGreater(v["n_scored"], 0)

    def test_no_verdict_it_can_emit_reads_as_a_pass(self):
        for rows in (self.rows(), self.rows(result="no", yes_ask=90),
                     self.rows(spot=101.0, yes_ask=5)):
            v = ab.analyse(rows, "sha")["verdict"]
            self.assertTrue(v.startswith("DIAGNOSTIC_ONLY")
                            or v.startswith("INVALID"), v)
            self.assertNotIn("PASS", v)

    def test_a_store_the_tree_cannot_reproduce_invalidates_everything(self):
        rep = ab.analyse([row(p=0.111) for _ in range(60)], "sha")
        self.assertEqual(rep["verdict"],
                         "INVALID_MODEL_IN_TREE_DOES_NOT_REPRODUCE_THE_STORE")

    def test_the_in_sample_warning_is_in_the_report_not_just_the_docstring(self):
        rep = ab.analyse(self.rows(), "sha")
        self.assertIn("never seen", rep["warning"])

    def test_the_control_variant_is_never_counted_as_a_winner(self):
        # as_recorded beating the market is the baseline's own result, not
        # a variant discovered by the search.
        rep = ab.analyse(self.rows(spot=101.0, yes_ask=5), "sha")
        self.assertNotIn("as_recorded", rep["variants_beating_market_in_sample"])

    def test_a_row_the_model_refuses_is_skipped_not_scored_at_one_half(self):
        # Defaulting an unscorable row to 0.5 would quietly pad every
        # variant with coin flips and drag each Brier toward 0.25.
        good = self.rows(20, spot=101.0)
        bad = self.rows(20, spot=101.0)
        for r in bad:
            r["sigma_1m"] = None
        rep = ab.analyse(good + bad, "sha")
        v = next(x for x in rep["variants"] if x["variant"] == "as_recorded")
        self.assertGreater(v["n_skipped_missing_inputs"], 0)
        self.assertLess(v["n_scored"], rep["n_test_rows"])

    def test_every_variant_is_scored_on_the_same_rows_as_the_market(self):
        rep = ab.analyse(self.rows(), "sha")
        for v in rep["variants"]:
            self.assertEqual(v["n_scored"] + v["n_skipped_missing_inputs"],
                             rep["n_test_rows"])


if __name__ == "__main__":
    unittest.main()
