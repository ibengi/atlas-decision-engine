# -*- coding: utf-8 -*-
"""AIR-001 Wave 5 (DE-P0-006) — KXBTC15M strike semantics.

Reproduced defect: when the API omits the strike field, KXBTC15M
decisions substituted the CURRENT spot for the strike. The authentic
KXBTC15M strike is the reference price fixed at window OPEN; the
current spot has already drifted from it, so the proxy systematically
recenters the model at p≈0.5 and the error is direction-correlated
with recent drift. The sensitivity benchmark below MEASURES that error
through the model's own formula.

Policy: proxy strike stays available for SHADOW/RESEARCH observation,
labeled STRIKE_SOURCE=PROXY_CURRENT_SPOT with LIVE_ELIGIBLE=false, and
the live order path refuses such decisions.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from btc_probability_model import probability_yes  # noqa: E402
from execution_engine import live_eligibility_gate  # noqa: E402
from strategy_router import BtcDailyStrategy, BtcModelStrategy  # noqa: E402


class FakeCtx:
    valid = True
    reason = "ok"
    spot = 65200.0
    realized_vol_1m = 8e-4
    returns = {"5m": 0.001}
    data_quality_score = 85.0


def fake_ctx(strike=None, minutes_remaining=None, **kw):
    return FakeCtx()


class _Dec:
    def __init__(self, features):
        self.model_output = {"features": features}


class TestProxyLabeling(unittest.TestCase):
    def test_proxy_labeled_and_not_live_eligible(self):
        s = BtcModelStrategy(fake_ctx)
        out = s.evaluate({"ticker": "KXBTC15M-26AUG231215-30"}, {}, 12.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.features["strike_source"],
                         "PROXY_CURRENT_SPOT")
        self.assertIs(out.features["live_eligible"], False)

    def test_authoritative_field_strike_stays_eligible(self):
        s = BtcModelStrategy(fake_ctx)
        out = s.evaluate({"ticker": "KXBTC15M-26AUG231215-30",
                          "floor_strike": 65100.0}, {}, 12.0)
        self.assertTrue(out.valid, out.reason)
        self.assertNotIn("live_eligible", out.features)
        self.assertNotIn("strike_source", out.features)

    def test_ticker_strike_stays_eligible(self):
        # KXBTCD encodes the authoritative strike in the market's own
        # identity (T<price>): deterministic, not time-varying.
        s = BtcDailyStrategy(fake_ctx)
        out = s.evaluate({"ticker": "KXBTCD-26AUG0111-T72799.99"}, {},
                         300.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.features["strike_source"], "ticker")
        self.assertNotIn("live_eligible", out.features)


class TestLiveGate(unittest.TestCase):
    def test_gate_refuses_proxy_strike_decisions(self):
        ok, reason = live_eligibility_gate(_Dec(
            {"strike_source": "PROXY_CURRENT_SPOT",
             "live_eligible": False}))
        self.assertFalse(ok)
        self.assertEqual(
            reason,
            "strike_proxy_not_live_eligible:PROXY_CURRENT_SPOT")

    def test_gate_passes_authoritative_strikes(self):
        for feats in ({}, {"strike_source": "ticker"},
                      {"strike": 65100.0}):
            ok, reason = live_eligibility_gate(_Dec(feats))
            self.assertTrue(ok, feats)

    def test_gate_tolerates_missing_model_output(self):
        class Bare:
            pass
        ok, _ = live_eligibility_gate(Bare())
        self.assertTrue(ok)


class TestStrikeSensitivityBenchmark(unittest.TestCase):
    """MEASURED probability error of the proxy substitution, via the
    model's own formula (no market data, fully deterministic).

    Setup: spot=65200, sigma_1m=8e-4, t=12 min, momentum 0 (isolates
    the strike term). The proxy sets strike=spot, i.e. p=0.5 always.
    The true strike is the window-open reference the spot drifted from.
    """
    SPOT, SIGMA, T = 65200.0, 8e-4, 12.0

    def _p_true(self, drift_frac):
        true_strike = self.SPOT / (1.0 + drift_frac)
        return probability_yes(self.SPOT, true_strike, self.SIGMA,
                               self.T, ret_5m=0.0)

    def _p_proxy(self):
        return probability_yes(self.SPOT, self.SPOT, self.SIGMA,
                               self.T, ret_5m=0.0)

    def test_error_material_at_ordinary_intra_window_drift(self):
        # 0.1% drift since window open is ordinary for BTC in 15 min
        # (sigma_1m=8e-4 => ~0.28% expected absolute move over 12 min).
        err = abs(self._p_true(0.001) - self._p_proxy())
        self.assertGreater(err, 0.10)

    def test_error_grows_monotonically_with_drift(self):
        errors = [abs(self._p_true(d) - self._p_proxy())
                  for d in (0.0005, 0.001, 0.002, 0.005)]
        self.assertEqual(errors, sorted(errors))
        self.assertGreater(errors[-1], 0.40)   # 0.5% drift: > 40 points

    def test_error_direction_correlated_with_drift_sign(self):
        # Upward drift makes the proxy UNDERSTATE p(yes); downward
        # drift makes it overstate — a systematic, sign-following bias,
        # not symmetric noise.
        p = self._p_proxy()
        self.assertGreater(self._p_true(0.001), p)
        self.assertLess(self._p_true(-0.001), p)

    def test_zero_drift_is_the_only_free_case(self):
        self.assertAlmostEqual(self._p_true(0.0), self._p_proxy(),
                               places=12)


if __name__ == "__main__":
    unittest.main()
