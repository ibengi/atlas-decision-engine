# -*- coding: utf-8 -*-
"""Exigence 12 : tests du protocole de preuve d'execution reelle.
- aucun mock possible en real_demo (arret FATAL) ;
- aucun ordre marque execute sans reponse API ; accepted != filled ;
- position declaree OUVERTE seulement apres confirmation API ;
- erreurs HTTP journalisees [ORDER_SUBMIT_FAILED] ;
- ordre non rempli expire proprement [ORDER_CANCELED_UNFILLED] ;
- le script d'integration refuse toute URL de production et exige le flag ;
- les secrets ne sont jamais journalises."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import io
import json
import logging
import subprocess
import tempfile

import kalshi_alpha_bot as bot
from test_pipeline_integration import FakeClient, make_engine, BTCD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "kalshi_demo_execution_check.py")


class _Cap:
    """Capture les logs des canaux API/POSITION/BOT."""
    def __enter__(self):
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setLevel(logging.DEBUG)
        for name in ("API", "POSITION", "BOT"):
            logging.getLogger(name).addHandler(self.h)
        return self

    def __exit__(self, *a):
        for name in ("API", "POSITION", "BOT"):
            logging.getLogger(name).removeHandler(self.h)

    @property
    def text(self):
        return self.buf.getvalue()


class TestAntiMockGuard(unittest.TestCase):
    def setUp(self):
        bot.CFG.EXECUTION_MODE = "standard"
        bot.CFG.DRY_RUN = False
        bot.CFG.ALLOW_ORDER_SUBMISSION = True

    tearDown = setUp

    def test_fake_client_fatal_in_real_demo(self):
        bot.CFG.EXECUTION_MODE = "real_demo"
        with self.assertRaises(SystemExit) as cm:
            bot.assert_real_demo_integrity(FakeClient(), shadow_mode=False)
        self.assertEqual(cm.exception.code, 3)

    def test_shadow_mode_fatal_in_real_demo(self):
        bot.CFG.EXECUTION_MODE = "real_demo"
        cli = bot.KalshiClient("demo")
        with self.assertRaises(SystemExit):
            bot.assert_real_demo_integrity(cli, shadow_mode=True)

    def test_dry_run_fatal_in_real_demo(self):
        bot.CFG.EXECUTION_MODE = "real_demo"
        bot.CFG.DRY_RUN = True
        cli = bot.KalshiClient("demo")
        with self.assertRaises(SystemExit):
            bot.assert_real_demo_integrity(cli, shadow_mode=False)

    def test_standard_mode_allows_test_doubles(self):
        # les mocks restent autorises dans les tests automatises
        bot.assert_real_demo_integrity(FakeClient(), shadow_mode=True)

    def test_genuine_demo_client_passes(self):
        bot.CFG.EXECUTION_MODE = "real_demo"
        cli = bot.KalshiClient("demo")
        self.assertTrue(bot._client_is_genuine(cli))
        self.assertTrue(cli.base_url.startswith(
            "https://demo-api.kalshi.co"))
        bot.assert_real_demo_integrity(cli, shadow_mode=False)  # ne leve pas


class TestProofProtocolLogs(unittest.TestCase):
    def test_submit_attempt_response_fill_position_sequence(self):
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        with _Cap() as cap:
            placed = eng.cycle(1)
        t = cap.text
        self.assertEqual(placed, 1)
        for tag in ("[ORDER_SUBMIT_ATTEMPT]", "[ORDER_SUBMIT_RESPONSE]",
                    "[ORDER_VERIFY]", "[FILL_VERIFY]", "[ORDER_FILLED]",
                    "[POSITION_VERIFY]", "position_found=true",
                    "[POSITION_OPENED]"):
            self.assertIn(tag, t, tag)
        # ordre des evenements
        self.assertLess(t.index("[ORDER_SUBMIT_ATTEMPT]"),
                        t.index("[ORDER_SUBMIT_RESPONSE]"))
        self.assertLess(t.index("[ORDER_VERIFY]"), t.index("[FILL_VERIFY]"))
        self.assertLess(t.index("[FILL_VERIFY]"),
                        t.index("[POSITION_OPENED]"))
        self.assertIn("client_order_id=alpha_", t)
        self.assertIn("http_status=201", t)

    def test_accepted_but_unfilled_never_marked_filled(self):
        cli = FakeClient(order_scenario="none")
        eng, tmp = make_engine(cli)
        with _Cap() as cap:
            placed = eng.cycle(1)
        t = cap.text
        self.assertEqual(placed, 0)
        self.assertIn("[ORDER_SUBMIT_RESPONSE]", t)      # accepte...
        self.assertIn("[ORDER_CANCELED_UNFILLED]", t)    # ...mais pas rempli
        self.assertNotIn("[ORDER_FILLED]", t)
        self.assertNotIn("[POSITION_OPENED]", t)
        self.assertIn("accepted != filled", t.replace("\n", " ")
                      .replace("  ", " ") if "accepted != filled" in t else t)

    def test_http_error_logged_not_swallowed(self):
        class RefusingClient(FakeClient):
            def create_order(self, *a, **k):
                raise bot.KalshiAPIError(400, "POST /portfolio/orders",
                                         '{"error":"insufficient_balance"}')
        cli = RefusingClient()
        eng, tmp = make_engine(cli)
        with _Cap() as cap:
            placed = eng.cycle(1)
        self.assertEqual(placed, 0)
        t = cap.text
        self.assertIn("[ORDER_SUBMIT_FAILED]", t)
        self.assertIn("http_status=400", t)
        self.assertIn("insufficient_balance", t)

    def test_position_opened_requires_api_position(self):
        class NoPosClient(FakeClient):
            def get_positions(self):
                return []                                 # jamais visible
        cli = NoPosClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        with _Cap() as cap:
            eng.cycle(1)
        t = cap.text
        self.assertIn("position_found=false", t)
        self.assertNotIn("[POSITION_OPENED]", t)

    def test_no_secrets_in_logs(self):
        secret = "SUPER_SECRET_PRIVATE_KEY_MATERIAL"
        old = bot.CFG.DEMO_PRIV_KEY
        bot.CFG.DEMO_PRIV_KEY = secret
        try:
            cli = FakeClient(order_scenario="fill")
            eng, tmp = make_engine(cli)
            with _Cap() as cap:
                eng.cycle(1)
            self.assertNotIn(secret, cap.text)
            self.assertNotIn("Authorization", cap.text)
            self.assertNotIn("signature", cap.text.lower())
        finally:
            bot.CFG.DEMO_PRIV_KEY = old

    def test_unverified_order_records_no_trade(self):
        """Exigence 5 : relecture impossible => l'ordre n'est PAS confirme,
        aucun trade enregistre."""
        class VanishingClient(FakeClient):
            def get_order(self, oid):
                raise bot.KalshiAPIError(404, "GET order", "not found")
        cli = VanishingClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        with _Cap() as cap:
            placed = eng.cycle(1)
        self.assertEqual(placed, 0)
        self.assertIn("[ORDER_VERIFY_FAILED]", cap.text)
        self.assertIn("order_not_found_after_submission", cap.text)


class TestExecutionBanner(unittest.TestCase):
    def test_banner_fields_and_demo_disclaimer(self):
        cli = FakeClient()
        with _Cap() as cap:
            eng, tmp = make_engine(cli)
        t = cap.text
        for line in ("[EXECUTION]", "environment=DEMO",
                     "execution_mode=", "dry_run=", "mock_enabled=",
                     "order_submission_enabled="):
            self.assertIn(line, t, line)
        self.assertIn("fonds DEMO", t)                    # jamais ambigu


class TestIntegrationScriptGuards(unittest.TestCase):
    def _run(self, env_extra):
        env = dict(os.environ)
        env.update({"KALSHI_DEMO_KEY_ID": "k", "KALSHI_DEMO_PRIVATE_KEY": "p",
                    "DATA_DIR": tempfile.mkdtemp()})
        env.update(env_extra)
        return subprocess.run([sys.executable, SCRIPT], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_refuses_without_explicit_flag(self):
        r = self._run({"ENABLE_DEMO_INTEGRATION_TEST": "false"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("ENABLE_DEMO_INTEGRATION_TEST", r.stdout)

    def test_refuses_live_context(self):
        r = self._run({"ENABLE_DEMO_INTEGRATION_TEST": "true",
                       "LIVE_TRADING": "1"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("DEMO uniquement", r.stdout)

    def test_refuses_non_demo_url(self):
        # DEMO_URL est code en dur (aucun override env — voulu) ; on simule
        # une Config alteree et on verifie que le script refuse AVANT tout
        # appel d'ordre.
        code = (
            "import os, sys, runpy\n"
            f"sys.path.insert(0, {ROOT + '/src/utils'!r})\n"
            "from paths import add_src_to_path; add_src_to_path()\n"
            "import kalshi_alpha_bot as bot\n"
            "bot.CFG.DEMO_URL = "
            "'https://api.elections.kalshi.com/trade-api/v2'\n"
            f"sys.argv = [{SCRIPT!r}]\n"
            f"runpy.run_path({SCRIPT!r}, run_name='__main__')\n")
        env = dict(os.environ)
        env.update({"KALSHI_DEMO_KEY_ID": "k",
                    "KALSHI_DEMO_PRIVATE_KEY": "p",
                    "DATA_DIR": tempfile.mkdtemp(),
                    "ENABLE_DEMO_INTEGRATION_TEST": "true"})
        r = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("URL inattendue", r.stdout)
        self.assertNotIn("[ORDER_SUBMIT_ATTEMPT]", r.stdout)


if __name__ == "__main__":
    unittest.main()
