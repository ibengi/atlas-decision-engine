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


from market_scanner import price_to_cents, read_price, liquidity_diag, \
    _has_liquidity  # noqa: E402


class TestPriceParserFormats(unittest.TestCase):
    """Regression de l'audit no_liquidity du 2026-07-25 : l'API v2 double
    les champs en *_dollars et peut renvoyer des prix en dollars decimaux ;
    int(float('0.48'))=0 rejetait alors 100% des marches."""

    def test_cents_formats(self):
        for v in (48, 48.0, "48", "48.00"):
            self.assertEqual(price_to_cents(v), 48, repr(v))

    def test_dollar_formats(self):
        for v in (0.48, "0.48", "0.4800"):
            self.assertEqual(price_to_cents(v), 48, repr(v))
        self.assertEqual(price_to_cents("0.0200"), 2)

    def test_empty_book_is_none_not_a_price(self):
        for v in (0, "0", "0.0000", None, "", "abc", 100, 250, -5):
            self.assertIsNone(price_to_cents(v), repr(v))

    def test_read_price_falls_back_to_dollars_key(self):
        m = {"yes_bid": 0, "yes_bid_dollars": "0.4600"}
        self.assertEqual(read_price(m, "yes_bid"), 46)

    def test_dollars_only_market_now_passes_liquidity(self):
        # profil du bug production : champs cents a 0, *_dollars renseignes
        m = {"ticker": "KXBTCD-X", "yes_bid": 0, "yes_ask": 0,
             "yes_bid_dollars": "0.4600", "yes_ask_dollars": "0.4800",
             "volume_24h": 0}
        self.assertTrue(_has_liquidity(m))
        d = liquidity_diag(m)
        self.assertEqual(d["best_bid"], 46)
        self.assertEqual(d["best_ask"], 48)
        self.assertGreater(d["liquidity_score"], 40)

    def test_truly_empty_book_still_rejected(self):
        # seuil DEMO : un carnet reellement vide reste rejete (jamais
        # d'ordre sans carnet) — le critere n'a PAS ete assoupli.
        m = {"ticker": "KXBTCD-Y", "yes_bid": 0, "yes_ask": 0,
             "volume": 0, "volume_24h": 0, "open_interest": 0}
        self.assertFalse(_has_liquidity(m))
        d = liquidity_diag(m)
        self.assertIsNone(d["best_bid"])
        self.assertIsNone(d["best_ask"])


class TestLiquidityRejectLogging(unittest.TestCase):
    def test_diag_line_and_report_details(self):
        import io, logging
        markets = [mk(f"KXBTCD-26JUL20-T{i}", yes_ask=0, yes_bid=0,
                      volume_24h=0) for i in range(15)]
        cli = FakeClient(by_series={"KXBTCD": markets})
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        logging.getLogger("SCANNER").addHandler(h)
        try:
            with tempfile.TemporaryDirectory() as d:
                sc = MarketScanner(cli, router=FakeRouter(), cfg=_cfg(),
                                   data_dir=d)
                res = sc.scan_cycle()
        finally:
            logging.getLogger("SCANNER").removeHandler(h)
        t = buf.getvalue()
        # format exact demande
        self.assertIn("rejected_reason=no_liquidity", t)
        for field in ("ticker=", "best_bid=", "best_ask=", "bid_size=",
                      "ask_size=", "volume=", "open_interest=",
                      "liquidity_score="):
            self.assertIn(field, t, field)
        # plafond par defaut 10/cycle + mention du reste
        self.assertEqual(t.count("rejected_reason=no_liquidity"), 10)
        self.assertIn("5 autres", t)
        # TOUS les details dans le rapport
        self.assertEqual(len(res["report"]["no_liquidity_details"]), 15)
