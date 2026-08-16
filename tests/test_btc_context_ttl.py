"""AAE-012 : cablage de BTC_CONTEXT_CYCLE_TTL_S (incident AAE-34F38CFC).

Le knob etait declare (config.py) mais consomme nulle part : la constante
CYCLE_CACHE_TTL_S de btc_context restait cablee en dur a 3600 s. Ces tests
prouvent (1) le parsing config, (2) la consommation runtime reelle,
(3) les bornes, (4) l'equivalence de comportement a configuration par
defaut, (5) le repli sur le defaut pour une valeur invalide.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")

from config import _env_f                  # noqa: E402
import btc_context as bc                   # noqa: E402


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
        return [{"ts": now - (30 - i) * 60, "open": 65000.0, "high": 65100.0,
                 "low": 64900.0, "close": 65000.0 + i, "volume": 1.0}
                for i in range(30)]
    return f


class _EnvIsolated(unittest.TestCase):
    """Sauvegarde/restaure les deux variables et purge les caches."""

    _VARS = ("BTC_CONTEXT_CYCLE_CACHE", "BTC_CONTEXT_CYCLE_TTL_S")

    def setUp(self):
        self._old = {v: os.environ.get(v) for v in self._VARS}
        for v in self._VARS:
            os.environ.pop(v, None)
        bc.clear_cache()

    def tearDown(self):
        for v, val in self._old.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val
        bc.clear_cache()


class TestConfigParsing(_EnvIsolated):
    def test_default_when_unset(self):
        self.assertEqual(_env_f("BTC_CONTEXT_CYCLE_TTL_S", 3600.0), 3600.0)

    def test_parses_float(self):
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "120.5"
        self.assertEqual(_env_f("BTC_CONTEXT_CYCLE_TTL_S", 3600.0), 120.5)

    def test_invalid_falls_back_to_default(self):
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "not-a-number"
        self.assertEqual(_env_f("BTC_CONTEXT_CYCLE_TTL_S", 3600.0), 3600.0)


class TestTtlKnobConsumed(_EnvIsolated):
    """Le knob gouverne desormais reellement le TTL long du cache de cycle
    — la meme semantique _env_f que la declaration config."""

    def test_default_equals_legacy_constant(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        self.assertEqual(bc._data_ttl(), bc.CYCLE_CACHE_TTL_S)  # 3600.0

    def test_env_value_consumed(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "123.5"
        self.assertEqual(bc._data_ttl(), 123.5)

    def test_ignored_when_cycle_cache_off(self):
        """Flag OFF (defaut production) : TTL court inchange, le knob est
        sans effet — exactement le perimetre documente."""
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "123.5"
        self.assertEqual(bc._data_ttl(), bc.CACHE_TTL_S)  # 10.0

    def test_invalid_value_falls_back(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "abc"
        self.assertEqual(bc._data_ttl(), bc.CYCLE_CACHE_TTL_S)

    def test_boundary_zero_means_no_reuse(self):
        """TTL 0 : semantique _cached 'now - ts < ttl' => jamais de hit.
        Pas de clamp ajoute — _env_f n'en a pas non plus."""
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "0"
        self.assertEqual(bc._data_ttl(), 0.0)
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return calls["n"]
        bc._cached("k", bc._data_ttl(), fetch)
        bc._cached("k", bc._data_ttl(), fetch)
        self.assertEqual(calls["n"], 2)          # aucun re-emploi

    def test_boundary_negative_parses_no_reuse(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "-5"
        self.assertEqual(bc._data_ttl(), -5.0)   # miroir _env_f : pas de clamp


class TestRuntimeConsumption(_EnvIsolated):
    """Consommation de bout en bout par get_btc_context."""

    def test_ttl_zero_forces_refetch_within_cycle(self):
        os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "1"
        os.environ["BTC_CONTEXT_CYCLE_TTL_S"] = "0"
        counters = {"spot": 0, "klines": 0}
        bc.begin_cycle()
        c1 = bc.get_btc_context(strike=64999.99, minutes_remaining=300.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        c2 = bc.get_btc_context(strike=66000.0, minutes_remaining=500.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        self.assertTrue(c1.valid and c2.valid)
        # TTL 0 : les donnees brutes sont re-tirees pour le second strike
        # (avant cablage, ce scenario ne refaisait AUCUN fetch).
        self.assertEqual(counters["spot"], 4)
        self.assertEqual(counters["klines"], 2)

    def test_default_config_equivalence(self):
        """Env TTL ABSENT : comportement identique a l'ancien code cable en
        dur — une seule pull partagee pour tout le cycle (le scenario exact
        de test_performance.test_cycle_cache_shares_fetches_across_strikes)."""
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
        self.assertEqual(counters["spot"], 2)    # UNE pull, comme avant
        self.assertEqual(counters["klines"], 1)

    def test_default_flag_off_unchanged(self):
        """Configuration 100% par defaut (flag off, TTL absent) : strictement
        le comportement pre-patch (memes compteurs que
        test_performance.test_disabled_by_default_no_memo)."""
        counters = {"spot": 0, "klines": 0}
        c1 = bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        c2 = bc.get_btc_context(strike=100.0, minutes_remaining=10.0,
                                spot_sources=_spot_sources(counters),
                                klines_fn=_klines_fn(counters))
        self.assertTrue(c1.valid)
        self.assertIsNot(c1, c2)
        self.assertEqual(counters["spot"], 2)
        self.assertEqual(counters["klines"], 1)


if __name__ == "__main__":
    unittest.main()
