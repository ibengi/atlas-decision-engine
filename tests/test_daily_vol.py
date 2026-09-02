"""Volatility estimators: causal, honest about small samples, and close to
the truth where the truth is known (synthetic GBM)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import daily_vol as dv

SIGMA = 0.0005
CANDLES = dv.gbm_candles(3000, SIGMA, seed=11)
T = 2900 * 60.0


class CausalityTest(unittest.TestCase):
    def test_no_estimator_reads_a_candle_that_has_not_closed(self):
        # Scale every candle at or after T by 1.5x. A causal estimator
        # cannot notice.
        future = [dict(c) for c in CANDLES]
        for c in future:
            if c["ts"] + 60.0 > T:
                for k in ("open", "high", "low", "close"):
                    c[k] *= 1.5
        for f in (lambda cc: dv.realized_1m(cc, T, 240),
                  lambda cc: dv.ewma_1m(cc, T, 240, 60),
                  lambda cc: dv.parkinson_1m(cc, T, 240),
                  lambda cc: dv.garman_klass_1m(cc, T, 240)):
            self.assertEqual(f(CANDLES), f(future))

    def test_the_in_progress_candle_is_excluded(self):
        # A candle opening at T-30s has not closed at T and must not count.
        with_open = CANDLES + [{"ts": T - 30.0, "open": 1.0, "high": 1e9,
                                "low": 1e-9, "close": 1e9}]
        self.assertEqual(dv.realized_1m(with_open, T, 240),
                         dv.realized_1m(CANDLES, T, 240))
        self.assertEqual(dv.parkinson_1m(with_open, T, 240),
                         dv.parkinson_1m(CANDLES, T, 240))


class SmallSampleTest(unittest.TestCase):
    def test_too_few_returns_yield_None_not_a_number(self):
        self.assertIsNone(dv.realized_1m(CANDLES, T, 10))
        self.assertIsNone(dv.ewma_1m(CANDLES, T, 10, 5))
        self.assertIsNone(dv.parkinson_1m(CANDLES, T, 10))
        self.assertIsNone(dv.garman_klass_1m(CANDLES, T, 10))

    def test_an_empty_series_yields_None(self):
        self.assertIsNone(dv.realized_1m([], T, 240))
        self.assertIsNone(dv.scale_to_horizon(None, 900))
        self.assertIsNone(dv.realized_forward([], T, 60))


class BiasTest(unittest.TestCase):
    """Sampling error at n returns is about sigma/sqrt(2n): ~13% at 30,
    ~4.5% at 240, ~2% at 1440. The bounds below are two of those."""

    def test_close_to_close_is_unbiased_at_a_long_window(self):
        v = dv.realized_1m(CANDLES, T, 1440)
        self.assertLess(abs(v / SIGMA - 1), 0.05)

    def test_close_to_close_at_the_deployed_window_is_noisy(self):
        # The deployed estimator's window. Not wrong on average — just far
        # noisier than a longer one, which is the point of the comparison.
        v = dv.realized_1m(CANDLES, T, 30)
        self.assertLess(abs(v / SIGMA - 1), 0.30)

    def test_range_estimators_are_within_tolerance(self):
        for v in (dv.parkinson_1m(CANDLES, T, 1440),
                  dv.garman_klass_1m(CANDLES, T, 1440)):
            self.assertLess(abs(v / SIGMA - 1), 0.12)

    def test_ewma_tracks_the_truth(self):
        v = dv.ewma_1m(CANDLES, T, 1440, 240)
        self.assertLess(abs(v / SIGMA - 1), 0.10)


class HorizonTest(unittest.TestCase):
    def test_sqrt_time_scaling(self):
        self.assertAlmostEqual(dv.scale_to_horizon(0.001, 900), 0.03)

    def test_term_structure_reports_every_window_and_the_ratio(self):
        ts = dv.term_structure(CANDLES, T)
        for k in ("rv_60m", "rv_240m", "rv_720m", "rv_1440m"):
            self.assertIsNotNone(ts[k])
        self.assertIsNotNone(ts["long_over_short"])

    def test_realized_forward_reads_only_the_named_horizon(self):
        a = dv.realized_forward(CANDLES, T, 60)
        b = dv.realized_forward(CANDLES, T, 120)
        self.assertIsNotNone(a)
        self.assertNotEqual(a, b)

    def test_synthetic_candles_are_deterministic_for_a_seed(self):
        self.assertEqual(dv.gbm_candles(50, SIGMA, seed=3),
                         dv.gbm_candles(50, SIGMA, seed=3))
        self.assertNotEqual(dv.gbm_candles(50, SIGMA, seed=3),
                            dv.gbm_candles(50, SIGMA, seed=4))


if __name__ == "__main__":
    unittest.main()
