# -*- coding: utf-8 -*-
"""AUD-DEMO-404-001 (2026-08-27) — forensique du POST demo 404 user_not_found.

Pinne, SANS AUCUN POST externe (transport factice, cle RSA generee en
test), les 12 exigences de l'audit :
 1. selection de l'URL de base DEMO ;
 2. refus des URL LIVE par la sonde (pin complementaire du test
    subprocess existant) ;
 3. endpoint create-order etabli par la preuve (POST 201 + fills reels
    sur demo le 2026-07-25/26, CHANGELOG 12.4/12.5) ;
 4. chemin canonique de signature == chemin HTTP transporte
    (verification RSA-PSS REELLE contre la cle publique — le GET solde
    et le POST ordre signent via la MEME fonction, hote/query/corps
    exclus, /trade-api/v2 inclus) ;
 5. methode HTTP correcte ;
 6. schema exact du payload V2 ;
 7. MEME identite (cle) pour solde et create-order ;
 8. UN SEUL POST logique par create_order reussi ;
 9. echec transport ambigu -> jamais un 2e ordre LOGIQUE (meme
    client_order_id sur chaque tentative transport) ;
10. user_not_found remonte clairement, jamais confondu avec
    l'absence de marche ;
11. aucun secret dans les logs ;
12. gardes 30c / 5c / 1 contrat inchanges.
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import base64
import contextlib
import io
import json
import logging
from urllib.parse import urlparse

import kalshi_client as kc
from kalshi_client import KalshiClient, KalshiAPIError
from config import CFG
import kalshi_demo_execution_check as kdc


# ── cle RSA de test (generee, jamais un vrai secret) ────────────────────────

def _gen_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    return key, pem


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return json.loads(self.text)


class _RecordingSession:
    """Transport factice : enregistre chaque requete (methode, URL,
    headers, corps) et sert des reponses scriptees. RIEN ne sort."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}),
                           "json": kw.get("json"),
                           "params": kw.get("params")})
        if not self.responses:
            raise AssertionError("appel transport inattendu")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _verify_signature(call, pubkey):
    """Verifie CRYPTOGRAPHIQUEMENT la signature RSA-PSS du header contre
    le message canonique ts+METHOD+path(du chemin HTTP transporte).
    Leve InvalidSignature si le chemin signe != chemin envoye."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    h = call["headers"]
    path = urlparse(call["url"]).path
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + call["method"].upper()
           + path).encode()
    pubkey.verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())


BALANCE_RESP = _Resp(200, {"balance": 13656})
ORDER_RESP = _Resp(201, {"order": {"order_id": "ord-demo-1",
                                   "client_order_id": "democheck_x",
                                   "fill_count": "0",
                                   "remaining_count": "1"}})
NOT_FOUND_RESP = _Resp(404, {"error": {"code": "user_not_found",
                                       "message": "user not found"}})


class ForensicBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key, cls.pem = _gen_key()
        cls.pub = cls.key.public_key()

    def setUp(self):
        self._old = (CFG.DEMO_KEY_ID, CFG.DEMO_PRIV_KEY)
        CFG.DEMO_KEY_ID = "forensic-key-id"
        CFG.DEMO_PRIV_KEY = self.pem

    def tearDown(self):
        CFG.DEMO_KEY_ID, CFG.DEMO_PRIV_KEY = self._old

    def client(self, *responses):
        cli = KalshiClient("demo")
        cli.session = _RecordingSession(responses)
        return cli


class TestRequestPathForensics(ForensicBase):
    # 1. selection d'URL : env demo -> DEMO_URL codee en dur
    def test_demo_base_url_selection(self):
        cli = self.client()
        self.assertEqual(cli.base_url,
                         "https://demo-api.kalshi.co/trade-api/v2")
        self.assertEqual(cli.base_url, CFG.DEMO_URL)
        self.assertEqual(cli.cred_src, "cles DEMO dediees")

    # 2. la constante de verrouillage de la sonde == URL demo du client
    def test_probe_demo_lock_matches_client_url(self):
        self.assertEqual(kdc.DEMO_BASE, CFG.DEMO_URL)
        self.assertNotEqual(kdc.DEMO_BASE, CFG.PROD_URL)

    # 3+5. endpoint et methode etablis par la preuve (POST 201 + fills
    #      reels sur demo, CHANGELOG 12.4/12.5 des 2026-07-25/26)
    def test_create_order_endpoint_and_method(self):
        cli = self.client(ORDER_RESP)
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        self.assertEqual(len(cli.session.calls), 1)      # 8. UN POST
        call = cli.session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            "https://demo-api.kalshi.co/trade-api/v2"
            "/portfolio/events/orders")
        self.assertEqual(cli.ORDERS_V2_PATH, "/portfolio/events/orders")

    # 4. chemin de signature == chemin transporte (GET et POST, meme
    #    fonction) ; hote exclu, query exclue, corps exclu,
    #    /trade-api/v2 inclus — preuve cryptographique
    def test_signature_canonical_path_get_and_post(self):
        cli = self.client(BALANCE_RESP, ORDER_RESP)
        cli.get_balance()
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        get_call, post_call = cli.session.calls
        self.assertTrue(urlparse(get_call["url"]).path
                        .startswith("/trade-api/v2/"))
        _verify_signature(get_call, self.pub)      # leve si mismatch
        _verify_signature(post_call, self.pub)
        # query params exclus du canonique : GET avec params verifie
        cli2 = self.client(_Resp(200, {"orders": []}))
        cli2.get_orders("KXBTCD-T1")
        call = cli2.session.calls[0]
        self.assertEqual(call["params"], {"ticker": "KXBTCD-T1"})
        _verify_signature(call, self.pub)          # path sans query

    # 6. schema exact du payload V2 (verifie contre OpenAPI 3.20.0 lors
    #    de la migration 12.4.0, PROUVE sur demo par les 201+fills)
    def test_payload_schema_v2_exact(self):
        cli = self.client(ORDER_RESP)
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        body = cli.session.calls[0]["json"]
        self.assertEqual(set(body), {"ticker", "client_order_id", "side",
                                     "count", "price", "time_in_force",
                                     "self_trade_prevention_type"})
        self.assertEqual(body["ticker"], "KXBTCD-T1")
        self.assertEqual(body["client_order_id"], "c1")
        self.assertEqual(body["side"], "bid")            # yes -> bid
        self.assertEqual(body["count"], "1")             # chaine, 1 contrat
        self.assertEqual(body["price"], "0.0200")        # fixed-point $
        self.assertEqual(body["time_in_force"], "good_till_canceled")
        # cote NO : ask sur le carnet YES a (100-n) cents
        cli2 = self.client(ORDER_RESP)
        cli2.create_order("KXBTCD-T1", "no", 1, 30, client_order_id="c2")
        body2 = cli2.session.calls[0]["json"]
        self.assertEqual(body2["side"], "ask")
        self.assertEqual(body2["price"], "0.7000")

    # 7. MEME identite pour solde et ordre : meme header cle, signatures
    #    des DEUX requetes verifiees par la MEME cle publique
    def test_same_identity_for_balance_and_order(self):
        cli = self.client(BALANCE_RESP, ORDER_RESP)
        cli.get_balance()
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        get_call, post_call = cli.session.calls
        self.assertEqual(get_call["headers"]["KALSHI-ACCESS-KEY"],
                         post_call["headers"]["KALSHI-ACCESS-KEY"])
        self.assertEqual(get_call["headers"]["KALSHI-ACCESS-KEY"],
                         "forensic-key-id")
        _verify_signature(get_call, self.pub)
        _verify_signature(post_call, self.pub)
        host_get = urlparse(get_call["url"]).netloc
        host_post = urlparse(post_call["url"]).netloc
        self.assertEqual(host_get, host_post)      # meme environnement

    # 9. echec transport ambigu : chaque tentative transport porte le
    #    MEME client_order_id / le MEME corps -> jamais un 2e ordre
    #    LOGIQUE ; l'erreur remonte (statut 0), jamais un faux succes
    def test_ambiguous_transport_never_second_logical_order(self):
        import requests
        errs = [requests.ConnectionError("cut")] * 4
        cli = self.client(*errs)
        real_sleep = kc.time.sleep
        kc.time.sleep = lambda s: None            # pas d'attente en test
        try:
            with self.assertRaises(KalshiAPIError) as cm:
                cli.create_order("KXBTCD-T1", "yes", 1, 2,
                                 client_order_id="c-amb")
        finally:
            kc.time.sleep = real_sleep
        self.assertEqual(cm.exception.status, 0)   # AMBIGU, type
        bodies = [c["json"] for c in cli.session.calls]
        self.assertGreater(len(bodies), 1)         # retries transport...
        self.assertEqual(len({b["client_order_id"] for b in bodies}), 1)
        self.assertTrue(all(b == bodies[0] for b in bodies))  # ...idem

    # 10a. user_not_found remonte avec statut et corps exacts
    def test_user_not_found_surfaced_with_status_and_body(self):
        cli = self.client(NOT_FOUND_RESP)
        with self.assertRaises(KalshiAPIError) as cm:
            cli.create_order("KXBTCD-T1", "yes", 1, 2,
                             client_order_id="c1")
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("user_not_found", cm.exception.body)
        self.assertEqual(len(cli.session.calls), 1)   # pas de retry (404)

    # 11. aucun secret dans les logs pendant les requetes signees
    def test_no_secret_material_in_logs(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.DEBUG)
        api_log = logging.getLogger("API")
        api_log.addHandler(h)
        old = api_log.level
        api_log.setLevel(logging.DEBUG)
        try:
            cli = self.client(BALANCE_RESP, ORDER_RESP)
            cli.get_balance()
            cli.create_order("KXBTCD-T1", "yes", 1, 2,
                             client_order_id="c1")
            sig = cli.session.calls[0]["headers"]["KALSHI-ACCESS-SIGNATURE"]
        finally:
            api_log.removeHandler(h)
            api_log.setLevel(old)
        t = buf.getvalue()
        key_material = self.pem.splitlines()[1]        # 1re ligne base64
        self.assertNotIn(key_material, t)
        self.assertNotIn(sig, t)
        self.assertNotIn("KALSHI-ACCESS-SIGNATURE", t)

    # 12. gardes de la sonde inchanges
    def test_probe_guards_unchanged(self):
        self.assertEqual(kdc.MAX_ASK_CENTS, 30)
        self.assertEqual(kdc.MAX_SPREAD_CENTS, 5)


class TestProbeSurfacesUserNotFound(unittest.TestCase):
    """10b. niveau sonde : un 404 user_not_found au POST est un ECHEC
    D'EXECUTION visible (exit 1, ORDER_SUBMIT_FAILED) — jamais confondu
    avec l'absence de marche eligible (NO_ELIGIBLE_MARKET, exit 0)."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in (
            "ENABLE_DEMO_INTEGRATION_TEST", "DEMO_PROBE_MAX_WAIT_SECONDS",
            "DEMO_PROBE_DISCOVERY_ONLY")}
        os.environ["ENABLE_DEMO_INTEGRATION_TEST"] = "true"
        os.environ["DEMO_PROBE_MAX_WAIT_SECONDS"] = "0"
        os.environ.pop("DEMO_PROBE_DISCOVERY_ONLY", None)
        self._KalshiClient = kdc.KalshiClient
        self._genuine = kdc._client_is_genuine
        kdc._client_is_genuine = lambda c: True

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        kdc.KalshiClient = self._KalshiClient
        kdc._client_is_genuine = self._genuine

    def test_404_user_not_found_is_execution_failure_not_no_market(self):
        class Cli:
            base_url = kdc.DEMO_BASE
            ORDERS_V2_PATH = "/portfolio/events/orders"
            create_calls = 0

            def get_balance(self):
                return 136.56

            def get_markets(self, series, status="open", limit=100):
                if series == kdc.CANDIDATE_SERIES[0]:
                    return [{"ticker": "KXBTCD-PROBE", "status": "active",
                             "yes_ask": 2, "yes_bid": 1}]
                return []

            def create_order(self, *a, **k):
                Cli.create_calls += 1
                raise KalshiAPIError(
                    404, "POST /portfolio/events/orders",
                    '{"error":{"code":"user_not_found",'
                    '"message":"user not found"}}')

        kdc.KalshiClient = lambda env: Cli()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                kdc.main()
        t = buf.getvalue()
        self.assertEqual(cm.exception.code, 1)         # ECHEC, pas 0
        self.assertEqual(Cli.create_calls, 1)          # un seul POST
        self.assertIn("[ORDER_SUBMIT_FAILED]", t)
        self.assertIn("http_status=404", t)
        self.assertIn("user_not_found", t)
        self.assertNotIn("[NO_ELIGIBLE_MARKET]", t)    # jamais confondu


if __name__ == "__main__":
    unittest.main()
