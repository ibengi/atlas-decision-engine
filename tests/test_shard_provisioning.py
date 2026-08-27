# -*- coding: utf-8 -*-
"""AUD-DEMO-TRADING-IDENTITY-003 (2026-08-27) — sharding des instances.

Contrat Kalshi 2026-08 (documente) : le trading est reparti sur
plusieurs instances d'echange ('exchange sharding') ; les soldes sont
LOCAUX a une instance ; GET /markets expose exchange_index ; le Create
Order route vers l'instance du marche ; GET /portfolio/subaccounts/
balances rend un solde PAR exchange_index. Mesure operateur : les fonds
demo ($136.56) sont ENTIEREMENT sur exchange_index 0 (indexes 1-3 a 0).

Pinne le pre-vol LECTURE SEULE de la sonde :
- marche sur une instance ou le compte n'a AUCUN fonds -> arret PROPRE
  type SHARD_NOT_PROVISIONED, ZERO POST, remede opérateur affiche ;
- fonds presents sur l'instance du marche -> comportement inchange ;
- diagnostic indisponible (pas d'exchange_index sur le marche, endpoint
  absent, erreur) -> comportement historique STRICTEMENT inchange ;
- l'endpoint de diagnostic est un GET signe (jamais d'ecriture) ;
- gardes 30c/5c/1 contrat inchanges.

Aucun reseau, aucun ordre externe.
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import contextlib
import io
from urllib.parse import urlparse

import kalshi_demo_execution_check as kdc
from test_demo_post_forensics import (ForensicBase, _Resp,
                                      _verify_signature)


class TestClientSubaccountsBalancesRead(ForensicBase):
    """Le diagnostic repose sur un GET signe — jamais une ecriture."""

    def test_get_subaccounts_balances_is_signed_get(self):
        cli = self.client(_Resp(200, {"balances": [
            {"subaccount": 0, "exchange_index": 0,
             "balance_dollars": "136.5640"},
            {"subaccount": 0, "exchange_index": 1,
             "balance_dollars": "0.0000"}]}))
        rows = cli.get_subaccounts_balances()
        self.assertEqual(len(rows), 2)
        call = cli.session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(urlparse(call["url"]).path,
                         "/trade-api/v2/portfolio/subaccounts/balances")
        _verify_signature(call, self.pub)


class TestShardPreflight(unittest.TestCase):
    class Cli:
        def __init__(self, rows=None, exc=None):
            self.rows, self.exc = rows, exc
            self.calls = 0

        def get_subaccounts_balances(self):
            self.calls += 1
            if self.exc:
                raise self.exc
            return self.rows

    def _run(self, cli, idx):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            state, funds = kdc.shard_preflight(cli, idx)
        return state, funds, buf.getvalue()

    def test_not_provisioned_on_market_shard(self):
        cli = self.Cli(rows=[
            {"exchange_index": 0, "balance_dollars": "136.5640"},
            {"exchange_index": 1, "balance_dollars": "0"},
            {"exchange_index": 2, "balance_dollars": "0"}])
        state, funds, t = self._run(cli, 1)
        self.assertEqual(state, "NOT_PROVISIONED_ON_MARKET_SHARD")
        self.assertEqual(funds[0], 136.564)
        self.assertIn("state=NOT_PROVISIONED_ON_MARKET_SHARD", t)
        self.assertIn("market_exchange_index=1", t)
        self.assertIn("funds_by_index=0:136.56", t)

    def test_provisioned_proceeds(self):
        cli = self.Cli(rows=[
            {"exchange_index": 0, "balance_dollars": "100.00"},
            {"exchange_index": 1, "balance_dollars": "36.56"}])
        state, funds, t = self._run(cli, 1)
        self.assertEqual(state, "PROVISIONED")

    def test_legacy_cents_field_supported(self):
        cli = self.Cli(rows=[{"exchange_index": 0, "balance": 13656}])
        state, funds, _ = self._run(cli, 0)
        self.assertEqual(state, "PROVISIONED")
        self.assertAlmostEqual(funds[0], 136.56)

    def test_unavailable_paths_fail_open_to_legacy_behavior(self):
        # marche sans exchange_index -> AUCUN appel reseau
        cli = self.Cli(rows=[])
        state, _, _ = self._run(cli, None)
        self.assertEqual(state, "UNAVAILABLE")
        self.assertEqual(cli.calls, 0)
        # endpoint absent/erreur -> UNAVAILABLE, pas de crash
        from kalshi_client import KalshiAPIError
        cli = self.Cli(exc=KalshiAPIError(404, "GET", "not found"))
        state, _, t = self._run(cli, 1)
        self.assertEqual(state, "UNAVAILABLE")
        self.assertIn("state=UNAVAILABLE", t)
        # reponse vide / lignes illisibles -> UNAVAILABLE
        cli = self.Cli(rows=[{"exchange_index": "x"}])
        state, _, _ = self._run(cli, 1)
        self.assertEqual(state, "UNAVAILABLE")


class ShardClient:
    """Client factice complet pour main() : marche eligible sur
    l'instance 1, fonds pilotables par instance."""

    base_url = kdc.DEMO_BASE
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def __init__(self, funds_idx1="0.0000", market_exchange_index=1,
                 balances_exc=None):
        self.create_calls = 0
        self.funds_idx1 = funds_idx1
        self.market_exchange_index = market_exchange_index
        self.balances_exc = balances_exc

    def get_balance(self):
        return 136.56

    def get_markets(self, series, status="open", limit=100):
        if series == kdc.CANDIDATE_SERIES[0]:
            m = {"ticker": "KXBTCD-SHARD", "status": "active",
                 "yes_ask": 3, "yes_bid": 1}
            if self.market_exchange_index is not None:
                m["exchange_index"] = self.market_exchange_index
            return [m]
        return []

    def get_subaccounts_balances(self):
        if self.balances_exc:
            raise self.balances_exc
        return [{"subaccount": 0, "exchange_index": 0,
                 "balance_dollars": "136.5640"},
                {"subaccount": 0, "exchange_index": 1,
                 "balance_dollars": self.funds_idx1}]

    def create_order(self, ticker, side, count, price_cents,
                     client_order_id=None):
        self.create_calls += 1
        self.last_http_status = 201
        return {"order_id": "o1", "client_order_id": client_order_id,
                "status": "executed", "taker_fill_count": 1,
                "remaining_count": 0}

    def get_order(self, oid):
        return {"order_id": oid, "status": "executed",
                "taker_fill_count": 1, "remaining_count": 0}

    def get_fills(self, order_id, **kw):
        return [{"fill_id": "f1", "count": 1, "yes_price": 3,
                 "fees": "0.01"}]

    def cancel_order(self, oid):
        return {"order_id": oid, "reduced_by": 0}

    def get_positions(self):
        return [{"ticker": "KXBTCD-SHARD", "position": 1,
                 "market_exposure": 3, "realized_pnl": 0, "fees_paid": 1}]


class TestMainShardPreflight(unittest.TestCase):
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

    def _run_main(self, client):
        kdc.KalshiClient = lambda env: client
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                kdc.main()
        return cm.exception.code, buf.getvalue()

    def test_unfunded_market_shard_no_post_clean_exit(self):
        cli = ShardClient(funds_idx1="0.0000")
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)                      # arret PROPRE
        self.assertEqual(cli.create_calls, 0)          # ZERO POST
        self.assertIn("[PROBE_ELIGIBLE]", t)
        self.assertIn("exchange_index=1", t)
        self.assertIn("[SHARD_NOT_PROVISIONED]", t)
        self.assertIn("intra_exchange_instance_transfer", t)  # remede
        self.assertNotIn("[ORDER_SUBMIT_ATTEMPT]", t)

    def test_funded_market_shard_proceeds_one_post(self):
        cli = ShardClient(funds_idx1="25.0000")
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)          # comportement normal
        self.assertIn("state=PROVISIONED", t)
        self.assertIn("[ORDER_SUBMIT_ATTEMPT]", t)
        self.assertIn("[DEMO_EXECUTION_PROVED]", t)

    def test_diag_unavailable_preserves_legacy_behavior(self):
        from kalshi_client import KalshiAPIError
        cli = ShardClient(balances_exc=KalshiAPIError(404, "GET", "nf"))
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)          # POST inchange
        self.assertIn("state=UNAVAILABLE", t)

    def test_market_without_exchange_index_preserves_legacy(self):
        cli = ShardClient(market_exchange_index=None)
        code, t = self._run_main(cli)
        self.assertEqual(code, 0)
        self.assertEqual(cli.create_calls, 1)
        self.assertNotIn("[SHARD_NOT_PROVISIONED]", t)

    def test_guards_unchanged(self):
        self.assertEqual(kdc.MAX_ASK_CENTS, 30)
        self.assertEqual(kdc.MAX_SPREAD_CENTS, 5)


if __name__ == "__main__":
    unittest.main()
