# -*- coding: utf-8 -*-
"""P6.2 — Extension d'horizon BTC (audit : 100 KXBTCD rejetes 'horizon',
46 KXBTC15M rejetes 'strike_absent' a chaque scan).

- KXBTCD au-dela de 26h (jusqu'a 14 jours) : MEME formule du modele avec
  vol ancree (LONG_HORIZON_VOL_ANCHOR) et confiance plafonnee ;
- strike absent (trou de donnees API demo) : KXBTCD via le ticker
  'T<strike>' ; KXBTC15M via proxy spot (marche 'up/down' dont le strike
  est le prix de reference a l'ouverture) ;
- horizons deja couverts : resultats byte-identiques (features comprises)."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import math
from strategy_router import BtcDailyStrategy, BtcModelStrategy
from btc_probability_model import norm_cdf, P_FLOOR, P_CEIL


class FakeCtx:
    valid = True
    reason = "ok"
    spot = 65200.0
    realized_vol_1m = 8e-4
    returns = {"5m": 0.001}
    data_quality_score = 85.0


def fake_ctx(strike=None, minutes_remaining=None, **kw):
    return FakeCtx()


def _model_p(spot, strike, sigma, t, ret_5m=0.001):
    """Formule exacte du modele (voir btc_probability_model)."""
    denom = sigma * math.sqrt(t)
    d = math.log(spot / strike) / denom
    mu = min(0.5, (ret_5m / 5.0) * t / denom)
    return min(P_CEIL, max(P_FLOOR, norm_cdf(d + mu)))


class TestDailyHorizonExtension(unittest.TestCase):
    def setUp(self):
        self.s = BtcDailyStrategy(fake_ctx)

    def test_legacy_horizon_unchanged(self):
        """t=300 (5h) : features BYTE-IDENTIQUES a la ligne de base
        (aucune cle supplementaire), confiance inchangee."""
        m = {"ticker": "KXBTCD-26JUL2017-T64999.99", "floor_strike": 64999.99}
        out = self.s.evaluate(m, {}, 300.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.confidence, 8)          # 85//10, t <= 24h
        self.assertEqual(out.features, {
            "model_version": "btc15m-v1.0-ref",
            "spot": 65200.0, "strike": 64999.99,
            "sigma_1m": 8e-4, "minutes_remaining": 300.0,
            "ret_5m": 0.001, "data_quality": 85.0})
        self.assertAlmostEqual(
            out.probability_yes,
            _model_p(65200.0, 64999.99, 8e-4, 300.0), places=9)

    def test_long_horizon_extension_valid(self):
        """t=10080 (7 jours) : probabilite produite (fini le rejet
        horizon_hors_perimetre), vol ancree, confiance plafonnee."""
        m = {"ticker": "KXBTCD-26AUG0717-T66499.99", "floor_strike": 66499.99}
        out = self.s.evaluate(m, {}, 10080.0)
        self.assertTrue(out.valid, out.reason)
        self.assertLessEqual(out.confidence, 6)
        self.assertEqual(out.features["horizon_mode"], "extended")
        self.assertEqual(out.features["sigma_effective"], 8e-4)
        self.assertEqual(out.features["strike"], 66499.99)
        self.assertNotIn("strike_source", out.features)
        self.assertAlmostEqual(
            out.probability_yes,
            _model_p(65200.0, 66499.99, 8e-4, 10080.0), places=9)
        self.assertGreater(out.probability_yes, 0.0)
        self.assertLess(out.probability_yes, 1.0)

    def test_long_horizon_vol_floor_used(self):
        """Vol 1m inferieure a l'ancre -> sigma_effective = ancre."""
        class QuietCtx(FakeCtx):
            realized_vol_1m = 2e-4
        s = BtcDailyStrategy(lambda **kw: QuietCtx())
        m = {"floor_strike": 66000.0}
        out = s.evaluate(m, {}, 10080.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.features["sigma_effective"],
                         s.LONG_HORIZON_VOL_ANCHOR)
        self.assertAlmostEqual(
            out.probability_yes,
            _model_p(65200.0, 66000.0, s.LONG_HORIZON_VOL_ANCHOR, 10080.0),
            places=9)

    def test_beyond_extension_cap_rejected(self):
        m = {"floor_strike": 65000.0}
        out = self.s.evaluate(m, {}, 14 * 24 * 60 + 1.0)
        self.assertFalse(out.valid)
        self.assertIn("horizon_hors_perimetre", out.reason)

    def test_ticker_strike_fallback(self):
        """KXBTCD sans floor_strike : le strike vient du ticker 'T<prix>'
        (trou de donnees API)."""
        m = {"ticker": "KXBTCD-26AUG0111-T72799.99"}
        out = self.s.evaluate(m, {}, 300.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.features["strike"], 72799.99)
        self.assertEqual(out.features["strike_source"], "ticker")

    def test_unknown_ticker_no_strike_rejects(self):
        m = {"ticker": "KXBTCD-X"}
        out = self.s.evaluate(m, {}, 300.0)
        self.assertFalse(out.valid)
        self.assertIn("strike_absent", out.reason)


class TestModelStrikeProxy(unittest.TestCase):
    def setUp(self):
        self.s = BtcModelStrategy(fake_ctx)

    def test_spot_proxy_when_strike_missing(self):
        """KXBTC15M sans champ de strike : strike = spot consensus, p =
        Phi(momentum), confiance plafonnee, flag strike_source."""
        m = {"ticker": "KXBTC15M-26JUL311245-45"}
        out = self.s.evaluate(m, {}, 12.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.features["strike_source"], "spot_proxy")
        self.assertEqual(out.features["strike"], 65200.0)
        self.assertLessEqual(out.confidence, 6)
        self.assertAlmostEqual(
            out.probability_yes,
            _model_p(65200.0, 65200.0, 8e-4, 12.0), places=9)

    def test_spot_proxy_invalid_context_rejects(self):
        class Bad:
            valid = False
            reason = "sources_spot_insuffisantes (1/2)"
        s = BtcModelStrategy(lambda **kw: Bad())
        out = s.evaluate({"ticker": "KXBTC15M-X"}, {}, 12.0)
        self.assertFalse(out.valid)
        self.assertIn("sources_spot_insuffisantes", out.reason)

    def test_legacy_field_strike_unchanged(self):
        """floor_strike present (production) : chemin byte-identique,
        aucune cle supplementaire."""
        m = {"ticker": "KXBTC15M-26JUL311230-30", "floor_strike": 65100.0}
        out = self.s.evaluate(m, {}, 12.0)
        self.assertTrue(out.valid, out.reason)
        self.assertEqual(out.features["strike"], 65100.0)
        self.assertNotIn("strike_source", out.features)
        self.assertEqual(out.confidence, 8)
        self.assertAlmostEqual(
            out.probability_yes,
            _model_p(65200.0, 65100.0, 8e-4, 12.0), places=9)

    def test_horizon_beyond_15m_still_rejected(self):
        """Pas d'extension d'horizon pour la serie 15 minutes."""
        m = {"ticker": "KXBTC15M-26JUL31-00", "floor_strike": 65000.0}
        out = self.s.evaluate(m, {}, 60.0)          # > max_minutes = 20
        self.assertFalse(out.valid)
        self.assertIn("horizon_hors_perimetre", out.reason)

    def test_no_proxy_for_daily_strategy(self):
        """KXBTC15M-like ticker SANS strike sur la strategie daily :
        pas de proxy spot (marche a strike fixe) -> strike_absent."""
        m = {"ticker": "KXBTCD-26AUG0111-T65000.00"}  # ticker sans 'T<prix>'
        m["ticker"] = "KXBTCD-26AUG0111-XX"
        out = BtcDailyStrategy(fake_ctx).evaluate(m, {}, 300.0)
        self.assertFalse(out.valid)
        self.assertIn("strike_absent", out.reason)


if __name__ == "__main__":
    unittest.main()
