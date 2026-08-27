# -*- coding: utf-8 -*-
"""AUD-DEMO-ORDER-IDENTITY-002 (2026-08-27) — identite demo + hote actuel.

Constat documentaire : la racine Trade API demo RECOMMANDEE est desormais
external-api.demo.kalshi.co ; demo-api.kalshi.co (utilisee par ATLAS,
y compris lors des POST demo reussis de juillet) n'est plus que l'hote
partage "compatibilite". L'anomalie observee (GET solde OK, POST ordre
404 user_not_found) est exactement la forme d'une divergence de routage
d'identite sur l'hote legacy pour la route d'execution la plus recente.

Pinne : allowlist d'hotes DEMO (les DEUX racines officielles, JAMAIS
prod/LIVE — toute autre valeur retombe sur l'hote legacy), empreinte
d'identite sans secret identique pour solde et ordre, signature
canonique verifiee sur l'hote actuel, payload V2 courant SANS champ
subaccount/exchange_index (= compte par defaut, celui du solde), et les
gardes 30c/5c/1 contrat inchanges.

Aucun reseau, aucun ordre externe (transport factice).
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import hashlib
from urllib.parse import urlparse

import config
from config import CFG
from kalshi_client import KalshiClient
import kalshi_demo_execution_check as kdc
from test_demo_post_forensics import (ForensicBase, _RecordingSession,
                                      _verify_signature, BALANCE_RESP,
                                      ORDER_RESP)


class TestDemoHostAllowlist(unittest.TestCase):
    """3+6. Hote demo : surchargeable UNIQUEMENT vers une racine DEMO
    connue ; prod/LIVE/typo -> retombee sur l'hote legacy (fail-safe)."""

    def _resolve(self, value):
        old = os.environ.get("KALSHI_DEMO_BASE_URL")
        if value is None:
            os.environ.pop("KALSHI_DEMO_BASE_URL", None)
        else:
            os.environ["KALSHI_DEMO_BASE_URL"] = value
        try:
            return config.resolve_demo_url()
        finally:
            if old is None:
                os.environ.pop("KALSHI_DEMO_BASE_URL", None)
            else:
                os.environ["KALSHI_DEMO_BASE_URL"] = old

    def test_default_is_legacy_host(self):
        self.assertEqual(self._resolve(None), config.DEMO_URL_LEGACY)

    def test_current_documented_host_allowed(self):
        self.assertEqual(self._resolve(config.DEMO_URL_CURRENT),
                         config.DEMO_URL_CURRENT)
        self.assertEqual(
            self._resolve("https://external-api.demo.kalshi.co"
                          "/trade-api/v2/"),        # slash tolere
            config.DEMO_URL_CURRENT)

    def test_prod_live_and_garbage_fall_back_to_legacy(self):
        for bad in (CFG.PROD_URL,
                    "https://api.elections.kalshi.com/trade-api/v2",
                    "https://evil.example.com/trade-api/v2",
                    "demo-api.kalshi.co",           # sans schema
                    ""):
            self.assertEqual(self._resolve(bad), config.DEMO_URL_LEGACY,
                             bad)

    def test_probe_allowlist_is_exactly_the_two_demo_roots(self):
        self.assertEqual(set(kdc.DEMO_ALLOWED_BASES),
                         {config.DEMO_URL_LEGACY, config.DEMO_URL_CURRENT})
        self.assertNotIn(CFG.PROD_URL, kdc.DEMO_ALLOWED_BASES)
        for base in kdc.DEMO_ALLOWED_BASES:        # jamais un hote LIVE
            host = urlparse(base).netloc
            self.assertIn("demo", host)
            self.assertNotIn("elections", host)


class TestIdentityFingerprint(ForensicBase):
    """2. La MEME empreinte d'identite (sans secret) sert le GET solde et
    le POST ordre ; l'empreinte est comparable a la cle visible en UI."""

    def test_same_fingerprint_for_balance_and_order(self):
        cli = self.client(BALANCE_RESP, ORDER_RESP)
        fp = kdc.identity_fingerprint(cli)
        cli.get_balance()
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        get_call, post_call = cli.session.calls
        for call in (get_call, post_call):
            kid = call["headers"]["KALSHI-ACCESS-KEY"]
            self.assertEqual(
                hashlib.sha256(kid.encode()).hexdigest()[:12],
                fp["key_id_sha256_12"])
            self.assertEqual(kid[-4:], fp["key_id_suffix4"])
        # 1. meme hote demo pour les deux requetes
        self.assertEqual(urlparse(get_call["url"]).netloc,
                         urlparse(post_call["url"]).netloc)

    def test_fingerprint_contains_no_secret(self):
        cli = self.client()
        fp = kdc.identity_fingerprint(cli)
        s = str(fp)
        self.assertNotIn(self.pem.splitlines()[1], s)   # materiau prive
        self.assertNotIn(CFG.DEMO_KEY_ID, s)            # key id complet
        self.assertEqual(len(fp["key_id_sha256_12"]), 12)
        self.assertEqual(len(fp["pubkey_sha256_12"]), 12)
        self.assertEqual(len(fp["key_id_suffix4"]), 4)

    def test_pubkey_fingerprint_tracks_private_key(self):
        """Deux cles privees differentes -> empreintes publiques
        differentes : l'empreinte discrimine reellement l'identite."""
        from test_demo_post_forensics import _gen_key
        cli1 = self.client()
        fp1 = kdc.identity_fingerprint(cli1)
        _, pem2 = _gen_key()
        CFG.DEMO_PRIV_KEY = pem2
        cli2 = self.client()
        fp2 = kdc.identity_fingerprint(cli2)
        self.assertNotEqual(fp1["pubkey_sha256_12"], fp2["pubkey_sha256_12"])


class TestCurrentHostExecutionPath(ForensicBase):
    """3+4+5. Sur l'hote documente ACTUEL : meme endpoint, meme methode,
    signature canonique verifiee, payload V2 courant, compte par defaut
    (AUCUN champ subaccount/exchange_index — l'identite est celle du
    solde)."""

    def setUp(self):
        super().setUp()
        self._url = CFG.DEMO_URL
        CFG.DEMO_URL = config.DEMO_URL_CURRENT

    def tearDown(self):
        CFG.DEMO_URL = self._url
        super().tearDown()

    def test_order_path_and_signature_on_current_host(self):
        cli = self.client(BALANCE_RESP, ORDER_RESP)
        self.assertEqual(cli.base_url, config.DEMO_URL_CURRENT)
        cli.get_balance()
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        get_call, post_call = cli.session.calls
        self.assertEqual(
            post_call["url"],
            "https://external-api.demo.kalshi.co/trade-api/v2"
            "/portfolio/events/orders")
        self.assertEqual(post_call["method"], "POST")
        _verify_signature(get_call, self.pub)      # meme canonique
        _verify_signature(post_call, self.pub)     # sur l'hote actuel

    def test_payload_targets_default_account_no_subaccount_fields(self):
        cli = self.client(ORDER_RESP)
        cli.create_order("KXBTCD-T1", "yes", 1, 2, client_order_id="c1")
        body = cli.session.calls[0]["json"]
        # champs OPTIONNELS du contrat V2 courant, volontairement ABSENTS
        # -> l'ordre vise le compte PAR DEFAUT de la cle (= celui que le
        # GET solde lit). Aucun champ 'action' n'existe dans le contrat
        # V2 (side bid/ask le remplace).
        self.assertNotIn("subaccount", body)
        self.assertNotIn("exchange_index", body)
        self.assertNotIn("action", body)
        self.assertEqual(body["side"], "bid")
        self.assertEqual(body["count"], "1")       # 11. 1 contrat
        self.assertEqual(body["price"], "0.0200")


class TestGuardsUnchanged(unittest.TestCase):
    def test_probe_criteria_and_contract_limits(self):
        self.assertEqual(kdc.MAX_ASK_CENTS, 30)
        self.assertEqual(kdc.MAX_SPREAD_CENTS, 5)
        self.assertEqual(kdc.DEMO_BASE, config.DEMO_URL_LEGACY)


if __name__ == "__main__":
    unittest.main()
