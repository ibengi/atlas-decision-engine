# -*- coding: utf-8 -*-
"""AUD-PROBE-001 (2026-08-26) — attente bornee de la sonde demo.

Pinne : bornes de polling (intervalle/plafond), detection exacte des
marches conformes (criteres ask<=30c / spread<=5c INCHANGES), timeout
propre NO_ELIGIBLE_MARKET (exit 0, pas un echec), garantie UN SEUL ordre
par execution, reponse POST ambigue -> interrogation broker uniquement
(jamais de re-POST), et nettoyage (cancel) d'un ordre non rempli.

Aucun test ne touche le reseau ; horloge et sommeil INJECTES (aucune
attente reelle). Les gardes anti-mock de main() sont neutralisees ICI
UNIQUEMENT (monkeypatch de test) — leurs propres tests restent dans
test_real_demo_proof.py.
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import contextlib
import io

import kalshi_demo_execution_check as kdc
from kalshi_client import KalshiAPIError


def mkt(ticker="KXBTCD-T1", ask=25, bid=22, **kw):
    m = {"ticker": ticker, "yes_ask": ask, "yes_bid": bid}
    m.update(kw)
    return m


class FakeClock:
    """Horloge + sommeil deterministes : sleep(n) avance l'horloge."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class PollClient:
    """Client factice pour le polling : sert une sequence de reponses
    get_markets (une entree = un scan complet d'une serie)."""

    base_url = kdc.DEMO_BASE

    def __init__(self, per_attempt_markets):
        # per_attempt_markets[i] = liste servie a la tentative i (pour la
        # PREMIERE serie ; les autres series repondent vide)
        self.per_attempt = list(per_attempt_markets)
        self.calls = 0          # nombre total de GET /markets
        self.attempt = 0

    def get_markets(self, series, status="open", limit=100):
        self.calls += 1
        if series == kdc.CANDIDATE_SERIES[0]:
            i = min(self.attempt, len(self.per_attempt) - 1)
            out = self.per_attempt[i]
            self.attempt += 1
            return out
        return []


class TestPollBounds(unittest.TestCase):
    def _with_env(self, **env):
        old = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            return kdc.poll_bounds()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_defaults(self):
        self.assertEqual(self._with_env(), (60.0, 21600.0))

    def test_interval_clamped_low_and_high(self):
        self.assertEqual(
            self._with_env(DEMO_PROBE_POLL_INTERVAL_SECONDS=1)[0], 15.0)
        self.assertEqual(
            self._with_env(DEMO_PROBE_POLL_INTERVAL_SECONDS=9999)[0], 300.0)

    def test_max_wait_clamped_and_zero_allowed(self):
        self.assertEqual(
            self._with_env(DEMO_PROBE_MAX_WAIT_SECONDS=999999)[1], 86400.0)
        self.assertEqual(
            self._with_env(DEMO_PROBE_MAX_WAIT_SECONDS=0)[1], 0.0)

    def test_invalid_values_fall_back_to_defaults(self):
        self.assertEqual(self._with_env(
            DEMO_PROBE_POLL_INTERVAL_SECONDS="abc",
            DEMO_PROBE_MAX_WAIT_SECONDS=""), (60.0, 21600.0))


class TestEligibility(unittest.TestCase):
    """Les criteres sont EXACTS et inchanges : toute derive (31c, 6c)
    doit faire echouer ces tests."""

    def test_ask_bound_exact(self):
        self.assertIsNotNone(kdc.market_is_eligible(mkt(ask=30, bid=27)))
        self.assertIsNone(kdc.market_is_eligible(mkt(ask=31, bid=28)))
        self.assertIsNone(kdc.market_is_eligible(mkt(ask=0, bid=1)))
        self.assertEqual(kdc.MAX_ASK_CENTS, 30)

    def test_spread_bound_exact(self):
        self.assertIsNotNone(kdc.market_is_eligible(mkt(ask=25, bid=20)))
        self.assertIsNone(kdc.market_is_eligible(mkt(ask=25, bid=19)))
        self.assertEqual(kdc.MAX_SPREAD_CENTS, 5)

    def test_malformed_quotes_rejected(self):
        self.assertIsNone(kdc.market_is_eligible(mkt(ask=None, bid=22)))
        self.assertIsNone(kdc.market_is_eligible(mkt(ask="x", bid=22)))
        self.assertIsNone(kdc.market_is_eligible({"ticker": "T"}))
        self.assertIsNone(kdc.market_is_eligible(mkt(ask=25, bid=0)))

    def test_find_picks_cheapest_in_first_series(self):
        cli = PollClient([[mkt("A", ask=28, bid=25), mkt("B", ask=12, bid=9),
                           mkt("C", ask=31, bid=28)]])
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertEqual(cand, ("B", 12))
        self.assertEqual(funnel["markets_total"], 3)
        self.assertEqual(funnel["best_ask_seen"], 12)
        self.assertEqual(funnel["eligible"], 2)
        self.assertEqual(funnel["rejections"], {"ask_too_high": 1})


class TestBoundedPolling(unittest.TestCase):
    def test_finds_market_on_later_attempt(self):
        clock = FakeClock()
        cli = PollClient([[], [mkt(ask=40, bid=38)],
                          [mkt("KXBTCD-OK", ask=22, bid=19)]])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cand = kdc.wait_for_eligible_market(
                cli, 60.0, 3600.0,
                sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
        self.assertEqual(cand, ("KXBTCD-OK", 22))
        self.assertEqual(clock.sleeps, [60.0, 60.0])    # 2 attentes bornees
        t = buf.getvalue()
        self.assertIn("[PROBE_POLL] attempt=1", t)
        self.assertIn("[PROBE_POLL] attempt=2", t)
        self.assertIn("best_ask_seen=40", t)            # visibilite du miss
        self.assertIn("[PROBE_ELIGIBLE] ticker=KXBTCD-OK", t)
        self.assertIn("attempt=3", t)

    def test_timeout_returns_none_cleanly_no_hammering(self):
        clock = FakeClock()
        cli = PollClient([[]])                          # jamais conforme
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cand = kdc.wait_for_eligible_market(
                cli, 60.0, 150.0,
                sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
        self.assertIsNone(cand)
        # scans a t=0,60,120,150 : 4 tentatives, derniere attente CLAMPEE
        # au temps restant (30 s) — jamais au-dela du plafond.
        self.assertEqual(clock.sleeps, [60.0, 60.0, 30.0])
        self.assertEqual(cli.attempt, 4)
        t = buf.getvalue()
        self.assertIn("[NO_ELIGIBLE_MARKET] attempts=4", t)
        self.assertIn("max_wait_s=150", t)
        self.assertIn("INCHANGES", t)

    def test_api_error_during_scan_keeps_waiting(self):
        """get_markets -> [] (erreur API absorbee par le client) : le
        polling continue en securite jusqu'au timeout, sans crash."""
        clock = FakeClock()
        cli = PollClient([[]])
        with contextlib.redirect_stdout(io.StringIO()):
            cand = kdc.wait_for_eligible_market(
                cli, 60.0, 60.0,
                sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)
        self.assertIsNone(cand)


# ── main() complet hors-ligne : un seul ordre, ambiguite, nettoyage ─────────

class OneShotClient:
    """Client factice complet pour main() : marche conforme immediat,
    scenarios d'ordre pilotables. Compte chaque POST."""

    base_url = kdc.DEMO_BASE
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def __init__(self, order_scenario="fill"):
        self.scenario = order_scenario
        self.create_calls = 0
        self.cancel_calls = 0
        self.last_coid = None
        self.last_http_status = None

    def get_balance(self):
        return 136.56

    def get_markets(self, series, status="open", limit=100):
        if series == kdc.CANDIDATE_SERIES[0]:
            return [mkt("KXBTCD-PROBE", ask=24, bid=21)]
        return []

    def _order(self, filled):
        return {"order_id": "ord-1", "client_order_id": self.last_coid,
                "status": "executed" if filled else "resting",
                "taker_fill_count": 1 if filled else 0,
                "remaining_count": 0 if filled else 1}

    def create_order(self, ticker, side, count, price_cents,
                     client_order_id=None):
        self.create_calls += 1
        self.last_coid = client_order_id
        if self.scenario == "ambiguous":
            raise KalshiAPIError(0, "reseau: timeout pendant le POST")
        self.last_http_status = 201
        return self._order(self.scenario == "fill")

    def get_order(self, oid):
        return self._order(self.scenario in ("fill", "ambiguous"))

    def get_orders(self, ticker=None):
        if self.scenario == "ambiguous":
            # le broker A l'ordre : le POST ambigu etait passe
            self.last_coid = self.last_coid  # inchange
            return [self._order(True)]
        return []

    def get_fills(self, order_id, **kw):
        if self.scenario in ("fill", "ambiguous"):
            return [{"fill_id": "f-1", "count": 1, "yes_price": 24,
                     "fees": "0.02"}]
        return []

    def cancel_order(self, oid):
        self.cancel_calls += 1
        return {"order_id": oid, "reduced_by": 1, "status": "canceled"}

    def get_positions(self):
        if self.scenario in ("fill", "ambiguous"):
            return [{"ticker": "KXBTCD-PROBE", "position": 1,
                     "market_exposure": 24, "realized_pnl": 0,
                     "fees_paid": 2}]
        return []


class TestMainOneShot(unittest.TestCase):
    """Execute le VRAI main() hors-ligne. Les gardes anti-mock sont
    neutralisees par monkeypatch DE TEST uniquement ; leurs tests propres
    restent dans test_real_demo_proof.py (subprocess, non patches)."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (
            "ENABLE_DEMO_INTEGRATION_TEST", "DEMO_PROBE_MAX_WAIT_SECONDS",
            "DEMO_PROBE_POLL_INTERVAL_SECONDS", "LIVE_TRADING",
            "KALSHI_ENV_CONFIRM")}
        os.environ["ENABLE_DEMO_INTEGRATION_TEST"] = "true"
        os.environ.pop("LIVE_TRADING", None)
        os.environ.pop("KALSHI_ENV_CONFIRM", None)
        os.environ["DEMO_PROBE_MAX_WAIT_SECONDS"] = "0"   # un seul scan
        self._KalshiClient = kdc.KalshiClient
        self._genuine = kdc._client_is_genuine
        self._fill_timeout = kdc.FILL_TIMEOUT_S
        kdc._client_is_genuine = lambda c: True

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        kdc.KalshiClient = self._KalshiClient
        kdc._client_is_genuine = self._genuine
        kdc.FILL_TIMEOUT_S = self._fill_timeout

    def _run_main(self, client):
        kdc.KalshiClient = lambda env: client
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                kdc.main()
        return cm.exception.code, buf.getvalue()

    def test_filled_lifecycle_single_order(self):
        cli = OneShotClient("fill")
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)           # UN SEUL POST
        for tag in ("[PROBE_CONFIG]", "[PROBE_ELIGIBLE]",
                    "[ORDER_SUBMIT_ATTEMPT]", "[ORDER_SUBMIT_RESPONSE]",
                    "[ORDER_VERIFY]", "[FILL_VERIFY]", "[ORDER_FILLED]",
                    "[POSITION_VERIFY]", "[DEMO_EXECUTION_PROVED]"):
            self.assertIn(tag, t, tag)
        self.assertIn("endpoint=/portfolio/events/orders", t)

    def test_no_eligible_market_clean_exit_zero(self):
        class NoMarket(OneShotClient):
            def get_markets(self, series, status="open", limit=100):
                return []
        cli = NoMarket()
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)                        # PAS un echec
        self.assertEqual(cli.create_calls, 0)            # aucun POST
        self.assertIn("[NO_ELIGIBLE_MARKET]", t)
        self.assertNotIn("[ORDER_SUBMIT_ATTEMPT]", t)
        self.assertNotIn("[FATAL]", t)

    def test_unfilled_order_cancelled_cleanly(self):
        kdc.FILL_TIMEOUT_S = 0.0                         # pas d'attente
        cli = OneShotClient("none")
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)
        self.assertEqual(cli.cancel_calls, 1)            # nettoyage prouve
        self.assertIn("[ORDER_CANCELED_UNFILLED]", t)
        self.assertNotIn("[ORDER_FILLED]", t)

    def test_ambiguous_post_broker_query_never_repost(self):
        cli = OneShotClient("ambiguous")
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)                        # ordre retrouve
        self.assertEqual(cli.create_calls, 1)            # JAMAIS re-POST
        self.assertIn("[ORDER_SUBMIT_AMBIGUOUS]", t)
        self.assertIn("never_repost=true", t)
        self.assertIn("broker_has_order=true", t)
        self.assertIn("[DEMO_EXECUTION_PROVED]", t)

    def test_ambiguous_not_found_exits_without_retry(self):
        class AmbNotPlaced(OneShotClient):
            def get_orders(self, ticker=None):
                return []
        cli = AmbNotPlaced("ambiguous")
        code, t = self._run_main(cli)
        self.assertEqual(code, 1)
        self.assertEqual(cli.create_calls, 1)            # pas de 2e POST
        self.assertIn("broker_has_order=false", t)

    def test_discovery_only_never_posts_even_when_eligible(self):
        """AUD-PROBE-002 : mode visibilite — marche ELIGIBLE observe,
        AUCUN POST, sortie propre (sonde courte de diagnostic)."""
        os.environ["DEMO_PROBE_DISCOVERY_ONLY"] = "true"
        try:
            cli = OneShotClient("fill")
            code, t = self._run_main(cli)
        finally:
            os.environ.pop("DEMO_PROBE_DISCOVERY_ONLY", None)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 0)            # JAMAIS de POST
        self.assertIn("[PROBE_MODE] discovery_only=true", t)
        self.assertIn("[PROBE_ELIGIBLE]", t)             # cote observee
        self.assertIn("[DISCOVERY_ONLY]", t)
        self.assertNotIn("[ORDER_SUBMIT_ATTEMPT]", t)

    def test_ambiguous_unresolved_fail_closed_exit_3(self):
        class AmbLookupFails(OneShotClient):
            def get_orders(self, ticker=None):
                raise KalshiAPIError(0, "reseau: lookup impossible")
        cli = AmbLookupFails("ambiguous")
        code, t = self._run_main(cli)
        self.assertEqual(code, 3)
        self.assertEqual(cli.create_calls, 1)
        self.assertIn("[ORDER_SUBMIT_AMBIGUOUS_UNRESOLVED]", t)
        self.assertIn("aucun re-POST", t)


if __name__ == "__main__":
    unittest.main()
