"""What the P&L replay must refuse to call profitable.

A replay is the easiest place in this repository to produce a flattering
number: score only the winners, fill at mid, forget the fee, let one good
day carry the total. These tests pin each of those shut, and pin the one
case the tool exists for — a profit that appears while the model is known
to forecast worse than the market.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import shadow_pnl as sp


def row(ts="2026-09-01T00:00:00+00:00", side="yes", result="yes",
        yes_ask=50, no_ask=50, fee=0.0, slip=0.0, day="01"):
    return {"ts": ts, "ticker": "KXBTC15M-X", "shadow_decision": side,
            "result": result, "yes_ask": yes_ask, "no_ask": no_ask,
            "probability_yes": 0.5, "estimated_fee": fee,
            "estimated_slippage": slip,
            "settled_at": f"2026-09-{day}T23:00:00+00:00"}


def series(n, **kw):
    return [row(ts=f"2026-09-{kw.pop('day','01')}T{i//60:02d}:{i%60:02d}:00+00:00",
                **kw) for i in range(n)]


class FillTest(unittest.TestCase):
    def test_a_winning_yes_pays_one_minus_the_ask_not_one_minus_the_mid(self):
        # Filled at 60c, settles yes: +0.40, never +0.50 from a mid of 50c.
        self.assertAlmostEqual(sp.replay(row(yes_ask=60)), 0.40, places=9)

    def test_a_losing_yes_loses_the_whole_ask(self):
        self.assertAlmostEqual(sp.replay(row(yes_ask=60, result="no")),
                               -0.60, places=9)

    def test_the_no_side_is_priced_on_the_no_ask(self):
        r = row(side="no", result="no", yes_ask=90, no_ask=30)
        self.assertAlmostEqual(sp.replay(r), 0.70, places=9)

    def test_fees_and_slippage_are_charged_on_a_win(self):
        self.assertAlmostEqual(sp.replay(row(yes_ask=60, fee=0.02, slip=0.01)),
                               0.37, places=9)

    def test_fees_and_slippage_are_charged_on_a_loss_too(self):
        r = row(yes_ask=60, result="no", fee=0.02, slip=0.01)
        self.assertAlmostEqual(sp.replay(r), -0.63, places=9)

    def test_a_negative_recorded_cost_cannot_become_a_credit(self):
        self.assertAlmostEqual(sp.replay(row(yes_ask=60, fee=-0.02)),
                               0.38, places=9)


class SelectionTest(unittest.TestCase):
    def test_a_row_the_engine_declined_is_not_replayed(self):
        self.assertEqual(sp.traded_rows([row(side="none")]), [])

    def test_an_unsettled_decision_is_not_replayed(self):
        self.assertEqual(sp.traded_rows([row(result=None)]), [])

    def test_a_side_without_an_ask_is_dropped_rather_than_defaulted(self):
        self.assertEqual(sp.traded_rows([row(side="no", no_ask=None)]), [])

    def test_the_control_row_survives(self):
        # Anti-vacuity: the assertions above would also hold for a tool
        # that replayed nothing at all.
        self.assertEqual(len(sp.traded_rows([row()])), 1)


class VerdictTest(unittest.TestCase):
    def _report(self, rows, **kw):
        return sp.analyse(rows, "sha", min_traded=10, **kw)

    def test_a_losing_test_slice_is_unprofitable(self):
        rows = series(100, result="no", yes_ask=50)
        self.assertEqual(self._report(rows)["verdict"], "UNPROFITABLE")

    def test_a_handful_of_contracts_decides_nothing(self):
        rows = series(12, yes_ask=10)          # winners, but only 2 in test
        self.assertEqual(sp.analyse(rows, "sha")["verdict"],
                         "INDETERMINATE_SAMPLE_TOO_SMALL")

    def test_profit_under_a_failing_brier_is_not_called_profitable(self):
        # The trap the module exists to name: the rows picked won, while
        # the model forecasts WORSE than the market (delta >= 0).
        rows = series(100, yes_ask=10)
        self.assertEqual(self._report(rows, brier_delta=+0.02)["verdict"],
                         "SELECTION_WITHOUT_FORECAST_EDGE")

    def test_profit_with_a_passing_brier_is_still_only_the_test_slice(self):
        rows = series(100, yes_ask=10)
        self.assertEqual(self._report(rows, brier_delta=-0.02)["verdict"],
                         "PROFITABLE_ON_TEST_SLICE_ONLY")

    def test_an_unknown_brier_does_not_silently_clear_the_trap(self):
        # No delta supplied: the tool may not assume the model forecasts
        # well, but it also must not invent a contradiction.
        rows = series(100, yes_ask=10)
        self.assertEqual(self._report(rows)["verdict"],
                         "PROFITABLE_ON_TEST_SLICE_ONLY")


class ConcentrationTest(unittest.TestCase):
    def test_a_total_carried_by_one_day_is_reported_as_such(self):
        rows = (series(60, yes_ask=50, result="no", day="01")
                + series(60, yes_ask=10, result="yes", day="02"))
        rep = sp.analyse(rows, "sha", min_traded=10)
        big = rep["largest_single_date"]
        self.assertIsNotNone(big)
        self.assertGreaterEqual(rep["n_settlement_dates_in_test"], 1)
        self.assertIn("share_of_total", big)


class CliTest(unittest.TestCase):
    def test_it_writes_json_carrying_the_dataset_hash(self):
        with tempfile.TemporaryDirectory() as d:
            p, out = os.path.join(d, "s.json"), os.path.join(d, "r.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(series(100, result="no"), f)
            self.assertEqual(sp.main([p, "--out", out, "--min-traded", "10"]), 1)
            with open(out, encoding="utf-8") as f:
                rep = json.load(f)
            self.assertEqual(len(rep["dataset_sha256"]), 64)
            self.assertEqual(rep["verdict"], "UNPROFITABLE")


if __name__ == "__main__":
    unittest.main()
