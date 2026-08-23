# -*- coding: utf-8 -*-
"""P8 — Performance : caches de requete + parallelisme (tous desactives par
defaut ; ce fichier active chaque flag UNIQUEMENT le temps d'un test).

Couvre :
- TTLCache (api_cache) : get/set/expiration/clear ;
- KalshiClient : solde et listings en cache TTL, jamais de valeur None /
  liste vide en cache, purge par cycle (clear_caches) ;
- MarketScanner : fetch PARALLELE des series (ThreadPoolExecutor) avec
  ORDRE byte-identical au sequentiel et concurrency prouvee ;
- btc_context : contexte BTC calcule UNE FOIS par cycle (BTC_CONTEXT_CYCLE_CACHE),
  purge par begin_cycle() ;
- ExecutionEngine : cycle parallele (solde+sante en arriere-plan pendant le
  scan) — meme resultat que le sequentiel, solde fetch UNE fois.
"""
import os
import sys
import json
import time
import copy
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")

from api_cache import TTLCache            # noqa: E402
from kalshi_client import KalshiClient    # noqa: E402
from market_scanner import MarketScanner, ScannerConfig  # noqa: E402
from config import CFG                     # noqa: E402
import btc_context as bc                   # noqa: E402


# ══════════════════════════════════════════════════════════════════════
# TTLCache
# ══════════════════════════════════════════════════════════════════════

class TestTTLCache(unittest.TestCase):
    def test_set_get_roundtrip(self):
        c = TTLCache(ttl_s=60)
        self.assertIsNone(c.get("a"))
        c.set("a", {"v": 1})
        self.assertEqual(c.get("a"), {"v": 1})

    def test_expiry_and_purge(self):
        c = TTLCache(ttl_s=0.05)
        c.set("a", 1)
        time.sleep(0.07)
        self.assertIsNone(c.get("a"))          # expire -> None
        self.assertEqual(len(c), 0)            # et purgee

    def test_clear(self):
        c = TTLCache(ttl_s=60)
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        self.assertIsNone(c.get("a"))
        self.assertIsNone(c.get("b"))
        self.assertEqual(len(c), 0)

    def test_independent_keys(self):
        c = TTLCache(ttl_s=60)
        c.set("k1", 1)
        c.set("k2", 2)
        self.assertEqual(c.get("k1"), 1)
        self.assertEqual(c.get("k2"), 2)


# ══════════════════════════════════════════════════════════════════════
# KalshiClient — caches de requete (API_CACHE_ENABLED=1 par instance)
# ══════════════════════════════════════════════════════════════════════

class TestClientCaching(unittest.TestCase):
    def _client(self):
        cli = KalshiClient("demo", cache_enabled=True)
        return cli

    def test_balance_cached_within_ttl(self):
        cli = self._client()
        calls = []

        def fake_fetch():
            calls.append(1)
            return 123.45

        with mock.patch.object(cli, "_fetch_balance", fake_fetch):
            self.assertEqual(cli.get_balance(), 123.45)
            self.assertEqual(cli.get_balance(), 123.45)   # cache hit
            self.assertEqual(cli.get_balance(), 123.45)
        self.assertEqual(len(calls), 1)                   # 1 seul fetch

    def test_balance_none_never_cached(self):
        cli = self._client()
        with mock.patch.object(cli, "_fetch_balance",
                               mock.Mock(side_effect=[None, 55.0])):
            self.assertIsNone(cli.get_balance())          # erreur -> None
            self.assertEqual(cli.get_balance(), 55.0)     # re-fetch OK
        self.assertEqual(cli._balance_cache.get("balance"), 55.0)

    def test_balance_clear_caches_refetches(self):
        cli = self._client()
        calls = []

        def fake_fetch():
            calls.append(1)
            return 10.0

        with mock.patch.object(cli, "_fetch_balance", fake_fetch):
            cli.get_balance()
            cli.clear_caches()                            # debut de cycle
            cli.get_balance()
        self.assertEqual(len(calls), 2)                   # purge => re-fetch

    def test_markets_cached_per_key(self):
        cli = self._client()
        cli.cache_enabled = True
        cli._markets_cache = TTLCache(60)
        seen = []

        def fake_req(method, path, params=None, **kw):
            seen.append((params or {}).get("series_ticker"))
            return {"markets": [{"ticker": "KXBTCD-A",
                                 "series_ticker": (params or {}).get("series_ticker")}]}

        with mock.patch.object(cli, "_req", fake_req):
            r1 = cli.get_markets("KXBTCD", status="open", limit=200)
            r2 = cli.get_markets("KXBTCD", status="open", limit=200)   # hit
            r3 = cli.get_markets("KXBTC15M", status="open", limit=200)  # autre cle
        self.assertEqual(len(seen), 2)                    # KXBTCD x1 + KXBTC15M x1
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1, r2)
        self.assertEqual(r3[0]["series_ticker"], "KXBTC15M")

    def test_markets_empty_never_cached(self):
        cli = self._client()
        cli.cache_enabled = True
        cli._markets_cache = TTLCache(60)
        with mock.patch.object(cli, "_req", return_value={"markets": []}):
            self.assertEqual(cli.get_markets("KXBTCD"), [])
            self.assertEqual(cli.get_markets("KXBTCD"), [])   # re-fetch a chaque fois
        self.assertEqual(len(cli._markets_cache), 0)

    def test_cache_disabled_by_default_is_direct(self):
        # Sans flag, un client "standard" n'a aucun cache (zero changement).
        cli = KalshiClient("demo")
        self.assertFalse(cli.cache_enabled)
        self.assertIsNone(cli._balance_cache)
        cli.clear_caches()            # no-op sans crash


# ══════════════════════════════════════════════════════════════════════
# MarketScanner — fetch parallele des series, ordre byte-identical
# ══════════════════════════════════════════════════════════════════════

class _ConcurrentClient:
    """FakeClient qui mesure la concurrency reelle (lock) et dort un peu
    pour permettre le chevauchement des threads."""

    def __init__(self, by_series):
        self.by_series = by_series
        self.series_calls = 0
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def _req(self, method, path, params=None):
        return {"markets": [], "cursor": None}

    def get_markets(self, series, status="open", limit=200):
        with self._lock:
            self.series_calls += 1
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            time.sleep(0.03)
            return [dict(m) for m in self.by_series.get(series, [])]
        finally:
            with self._lock:
                self._active -= 1


def _mk(ticker, mins=120, yes_ask=45, yes_bid=44):
    from datetime import datetime, timedelta, timezone
    return {"ticker": ticker, "status": "open",
            "close_time": (datetime.now(timezone.utc)
                           + timedelta(minutes=mins)).isoformat(),
            "yes_ask": yes_ask, "yes_bid": yes_bid, "volume_24h": 100}


class _FakeRouter:
    def supported_market_types(self):
        return ("btc_above_strike_daily", "sports_moneyline")

    def tradeable_market_types(self):
        return ("btc_above_strike_daily", "sports_moneyline")


_SERIES = ["KXBTCD", "KXWNBAGAME", "KXNBAGAME", "KXNFLGAME"]


def _by_series():
    return {
        "KXBTCD": [_mk("KXBTCD-26JUL20-T64000", mins=300, yes_ask=45)],
        "KXWNBAGAME": [_mk("KXWNBAGAME-26JUL20-1", mins=200, yes_ask=47)],
        "KXNBAGAME": [_mk("KXNBAGAME-26JUL20-1", mins=250, yes_ask=49)],
        "KXNFLGAME": [_mk("KXNFLGAME-26JUL20-1", mins=180, yes_ask=51)],
    }


def _scanner(by_series, parallel, workers=4):
    cfg = ScannerConfig()
    cfg.general_crawl = False
    cfg.priority_series = list(_SERIES)
    cfg.parallel_series = parallel
    cfg.parallel_workers = workers
    with tempfile.TemporaryDirectory() as d:
        return MarketScanner(_ConcurrentClient(by_series), router=_FakeRouter(),
                             cfg=cfg, data_dir=d)


class TestScannerParallelSeries(unittest.TestCase):
    def test_default_is_sequential(self):
        """Flag OFF : aucun cache/parallelisme, comportement historique."""
        cfg = ScannerConfig()
        self.assertFalse(cfg.parallel_series)
        self.assertGreaterEqual(cfg.parallel_workers, 1)

    def test_parallel_equivalent_to_sequential(self):
        """Le resultat parallele est byte-identical au sequentiel (meme
        ordre de fusion, donc meme tri aval)."""
        by = _by_series()
        seq_res = _scanner(by, parallel=False).scan_cycle()
        par_res = _scanner(by, parallel=True).scan_cycle()
        seq_m = seq_res["markets"]
        par_m = par_res["markets"]
        self.assertEqual([m["ticker"] for m in seq_m],
                         [m["ticker"] for m in par_m])
        self.assertEqual(seq_res["report"]["funnel"],
                         par_res["report"]["funnel"])
        self.assertTrue(par_res["report"]["parallel_series"])
        self.assertFalse(seq_res["report"]["parallel_series"])

    def test_parallel_really_concurrent(self):
        """Les series sont fetch en PARALLELE (concurrency observee >= 2)
        et TOUTES les series sont interrogees."""
        cli = _ConcurrentClient(_by_series())
        cfg = ScannerConfig()
        cfg.general_crawl = False
        cfg.priority_series = list(_SERIES)
        cfg.parallel_series = True
        cfg.parallel_workers = 4
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=_FakeRouter(), cfg=cfg, data_dir=d)
            sc.scan_cycle()
        self.assertGreaterEqual(cli.max_concurrent, 2)
        self.assertEqual(cli.series_calls, len(_SERIES))

    def test_sequential_no_concurrency(self):
        cli = _ConcurrentClient(_by_series())
        cfg = ScannerConfig()
        cfg.general_crawl = False
        cfg.priority_series = list(_SERIES)
        cfg.parallel_series = False
        with tempfile.TemporaryDirectory() as d:
            sc = MarketScanner(cli, router=_FakeRouter(), cfg=cfg, data_dir=d)
            sc.scan_cycle()
        self.assertEqual(cli.max_concurrent, 1)          # strictement seq.
        self.assertEqual(cli.series_calls, len(_SERIES))


# ══════════════════════════════════════════════════════════════════════
# btc_context — contexte calcule UNE FOIS par cycle
# ══════════════════════════════════════════════════════════════════════

def _spot_sources(counters):
    def mk(name, price):
        def f():
            counters["spot"] += 1
            return {"source": name, "price": price, "ts": time.time()}
        return f
    return (mk("coinbase", 65200.0), mk("kraken", 65210.0))


def _klines_fn(counters):
    def f():
        counters["klines"] += 1
        now = time.time()
        # 30 bougies 1m : fraiches, monotones, closes > 0 -> contexte VALIDE
        return [{"ts": now - (30 - i) * 60, "open": 65000.0, "high": 65100.0,
                 "low": 64900.0, "close": 65000.0 + i, "volume": 1.0}
                for i in range(30)]
    return f


class TestBtcContextCycleCache(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("BTC_CONTEXT_CYCLE_CACHE")
        bc.clear_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BTC_CONTEXT_CYCLE_CACHE", None)
        else:
            os.environ["BTC_CONTEXT_CYCLE_CACHE"] = self._old
        bc.clear_cache()

    def test_disabled_by_default_no_memo(self):
        """Flag OFF : PAS de memo du contexte — deux appels identiques
        produisent deux objets distincts (le cache brut 10 s, pre-existant,
        deduplique les fetches reseau ; il ne memoise PAS le contexte)."""
        counters = {"spot": 0, "klines": 0}
        c1 = bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        c2 = bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        self.assertTrue(c1.valid)
        self.assertIsNot(c1, c2)                # aucun memo par defaut
        # 1 pull = 2 sources ; le cache brut 10 s est partage (pre-existant)
        self.assertEqual(counters["spot"], 2)
        self.assertEqual(counters["klines"], 1)

    def test_cycle_cache_computes_once(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        counters = {"spot": 0, "klines": 0}
        bc.begin_cycle()
        c1 = bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        self.assertTrue(c1.valid)
        c2 = bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        self.assertIs(c1, c2)                   # MEME objet contexte
        # meme strike/horizon => AUCUN nouveau fetch ni revalidation
        self.assertEqual(counters["spot"], 2)
        self.assertEqual(counters["klines"], 1)

    def test_cycle_cache_shares_fetches_across_strikes(self):
        """Des strikes/horizons DIFFERENTS reconstruisent le contexte (champ
        strike/minutes propres) mais ne refont AUCUN fetch reseau : les
        donnees brutes (spot + klines) sont partagees pour tout le cycle."""
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        counters = {"spot": 0, "klines": 0}
        bc.begin_cycle()
        c1 = bc.get_btc_context(strike=64999.99, minutes_remaining=300.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        c2 = bc.get_btc_context(strike=66000.0, minutes_remaining=500.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        self.assertTrue(c1.valid and c2.valid)
        self.assertEqual(c2.strike, 66000.0)
        self.assertEqual(c2.minutes_remaining, 500.0)
        self.assertEqual(counters["spot"], 2)   # UNE seule pull par cycle
        self.assertEqual(counters["klines"], 1)

    def test_begin_cycle_purges(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        counters = {"spot": 0, "klines": 0}
        bc.begin_cycle()
        bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                           spot_sources=_spot_sources(counters),
                           klines_fn=_klines_fn(counters))
        self.assertEqual(counters["spot"], 2)
        bc.begin_cycle()                        # cycle suivant
        bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                           spot_sources=_spot_sources(counters),
                           klines_fn=_klines_fn(counters))
        self.assertEqual(counters["spot"], 4)   # donnees FRAICHES au cycle 2
        self.assertEqual(counters["klines"], 2)


# ══════════════════════════════════════════════════════════════════════
# ExecutionEngine — cycle parallele (solde+sante pendant le scan)
# ══════════════════════════════════════════════════════════════════════

from datetime import datetime, timedelta, timezone  # noqa: E402

_BTCD = "KXBTCD-26JUL2017-T64999.99"


def _btcd_market(**kw):
    m = {"ticker": _BTCD, "status": "open",
         "close_time": (datetime.now(timezone.utc)
                        + timedelta(minutes=300)).isoformat(),
         "floor_strike": 64999.99, "title": "BTC above 65,000 today?",
         "yes_bid": 46, "yes_ask": 48, "no_bid": 52, "no_ask": 54,
         "volume_24h": 5000}
    m.update(kw)
    return m


class _EngCtx:
    valid = True
    reason = "ok"
    spot = 65200.0
    realized_vol_1m = 8e-4
    returns = {"5m": 0.001}
    data_quality_score = 85.0


def _fake_ctx(strike=None, minutes_remaining=None, **kw):
    return _EngCtx()


class _EngClient:
    """FakeClient minimal avec compteur d'appels get_balance (aucun reseau)."""
    env = "demo"
    base_url = "fake://demo"
    cred_src = "fake"

    def __init__(self):
        self.balance_calls = 0
        self.created_orders = []
        self.cancelled = []
        self._orders = {}
        self.market = _btcd_market()

    def get_balance(self):
        self.balance_calls += 1
        return 93.26

    def get_markets(self, series, status="open", limit=200):
        return [dict(self.market)] if series == "KXBTCD" else []

    def get_market(self, ticker):
        return dict(self.market) if ticker == self.market["ticker"] else {}

    def get_positions(self):
        out = []
        for o in self._orders.values():
            n = int(o.get("taker_fill_count") or 0)
            if n > 0:
                out.append({"ticker": self.market["ticker"],
                            "position": n, "market_exposure": n * o["price"],
                            "realized_pnl": 0, "fees_paid": 2})
        return out

    def _req(self, method, path, params=None, **kw):
        return {"markets": [], "cursor": None}

    def create_order(self, ticker, side, count, price_cents,
                     client_order_id=None):
        oid = f"o{len(self.created_orders) + 1}"
        self.last_http_status = 201
        self.created_orders.append({"order_id": oid, "ticker": ticker,
                                    "side": side, "count": count,
                                    "price": price_cents,
                                    "client_order_id": client_order_id})
        self._orders[oid] = {"order_id": oid, "status": "executed",
                             "taker_fill_count": count, "side": side,
                             "price": price_cents}
        return dict(self._orders[oid])

    def get_order(self, oid):
        return dict(self._orders.get(oid, {}))

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return dict(self._orders.get(oid, {}))

    def get_fills(self, order_id, *, strict=False):
        o = self._orders.get(order_id) or {}
        n = int(o.get("taker_fill_count") or 0)
        if n <= 0:
            return []
        return [{"fill_id": f"f-{order_id}", "count": n,
                 f"{o['side']}_price": o["price"], "fees": "0.02"}]


def _make_engine(client, parallel):
    import kalshi_alpha_bot as bot
    from strategy_router import build_default_registry
    tmp = tempfile.mkdtemp(prefix="kalshi_p8_")
    bot.CFG.DATA_DIR = tmp
    bot.CFG.SHADOW_MODE = False
    bot.CFG.KILL_SWITCH = False
    bot.CFG.ORDER_TTL_SECONDS = 0
    old = getattr(bot.CFG, "API_PARALLEL_ENABLED", False)
    bot.CFG.API_PARALLEL_ENABLED = bool(parallel)
    try:
        eng = bot.ExecutionEngine(client, capital=500.0)
        eng.router = build_default_registry(btc_context_provider=_fake_ctx)
        cfg = ScannerConfig()
        cfg.general_crawl = False
        eng.scanner = MarketScanner(client, router=eng.router, data_dir=tmp)
        eng.scanner.cfg = cfg
        eng.pipeline.router = eng.router
        eng.pipeline.scanner = eng.scanner
    finally:
        bot.CFG.API_PARALLEL_ENABLED = old
    return eng, tmp


class TestEngineParallelCycle(unittest.TestCase):
    def test_parallel_cycle_places_same_order_as_sequential(self):
        cli_seq = _EngClient()
        eng_seq, tmp_seq = _make_engine(cli_seq, parallel=False)
        with mock.patch("health_monitor.HEALTH.run_all", return_value={}):
            placed_seq = eng_seq.cycle(1)

        cli_par = _EngClient()
        eng_par, tmp_par = _make_engine(cli_par, parallel=True)
        with mock.patch("health_monitor.HEALTH.run_all", return_value={}):
            placed_par = eng_par.cycle(1)

        self.assertEqual(placed_seq, 1)
        self.assertEqual(placed_par, 1)
        self.assertEqual(len(cli_par.created_orders), 1)
        o_seq = cli_seq.created_orders[0]
        o_par = cli_par.created_orders[0]
        self.assertEqual(o_par["ticker"], o_seq["ticker"])
        self.assertEqual(o_par["side"], o_seq["side"])
        self.assertEqual(o_par["price"], o_seq["price"])

    def test_parallel_balance_fetched_once(self):
        """Le solde est fetch UNE fois par cycle (arriere-plan) et reutilise
        par la porte de solde : aucun second appel HTTP."""
        cli = _EngClient()
        eng, _ = _make_engine(cli, parallel=True)
        with mock.patch("health_monitor.HEALTH.run_all", return_value={}):
            eng.cycle(1)
        self.assertEqual(cli.balance_calls, 1)
        self.assertEqual(eng.last_balance, 93.26)
        self.assertEqual(eng.capital, min(500.0, 93.26))

    def test_parallel_kill_switch_no_scan_no_order(self):
        cli = _EngClient()
        eng, _ = _make_engine(cli, parallel=True)
        import kalshi_alpha_bot as bot
        old = bot.CFG.KILL_SWITCH
        bot.CFG.KILL_SWITCH = True
        try:
            with mock.patch("health_monitor.HEALTH.run_all", return_value={}):
                placed = eng.cycle(1)
        finally:
            bot.CFG.KILL_SWITCH = old
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertEqual(cli.balance_calls, 0)   # AUCUN fetch inutile

    def test_sequential_default_engine_not_parallel(self):
        cli = _EngClient()
        eng, _ = _make_engine(cli, parallel=False)
        self.assertFalse(eng._parallel_enabled)
        self.assertIsNone(eng._executor)


if __name__ == "__main__":
    unittest.main()
