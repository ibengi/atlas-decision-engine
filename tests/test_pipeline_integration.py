# -*- coding: utf-8 -*-
"""Tests d'integration (exigences F, H, I) sur le VRAI pipeline demo,
hors-ligne et deterministe : FakeClient injecte, contexte BTC injecte.
Couvre : cycle complet reussi (strategy_supported>0, model_probability>0,
ordre soumis/suivi/rempli), ordre non rempli, fill partiel, annulation TTL,
doublons, redemarrage avec ordre ouvert, reconciliation PnL, shadow,
separation DEMO/LIVE."""
import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP0 = tempfile.mkdtemp(prefix="kalshi_it_")
os.environ["DATA_DIR"] = _TMP0
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
import _bootstrap  # noqa: F401  (ajoute src/* au sys.path)

import kalshi_alpha_bot as bot                        # noqa: E402
from strategy_router import build_default_registry    # noqa: E402
from market_scanner import MarketScanner, ScannerConfig  # noqa: E402


def _iso(minutes_ahead):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes_ahead)).isoformat()


BTCD = "KXBTCD-26JUL2017-T64999.99"


def btcd_market(**kw):
    m = {"ticker": BTCD, "status": "open", "close_time": _iso(300),
         "floor_strike": 64999.99, "title": "BTC above 65,000 today?",
         "yes_bid": 46, "yes_ask": 48, "no_bid": 52, "no_ask": 54,
         "volume_24h": 5000}
    m.update(kw)
    return m


class FakeCtx:
    valid = True
    reason = "ok"
    spot = 65200.0
    realized_vol_1m = 8e-4
    returns = {"5m": 0.001}
    data_quality_score = 85.0


def fake_ctx(strike=None, minutes_remaining=None, **kw):
    return FakeCtx()


from mock_kalshi import MockKalshiClient

# Backwards-compatible fixture name used by the existing integration tests.
FakeClient = MockKalshiClient


def make_engine(client):
    """ExecutionEngine reel + registre a contexte BTC INJECTE + scanner
    cible sur le FakeClient. DATA_DIR isole par test."""
    tmp = tempfile.mkdtemp(prefix="kalshi_eng_")
    bot.CFG.DATA_DIR = tmp
    bot.CFG.SHADOW_MODE = False
    bot.CFG.KILL_SWITCH = False
    bot.CFG.ORDER_TTL_SECONDS = 0          # pas d'attente en test
    eng = bot.ExecutionEngine(client, capital=500.0)
    eng.router = build_default_registry(btc_context_provider=fake_ctx)
    cfg = ScannerConfig()
    cfg.general_crawl = False
    eng.scanner = MarketScanner(client, router=eng.router, data_dir=tmp)
    eng.scanner.cfg = cfg
    eng.pipeline.router = eng.router
    eng.pipeline.scanner = eng.scanner
    return eng, tmp


class TestFullDemoCycle(unittest.TestCase):
    def test_cycle_supported_model_and_confirmed_fill(self):
        """Livrables 6, 8, 9 : cycle complet reussi sur le vrai pipeline
        demo — strategy_supported>0, model_probability>0, ordre soumis,
        suivi, rempli, trade enregistre UNIQUEMENT apres confirmation."""
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        placed = eng.cycle(1)
        rep = json.load(open(os.path.join(tmp, "cycle_report.json")))
        self.assertGreater(rep["supported"], 0)            # etait 0
        self.assertGreater(rep["model_evaluated"], 0)      # etait 0
        self.assertGreater(rep["positive_edge"], 0)
        self.assertGreater(rep["positive_net_ev"], 0)
        self.assertEqual(rep["orders_submitted"], 1)
        self.assertEqual(placed, 1)                        # fill confirme
        self.assertEqual(len(cli.created_orders), 1)
        o = cli.created_orders[0]
        self.assertEqual(o["ticker"], BTCD)
        self.assertEqual(o["price"], 48)                   # ASK, pas last
        # trade enregistre avec fill confirme par /fills
        trades = json.load(open(os.path.join(tmp, "kalshi_trades.json")))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["filled_count"], 1)
        self.assertEqual(trades[0]["analysis"]["fee_source"], "api")
        # sizing petit compte : cout <= 1% de 93,26$
        self.assertLessEqual(o["count"] * o["price"] / 100.0,
                             93.26 * 0.01 + 1e-9)

    def test_duplicate_prevented_next_cycle(self):
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        eng.cycle(1)
        placed2 = eng.cycle(2)
        self.assertEqual(placed2, 0)
        self.assertEqual(len(cli.created_orders), 1)       # pas de doublon
        rep = json.load(open(os.path.join(tmp, "cycle_report.json")))
        self.assertIn("already_positioned", rep["rejections_by_reason"])

    def test_shadow_mode_never_creates_order(self):
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        bot.CFG.SHADOW_MODE = True
        try:
            placed = eng.cycle(1)
        finally:
            bot.CFG.SHADOW_MODE = False
        self.assertEqual(placed, 0)
        self.assertEqual(len(cli.created_orders), 0)

    def test_unfilled_order_records_no_trade(self):
        cli = FakeClient(order_scenario="none")
        eng, tmp = make_engine(cli)
        placed = eng.cycle(1)
        self.assertEqual(placed, 0)
        self.assertEqual(cli.cancelled, ["o1"])            # TTL -> annulation
        p = os.path.join(tmp, "kalshi_trades.json")
        trades = json.load(open(p)) if os.path.exists(p) else []
        self.assertEqual(trades, [])                       # AUCUN trade
        self.assertEqual(eng.posmgr.open_count(), 0)


class TestOrderLifecycle(unittest.TestCase):
    def _om(self, cli):
        tmp = tempfile.mkdtemp(prefix="kalshi_om_")
        bot.CFG.DATA_DIR = tmp
        bot.CFG.ORDER_TTL_SECONDS = 0
        return bot.OrderManager(cli), tmp

    def test_partial_fill_counts_real_quantity(self):
        cli = FakeClient(order_scenario="partial")
        om, _ = self._om(cli)
        res = om.place_and_track(BTCD, "yes", 3, 48)
        self.assertEqual(res.state, "partial")
        self.assertEqual(res.filled, 2)                    # quantite REELLE
        self.assertEqual(res.avg_price, 48)

    def test_ttl_cancel_only_if_still_open(self):
        cli = FakeClient(order_scenario="none")
        om, _ = self._om(cli)
        res = om.place_and_track(BTCD, "yes", 1, 48)
        self.assertEqual(res.filled, 0)
        self.assertIn("o1", cli.cancelled)
        self.assertEqual(res.state, "cancelled")
        # scenario fill : PAS d'annulation (statut deja terminal)
        cli2 = FakeClient(order_scenario="fill")
        om2, _ = self._om(cli2)
        res2 = om2.place_and_track(BTCD, "yes", 1, 48)
        self.assertEqual(res2.filled, 1)
        self.assertEqual(cli2.cancelled, [])

    def test_invariant_blocks_invalid_price(self):
        cli = FakeClient()
        om, _ = self._om(cli)
        res = om.place_and_track(BTCD, "yes", 1, None)
        self.assertEqual(res.state, "rejected")
        self.assertEqual(cli.created_orders, [])           # jamais appele

    def test_restart_with_open_order_is_idempotent(self):
        """Redemarrage avec un ordre reste ouvert : reconcilie UNE fois,
        aucun doublon au second redemarrage."""
        cli = FakeClient(order_scenario="fill")
        tmp = tempfile.mkdtemp(prefix="kalshi_rec_")
        bot.CFG.DATA_DIR = tmp
        cli._orders["o9"] = {"order_id": "o9", "status": "executed",
                             "taker_fill_count": 1, "side": "yes",
                             "price": 48}
        bot.JsonStore.save(os.path.join(tmp, bot.CFG.ORDERS_FILE),
                           {"o9": {"ticker": BTCD, "side": "yes",
                                   "count": 1, "price": 48,
                                   "placed_at": "2026-07-24T00:00:00+00:00"}})
        tlog = bot.TradeLogger()
        pm = bot.PositionManager(cli, tlog)
        om = bot.OrderManager(cli)
        om.reconcile_startup(tlog, pm)
        self.assertEqual(len(tlog.trades), 1)
        # second redemarrage : l'ordre n'est plus dans orders_state -> rien
        om2 = bot.OrderManager(cli)
        om2.reconcile_startup(tlog, pm)
        self.assertEqual(len(tlog.trades), 1)              # zero doublon


class TestSettlementReconciliation(unittest.TestCase):
    def test_pnl_realized_only_on_api_result(self):
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        eng.cycle(1)
        # pas de resultat API -> rien n'est regle
        self.assertEqual(eng.posmgr.check_settlements(), [])
        # resultat API yes -> reglement, PnL net = (1-0.48)*1 - frais
        cli.market["result"] = "yes"
        realized = eng.posmgr.check_settlements()
        self.assertEqual(len(realized), 1)
        t = realized[0]
        self.assertTrue(t["won"])
        self.assertAlmostEqual(t["gross_pnl"], 0.52, places=2)
        self.assertAlmostEqual(t["net_pnl"], t["gross_pnl"] - t["fees"],
                               places=2)
        self.assertEqual(eng.posmgr.open_count(), 0)


class TestDemoLiveSeparation(unittest.TestCase):
    def test_demo_requires_demo_keys(self):
        old = bot.CFG.DEMO_KEY_ID, bot.CFG.DEMO_PRIV_KEY
        bot.CFG.DEMO_KEY_ID, bot.CFG.DEMO_PRIV_KEY = "", ""
        try:
            with self.assertRaises(RuntimeError):
                bot.KalshiClient("demo")
        finally:
            bot.CFG.DEMO_KEY_ID, bot.CFG.DEMO_PRIV_KEY = old

    def test_prod_without_confirmations_exits(self):
        for var in ("KALSHI_ENV_CONFIRM", "LIVE_TRADING_CONFIRMED",
                    "LIVE_TRADING", "DEMO_TRADING"):
            os.environ.pop(var, None)
        argv = sys.argv
        sys.argv = ["kalshi_alpha_bot.py"]
        try:
            with self.assertRaises(SystemExit) as cm:
                bot.main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.argv = argv

    def test_gatekeeper_denies_by_default(self):
        from model_gatekeeper import check_live_allowed
        os.environ.pop("NO_LIVE_PROMOTION", None)
        os.environ.pop("MODEL_APPROVED_FOR_LIVE", None)
        ok, failed = check_live_allowed()
        self.assertFalse(ok)
        self.assertTrue(any("NO_LIVE_PROMOTION" in f for f in failed))


class TestRegistryFailFast(unittest.TestCase):
    def test_empty_registry_stops_engine(self):
        """BTC desactive + registre requis complet -> les strategies
        sports/election restent enregistrees ; mais un registre reellement
        vide arrete le moteur (SystemExit 2)."""
        import strategy_router as sr
        orig = sr.build_default_registry

        def broken(**kw):
            raise sr.RegistryValidationError("registre vide (test)")
        sr.build_default_registry = broken
        try:
            with self.assertRaises(SystemExit) as cm:
                make_engine(FakeClient())
            self.assertEqual(cm.exception.code, 2)
        finally:
            sr.build_default_registry = orig


if __name__ == "__main__":
    unittest.main()
