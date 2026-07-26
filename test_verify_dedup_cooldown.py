# -*- coding: utf-8 -*-
"""Regression des logs de prod 2026-07-25 23:14+ :
1. GET /portfolio/orders/{id} -> 404 pour les ordres crees en V2 : les fills
   REELS etaient rejetes "unverified" (aucun trade local) ;
2. consequence : le MEME signal etait re-soumis et REMPLI a chaque cycle
   (solde 80.21 -> 74.93$ en 8 cycles) -> garde anti-doublon de session ;
3. spam de retries sur 503 exchange -> cooldown global."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot


class ProdLikeClient:
    """Reproduit le comportement observe : create 201 executed,
    get_order 404, get_fills et positions fonctionnels."""
    env = "demo"
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def __init__(self, fail_503=False):
        self.last_http_status = 201
        self.fail_503 = fail_503
        self.create_calls = 0

    def create_order(self, ticker, side, count, price_cents,
                     client_order_id=None):
        self.create_calls += 1
        if self.fail_503:
            raise bot.KalshiAPIError(503, "POST /portfolio/events/orders",
                                     '{"error":{"code":"service_unavailable"}}')
        self.last_http_status = 201
        return {"order_id": "ord_prod_1", "client_order_id": client_order_id,
                "status": "executed", "taker_fill_count": int(count),
                "remaining_count": 0, "avg_price_cents": int(price_cents),
                "average_fee_paid": "0.0135", "ts_ms": 1785021275816,
                "v2_side": "bid", "v2_price": "0.7400",
                "raw": {"fill_count": f"{int(count)}.00",
                        "remaining_count": "0.00"}}

    def get_order(self, order_id):
        raise bot.KalshiAPIError(404, f"GET /portfolio/orders/{order_id}", "")

    def get_fills(self, order_id):
        return [{"count": 1, "price": 74, "fees": "0.0135"}]

    def get_positions(self):
        return [{"ticker": "KXBTCD-26JUL2617-T63749.99", "position": 1}]

    def cancel_order(self, order_id):
        return {}


def make_om(tmp, client=None):
    bot.CFG.DATA_DIR = tmp
    os.makedirs(tmp, exist_ok=True)
    om = bot.OrderManager(client or ProdLikeClient())
    om.open_orders = {}
    return om


class TestVerifyFallbackOnV2Get404(unittest.TestCase):
    def test_filled_order_confirmed_via_create_response_and_fills(self):
        """Le scenario de prod exact : plus jamais 'unverified' quand la
        reponse de creation certifie le fill."""
        om = make_om("/tmp/t_v2fix_a")
        res = om.place_and_track("KXBTCD-26JUL2617-T63749.99", "yes", 1, 74)
        self.assertNotEqual(res.status, "unverified")
        self.assertEqual(res.filled, 1)            # fill reel comptabilise
        self.assertEqual(res.state, "filled")

    def test_dedup_guard_blocks_resubmission_same_ticker(self):
        om = make_om("/tmp/t_v2fix_b")
        cli = om.client
        om.place_and_track("KXBTCD-26JUL2617-T63749.99", "yes", 1, 74)
        n = cli.create_calls
        res2 = om.place_and_track("KXBTCD-26JUL2617-T63749.99", "yes", 1, 74)
        self.assertEqual(res2.status, "blocked:duplicate_submission_guard")
        self.assertEqual(cli.create_calls, n)       # AUCUN appel API en plus
        # autre ticker : non bloque
        res3 = om.place_and_track("KXBTC15M-26JUL252030-30", "no", 1, 40)
        self.assertNotEqual(res3.status, "blocked:duplicate_submission_guard")

    def test_dedup_guard_survives_failed_verification(self):
        """La garde s'arme des le 201, meme si aucune verification
        n'aboutit : c'est le point qui manquait en prod."""
        class NoFills(ProdLikeClient):
            def get_fills(self, order_id):
                raise bot.KalshiAPIError(404, "GET /portfolio/fills", "")
        om = make_om("/tmp/t_v2fix_c", NoFills())
        om.place_and_track("KXBTCD-X", "yes", 1, 74)
        res2 = om.place_and_track("KXBTCD-X", "yes", 1, 74)
        self.assertEqual(res2.status, "blocked:duplicate_submission_guard")


class TestExchange503Cooldown(unittest.TestCase):
    def test_cooldown_after_503_blocks_without_api_call(self):
        cli = ProdLikeClient(fail_503=True)
        om = make_om("/tmp/t_v2fix_d", cli)
        res = om.place_and_track("KXBTCD-X", "yes", 1, 74)
        self.assertEqual(res.status, "rejected:503")
        n = cli.create_calls
        res2 = om.place_and_track("KXBTCD-Y", "yes", 1, 74)   # autre ticker
        self.assertEqual(res2.status,
                         "blocked:exchange_unavailable_cooldown")
        self.assertEqual(cli.create_calls, n)       # pas d'appel pendant pause
        # reprise apres expiration du cooldown
        om.exchange_pause_until = 0.0
        cli.fail_503 = False
        res3 = om.place_and_track("KXBTCD-Y", "yes", 1, 74)
        self.assertEqual(res3.state, "filled")


if __name__ == "__main__":
    unittest.main()
