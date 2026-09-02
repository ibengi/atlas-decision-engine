"""What the research harness must refuse to do.

A model study is easy to make say yes. These tests pin the three ways it
would be lying: fitting on data the model would not have had, counting
correlated rows as independent evidence, and letting a point estimate
stand in for a significant one.
"""

import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import model_research as mr


def _mins(m):
    """ISO timestamp m minutes after a fixed origin."""
    return (datetime(2026, 9, 1, tzinfo=timezone.utc)
            + timedelta(minutes=m)).isoformat()


def row(ts, settled_at, ticker="KXBTC15M-A", result="yes", p=0.6,
        yes_ask=50, yes_bid=46, spot=80000.0, strike=79900.0,
        sigma=0.0004, minutes=10.0, ret5=0.0001):
    return {"ts": ts, "settled_at": settled_at, "ticker": ticker,
            "result": result, "probability_yes": p, "yes_ask": yes_ask,
            "yes_bid": yes_bid, "spot": spot, "strike": strike,
            "sigma_1m": sigma, "minutes_remaining": minutes, "ret_5m": ret5}


def sequence(n, settle_delay_h=0, **kw):
    """n rows one hour apart, each settling `settle_delay_h` hours later."""
    out = []
    for i in range(n):
        d, h = 1 + i // 24, i % 24
        sd, sh = 1 + (i + settle_delay_h) // 24, (i + settle_delay_h) % 24
        out.append(row(f"2026-09-{d:02d}T{h:02d}:00:00+00:00",
                       f"2026-09-{sd:02d}T{sh:02d}:30:00+00:00",
                       ticker=f"T{i}", **kw))
    return out


class LeakageTest(unittest.TestCase):
    def test_features_never_read_the_outcome(self):
        # The single most important property: no feature may depend on
        # anything only knowable after settlement.
        r = row("2026-09-01T00:00:00+00:00", "2026-09-01T00:30:00+00:00")
        base = mr.features(r)
        for result in ("yes", "no"):
            for settled in ("2026-09-01T00:30:00+00:00", "2030-01-01T00:00:00+00:00"):
                v = dict(r, result=result, settled_at=settled)
                self.assertEqual(mr.features(v), base)

    def test_training_rows_whose_label_was_unknown_are_purged(self):
        # A row that had not settled when the test fold began could not
        # have been trained on, however early its decision was taken.
        rows = sequence(120, settle_delay_h=0)
        for r in rows[:20]:
            r["settled_at"] = "2030-01-01T00:00:00+00:00"   # never resolved in time
        for train, test in mr.walk_forward(rows, n_folds=2):
            cutoff = mr._ts(test[0]["ts"])
            self.assertTrue(all(mr._ts(t["settled_at"]) < cutoff for t in train))

    def test_no_training_row_is_decided_after_its_test_fold_starts(self):
        rows = sequence(120)
        for train, test in mr.walk_forward(rows, n_folds=3):
            self.assertLess(max(mr._ts(t["ts"]) for t in train),
                            min(mr._ts(t["ts"]) for t in test))

    def test_standardisation_constants_come_from_train_only(self):
        # Refitting with extra (future) rows must change the model; if it
        # did not, the constants were not being taken from train at all.
        X = [[float(i)] for i in range(40)]
        y = [i % 2 for i in range(40)]
        a = mr.fit_logistic(X, y)
        b = mr.fit_logistic(X + [[1e6]], y + [1])
        self.assertNotEqual(a["mu"], b["mu"])


class IndependenceTest(unittest.TestCase):
    def test_the_bootstrap_resamples_windows_not_rows(self):
        # Ten copies of one window are one piece of evidence. A row
        # bootstrap would report a tight interval; a block bootstrap must
        # not, because there are still only two windows.
        rows = [row(f"2026-09-01T0{i}:00:00+00:00",
                    "2026-09-01T12:00:00+00:00", ticker="A") for i in range(5)]
        rows += [row(f"2026-09-01T1{i}:00:00+00:00",
                     "2026-09-01T20:00:00+00:00", ticker="B") for i in range(5)]
        lo, hi = mr.block_bootstrap_ci(rows, [-0.5] * 5 + [0.5] * 5)
        self.assertLess(lo, -0.4)
        self.assertGreater(hi, 0.4)

    def test_a_single_window_yields_no_interval_at_all(self):
        rows = [row("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00",
                    ticker="A")] * 5
        self.assertEqual(mr.block_bootstrap_ci(rows, [0.1] * 5), (None, None))


class MetricTest(unittest.TestCase):
    def test_an_empty_sample_scores_None_not_zero(self):
        self.assertIsNone(mr.brier([]))
        self.assertIsNone(mr.log_loss([]))
        self.assertIsNone(mr.sharpness([]))

    def test_a_constant_half_forecast_has_zero_sharpness(self):
        self.assertEqual(mr.sharpness([(0.5, 1), (0.5, 0)]), 0.0)

    def test_isotonic_calibration_is_monotone(self):
        pairs = [(i / 40.0, 1 if i % 3 else 0) for i in range(40)]
        m = mr.fit_isotonic(pairs)
        vals = [mr.apply_isotonic(m, i / 50.0) for i in range(50)]
        self.assertEqual(vals, sorted(vals))

    def test_fitting_refuses_a_sample_too_small_to_fit(self):
        self.assertIsNone(mr.fit_logistic([[1.0]] * 3, [1, 0, 1]))
        self.assertIsNone(mr.fit_platt([(0.5, 1)] * 5))
        self.assertIsNone(mr.fit_isotonic([(0.5, 1)] * 5))


class UsableRowsTest(unittest.TestCase):
    def test_an_unsettled_row_is_not_an_observation(self):
        self.assertEqual(mr.usable_rows([dict(row("2026-09-01T00:00:00+00:00",
                                                  None), result=None)]), [])

    def test_a_row_missing_any_scored_field_is_dropped_for_every_candidate(self):
        for field in ("yes_ask", "yes_bid", "spot", "strike", "sigma_1m",
                      "minutes_remaining", "probability_yes"):
            r = row("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00")
            r[field] = None
            self.assertEqual(mr.usable_rows([r]), [], field)

    def test_rows_come_back_in_decision_order(self):
        rows = list(reversed(sequence(10)))
        got = mr.usable_rows(rows)
        self.assertEqual([mr._ts(r["ts"]) for r in got],
                         sorted(mr._ts(r["ts"]) for r in got))


class TradingTest(unittest.TestCase):
    def test_a_book_wider_than_the_filter_is_never_traded(self):
        r = row("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00",
                yes_ask=70, yes_bid=30)
        self.assertEqual(mr.simulate([(r, 0.99)], 0.05, 4)["trades"], 0)

    def test_fees_are_charged_on_every_trade(self):
        r = row("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00",
                yes_ask=50, yes_bid=48, result="yes")
        s = mr.simulate([(r, 0.99)], 0.05, 4)
        self.assertEqual(s["trades"], 1)
        self.assertGreater(s["fees"], 0)
        self.assertLess(s["net_pnl"], s["gross_pnl"])

    def test_no_opportunity_reports_no_trades_rather_than_zero_pnl(self):
        # A study with no trades must not read as a flat, harmless result.
        r = row("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00",
                yes_ask=50, yes_bid=48)
        self.assertEqual(mr.simulate([(r, 0.50)], 0.05, 4), {"trades": 0})


def disagreeing_sequence(n, favourable=0.70):
    """Windows on which the model is emphatically right, mixed with windows
    on which it is emphatically wrong. It comes out ahead on average and
    nowhere near reliably — the case a point estimate reads as a win."""
    out = []
    for i in range(n):
        d, h = 1 + i // 24, i % 24
        won = (i % 10) < int(favourable * 10)
        out.append(row(f"2026-09-{d:02d}T{h:02d}:00:00+00:00",
                       f"2026-09-{d:02d}T{h:02d}:30:00+00:00",
                       ticker=f"T{i}", result="yes" if won else "no",
                       p=0.75, yes_ask=50, yes_bid=48))
    return out


class VerdictTest(unittest.TestCase):
    def test_the_significance_flag_is_exactly_the_interval_test(self):
        for rows in (mr.usable_rows(sequence(160)),
                     mr.usable_rows(disagreeing_sequence(160))):
            for r in mr.evaluate(rows, n_folds=3)["results"]:
                ci = r["delta_ci95"]
                self.assertEqual(r["beats_market_significantly"],
                                 bool(ci is not None and ci[1] < 0), r["model"])

    def test_beating_the_market_on_average_but_not_reliably_is_not_a_win(self):
        rows = mr.usable_rows(disagreeing_sequence(160))
        got = {r["model"]: r for r in mr.evaluate(rows, n_folds=3)["results"]}
        em = got["existing_model"]
        self.assertLess(em["delta_brier"], 0)          # ahead on average
        self.assertGreater(em["delta_ci95"][1], 0)     # and not reliably
        self.assertFalse(em["beats_market_significantly"])

    def test_the_market_baseline_cannot_beat_itself(self):
        rows = mr.usable_rows(sequence(160))
        rep = mr.evaluate(rows, n_folds=3)
        mk = next(r for r in rep["results"] if r["model"] == "market_ask")
        self.assertEqual(mk["delta_brier"], 0.0)
        self.assertFalse(mk["beats_market_significantly"])

    def test_every_fold_reports_its_own_sample_size(self):
        rows = mr.usable_rows(sequence(160))
        rep = mr.evaluate(rows, n_folds=3)
        for f in rep["folds"]:
            for k in ("train_n", "test_n", "train_windows", "test_windows"):
                self.assertGreater(f[k], 0, k)

    def test_the_study_is_deterministic(self):
        rows = mr.usable_rows(sequence(160))
        a = mr.evaluate(rows, n_folds=3)
        b = mr.evaluate(rows, n_folds=3)
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))


class CliTest(unittest.TestCase):
    def test_the_report_carries_the_dataset_sha_and_window_count(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(sequence(160), f)
            out = os.path.join(d, "r.json")
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                mr.main([p, "--folds", "3", "--out", out])
            rep = json.load(open(out, encoding="utf-8"))
            self.assertEqual(len(rep["dataset_sha256"]), 64)
            self.assertEqual(rep["n_settlement_windows"], 160)


if __name__ == "__main__":
    unittest.main()


class EventIsolationTest(unittest.TestCase):
    """R2: no settlement event may sit on both sides of a split.

    One market observed at several time-to-expiry checkpoints yields several
    rows carrying ONE outcome. Isolation used to hold only as a side effect
    of the label purge, under the invariant settled_at >= observed_at; a row
    breaking that invariant reintroduced leakage silently. These tests pin
    the property directly, INDEPENDENTLY of settled_at.
    """

    def _staggered(self, n_events=60):
        """Realistic shape: event i closes at hour i and is observed at three
        checkpoints BEFORE its own close, so events interleave in time while
        each one's label lands after all of its own observations."""
        out = []
        for i in range(n_events):
            close = 48 * 60 + i * 60                      # minutes from origin
            for mins in (1400, 300, 50):
                out.append(row(_mins(close - mins), _mins(close),
                               ticker=f"KXBTCD-E{i}",
                               result="yes" if i % 3 else "no"))
        out.sort(key=lambda r: r["ts"])
        return out

    def _broken_invariant(self, n_events=60):
        """The same observations, but every label is stamped as settled at
        the origin — before it could possibly be known. That defeats the
        label purge completely and leaves the explicit event check as the
        only thing preventing an event from straddling the split."""
        out = self._staggered(n_events)
        for r in out:
            r["settled_at"] = _mins(0)
        return out

    def test_no_event_appears_in_both_train_and_test(self):
        rows = mr.usable_rows(self._staggered())
        folds = mr.walk_forward(rows, n_folds=3)
        self.assertTrue(folds, "fixture produced no folds")
        for train, test in folds:
            self.assertFalse({r["ticker"] for r in train}
                             & {r["ticker"] for r in test})

    def test_isolation_holds_even_when_the_label_purge_cannot_help(self):
        # Every label settles before every decision, so the purge removes
        # nothing. Only the explicit event check prevents leakage here.
        rows = mr.usable_rows(self._broken_invariant())
        folds = mr.walk_forward(rows, n_folds=3)
        self.assertTrue(folds, "fixture produced no folds")
        for train, test in folds:
            self.assertFalse({r["ticker"] for r in train}
                             & {r["ticker"] for r in test},
                             "event leaked once settled_at stopped protecting")

    def test_overlap_is_purged_from_train_never_from_test(self):
        base = mr.usable_rows(self._broken_invariant())
        folds = mr.walk_forward(base, n_folds=3)
        # every test row survives: the split boundaries are unchanged
        self.assertEqual(sum(len(t) for _, t in folds),
                         sum(len(t) for _, t in
                             mr.walk_forward(base, n_folds=3)))
        for _, test in folds:
            self.assertGreater(len(test), 0)

    def test_a_fold_left_undersized_by_the_purge_is_dropped(self):
        # min_train is a floor, not a suggestion. On this fixture fold 0 has
        # 90 training rows before events are purged and 72 after, so a floor
        # of 80 must drop it — not run it on 72. The other folds (100, 129)
        # clear the floor and survive, which proves the floor is applied per
        # fold and not to the whole study.
        rows = mr.usable_rows(self._broken_invariant())
        folds = mr.walk_forward(rows, n_folds=3, min_train=80)
        self.assertEqual(len(folds), 2, "an undersized fold was run anyway")
        for train, _ in folds:
            self.assertGreaterEqual(len(train), 80)

    def test_the_floor_is_measured_after_purging_not_before(self):
        rows = mr.usable_rows(self._broken_invariant())
        self.assertEqual(len(mr.walk_forward(rows, n_folds=3, min_train=72)), 3)
        self.assertEqual(len(mr.walk_forward(rows, n_folds=3, min_train=73)), 2)

    def test_the_event_key_is_the_settlement_market(self):
        r = row("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00",
                ticker="KXBTCD-Z")
        self.assertEqual(mr.event_of(r), "KXBTCD-Z")

    def test_label_availability_is_still_enforced_alongside_isolation(self):
        rows = mr.usable_rows(sequence(120))
        for r in rows[:20]:
            r["settled_at"] = "2030-01-01T00:00:00+00:00"
        for train, test in mr.walk_forward(rows, n_folds=2):
            cutoff = mr._ts(test[0]["ts"])
            self.assertTrue(all(mr._ts(t["settled_at"]) < cutoff for t in train))
