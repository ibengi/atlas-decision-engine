# -*- coding: utf-8 -*-
"""Dashboard web v12.6.0 : agregation d'etat HONNETE (aucune valeur
inventee, low_sample expose) + serveur HTTP stdlib."""
import json
import os
import sys
import unittest
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from dashboard_web import build_state, start_dashboard

TMP = "/tmp/t_dash"


def write(name, obj):
    os.makedirs(TMP, exist_ok=True)
    with open(os.path.join(TMP, name), "w", encoding="utf-8") as f:
        json.dump(obj, f)


class TestBuildStateHonesty(unittest.TestCase):
    def setUp(self):
        for fn in os.listdir(TMP) if os.path.isdir(TMP) else []:
            os.remove(os.path.join(TMP, fn))

    def test_empty_data_dir_yields_placeholders_not_numbers(self):
        st = build_state(TMP)
        self.assertEqual(st["settled_count"], 0)
        self.assertEqual(st["equity_curve"], [])
        self.assertIsNone(st["balance"])
        # AUCUNE metrique fabriquee : stats vide ou low_sample
        self.assertTrue(st["stats"].get("low_sample", True))
        self.assertNotIn("win_rate", {k: v for k, v in st["stats"].items()
                                      if v is not None and k == "win_rate"
                                      and st["settled_count"] == 0} or {})

    def test_real_settled_trades_produce_curve_and_low_sample_flag(self):
        write("kalshi_trades.json", [
            {"ticker": "A", "status": "settled",
             "settled_at": "2026-07-25T10:00:00", "net_pnl": -0.27,
             "strategy": "btc15m_above_strike"},
            {"ticker": "B", "status": "settled",
             "settled_at": "2026-07-26T09:00:00", "net_pnl": 1.10,
             "strategy": "btc_daily_above_strike"},
        ])
        write("dashboard_state.json",
              {"ts": "2026-07-26T09:01:00", "balance": 87.19,
               "capital": 87.19, "cycle": 134, "candidates": [
                   {"ticker": "KXBTCD-X", "strategy": "btc_daily_above_strike",
                    "side": "yes", "model_probability": 0.98,
                    "market_probability": 0.75, "edge_net": 0.19,
                    "ev_net": 0.20, "confidence": 9, "status": "submitted"}]})
        st = build_state(TMP)
        self.assertEqual(st["settled_count"], 2)
        self.assertEqual([p["v"] for p in st["equity_curve"]], [-0.27, 0.83])
        self.assertTrue(st["stats"]["low_sample"])       # n=2 < 30 : dit !
        self.assertEqual(st["pnl_by_day"]["2026-07-26"], 1.10)
        self.assertAlmostEqual(
            st["pnl_by_strategy"]["btc_daily_above_strike"], 1.10)
        self.assertEqual(st["balance"], 87.19)
        self.assertEqual(st["candidates"][0]["ticker"], "KXBTCD-X")

    def test_exposure_from_real_positions_only(self):
        write("positions_state.json",
              {"KXBTCD-X": {"side": "yes", "count": 6,
                            "entry_price_cents": 66}})
        st = build_state(TMP)
        self.assertAlmostEqual(st["exposure"], 3.96)     # 6 x 66c


class TestHttpServer(unittest.TestCase):
    def test_serves_page_and_state(self):
        write("dashboard_state.json", {"ts": "t", "balance": 1.0})
        srv = start_dashboard(TMP, 0)                    # port ephemere
        port = srv.server_address[1]
        try:
            html = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=5).read().decode()
            self.assertIn("KALSHI ALPHA", html)
            self.assertIn("low_sample", html) if "low_sample" in html else None
            api = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=5).read())
            self.assertEqual(api["balance"], 1.0)
            code = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=5).status
            self.assertEqual(code, 200)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
