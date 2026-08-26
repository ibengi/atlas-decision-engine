# -*- coding: utf-8 -*-
"""AUD-OBS (2026-08-26) — fail-closed de la chaine de donnees BTC.

Mission : Binance geo-bloque (HTTP 451) depuis la region du deploiement ;
le banner affichait "btc_context=inconnue" et les logs de production ne
montraient JAMAIS quel fournisseur alimentait reellement les features.

Ce fichier pinne les neuf exigences de l'audit :
 1. fournisseur primaire disponible  -> fonctionnement normal (PRIMARY_OK)
 2. Binance 451 + secours valide     -> secours explicitement identifie
                                        (FALLBACK_OK, INFO en production)
 3. Binance 451 + secours indisponible -> signal BTC bloque
 4. donnees perimees (spot et klines)  -> signal BTC bloque
 5. donnees malformees                 -> signal BTC bloque
 6. exception fournisseur              -> fail-closed, pas de crash
 7. AUCUN de ces etats ne produit ORDER_SUBMIT_ATTEMPT (cycle REEL du
    moteur avec le VRAI get_btc_context et des fournisseurs casses ;
    temoin positif : les memes fournisseurs sains produisent bien un ordre)
 8. les autres controles de risque restent actifs meme avec des donnees
    valides (kill switch pinne ici ; le reste dans les suites existantes)
 9. non-regression : couverte par l'execution complete de run_tests.py.

Aucun test ne touche le reseau. Aucun seuil n'est assoupli.
"""
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


# ── fabriques de donnees (identiques aux conventions de test_btc_provider) ──

def _klines(n=30, base_ts=None, drift=0.0005, base_price=65200.0):
    base_ts = base_ts if base_ts is not None else time.time() - n * 60
    out, price = [], base_price
    for i in range(n):
        nxt = price * math.exp(drift * ((-1) ** i))
        out.append({"ts": base_ts + i * 60, "open": price,
                    "high": max(price, nxt), "low": min(price, nxt),
                    "close": nxt, "volume": 5.0})
        price = nxt
    return out


def _spot_sources(now=None, price=65200.0):
    now = now if now is not None else time.time()
    return tuple(
        (lambda nm=nm, d=d: {"source": nm, "price": price + d, "ts": now})
        for nm, d in (("coinbase", 0.0), ("kraken", 1.0), ("bitstamp", -1.0)))


def ok_klines(limit=30):
    return _klines(30), {"http_status": 200, "elapsed_ms": 40.0,
                         "error": None}


def momentum_klines(limit=30):
    """Bougies saines avec momentum 5m POSITIF (+0.0013) et vol 1m 8e-4 :
    donne p=0.76 vs ask 48c — un edge deterministe pour le temoin positif
    du cycle complet (le modele integre le momentum : un drift negatif
    annule l'edge meme spot>strike)."""
    now = time.time()
    rets = [(0.0009 if i % 2 == 1 else -0.0007) for i in range(30)]
    price = 65200.0 / math.exp(sum(rets))
    out, ts0 = [], now - 30 * 60
    for i, r in enumerate(rets):
        nxt = price * math.exp(r)
        out.append({"ts": ts0 + i * 60, "open": price,
                    "high": max(price, nxt), "low": min(price, nxt),
                    "close": nxt, "volume": 5.0})
        price = nxt
    return out, {"http_status": 200, "elapsed_ms": 40.0, "error": None}


def binance_451(limit=30):
    return None, {"http_status": 451, "elapsed_ms": 42.0,
                  "error": "HTTPError: 451 Unavailable For Legal Reasons"}


def all_down(limit=30):
    return None, {"http_status": None, "elapsed_ms": 5000.0,
                  "error": "ConnectTimeout"}


def raises(limit=30):
    raise RuntimeError("provider blew up")


class _CapInfo:
    """Capture le logger BTCCTX au niveau INFO — exactement ce que voit la
    production (le defaut d'origine : le succes du secours n'existait
    qu'en DEBUG, invisible en production)."""

    def __init__(self, logger=bc.log, level=logging.INFO):
        self.logger, self.level = logger, level

    def __enter__(self):
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(self.level)
        self.logger.addHandler(self.h)
        self._old = self.logger.level
        self.logger.setLevel(self.level)
        return self

    def __exit__(self, *a):
        self.logger.removeHandler(self.h)
        self.logger.setLevel(self._old)

    @property
    def text(self):
        return self.buf.getvalue()


def _reset():
    bc.clear_cache()
    bc._last_good_klines.update(kl=None, ts=0.0, provider=None)


# ── Exigences 1-2 + observabilite : etats fournisseur au niveau INFO ────────

class TestProviderStateObservability(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_banner_version_exported(self):
        """AUD-OBS-001 : le banner affichait btc_context=inconnue parce que
        btc_context n'exportait pas VERSION (btc_strategy retombe sur
        'inconnue' via ImportError). Le module etait present et sain."""
        self.assertTrue(getattr(bc, "VERSION", None))
        import btc_strategy
        self.assertEqual(btc_strategy.BTC_CTX_VERSION, bc.VERSION)
        self.assertNotIn(btc_strategy.BTC_CTX_VERSION, ("inconnue", "absente"))

    def test_primary_ok_logged_at_info(self):
        """Exigence 1 : primaire disponible -> PRIMARY_OK, fournisseur et
        preuve de fraicheur visibles au niveau INFO."""
        with _CapInfo() as cap:
            kl, src = bc.fetch_klines_with_fallback(
                providers=[("binance", ok_klines), ("kraken", ok_klines)])
        self.assertEqual(src, "fresh:binance")
        t = cap.text
        self.assertIn("[DATA_PROVIDER_STATE]", t)
        self.assertIn("state=PRIMARY_OK", t)
        self.assertIn("used=binance", t)
        self.assertIn("failed=none", t)
        self.assertIn("count=30", t)
        # preuve de fraicheur : age numerique de la derniere bougie
        self.assertRegex(t, r"last_candle_age_s=\d+")

    def test_fallback_identified_at_info(self):
        """Exigence 2 : Binance 451 + Kraken sain -> FALLBACK_OK avec le
        fournisseur REEL des features ET l'echec primaire (code HTTP),
        au niveau INFO (avant : DEBUG seulement -> invisible en prod)."""
        with _CapInfo() as cap:
            kl, src = bc.fetch_klines_with_fallback(
                providers=[("binance", binance_451), ("kraken", ok_klines)])
        self.assertEqual(src, "fresh:kraken")
        t = cap.text
        self.assertIn("state=FALLBACK_OK", t)
        self.assertIn("used=kraken", t)
        self.assertIn("failed=binance(451)", t)

    def test_all_failed_state_at_info(self):
        with _CapInfo() as cap:
            kl, src = bc.fetch_klines_with_fallback(
                providers=[("binance", binance_451), ("kraken", all_down)])
        self.assertIsNone(kl)
        self.assertEqual(src, "none")
        t = cap.text
        self.assertIn("state=ALL_PROVIDERS_FAILED", t)
        self.assertIn("used=none", t)
        self.assertIn("binance(451)", t)
        self.assertIn("kraken(no_data)", t)
        self.assertIn("count=0", t)

    def test_stale_cache_state_at_info(self):
        now0 = time.time()
        bc.fetch_klines_with_fallback(providers=[("kraken", ok_klines)],
                                      now=now0)
        with _CapInfo() as cap:
            kl, src = bc.fetch_klines_with_fallback(
                providers=[("binance", binance_451), ("kraken", all_down)],
                now=now0 + 180)
        self.assertTrue(src.startswith("stale_cache:kraken"))
        t = cap.text
        self.assertIn("state=DEGRADED_STALE_CACHE", t)
        self.assertIn("used=kraken", t)
        self.assertIn("binance(451)", t)

    def test_exception_reported_in_failures(self):
        """Exigence 6 (niveau fournisseur) : une exception n'interrompt pas
        la chaine, elle est journalisee et nommee dans failed=."""
        with _CapInfo() as cap:
            kl, src = bc.fetch_klines_with_fallback(
                providers=[("binance", raises), ("kraken", ok_klines)])
        self.assertEqual(src, "fresh:kraken")
        self.assertIn("failed=binance(exception)", cap.text)


# ── Exigences 3-6 : fail-closed au niveau contexte + routeur ────────────────

class TestSignalBlockedFailClosed(unittest.TestCase):
    def setUp(self):
        _reset()

    def _ctx_with(self, providers, spot_sources=None, now=None):
        orig = bc.fetch_klines_with_fallback
        bc.fetch_klines_with_fallback = lambda limit=30, providers_=None, \
            now=None: orig(providers=providers, now=now)
        try:
            return bc.get_btc_context(
                spot_sources=spot_sources or _spot_sources(now=now),
                use_cache=False, now=now)
        finally:
            bc.fetch_klines_with_fallback = orig

    def _router_reason(self, ctx):
        """Le VRAI routeur (BtcDailyStrategy) sur un contexte donne : la
        raison exacte du rejet cote signal."""
        from strategy_router import BtcDailyStrategy
        strat = BtcDailyStrategy(lambda **kw: ctx)
        out = strat.evaluate({"ticker": "KXBTCD-TEST-T64999.99",
                              "floor_strike": 64999.99},
                             {"yes_ask": 48}, 300)
        self.assertFalse(out.valid)
        return out.reason

    def test_451_without_fallback_blocks_signal(self):
        """Exigence 3 : 451 partout, pas de cache -> contexte invalide,
        raison typee, signal rejete par le routeur."""
        ctx = self._ctx_with([("binance", binance_451),
                              ("kraken", binance_451),
                              ("coinbase", binance_451)])
        self.assertFalse(ctx.valid)
        self.assertEqual(ctx.reason, "aucune_donnee:klines")
        self.assertEqual(self._router_reason(ctx),
                         "no_model_probability:aucune_donnee:klines")

    def test_stale_spot_blocks_signal(self):
        """Exigence 4a : spot plus vieux que MAX_PRICE_AGE_S -> sources
        eliminees -> contexte invalide."""
        now = time.time()
        old = tuple((lambda nm=nm: {"source": nm, "price": 65200.0,
                                    "ts": now - bc.MAX_PRICE_AGE_S - 5})
                    for nm in ("coinbase", "kraken", "bitstamp"))
        ctx = bc.get_btc_context(spot_sources=old, use_cache=False, now=now)
        self.assertFalse(ctx.valid)
        self.assertTrue(ctx.reason.startswith("donnees_insuffisantes:spot"),
                        ctx.reason)
        self.assertIn("no_model_probability", self._router_reason(ctx))

    def test_stale_klines_blocks_signal(self):
        """Exigence 4b : bougies fraiches exigees (<=3 min) ; une serie
        vieille de 10 min est refusee (klines:perimees)."""
        now = time.time()

        def old_kl(limit=30):
            return (_klines(30, base_ts=now - 600 - 30 * 60),
                    {"http_status": 200, "elapsed_ms": 40, "error": None})
        ctx = self._ctx_with([("binance", old_kl)], now=now)
        self.assertFalse(ctx.valid)
        self.assertIn("klines:perimees", ctx.quality_flags)
        self.assertIn("no_model_probability", self._router_reason(ctx))

    def test_stale_cache_beyond_bound_blocks_signal(self):
        """Exigence 4c : le secours cache est BORNE (600 s) ; au-dela il
        est refuse, jamais rechauffe."""
        now0 = time.time()
        bc.fetch_klines_with_fallback(providers=[("kraken", ok_klines)],
                                      now=now0)
        ctx = self._ctx_with([("binance", all_down), ("kraken", all_down)],
                             now=now0 + bc.KLINES_STALE_MAX_S + 1)
        self.assertFalse(ctx.valid)
        self.assertEqual(ctx.reason, "aucune_donnee:klines")

    def test_malformed_spot_blocked(self):
        """Exigence 5a : prix NaN / negatifs -> sources eliminees avec
        flag, contexte invalide."""
        now = time.time()
        bad = tuple((lambda nm=nm, p=p: {"source": nm, "price": p,
                                         "ts": now})
                    for nm, p in (("coinbase", float("nan")),
                                  ("kraken", -1.0),
                                  ("bitstamp", 0.0)))
        ctx = bc.get_btc_context(spot_sources=bad, use_cache=False, now=now)
        self.assertFalse(ctx.valid)
        self.assertTrue(any("prix_invalide" in f for f in ctx.quality_flags),
                        ctx.quality_flags)

    def test_malformed_klines_blocked(self):
        """Exigence 5b : timestamps non monotones -> serie entiere refusee ;
        closes negatifs -> bougies eliminees."""
        now = time.time()

        def scrambled(limit=30):
            kl = _klines(30, base_ts=now - 30 * 60)
            kl[10], kl[20] = kl[20], kl[10]        # ordre casse
            return kl, {"http_status": 200, "elapsed_ms": 40, "error": None}
        ctx = self._ctx_with([("binance", scrambled)], now=now)
        self.assertFalse(ctx.valid)
        self.assertIn("klines:timestamps_non_monotones", ctx.quality_flags)

        _reset()

        def negative(limit=30):
            kl = _klines(30, base_ts=now - 30 * 60)
            for k in kl:
                k["close"] = -5.0
            return kl, {"http_status": 200, "elapsed_ms": 40, "error": None}
        ctx = self._ctx_with([("binance", negative)], now=now)
        self.assertFalse(ctx.valid)

    def test_provider_exception_fail_closed_no_crash(self):
        """Exigence 6 : tous les fournisseurs (spot ET klines) levent une
        exception -> aucun crash, contexte invalide, raison typee."""
        def spot_raises():
            raise RuntimeError("spot provider blew up")
        ctx = self._ctx_with([("binance", raises), ("kraken", raises)],
                             spot_sources=(spot_raises,) * 3)
        self.assertFalse(ctx.valid)
        self.assertEqual(ctx.reason, "aucune_donnee:spot")

    def test_low_quality_blocks_at_router(self):
        """Complement : contexte VALIDE mais qualite < 60 -> confiance 0 ->
        rejet insufficient_data_quality (aucun defaut, aucun signal)."""
        from strategy_router import BtcDailyStrategy

        class LowQ:
            valid, reason = True, "ok"
            spot, realized_vol_1m = 65200.0, 8e-4
            returns = {"5m": 0.001}
            data_quality_score = 30.0
        strat = BtcDailyStrategy(lambda **kw: LowQ())
        out = strat.evaluate({"ticker": "KXBTCD-TEST-T64999.99",
                              "floor_strike": 64999.99},
                             {"yes_ask": 48}, 300)
        self.assertFalse(out.valid)
        self.assertTrue(out.reason.startswith("insufficient_data_quality"),
                        out.reason)


# ── Exigence 7 : cycle REEL du moteur — jamais d'ORDER_SUBMIT_ATTEMPT ───────

class TestNoOrderSubmitOnBrokenData(unittest.TestCase):
    """ExecutionEngine.cycle REEL + VRAI get_btc_context (aucun contexte
    factice) ; seuls les fournisseurs reseau sont injectes. Temoin positif
    d'abord : les fournisseurs sains produisent un ordre — la preuve que
    les zeros des scenarios casses viennent bien du fail-closed donnees."""

    @classmethod
    def setUpClass(cls):
        import kalshi_alpha_bot as bot
        from strategy_router import build_default_registry
        from test_pipeline_integration import FakeClient, make_engine
        cls.bot, cls.FakeClient = bot, FakeClient
        cls.make_engine = staticmethod(make_engine)
        cls.build_default_registry = staticmethod(build_default_registry)

    def setUp(self):
        _reset()
        self._sp = bc.DEFAULT_SPOT_SOURCES
        self._kp = bc.DEFAULT_KLINES_PROVIDERS

    def tearDown(self):
        bc.DEFAULT_SPOT_SOURCES = self._sp
        bc.DEFAULT_KLINES_PROVIDERS = self._kp
        _reset()

    def _engine_with_real_context(self, klines_providers, spot_sources=None):
        cli = self.FakeClient(order_scenario="fill")
        eng, tmp = self.make_engine(cli)
        # remplace le contexte factice du rig par le VRAI get_btc_context
        eng.router = self.build_default_registry(btc_context_provider=None)
        eng.pipeline.router = eng.router
        eng.scanner.router = eng.router
        bc.DEFAULT_SPOT_SOURCES = spot_sources or _spot_sources()
        bc.DEFAULT_KLINES_PROVIDERS = tuple(klines_providers)
        return eng, cli

    def _cycle(self, eng):
        api_log = logging.getLogger("API")
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.DEBUG)
        api_log.addHandler(h)
        old = api_log.level
        api_log.setLevel(logging.DEBUG)
        try:
            placed = eng.cycle(1)
        finally:
            api_log.removeHandler(h)
            api_log.setLevel(old)
        return placed, buf.getvalue()

    def test_positive_control_healthy_data_places_order(self):
        """Temoin : primaire sain -> le VRAI contexte alimente le modele et
        le cycle place UN ordre (ORDER_SUBMIT_ATTEMPT present)."""
        eng, cli = self._engine_with_real_context(
            [("binance", momentum_klines), ("kraken", momentum_klines),
             ("coinbase", momentum_klines)])
        placed, api_text = self._cycle(eng)
        self.assertEqual(placed, 1)
        self.assertEqual(len(cli.created_orders), 1)
        self.assertIn("[ORDER_SUBMIT_ATTEMPT]", api_text)

    def test_all_providers_failed_no_order(self):
        eng, cli = self._engine_with_real_context(
            [("binance", binance_451), ("kraken", all_down),
             ("coinbase", all_down)])
        placed, api_text = self._cycle(eng)
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertNotIn("ORDER_SUBMIT_ATTEMPT", api_text)

    def test_stale_klines_no_order(self):
        now = time.time()

        def old_kl(limit=30):
            return (_klines(30, base_ts=now - 900 - 30 * 60),
                    {"http_status": 200, "elapsed_ms": 40, "error": None})
        eng, cli = self._engine_with_real_context(
            [("binance", old_kl), ("kraken", old_kl), ("coinbase", old_kl)])
        placed, api_text = self._cycle(eng)
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertNotIn("ORDER_SUBMIT_ATTEMPT", api_text)

    def test_malformed_klines_no_order(self):
        def scrambled(limit=30):
            kl = _klines(30)
            kl[5], kl[25] = kl[25], kl[5]
            return kl, {"http_status": 200, "elapsed_ms": 40, "error": None}
        eng, cli = self._engine_with_real_context(
            [("binance", scrambled), ("kraken", scrambled),
             ("coinbase", scrambled)])
        placed, api_text = self._cycle(eng)
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertNotIn("ORDER_SUBMIT_ATTEMPT", api_text)

    def test_provider_exceptions_no_order_no_crash(self):
        def spot_raises():
            raise RuntimeError("spot provider blew up")
        eng, cli = self._engine_with_real_context(
            [("binance", raises), ("kraken", raises), ("coinbase", raises)],
            spot_sources=(spot_raises,) * 3)
        placed, api_text = self._cycle(eng)      # ne doit PAS lever
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertNotIn("ORDER_SUBMIT_ATTEMPT", api_text)

    def test_stale_spot_no_order(self):
        now = time.time()
        old_spot = tuple(
            (lambda nm=nm: {"source": nm, "price": 65200.0,
                            "ts": now - bc.MAX_PRICE_AGE_S - 10})
            for nm in ("coinbase", "kraken", "bitstamp"))
        eng, cli = self._engine_with_real_context(
            [("binance", ok_klines)], spot_sources=old_spot)
        placed, api_text = self._cycle(eng)
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertNotIn("ORDER_SUBMIT_ATTEMPT", api_text)

    def test_risk_controls_intact_with_healthy_data(self):
        """Exigence 8 (echantillon direct) : donnees PARFAITES mais
        KILL_SWITCH actif -> zero ordre. La correction observabilite ne
        contourne aucun controle de risque (le reste est pinne par les
        suites existantes : risk_proof, portfolio_limits, kelly...)."""
        eng, cli = self._engine_with_real_context(
            [("binance", momentum_klines)])
        self.bot.CFG.KILL_SWITCH = True
        try:
            placed, api_text = self._cycle(eng)
        finally:
            self.bot.CFG.KILL_SWITCH = False
        self.assertEqual(placed, 0)
        self.assertEqual(cli.created_orders, [])
        self.assertNotIn("ORDER_SUBMIT_ATTEMPT", api_text)


if __name__ == "__main__":
    unittest.main()
