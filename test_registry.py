# -*- coding: utf-8 -*-
"""Tests du registre canonique (exigence A) : routage des 5 market_type
requis, echec sur registre vide, doublon interdit, honnetete des
strategies sans modele."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from strategy_router import (StrategyRouter, RegistryValidationError,
                             build_default_registry, REQUIRED_MARKET_TYPES,
                             BtcDailyStrategy, SportsMoneylineStrategy,
                             validate_strategy_registry, ModelOutput,
                             Decision, GateConfig, price_and_gate,
                             estimated_fee_per_contract)


class FakeCtx:
    valid = True
    reason = "ok"
    spot = 65200.0
    realized_vol_1m = 8e-4
    returns = {"5m": 0.001}
    data_quality_score = 85.0


def fake_ctx_provider(strike=None, minutes_remaining=None, **kw):
    return FakeCtx()


class TestRegistry(unittest.TestCase):
    def test_default_registry_serves_required_types(self):
        r = build_default_registry(btc_context_provider=fake_ctx_provider)
        matrix = r.registry_matrix()
        for mt in REQUIRED_MARKET_TYPES:
            self.assertIn(mt, matrix, f"{mt} non servi")
        # noms EXACTS journalisables
        self.assertEqual(matrix["btc_above_strike_daily"],
                         "btc_daily_above_strike")
        self.assertEqual(matrix["sports_spread"], "sports_spread_v1")

    def test_empty_registry_fails_startup(self):
        r = StrategyRouter()
        with self.assertRaises(RegistryValidationError):
            validate_strategy_registry(r)

    def test_missing_required_type_fails(self):
        r = StrategyRouter()
        r.register(BtcDailyStrategy(fake_ctx_provider))
        with self.assertRaises(RegistryValidationError) as cm:
            validate_strategy_registry(r)
        self.assertIn("sports_moneyline", str(cm.exception))

    def test_duplicate_market_type_rejected(self):
        r = StrategyRouter()
        r.register(SportsMoneylineStrategy())
        with self.assertRaises(RegistryValidationError):
            r.register(SportsMoneylineStrategy())

    def test_routing_is_by_market_type_not_category(self):
        """Un marche sportif mal categorise 'Commodities' doit quand meme
        etre route via son market_type (exigence A, derniere puce)."""
        r = build_default_registry(btc_context_provider=fake_ctx_provider)
        s = r.route("sports_spread")
        self.assertIsNotNone(s)
        self.assertEqual(s.name, "sports_spread_v1")

    def test_unknown_type_not_routed(self):
        r = build_default_registry(btc_context_provider=fake_ctx_provider)
        self.assertIsNone(r.route("unknown"))


class TestHonestNoModel(unittest.TestCase):
    def test_sports_without_provider_rejects(self):
        out = SportsMoneylineStrategy().evaluate({}, {}, 60.0)
        self.assertFalse(out.valid)
        self.assertIn("no_model_probability", out.reason)

    def test_sports_with_uncalibrated_prob_rejects(self):
        s = SportsMoneylineStrategy(lambda m, b, t: (1.5, 9))
        out = s.evaluate({}, {}, 60.0)
        self.assertFalse(out.valid)
        self.assertIn("prob_non_calibree", out.reason)

    def test_btc_daily_produces_real_probability(self):
        s = BtcDailyStrategy(fake_ctx_provider)
        m = {"ticker": "KXBTCD-26JUL2017-T64999.99", "floor_strike": 64999.99}
        out = s.evaluate(m, {}, 300.0)
        self.assertTrue(out.valid, out.reason)
        self.assertGreater(out.probability_yes, 0.0)
        self.assertLess(out.probability_yes, 1.0)
        self.assertGreaterEqual(out.confidence, 6)

    def test_btc_daily_invalid_context_rejects(self):
        class Bad:
            valid = False
            reason = "sources_spot_insuffisantes (1/2)"
        s = BtcDailyStrategy(lambda **kw: Bad())
        out = s.evaluate({"floor_strike": 65000.0}, {}, 300.0)
        self.assertFalse(out.valid)
        self.assertIn("no_model_probability", out.reason)


class TestPricingGates(unittest.TestCase):
    """Exigence E : ask a l'achat, jamais last_price ; frais+spread+
    slippage+tampon deduits ; EV nette exigee > 0."""

    def _dec(self):
        return Decision(ticker="T", strategy="s")

    def test_uses_ask_never_last_price(self):
        g = GateConfig()
        book = {"yes_ask": 40, "yes_bid": 38, "no_ask": 62, "no_bid": 60,
                "spread": 2, "last_price": 10}     # last_price aberrant
        model = ModelOutput(True, "ok", probability_yes=0.55, confidence=8)
        d = price_and_gate(self._dec(), model, book, g)
        self.assertTrue(d.accepted, d.rejection_reason)
        self.assertEqual(d.entry_ask, 40)          # ask, pas last_price
        self.assertAlmostEqual(d.market_probability, 0.40)

    def test_missing_ask_means_no_fill_assumed(self):
        g = GateConfig()
        book = {"yes_ask": None, "yes_bid": 38, "no_ask": None, "no_bid": 60,
                "spread": 2}
        model = ModelOutput(True, "ok", probability_yes=0.9, confidence=9)
        d = price_and_gate(self._dec(), model, book, g)
        self.assertFalse(d.accepted)
        self.assertEqual(d.rejection_reason, "no_positive_edge")

    def test_spread_too_wide(self):
        g = GateConfig(MAX_ACCEPTABLE_SPREAD=4)
        book = {"yes_ask": 50, "yes_bid": 40, "no_ask": 60, "no_bid": 50,
                "spread": 10}
        model = ModelOutput(True, "ok", probability_yes=0.9, confidence=9)
        d = price_and_gate(self._dec(), model, book, g)
        self.assertFalse(d.accepted)
        self.assertIn("spread_too_wide", d.rejection_reason)

    def test_net_edge_deducts_fee_slippage_buffer(self):
        g = GateConfig()
        book = {"yes_ask": 40, "yes_bid": 39, "spread": 1}
        model = ModelOutput(True, "ok", probability_yes=0.50, confidence=8)
        d = price_and_gate(self._dec(), model, book, g)
        fee = estimated_fee_per_contract(40, g.FEE_RATE)
        self.assertAlmostEqual(d.gross_edge, 0.10, places=6)
        self.assertAlmostEqual(d.net_edge,
                               0.10 - fee - 0.01 - g.UNCERTAINTY_BUFFER,
                               places=6)

    def test_low_confidence_rejected(self):
        g = GateConfig(MIN_MODEL_CONFIDENCE=6)
        book = {"yes_ask": 40, "yes_bid": 39, "spread": 1}
        model = ModelOutput(True, "ok", probability_yes=0.6, confidence=3)
        d = price_and_gate(self._dec(), model, book, g)
        self.assertFalse(d.accepted)
        self.assertIn("low_model_confidence", d.rejection_reason)


if __name__ == "__main__":
    unittest.main()
