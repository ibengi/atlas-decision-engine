# -*- coding: utf-8 -*-
"""Migration Create Order V2 (HTTP 410 deprecated_v1_order_endpoint du
2026-07-25) + regression du prix fantome : no_ask_dollars="1.0000" (cote
VIDE, 1,00$) etait parse 1 cent => edge +0.96 imaginaire et spam d'ordres.

Schema V2 verifie contre l'OpenAPI officielle (docs.kalshi.com,
api-reference/orders/create-order-v2, spec 3.20.0) :
  POST /portfolio/events/orders
  side: bid|ask (carnet YES uniquement), price: dollars fixed-point,
  count: chaine decimale, time_in_force + self_trade_prevention requis ;
  reponse SANS status : order_id, fill_count, remaining_count,
  average_fill_price (dollars YES), average_fee_paid, ts_ms."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import inspect

import kalshi_alpha_bot as bot
from market_scanner import price_to_cents, read_price, _has_liquidity


def make_client(v2_response):
    """Vrai KalshiClient, _req remplace par une capture (aucun reseau)."""
    cli = bot.KalshiClient("demo")
    cli.captured = []

    def fake_req(method, path, retries=3, **kw):
        cli.captured.append((method, path, kw.get("json")))
        cli.last_http_status = 201
        return v2_response
    cli._req = fake_req
    return cli


RESP_FILLED = {"order_id": "ord_v2_1", "client_order_id": "c1",
               "fill_count": "93.00", "remaining_count": "0.00",
               "average_fill_price": "0.9900", "average_fee_paid": "0.0002",
               "ts_ms": 1785008000000}
RESP_RESTING = {"order_id": "ord_v2_2", "fill_count": "0.00",
                "remaining_count": "93.00", "ts_ms": 1785008000001}


class TestCreateOrderV2Payload(unittest.TestCase):
    def test_endpoint_and_required_fields(self):
        cli = make_client(RESP_FILLED)
        cli.create_order("KXBTCD-X", "yes", 10, 48, client_order_id="cid")
        method, path, body = cli.captured[0]
        self.assertEqual((method, path), ("POST", "/portfolio/events/orders"))
        # champs REQUIS du schema V2, formats exacts
        self.assertEqual(body["ticker"], "KXBTCD-X")
        self.assertEqual(body["client_order_id"], "cid")
        self.assertEqual(body["side"], "bid")            # yes -> bid
        self.assertEqual(body["count"], "10")            # chaine decimale
        self.assertEqual(body["price"], "0.4800")        # dollars fixed-point
        self.assertEqual(body["time_in_force"], "good_till_canceled")
        self.assertEqual(body["self_trade_prevention_type"], "taker_at_cross")
        # AUCUN champ V1
        for k in ("action", "type", "yes_price", "no_price"):
            self.assertNotIn(k, body)

    def test_no_side_maps_to_ask_at_complement(self):
        """Acheter NO a 1c == vendre YES (ask) a 0.9900$ — convention V2
        'carnet YES uniquement' de la spec."""
        cli = make_client(RESP_FILLED)
        cli.create_order("KXBTCD-X", "no", 93, 1)
        _, _, body = cli.captured[0]
        self.assertEqual(body["side"], "ask")
        self.assertEqual(body["price"], "0.9900")

    def test_response_normalized_to_internal_schema(self):
        cli = make_client(RESP_FILLED)
        out = cli.create_order("KXBTCD-X", "no", 93, 1)
        self.assertEqual(out["order_id"], "ord_v2_1")
        self.assertEqual(out["status"], "executed")      # derive fill==count
        self.assertEqual(out["taker_fill_count"], 93)    # "93.00" -> 93
        # avg YES 0.99$ -> NOTRE cote NO = 1c
        self.assertEqual(out["avg_price_cents"], 1)

    def test_resting_derived_without_status_field(self):
        cli = make_client(RESP_RESTING)
        out = cli.create_order("KXBTCD-X", "yes", 93, 40)
        self.assertEqual(out["status"], "resting")
        self.assertEqual(out["taker_fill_count"], 0)
        # accepted != filled : _extract de l'OrderManager voit 0 rempli
        status, filled = bot.OrderManager._extract(out, 93)
        self.assertEqual((status, filled), ("resting", 0))


class TestNoV1OrderEndpointLeft(unittest.TestCase):
    def test_no_post_to_legacy_portfolio_orders(self):
        src = inspect.getsource(bot)
        self.assertNotIn('"POST", "/portfolio/orders"', src)
        # GET/DELETE /portfolio/orders/{id} restent V2-compatibles ; tout
        # 410 'deprecated' futur leve un message de migration explicite
        self.assertIn("ENDPOINT V1 OBSOLETE", src)

    def test_demo_script_uses_client_hence_v2(self):
        p = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts",
            "kalshi_demo_execution_check.py")
        src = open(p, encoding="utf-8").read()
        self.assertIn("client.create_order", src)
        self.assertNotIn('"/portfolio/orders"', src)


class TestPhantomPriceRegression(unittest.TestCase):
    """no_ask_dollars="1.0000" = 1,00$ = cote VIDE — plus jamais 1 cent."""

    def test_dollars_field_one_dollar_is_empty(self):
        self.assertIsNone(price_to_cents("1.0000", dollars=True))
        self.assertIsNone(price_to_cents(1.0, dollars=True))
        self.assertEqual(price_to_cents("0.9900", dollars=True), 99)
        self.assertEqual(price_to_cents("0.0100", dollars=True), 1)

    def test_read_price_treats_dollars_variant_as_dollars(self):
        m = {"no_ask_dollars": "1.0000"}                 # profil du RAW reel
        self.assertIsNone(read_price(m, "no_ask"))
        m2 = {"no_ask_dollars": "0.5400"}
        self.assertEqual(read_price(m2, "no_ask"), 54)

    def test_legacy_bare_one_cent_still_valid(self):
        self.assertEqual(price_to_cents(1), 1)           # champ cents legacy
        self.assertEqual(price_to_cents("1"), 1)

    def test_empty_dollars_book_rejected_by_liquidity(self):
        # marche KXBTCD reel des logs : les DEUX cotes vides en dollars
        m = {"ticker": "KXBTCD-26JUL2617",
             "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000",
             "no_bid_dollars": "0.0000", "no_ask_dollars": "1.0000",
             "open_interest_fp": "0.00", "liquidity_dollars": "0.0000"}
        self.assertFalse(_has_liquidity(m))

    def test_engine_no_longer_sees_1c_phantom(self):
        """Le carnet du bug prod (yes 0/1.0000$) ne produit plus de book,
        donc plus d'edge +0.96 fantome ni d'ordre."""
        book = bot.MarketValidator.normalize_book(
            {"yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000",
             "no_bid_dollars": "0.0000", "no_ask_dollars": "1.0000"})
        self.assertIsNone(book)


if __name__ == "__main__":
    unittest.main()
