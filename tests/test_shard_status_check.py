# -*- coding: utf-8 -*-
"""AUD-DEMO-TRADING-IDENTITY-003 (complement) — diagnostic sharding
LECTURE SEULE (kalshi_shard_status_check.py).

Pinne : GET uniquement (methode verifiee sur CHAQUE appel transport),
statuts et soldes par exchange_index imprimes, erreurs par appel
absorbees (le diagnostic continue), gardes LIVE/URL, exit 0, aucun
secret. Aucun reseau reel.
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import contextlib
import io
import json

import kalshi_shard_status_check as ksc
from kalshi_client import KalshiAPIError


class FakeReadOnlyClient:
    """Client factice : _req enregistre (method, path, params) et sert
    des reponses scriptees. Toute methode d'ecriture est ABSENTE —
    un POST leverait AttributeError immediatement."""

    base_url = "https://demo-api.kalshi.co/trade-api/v2"

    def __init__(self, responses=None, fail_paths=()):
        self.calls = []
        self.responses = responses or {}
        self.fail_paths = set(fail_paths)

    def _req(self, method, path, params=None, **kw):
        self.calls.append((method, path, dict(params or {})))
        if path in self.fail_paths:
            raise KalshiAPIError(404, f"{method} {path}", "not found")
        key = (path, (params or {}).get("exchange_index"))
        return self.responses.get(key, self.responses.get(path, {}))

    def get_subaccounts_balances(self):
        self.calls.append(("GET", "/portfolio/subaccounts/balances", {}))
        if "/portfolio/subaccounts/balances" in self.fail_paths:
            raise KalshiAPIError(404, "GET", "not found")
        return self.responses.get("subaccounts", [])


def run_checks(cli, indexes=(0, 2)):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for idx in indexes:
            ksc.check_exchange_status(cli, idx)
            ksc.check_balance_for_index(cli, idx)
        ksc.check_subaccounts_table(cli)
    return buf.getvalue()


class TestShardStatusCheck(unittest.TestCase):
    def _client(self):
        return FakeReadOnlyClient(responses={
            ("/exchange/status", 0): {
                "exchange_active": True, "trading_active": True,
                "intra_exchange_transfers_active": True},
            ("/exchange/status", 2): {
                "exchange_active": True, "trading_active": True,
                "intra_exchange_transfers_active": False},
            ("/portfolio/balance", 0): {"balance": 13656},
            ("/portfolio/balance", 2): {"balance_dollars": "0.0000"},
            "subaccounts": [
                {"subaccount": 0, "exchange_index": 0,
                 "balance_dollars": "136.5640"},
                {"subaccount": 0, "exchange_index": 2,
                 "balance_dollars": "0.0000"}]})

    def test_expected_evidence_lines(self):
        cli = self._client()
        t = run_checks(cli)
        self.assertIn("[SHARD_STATUS] index=0 exchange_active=True "
                      "trading_active=True transfers_active=True", t)
        self.assertIn("[SHARD_STATUS] index=2 exchange_active=True "
                      "trading_active=True transfers_active=False", t)
        self.assertIn("[SHARD_BALANCE] index=0 balance=136.56", t)
        self.assertIn("[SHARD_BALANCE] index=2 balance=0.00", t)
        self.assertIn("[SHARD_TABLE] subaccount=0 exchange_index=0 "
                      "balance=136.56", t)

    def test_every_transport_call_is_get(self):
        cli = self._client()
        run_checks(cli)
        self.assertTrue(cli.calls)
        for method, path, params in cli.calls:
            self.assertEqual(method, "GET", (method, path))
        # les deux endpoints par index sont interroges avec le parametre
        self.assertIn(("GET", "/exchange/status", {"exchange_index": 0}),
                      cli.calls)
        self.assertIn(("GET", "/portfolio/balance", {"exchange_index": 2}),
                      cli.calls)

    def test_per_call_errors_do_not_stop_diagnostic(self):
        cli = FakeReadOnlyClient(
            responses={("/portfolio/balance", 0): {"balance": 13656}},
            fail_paths={"/exchange/status",
                        "/portfolio/subaccounts/balances"})
        t = run_checks(cli, indexes=(0,))
        self.assertIn("[SHARD_STATUS] index=0 state=UNAVAILABLE", t)
        self.assertIn("[SHARD_BALANCE] index=0 balance=136.56", t)
        self.assertIn("[SHARD_TABLE] state=UNAVAILABLE", t)

    def test_indexes_env_override_and_default(self):
        self.assertEqual(ksc._indexes(), (0, 2))
        os.environ["SHARD_STATUS_INDEXES"] = "0,1,2,3"
        try:
            self.assertEqual(ksc._indexes(), (0, 1, 2, 3))
        finally:
            os.environ.pop("SHARD_STATUS_INDEXES", None)

    def test_live_context_refused(self):
        os.environ["LIVE_TRADING"] = "1"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    ksc.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("LIVE", buf.getvalue())
        finally:
            os.environ.pop("LIVE_TRADING", None)

    def test_script_has_no_write_verbs(self):
        src = open(ksc.__file__, encoding="utf-8").read()
        for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"',
                     "create_order", "cancel_order",
                     "intra_exchange_instance_transfer("):
            self.assertNotIn(verb, src, verb)

    def test_no_secret_material_in_output(self):
        cli = self._client()
        t = run_checks(cli)
        self.assertNotIn("PRIVATE KEY", t)
        self.assertNotIn("SIGNATURE", t.upper().replace(
            "SANITIZED", ""))       # aucun header d'auth


if __name__ == "__main__":
    unittest.main()
