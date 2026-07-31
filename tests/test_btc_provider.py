# -*- coding: utf-8 -*-
"""Audit fournisseur de donnees BTC (panne Binance du 2026-07-25 :
klines:insuffisantes(0/11) -> volatilite indisponible -> 100% des marches
BTC rejetes). Verifie : telemetrie HTTP par fournisseur, secours Kraken/
Coinbase, cache des dernieres bougies valides (borne, momentum neutralise),
distinction aucune_donnee / donnees_insuffisantes / volatilite_nulle."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import io
import logging
import math
import time

import btc_context as bc


def _klines(n, base_ts=None, drift=0.0005):
    base_ts = base_ts if base_ts is not None else time.time() - n * 60
    out, price = [], 65000.0
    for i in range(n):
        nxt = price * math.exp(drift * ((-1) ** i))
        out.append({"ts": base_ts + i * 60, "open": price,
                    "high": max(price, nxt), "low": min(price, nxt),
                    "close": nxt, "volume": 5.0})
        price = nxt
    return out


def _spot_sources(now=None):
    now = now if now is not None else time.time()
    return tuple(
        (lambda nm=nm: {"source": nm, "price": 65000.0 + d, "ts": now})
        for nm, d in (("coinbase", 0.0), ("kraken", 1.0), ("bitstamp", -1.0)))


class _Cap:
    def __enter__(self):
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(logging.DEBUG)
        bc.log.addHandler(self.h)
        bc.log.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        bc.log.removeHandler(self.h)

    @property
    def text(self):
        return self.buf.getvalue()


def binance_451(limit=30):
    return None, {"http_status": 451, "elapsed_ms": 42.0,
                  "error": "HTTPError: 451 Unavailable For Legal Reasons"}


def kraken_ok(limit=30):
    return _klines(30), {"http_status": 200, "elapsed_ms": 80.0,
                         "error": None}


def all_down(limit=30):
    return None, {"http_status": None, "elapsed_ms": 5000.0,
                  "error": "ConnectTimeout"}


class TestFallbackChain(unittest.TestCase):
    def setUp(self):
        bc.clear_cache()
        bc._last_good_klines.update(kl=None, ts=0.0, provider=None)

    def test_binance_down_kraken_serves(self):
        with _Cap() as cap:
            kl, src = bc.fetch_klines_with_fallback(
                providers=[("binance", binance_451), ("kraken", kraken_ok)])
        self.assertEqual(len(kl), 30)
        self.assertEqual(src, "fresh:kraken")
        t = cap.text
        # telemetrie exigee : code HTTP, delai, erreur, accepte/rejete
        self.assertIn("provider=binance", t)
        self.assertIn("http_status=451", t)
        self.assertIn("accepted=false", t)
        self.assertIn("451 Unavailable", t)
        self.assertIn("provider=kraken", t)
        self.assertIn("accepted=true", t)
        self.assertIn("ok(30)", t)

    def test_context_valid_via_fallback(self):
        ctx = bc.get_btc_context(
            spot_sources=_spot_sources(), use_cache=False,
            klines_fn=None)
        # court-circuit reseau : on injecte la chaine via monkeypatch
        orig = bc.fetch_klines_with_fallback
        bc.fetch_klines_with_fallback = lambda limit=30, providers=None, \
            now=None: orig(providers=[("binance", binance_451),
                                      ("kraken", kraken_ok)], now=now)
        try:
            ctx = bc.get_btc_context(spot_sources=_spot_sources(),
                                     use_cache=False)
        finally:
            bc.fetch_klines_with_fallback = orig
        self.assertTrue(ctx.valid, ctx.reason)
        self.assertIsNotNone(ctx.realized_vol_1m)
        self.assertTrue(any("source=fresh:kraken" in f
                            for f in ctx.quality_flags))

    def test_stale_cache_keeps_engine_deciding(self):
        now0 = time.time()
        # 1) passage nominal : amorce le cache des dernieres bougies valides
        bc.fetch_klines_with_fallback(
            providers=[("kraken", kraken_ok)], now=now0)
        # 2) panne TOTALE 3 minutes plus tard -> secours cache
        kl, src = bc.fetch_klines_with_fallback(
            providers=[("binance", all_down), ("kraken", all_down)],
            now=now0 + 180)
        self.assertEqual(len(kl), 30)
        self.assertTrue(src.startswith("stale_cache:kraken"))
        # contexte : valide, momentum NEUTRALISE, qualite penalisee, flags
        orig = bc.fetch_klines_with_fallback
        bc.fetch_klines_with_fallback = \
            lambda limit=30, providers=None, now=None: (kl, src)
        try:
            ctx = bc.get_btc_context(
                spot_sources=_spot_sources(now=now0 + 180),
                use_cache=False, now=now0 + 180)
        finally:
            bc.fetch_klines_with_fallback = orig
        self.assertTrue(ctx.valid, ctx.reason)
        self.assertEqual(ctx.returns, {})              # jamais rechauffe
        self.assertIsNone(ctx.momentum_per_min)
        self.assertIn("klines:momentum_neutralise_car_cache",
                      ctx.quality_flags)
        self.assertLess(ctx.data_quality_score, 100.0)

    def test_stale_cache_is_bounded(self):
        now0 = time.time()
        bc.fetch_klines_with_fallback(providers=[("kraken", kraken_ok)],
                                      now=now0)
        kl, src = bc.fetch_klines_with_fallback(
            providers=[("kraken", all_down)],
            now=now0 + bc.KLINES_STALE_MAX_S + 1)      # trop vieux
        self.assertIsNone(kl)
        self.assertEqual(src, "none")


class TestDistinctReasons(unittest.TestCase):
    def setUp(self):
        bc.clear_cache()
        bc._last_good_klines.update(kl=None, ts=0.0, provider=None)

    def _ctx_with(self, providers):
        orig = bc.fetch_klines_with_fallback
        bc.fetch_klines_with_fallback = lambda limit=30, providers_=None, \
            now=None: orig(providers=providers, now=now)
        try:
            return bc.get_btc_context(spot_sources=_spot_sources(),
                                      use_cache=False)
        finally:
            bc.fetch_klines_with_fallback = orig

    def test_no_data_at_all(self):
        ctx = self._ctx_with([("binance", all_down), ("kraken", all_down),
                              ("coinbase", all_down)])
        self.assertFalse(ctx.valid)
        self.assertEqual(ctx.reason, "aucune_donnee:klines")

    def test_insufficient_data(self):
        def five(limit=30):
            return _klines(5), {"http_status": 200, "elapsed_ms": 50,
                                "error": None}
        ctx = self._ctx_with([("kraken", five)])
        self.assertFalse(ctx.valid)
        self.assertTrue(ctx.reason.startswith(
            "donnees_insuffisantes:klines(5/11"), ctx.reason)

    def test_zero_volatility_distinct(self):
        def flat(limit=30):
            kl = _klines(30, drift=0.0)
            for k in kl:
                k["close"] = 65000.0
            return kl, {"http_status": 200, "elapsed_ms": 50, "error": None}
        ctx = self._ctx_with([("kraken", flat)])
        self.assertFalse(ctx.valid)
        self.assertEqual(ctx.reason, "volatilite_nulle")

    def test_spot_no_data_vs_insufficient(self):
        dead = tuple((lambda: None) for _ in range(3))
        ctx = bc.get_btc_context(spot_sources=dead, use_cache=False)
        self.assertEqual(ctx.reason, "aucune_donnee:spot")
        one = (_spot_sources()[0], (lambda: None), (lambda: None))
        ctx = bc.get_btc_context(spot_sources=one, use_cache=False)
        self.assertTrue(ctx.reason.startswith(
            "donnees_insuffisantes:spot(1/2"), ctx.reason)


class TestNotBinanceExclusive(unittest.TestCase):
    def test_default_chain_has_three_providers(self):
        names = [n for n, _ in bc.DEFAULT_KLINES_PROVIDERS]
        self.assertEqual(names, ["binance", "kraken", "coinbase"])

    def test_order_configurable_via_env(self):
        os.environ["KLINES_PROVIDER_ORDER"] = "kraken,coinbase"
        try:
            called = []

            def spy(name):
                def f(limit=30):
                    called.append(name)
                    return (_klines(30), {"http_status": 200,
                                          "elapsed_ms": 1, "error": None})
                return f
            orig = bc.DEFAULT_KLINES_PROVIDERS
            bc.DEFAULT_KLINES_PROVIDERS = (("binance", spy("binance")),
                                           ("kraken", spy("kraken")),
                                           ("coinbase", spy("coinbase")))
            try:
                kl, src = bc.fetch_klines_with_fallback()
            finally:
                bc.DEFAULT_KLINES_PROVIDERS = orig
            self.assertEqual(called, ["kraken"])       # binance JAMAIS appele
            self.assertEqual(src, "fresh:kraken")
        finally:
            os.environ.pop("KLINES_PROVIDER_ORDER", None)


if __name__ == "__main__":
    unittest.main()
