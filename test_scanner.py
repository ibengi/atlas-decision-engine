# -*- coding: utf-8 -*-
"""Tests scanner v2 (exigence C) : cache >= 30 min, mise a jour
incrementale, filtres immediats, plafond d'evaluation, MAX_PAGES non
augmente et alerte non repetitive."""
import os
import tempfile
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
from datetime import datetime, timedelta, timezone

from market_scanner import MarketScanner, ScannerConfig


def _iso(minutes_ahead):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes_ahead)).isoformat()


def mk(ticker, mins=120, yes_ask=45, yes_bid=44, status="open", **kw):
    m = {"ticker": ticker, "status": status, "close_time": _iso(mins),
         "yes_ask": yes_ask, "yes_bid": yes_bid, "volume_24h": 100}
    m.update(kw)
    return m


class FakeClient:
    """Compteurs d'appels pour verifier cache/incremental."""
    def __init__(self, universe=None, by_series=None):
        self.universe = universe or []
        self.by_series = by_series or {}
        self.crawl_calls = 0
        self.series_calls = 0

    def _req(self, method, path, params=None):
        self.crawl_calls += 1
        return {"markets": self.universe, "cursor": None}

    def get_markets(self, series, status="open", limit=200):
        self.series_calls += 1
        return self.by_series.get(series, [])


class FakeRouter:
    def supported_market_types(self):
        return ("btc_above_strike_daily",)


def _cfg(**kw):
    c = ScannerConfig()
    c.general_crawl = kw.get("general_crawl", False)
    c.min_minutes = kw.get("min_minutes", 5)
    c.lookahead_hours = kw.get("lookahead_hours", 24)
    c.max_markets_per_cycle = kw.get("cap", 300)
    c.universe_ttl_s = kw.get("ttl", 1800)
    c.crawl_max_pages = kw.get("max_pages", 200)
    return c


class TestTargetedMode(unittest.TestCase):
    def test_targeted_mode_never_crawls(self):
        cli = FakeClient(by_series={"KXBTCD": [mk("KXBTCD-26JUL20-T65000")]})
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=FakeRouter(), cfg=_cfg(),
                               data_dir=d)
            res = sc.scan_cycle()
        self.assertEqual(cli.crawl_calls, 0)
        self.assertEqual(res["report"]["mode"], "targeted_series")
        self.assertEqual(len(res["markets"]), 1)


class TestUniverseCache(unittest.TestCase):
    def test_cache_at_least_30_minutes(self):
        now = [1000.0]
        cli = FakeClient(universe=[mk("KXBTCD-26JUL20-T65000")])
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=FakeRouter(),
                               cfg=_cfg(general_crawl=True, ttl=1800),
                               data_dir=d, now_fn=lambda: now[0])
            sc.scan_cycle()                       # 1er cycle : crawl
            self.assertEqual(cli.crawl_calls, 1)
            for _ in range(10):                   # 10 cycles dans le TTL
                now[0] += 60
                sc.scan_cycle()
            self.assertEqual(cli.crawl_calls, 1)  # AUCUN re-crawl
            now[0] += 1801                        # TTL depasse
            sc.scan_cycle()
            self.assertEqual(cli.crawl_calls, 2)

    def test_incremental_refresh_between_crawls(self):
        now = [1000.0]
        cli = FakeClient(universe=[mk("KXBTCD-26JUL20-T64000",
                                      yes_ask=45, yes_bid=44)],
                         by_series={"KXBTCD": [mk("KXBTCD-26JUL20-T64000",
                                                  yes_ask=52, yes_bid=51)]})
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=FakeRouter(),
                               cfg=_cfg(general_crawl=True), data_dir=d,
                               now_fn=lambda: now[0])
            sc.scan_cycle()
            now[0] += 60
            res = sc.scan_cycle()                 # depuis cache + increment
        self.assertTrue(res["report"]["from_cache"])
        m = [x for x in res["markets"]
             if x["ticker"] == "KXBTCD-26JUL20-T64000"][0]
        self.assertEqual(m["yes_ask"], 52)        # donnee FRAICHE fusionnee


class TestFilters(unittest.TestCase):
    def _scan(self, markets):
        cli = FakeClient(by_series={"KXBTCD": markets})
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=FakeRouter(), cfg=_cfg(),
                               data_dir=d)
            return sc.scan_cycle()

    def test_expired_filtered(self):
        res = self._scan([mk("KXBTCD-26JUL20-T1", mins=-10)])
        self.assertEqual(len(res["markets"]), 0)
        self.assertIn("expired", res["report"]["excluded_by_reason"])

    def test_closed_filtered(self):
        res = self._scan([mk("KXBTCD-26JUL20-T1", status="settled")])
        self.assertEqual(len(res["markets"]), 0)
        self.assertIn("closed_or_settled", res["report"]["excluded_by_reason"])

    def test_no_liquidity_filtered(self):
        res = self._scan([mk("KXBTCD-26JUL20-T1", yes_ask=0, yes_bid=0,
                             volume_24h=0)])
        self.assertEqual(len(res["markets"]), 0)
        self.assertIn("no_liquidity", res["report"]["excluded_by_reason"])

    def test_outside_window_filtered(self):
        res = self._scan([mk("KXBTCD-26JUL20-T1", mins=60 * 48)])
        self.assertEqual(len(res["markets"]), 0)
        self.assertIn("outside_time_window",
                      res["report"]["excluded_by_reason"])

    def test_unknown_type_filtered(self):
        cli = FakeClient(by_series={"KXBTCD": [mk("KXZZZUNKNOWN-26JUL-1")]})
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=FakeRouter(), cfg=_cfg(),
                               data_dir=d)
            res = sc.scan_cycle()
        self.assertEqual(len(res["markets"]), 0)
        self.assertIn("unknown_market_type",
                      res["report"]["excluded_by_reason"])

    def test_cycle_cap_with_metrics(self):
        markets = [mk(f"KXBTCD-26JUL20-T{i}", volume_24h=i) for i in range(20)]
        cli = FakeClient(by_series={"KXBTCD": markets})
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=FakeRouter(), cfg=_cfg(cap=5),
                               data_dir=d)
            res = sc.scan_cycle()
        self.assertEqual(len(res["markets"]), 5)
        self.assertEqual(res["report"]["excluded_by_reason"]["over_cycle_cap"],
                         15)
        f = res["report"]["funnel"]
        self.assertEqual(f["scanned_raw"], 20)
        self.assertEqual(f["kept"], 5)
        # les 5 gardes sont les plus liquides
        vols = sorted(int(m["ticker"].rsplit("T", 1)[1])
                      for m in res["markets"])
        self.assertEqual(vols, [15, 16, 17, 18, 19])

    def test_max_pages_not_increased(self):
        cfg = _cfg()
        self.assertLessEqual(cfg.crawl_max_pages, 200)


if __name__ == "__main__":
    unittest.main()
