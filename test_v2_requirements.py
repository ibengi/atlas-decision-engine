# -*- coding: utf-8 -*-
"""Tests des exigences v2 du cahier des charges :
- Tache 2 : diagnostic complet par decision, raison finale UNIQUE ;
- Tache 4/6 : le scanner ne cible que les types avec source de probabilite,
  exclusions explicites no_compatible_strategy / no_probability_provider ;
- Tache 5 : plus d'"unknown" quand la categorie API identifie le marche ;
- Tache 8 : funnel avec pourcentages de conversion ;
- Tache 9 : statistiques de performance honnetes (low_sample explicite)."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import json
import tempfile
from datetime import datetime, timedelta, timezone

from market_classifier import classify
from market_scanner import MarketScanner, ScannerConfig
from strategy_router import build_default_registry
from performance import compute_stats


def _iso(minutes):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes)).isoformat()


def fake_ctx(**kw):
    class C:
        valid = True; reason = "ok"; spot = 65200.0
        realized_vol_1m = 8e-4; returns = {"5m": 0.001}
        data_quality_score = 85.0
    return C()


class TestCoarseClassification(unittest.TestCase):
    """Tache 5 : identifiable => jamais unknown ; PGA => Sports."""

    def test_pga_is_sports_never_unknown_or_tech(self):
        c = classify({"ticker": "KXPGATOURWIN-26AUG-SCHEF",
                      "category": "Tech & Science"})   # categorie API fausse
        self.assertEqual(c.category, "Sports")
        self.assertEqual(c.family, "PGA")
        self.assertNotEqual(c.market_type, "unknown")

    def test_api_category_gives_coarse_type(self):
        for cat, expect in (("Sports", "sports_other"),
                            ("Weather", "weather_other"),
                            ("Financials", "financials_other"),
                            ("Politics", "politics_other")):
            c = classify({"ticker": "KXZZZ-26AUG-X", "category": cat})
            self.assertEqual(c.market_type, expect, cat)
            self.assertEqual(c.confidence, 1)

    def test_truly_unidentifiable_stays_unknown(self):
        c = classify({"ticker": "KXZZZ-26AUG-X"})
        self.assertEqual(c.market_type, "unknown")
        self.assertEqual(c.confidence, 0)


class TestTradeableScope(unittest.TestCase):
    """Tache 4/6 : perimetre du scanner = types AVEC source de probabilite."""

    def test_router_tradeable_subset(self):
        r = build_default_registry(btc_context_provider=fake_ctx)
        sup, trd = set(r.supported_market_types()), \
            set(r.tradeable_market_types())
        self.assertTrue(trd <= sup)
        self.assertIn("btc_above_strike_daily", trd)
        # sports/election SANS fournisseur : routables mais PAS tradables
        self.assertIn("sports_moneyline", sup)
        self.assertNotIn("sports_moneyline", trd)
        self.assertNotIn("election_winner", trd)

    def test_provider_extends_scope_automatically(self):
        r = build_default_registry(btc_context_provider=fake_ctx,
                                   sports_provider=lambda *a: 0.6)
        self.assertIn("sports_moneyline", r.tradeable_market_types())

    def test_scanner_targets_only_tradeable_series(self):
        r = build_default_registry(btc_context_provider=fake_ctx)
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(client=None, router=r, data_dir=d)
            series = sc.target_series()
        self.assertTrue(any(s.startswith("KXBTC") for s in series))
        # aucune serie sports/election telechargee sans fournisseur
        for s in series:
            self.assertFalse(any(k in s for k in
                                 ("WNBA", "NBA", "MLB", "ATP", "PRES")),
                             f"serie non tradable ciblee: {s}")

    def test_crawl_filter_reasons_are_explicit(self):
        r = build_default_registry(btc_context_provider=fake_ctx)

        class Cli:
            def _req(self, m, p, params=None):
                return {"markets": [
                    # sports supporte mais sans fournisseur
                    {"ticker": "KXWNBAGAME-26JUL25LVNY-LV", "status": "open",
                     "close_time": _iso(120), "yes_ask": 45, "yes_bid": 44,
                     "volume_24h": 10},
                    # type grossier, aucune strategie
                    {"ticker": "KXZZZ-26AUG-X", "category": "Weather",
                     "status": "open", "close_time": _iso(120),
                     "yes_ask": 45, "yes_bid": 44, "volume_24h": 10},
                ], "cursor": None}

            def get_markets(self, *a, **k):
                return []
        cfg = ScannerConfig()
        cfg.general_crawl = True
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(Cli(), router=r, cfg=cfg, data_dir=d)
            res = sc.scan_cycle()
        ex = res["report"]["excluded_by_reason"]
        self.assertEqual(ex.get("no_probability_provider"), 1)
        self.assertEqual(ex.get("no_compatible_strategy"), 1)
        self.assertEqual(len(res["markets"]), 0)


class TestDecisionDiagnostics(unittest.TestCase):
    """Tache 2 : chaque decision expose le diagnostic complet + raison
    finale unique. Verifie via le vrai pipeline demo."""

    def test_decision_jsonl_has_full_diagnostic(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_pipeline_integration import FakeClient, make_engine
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        eng.cycle(1)
        lines = open(os.path.join(tmp, "decisions.jsonl"),
                     encoding="utf-8").read().splitlines()
        recs = [json.loads(x)["decision"] for x in lines]
        acc = [d for d in recs if d.get("accepted")]
        self.assertEqual(len(acc), 1)
        d = acc[0]
        for field in ("ticker", "category", "market_type", "strategy",
                      "liquidity", "spread", "model_probability",
                      "market_probability", "net_edge", "net_ev",
                      "kelly_fraction", "confidence", "decision_id",
                      "final_decision"):
            self.assertIn(field, d)
            self.assertIsNotNone(d[field], field)
        self.assertEqual(d["final_decision"], "Accepted")
        self.assertGreater(d["kelly_fraction"], 0)
        self.assertEqual(d["spread"], 2)               # 48-46
        self.assertEqual(d["liquidity"], 5000.0)

    def test_rejected_has_single_final_reason(self):
        from test_pipeline_integration import FakeClient, make_engine
        cli = FakeClient(order_scenario="fill")
        cli.market["yes_ask"] = 60
        cli.market["yes_bid"] = 40                      # spread 20 -> rejet
        cli.market["no_ask"] = 60
        cli.market["no_bid"] = 40
        eng, tmp = make_engine(cli)
        eng.cycle(1)
        lines = open(os.path.join(tmp, "decisions.jsonl"),
                     encoding="utf-8").read().splitlines()
        d = json.loads(lines[-1])["decision"]
        self.assertFalse(d["accepted"])
        self.assertTrue(d["final_decision"].startswith("Rejected: "))
        self.assertNotIn("\n", d["final_decision"])     # UNE raison, une ligne


class TestFunnelConversion(unittest.TestCase):
    """Tache 8 : pourcentages de conversion a chaque etape."""

    def test_percentages_present_and_consistent(self):
        from test_pipeline_integration import FakeClient, make_engine
        eng, tmp = make_engine(FakeClient(order_scenario="fill"))
        eng.cycle(1)
        rep = json.load(open(os.path.join(tmp, "cycle_report.json")))
        conv = rep["funnel_conversion"]
        for k in ("scanned_raw", "liquid", "supported", "model_evaluated",
                  "positive_edge", "risk_passed", "orders_submitted",
                  "fills"):
            self.assertIn(k, conv)
            self.assertIn("pct_of_prev", conv[k])
        self.assertEqual(conv["fills"]["n"], 1)
        self.assertEqual(conv["fills"]["pct_of_prev"], 100.0)


class TestPerformanceStats(unittest.TestCase):
    """Tache 9 : stats exactes sur petits echantillons, honnetement
    marquees low_sample."""

    def _trade(self, pnl, price=48, count=1, edge=0.1, mins=30):
        t0 = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
        return {"net_pnl": pnl, "avg_fill_price": price,
                "filled_count": count, "edge": edge,
                "timestamp": t0.isoformat(),
                "settled_at": (t0 + timedelta(minutes=mins)).isoformat(),
                "state": "settled"}

    def test_known_values(self):
        s = compute_stats([self._trade(0.50), self._trade(0.50),
                           self._trade(-0.48)], capital=93.26)
        self.assertEqual(s["trades_executed"], 3)
        self.assertTrue(s["low_sample"])                # < 30 : explicite
        self.assertAlmostEqual(s["win_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(s["pnl"], 0.52, places=2)
        self.assertAlmostEqual(s["profit_factor"], 1.0 / 0.48, places=2)
        self.assertAlmostEqual(s["expectancy_per_trade"], 0.52 / 3, places=4)
        self.assertAlmostEqual(s["average_edge"], 0.1, places=4)
        self.assertEqual(s["average_holding_minutes"], 30.0)
        self.assertAlmostEqual(s["max_drawdown"], -0.48, places=2)

    def test_empty_is_safe(self):
        s = compute_stats([])
        self.assertEqual(s["trades_executed"], 0)
        self.assertNotIn("win_rate", s)


if __name__ == "__main__":
    unittest.main()
