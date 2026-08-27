# -*- coding: utf-8 -*-
"""AUD-DEMO-LIFECYCLE-005 (2026-08-28) — cycle de vie d'ordre + garde
persistante anti-duplication de la sonde demo.

Defauts mesures en production demo :
 (a) HTTP 201 (ordre CREE, resting) puis GET /portfolio/orders/{id} 404
     -> la sonde concluait ORDER_VERIFY_FAILED et sortait en echec —
     alors que le moteur connait ce 404 demo depuis 12.5.0 et verifie
     par la reponse de creation ;
 (b) chaque redemarrage Railway relancait la sonde avec un
     client_order_id ALEATOIRE neuf -> fills reels repetes (11 YES).

Pinne : 201 = preuve autoritaire (ORDER_CREATED_CONFIRMED persiste) ;
404 de relecture = ORDER_LOOKUP_UNAVAILABLE (source=create_response_v2),
jamais un echec ni un re-POST ; coid DETERMINISTE par run-id ; journal
d'intention persistant ecrit AVANT le POST ; pre-verification broker
(survit a un FS neuf) ; redemarrage => reconciliation lecture seule,
JAMAIS un 2e POST ; journal illisible / pre-verification impossible =>
fail-closed sans POST ; comptage de positions : ligne broker nulle
filtree, 4 lignes reelles = 4 ouvertes, garde MAX_OPEN_POSITIONS=3
inchangee. Aucun reseau, aucun ordre externe.
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import contextlib
import io
import json
import tempfile

import kalshi_demo_execution_check as kdc
import kalshi_position_state_check as kpc
from kalshi_client import KalshiAPIError


class LifecycleClient:
    """Client factice : marche eligible immediat, comportements de
    relecture/fills pilotables, broker orders consultable."""

    base_url = kdc.DEMO_BASE
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def __init__(self, get_order_behavior="ok", fills=True,
                 broker_orders=None, create_behavior="ok",
                 orders_exc=None):
        self.get_order_behavior = get_order_behavior
        self.fills = fills
        self.broker_orders = list(broker_orders or [])
        self.create_behavior = create_behavior
        self.orders_exc = orders_exc
        self.create_calls = 0
        self.cancel_calls = 0
        self.last_http_status = None

    def get_balance(self):
        return 131.91

    def get_markets(self, series, status="open", limit=100):
        if series == kdc.CANDIDATE_SERIES[0]:
            return [{"ticker": "KXBTCD-LIFE", "status": "active",
                     "yes_ask": 3, "yes_bid": 1}]
        return []

    def get_orders(self, ticker=None):
        if self.orders_exc:
            raise self.orders_exc
        return list(self.broker_orders)

    def create_order(self, ticker, side, count, price_cents,
                     client_order_id=None):
        self.create_calls += 1
        if self.create_behavior == "404":
            raise KalshiAPIError(404, "POST", '{"code":"user_not_found"}')
        self.last_http_status = 201
        order = {"order_id": "ord-life-1",
                 "client_order_id": client_order_id,
                 "status": "resting", "taker_fill_count": 0,
                 "remaining_count": 1}
        if self.create_behavior == "no_order_id":
            order.pop("order_id")
        self.broker_orders.append(dict(order))
        return order

    def get_order(self, oid):
        if self.get_order_behavior == "404":
            raise KalshiAPIError(404, f"GET /portfolio/orders/{oid}",
                                 '{"error":"not_found"}')
        return {"order_id": oid, "status": "resting",
                "taker_fill_count": 0, "remaining_count": 1}

    def get_fills(self, order_id, **kw):
        if self.fills:
            return [{"fill_id": "f1", "count": 1, "yes_price": 3,
                     "fees": "0.01"}]
        return []

    def cancel_order(self, oid):
        self.cancel_calls += 1
        return {"order_id": oid, "reduced_by": 1, "status": "canceled"}

    def get_positions(self):
        if self.fills:
            return [{"ticker": "KXBTCD-LIFE", "position": 1,
                     "market_exposure": 3, "realized_pnl": 0,
                     "fees_paid": 1}]
        return []


class MainRig(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (
            "ENABLE_DEMO_INTEGRATION_TEST", "DEMO_PROBE_MAX_WAIT_SECONDS",
            "DEMO_PROBE_DISCOVERY_ONLY", "DEMO_PROBE_RUN_ID", "DATA_DIR")}
        os.environ["ENABLE_DEMO_INTEGRATION_TEST"] = "true"
        os.environ["DEMO_PROBE_MAX_WAIT_SECONDS"] = "0"
        os.environ["DEMO_PROBE_RUN_ID"] = "testrun1"
        os.environ.pop("DEMO_PROBE_DISCOVERY_ONLY", None)
        os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lifecycle_")
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

    def run_main(self, client):
        kdc.KalshiClient = lambda env: client
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                kdc.main()
        return cm.exception.code, buf.getvalue()

    def guard(self):
        with open(os.path.join(os.environ["DATA_DIR"],
                               kdc.GUARD_FILE), encoding="utf-8") as f:
            return json.load(f)


class TestAuthoritative201(MainRig):
    def test_201_then_get404_never_repost_filled_confirmed(self):
        """Exigences 1+2 : 201 + relecture 404 -> l'ordre reste CONFIRME
        (source=create_response_v2), fills -> FILLED, UN SEUL POST."""
        cli = LifecycleClient(get_order_behavior="404", fills=True)
        code, t = self.run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)
        self.assertIn("[ORDER_CREATED_CONFIRMED]", t)
        self.assertIn("[ORDER_LOOKUP_UNAVAILABLE]", t)
        self.assertIn("source=create_response_v2", t)
        self.assertNotIn("[ORDER_VERIFY_FAILED]", t)
        self.assertIn("[ORDER_FILLED]", t)
        self.assertIn("[DEMO_EXECUTION_PROVED]", t)
        self.assertEqual(self.guard()[-1]["outcome"], "filled")

    def test_201_get_ok_resting_then_cancel(self):
        """Exigence 3 : relecture OK, pas de fill -> annulation propre,
        outcome persiste canceled_unfilled."""
        kdc.FILL_TIMEOUT_S = 0.0
        cli = LifecycleClient(get_order_behavior="ok", fills=False)
        code, t = self.run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)
        self.assertEqual(cli.cancel_calls, 1)
        self.assertIn("source=get_order", t)
        self.assertEqual(self.guard()[-1]["outcome"], "canceled_unfilled")

    def test_201_without_order_id_ambiguous_no_repost(self):
        """Exigence 4 : 201 illisible -> AMBIGUOUS_POST_CREATED_STATE
        persiste, exit 3, jamais de re-POST dans la meme execution."""
        cli = LifecycleClient(create_behavior="no_order_id")
        code, t = self.run_main(cli)
        self.assertEqual(code, 3)
        self.assertEqual(cli.create_calls, 1)
        self.assertIn("[AMBIGUOUS_POST_CREATED_STATE]", t)
        self.assertEqual(self.guard()[-1]["outcome"], "ambiguous")


class TestPersistentGuard(MainRig):
    def test_restart_blocks_duplicate_post(self):
        """Exigence 5 : le MEME DATA_DIR (redemarrage) -> le 2e run est
        BLOQUE par le journal et reconcilie en lecture seule."""
        cli1 = LifecycleClient(fills=True)
        code1, _ = self.run_main(cli1)
        self.assertEqual(code1, 0)
        self.assertEqual(cli1.create_calls, 1)
        # "redemarrage" : nouveau client, meme DATA_DIR, meme run-id
        cli2 = LifecycleClient(fills=True,
                               broker_orders=list(cli1.broker_orders))
        code2, t2 = self.run_main(cli2)
        self.assertEqual(code2, 0)
        self.assertEqual(cli2.create_calls, 0)          # JAMAIS de 2e POST
        self.assertIn("[SUBMISSION_GUARD] blocked=true", t2)
        self.assertIn("[GUARD_RECONCILE]", t2)
        self.assertIn("ALREADY_SUBMITTED_RECONCILED", t2)

    def test_fresh_fs_broker_precheck_blocks(self):
        """FS NEUF (redeploiement) mais l'ordre existe chez le broker
        sous le coid deterministe -> bloque AVANT tout POST."""
        coid = f"democheck_{kdc.probe_run_id()}"
        cli = LifecycleClient(broker_orders=[
            {"order_id": "ord-prev", "client_order_id": coid,
             "status": "resting"}])
        code, t = self.run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 0)
        self.assertIn("reason=ordre_broker", t)
        self.assertIn("broker_has_order=True", t)

    def test_corrupted_guard_fails_closed(self):
        with open(os.path.join(os.environ["DATA_DIR"], kdc.GUARD_FILE),
                  "w", encoding="utf-8") as f:
            f.write("{corrompu")
        cli = LifecycleClient()
        code, t = self.run_main(cli)
        self.assertEqual(code, 3)
        self.assertEqual(cli.create_calls, 0)
        self.assertIn("GUARD_UNREADABLE", t)

    def test_precheck_error_fails_closed(self):
        cli = LifecycleClient(orders_exc=KalshiAPIError(0, "reseau"))
        code, t = self.run_main(cli)
        self.assertEqual(code, 3)
        self.assertEqual(cli.create_calls, 0)
        self.assertIn("GUARD_UNVERIFIABLE", t)

    def test_deterministic_coid_and_override(self):
        """Exigence 6 : coid stable par run-id (les retries transport et
        les relances portent le MEME id logique)."""
        self.assertEqual(kdc.probe_run_id(), "testrun1")
        os.environ["DEMO_PROBE_RUN_ID"] = "run/2!x"
        self.assertEqual(kdc.probe_run_id(), "run2x")   # assaini

    def test_deterministic_failure_allows_deliberate_relaunch(self):
        """Exigence 7 : un echec DETERMINISTE (404 user_not_found —
        aucun ordre cree) n'empoisonne pas le run-id : une relance
        deliberee re-tente ; mais AUCUN retry dans la meme execution."""
        cli1 = LifecycleClient(create_behavior="404")
        code1, t1 = self.run_main(cli1)
        self.assertEqual(code1, 1)
        self.assertEqual(cli1.create_calls, 1)
        self.assertTrue(self.guard()[-1]["outcome"].startswith("failed:"))
        cli2 = LifecycleClient(fills=True)
        code2, _ = self.run_main(cli2)
        self.assertEqual(code2, 0)
        self.assertEqual(cli2.create_calls, 1)

    def test_guard_file_contains_no_secret(self):
        cli = LifecycleClient(fills=True)
        self.run_main(cli)
        raw = json.dumps(self.guard())
        self.assertNotIn("PRIVATE", raw.upper())
        self.assertNotIn("SIGNATURE", raw.upper())
        for rec in self.guard():
            self.assertLessEqual(
                set(rec), {"run_id", "client_order_id", "ticker", "ts",
                           "order_id", "outcome"})


class TestPositionCounting(unittest.TestCase):
    """Exigences 8-11 : lignes broker nulles filtrees ; positions
    reelles comptees ; 4 reelles -> garde 3 BLOQUANTE (comportement
    correct, pas un bug) ; seuils inchanges."""

    def test_zero_broker_line_not_counted(self):
        from exchange.kalshi_contracts import classify_positions
        out = classify_positions(
            [{"ticker": "KXBTCD-A", "position": 0},
             {"ticker": "KXBTCD-B", "position": 2,
              "market_exposure": 6}], [])
        kinds = [e["classification"] for e in out["entries"]]
        self.assertEqual(kinds, ["BROKER_ONLY"])        # la nulle filtree

    def test_four_real_positions_counted_and_guard_blocks(self):
        import kalshi_alpha_bot as bot
        rows = [{"ticker": f"KXBTCD-26AUG2717-T{s}", "position": n,
                 "market_exposure": n * 3}
                for s, n in (("80749.99", 1), ("80999.99", 2),
                             ("81499.99", 1), ("82999.99", 11))]
        tmp = tempfile.mkdtemp(prefix="poscount_")
        old = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = tmp
        try:
            pm = bot.PositionManager(
                type("C", (), {"get_positions":
                               staticmethod(lambda: rows)})(),
                bot.TradeLogger())
            pm.positions = {}
            pm.reconcile_with_broker()
            self.assertEqual(pm.open_count(), 4)
            self.assertGreaterEqual(pm.open_count(),
                                    bot.CFG.MAX_OPEN_POSITIONS)
        finally:
            bot.CFG.DATA_DIR = old
        self.assertEqual(bot.CFG.MAX_OPEN_POSITIONS, 3)  # INCHANGE

    def test_settled_local_position_not_counted(self):
        import kalshi_alpha_bot as bot
        tmp = tempfile.mkdtemp(prefix="possettle_")
        old = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = tmp
        try:
            pm = bot.PositionManager(type("C", (), {})(),
                                     bot.TradeLogger())
            pm.positions = {
                "a": {"ticker": "T1", "state": "open", "count": 1,
                      "avg_price": 3},
                "b": {"ticker": "T2", "state": "settled", "count": 1,
                      "avg_price": 3},
                "c": {"ticker": "T3", "state": "settlement_unknown",
                      "count": 1, "avg_price": 3}}
            self.assertEqual(pm.open_count(), 1)
            self.assertEqual(pm.tickers_open(), {"T1"})
        finally:
            bot.CFG.DATA_DIR = old

    def test_probe_guards_unchanged(self):
        self.assertEqual(kdc.MAX_ASK_CENTS, 30)
        self.assertEqual(kdc.MAX_SPREAD_CENTS, 5)


class TestPositionAuditScript(unittest.TestCase):
    """Audit lecture seule kalshi_position_state_check.py."""

    class Cli:
        def __init__(self, rows, markets):
            self.rows, self.markets = rows, markets

        def get_positions(self):
            return self.rows

        def get_market(self, tk):
            return self.markets.get(tk, {})

    def test_audit_lines_and_summary(self):
        rows = [
            {"ticker": "KXBTCD-OPEN", "position": 2, "market_exposure": 6},
            {"ticker": "KXBTCD-FLAT", "position": 0},
            {"ticker": "KXBTCD-SETTLED", "position": 11,
             "market_exposure": 33}]
        markets = {
            "KXBTCD-OPEN": {"status": "active", "close_time": "x",
                            "exchange_index": 2},
            "KXBTCD-SETTLED": {"status": "settled", "result": "yes"}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n, counted = kpc.audit_positions(self.Cli(rows, markets))
        t = buf.getvalue()
        self.assertEqual((n, counted), (3, 2))
        self.assertIn("ticker=KXBTCD-OPEN", t)
        self.assertIn("counted_open=true reason=position_broker_non_nulle",
                      t)
        self.assertIn("counted_open=false reason=position_nulle(filtree)",
                      t)
        self.assertIn("regle_mais_encore_compte", t)
        self.assertIn("[POSITION_SUMMARY] broker_positions=3 "
                      "counted_open=2 max_open_positions=3", t)

    def test_script_has_no_write_verbs(self):
        src = open(kpc.__file__, encoding="utf-8").read()
        for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"',
                     "create_order", "cancel_order",
                     "intra_exchange_instance_transfer"):
            self.assertNotIn(verb, src, verb)

    def test_live_context_refused(self):
        os.environ["LIVE_TRADING"] = "1"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    kpc.main()
            self.assertEqual(cm.exception.code, 2)
        finally:
            os.environ.pop("LIVE_TRADING", None)


if __name__ == "__main__":
    unittest.main()
