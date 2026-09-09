# -*- coding: utf-8 -*-
"""A03 (HIGH) -- a rebase commits only on the state that authorized it.

VIOLATED INVARIANT
    The execution and risk state used to AUTHORIZE a rebase must be the same
    logical state that exists when the rebase COMMITS.

ROOT CAUSE on 508899b
    The authorization hash (`evidence_sha256`) covered three files -- the
    journal, the ledger and the positions -- and nothing re-read them at
    commit. `orders_state.json` and `pending_intents.json` were not bound at
    all, so a second process could add a resting order after validation and
    the token stayed valid. The broker side was two sequential GETs treated
    as a transaction: an order appearing between the order query and the
    position query was invisible to both. And a settlement landing between
    validation and commit was simply absorbed.

ARCHITECTURAL CORRECTION
    `EquityLedger.bound_state()` fingerprints EVERY local file the
    preconditions read, per file, and the ledger's fencing generation covers
    the ledger itself. `equity_rebase_context` brackets the position read
    with two broker order listings and fingerprints the local files on both
    sides of the whole collection, publishing `evidence_unstable` when
    anything moved. `apply_rebase` then re-runs the caller's collection
    (`ctx["revalidate"]`), re-checks every precondition, compares the broker
    order set and re-verifies the local fingerprints IMMEDIATELY before the
    commit, and the write itself is fenced.

    Documented limitation: the broker exposes no atomic "prove I hold
    nothing" primitive, and two GETs never become one. The bracket narrows
    the window to "zero orders observed on both sides of the position read"
    and refuses on any disagreement; it does not claim atomicity. Combined
    with the post-rebase `capital_hold`, which no rebase can clear by
    itself, that is the most conservative protocol available here.

PRODUCTION PATHS
    ExecutionEngine.equity_rebase_context / _apply_equity_operator_actions,
    EquityLedger.bound_state / rebase_preconditions / apply_rebase /
    _commit, JsonStore.save(expect_generation=...).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import AstraCase, Client, trade                # noqa: E402

import execution_engine                                    # noqa: E402
from config import CFG, _p                                 # noqa: E402
from equity_ledger import EquityLedger                     # noqa: E402
from order_manager import OrderManager                     # noqa: E402
from persistence import JsonStore                          # noqa: E402
from position_manager import PositionManager               # noqa: E402
from risk_manager import RiskManager                       # noqa: E402


def order_row(order_id="brk-1", ticker="KXBTCD-A"):
    return {"order_id": order_id, "ticker": ticker, "status": "resting",
            "fill_count": 0, "remaining_count": 1,
            "client_order_id": "cid-" + order_id}


class _RacingClient(Client):
    """A broker double that changes its answer at a chosen call index.

    Deterministic interleaving, not a sleep race: `flip_on_order_call` says
    which `list_orders` call is the first to see the new order, so the test
    pins exactly where in the protocol the world moved.
    """

    def __init__(self, *a, flip_on_order_call=None, appears=None,
                 positions_after=None, flip_positions_on=None, **kw):
        super().__init__(*a, **kw)
        self.flip_on_order_call = flip_on_order_call
        self.appears = appears or []
        self.positions_after = positions_after
        self.flip_positions_on = flip_positions_on
        self.position_calls = 0

    def list_orders(self, **kw):
        self.order_calls += 1
        if self.orders_error:
            raise self.orders_error
        if self.flip_on_order_call is not None \
                and self.order_calls >= self.flip_on_order_call:
            return list(self.orders) + list(self.appears)
        return list(self.orders)

    def _positions_now(self):
        self.position_calls += 1
        if self.flip_positions_on is not None \
                and self.position_calls >= self.flip_positions_on:
            return list(self.positions_after or [])
        return list(self.positions)

    def get_positions(self):
        return self._positions_now()

    def get_positions_proof(self, **_kw):
        rows = self._positions_now()
        return {"rows": rows, "complete": bool(self.positions_complete),
                "pages": 1, "cursors": [],
                "reason": None if self.positions_complete else "truncated"}


class RebaseRaces(AstraCase):

    def build(self, client=None):
        client = client or Client()
        tlog = __import__("trade_logger").TradeLogger()
        pos = PositionManager(client, tlog)
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        orders = OrderManager(client)
        risk = RiskManager(tlog, pos, capital=10.0)
        risk.equity = led
        return client, tlog, pos, led, orders, risk

    def ctx(self, client, orders, pos, risk, led):
        return execution_engine.equity_rebase_context(
            client, orders, pos, risk, equity=led, quiescent=True)

    def attempt(self, led, ctx, action="OPS-40"):
        prop = led.propose_rebase("losses acknowledged", action)
        return led.apply_rebase("losses acknowledged", action,
                                prop["token"], ctx), prop

    def assert_refused(self, led, applied):
        self.assertFalse(applied)
        self.assertAlmostEqual(led.risk_equity_reference(), 10.0, places=9)
        self.assertIsNone(led.state["capital_hold"])
        on_disk = self.ledger_file()
        self.assertAlmostEqual(on_disk["hwm"]["risk_equity_reference"], 10.0,
                               places=9)
        self.assertIsNone(on_disk["capital_hold"])

    # ── 1-4: something appears after the authorizing read ───────────────
    def test_1_an_order_appears_after_validation(self):
        client, tlog, pos, led, orders, risk = self.build(_RacingClient())
        ctx = self.ctx(client, orders, pos, risk, led)
        self.assertEqual(ctx["orders"]["broker_open"], 0)
        client.appears = [order_row()]
        client.flip_on_order_call = client.order_calls + 1
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    def test_2_a_pending_intent_appears_after_validation(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        orders.pending_intents["KXBTCD-A"] = {
            "client_order_id": "cid-x", "count": 1, "price": 40,
            "at": "2026-09-09T00:00:00Z", "resolution": None}
        orders._flush_pending_intents()
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    def test_3_a_position_appears_after_validation(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        pos.positions["t-new"] = {"trade_id": "t-new", "ticker": "KXBTCD-A",
                                  "side": "yes", "count": 1, "avg_price": 40,
                                  "fees": 0.0, "state": "open",
                                  "count_initial": 1, "order_ids": [],
                                  "fill_ids": []}
        pos.flush()
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    def test_4_a_settlement_appears_after_validation(self):
        """Astra RACE_settlement_between_validation_and_commit."""
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        t = trade(tlog, ticker="KXBTC15M-LATE")
        tlog.settle_trade(t["trade_id"], "no", False, -1.0, -1.0)
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    # ── 5-6: another engine writes local state ──────────────────────────
    def test_5_another_engine_writes_orders_state(self):
        """Astra RACE_second_process_adds_order_after_validation. The second
        writer touches only `orders_state.json`, which the old evidence hash
        did not cover at all."""
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        JsonStore.save(_p(CFG.ORDERS_FILE),
                       {"brk-2": order_row("brk-2")})       # other process
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    def test_6_another_engine_writes_ledger_state(self):
        """A concurrent ledger writer advances the fencing generation; this
        instance's commit is refused rather than clobbering it."""
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        other = EquityLedger(tlog, pos, env="prod")
        other.state["daily"]["date"] = "2000-01-01"
        self.assertTrue(other.save())
        self.assertGreater(other.generation, led.generation)
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    # ── 7-8: the broker moves between the two queries ───────────────────
    def test_7_broker_order_state_changes_between_queries(self):
        """Astra RACE_order_between_broker_order_and_position_queries: the
        order appears AFTER the order listing and is invisible to the
        position query. The second bracketing listing sees it."""
        client = _RacingClient(appears=[order_row()], flip_on_order_call=2)
        client, tlog, pos, led, orders, risk = self.build(client)
        ctx = self.ctx(client, orders, pos, risk, led)
        self.assertTrue(ctx["evidence_unstable"], ctx)
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    def test_8_broker_position_state_changes_between_queries(self):
        client = _RacingClient(positions=[], flip_positions_on=2,
                               positions_after=[{"ticker": "KXBTCD-A",
                                                 "position": 2}])
        client, tlog, pos, led, orders, risk = self.build(client)
        ctx = self.ctx(client, orders, pos, risk, led)
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    # ── 9-10: the authorization itself moves ────────────────────────────
    def test_9_rebase_token_state_changes(self):
        """The token is single-use across the chain, so a second attempt
        with the same token after a successful one is refused even though
        every other precondition is satisfied again."""
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        applied, prop = self.attempt(led, ctx)
        self.assertTrue(applied)
        led2, tlog2, pos2 = self.reload()
        self.assertTrue(led2.token_consumed(prop["token"]))
        ctx2 = self.ctx(client, orders, pos2, risk, led2)
        self.assertFalse(led2.apply_rebase("losses acknowledged", "OPS-40",
                                           prop["token"], ctx2))

    def test_10_persistence_generation_changes_before_commit(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        led.generation -= 1                       # this writer is now stale
        applied, _ = self.attempt(led, ctx)
        self.assertFalse(applied)
        self.assertAlmostEqual(self.ledger_file()["hwm"]["risk_equity_reference"],
                               10.0, places=9)

    # ── contract shape ──────────────────────────────────────────────────
    def test_a_context_without_a_stability_proof_is_not_an_authorization(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        for missing in ("bound_state", "quiescent"):
            with self.subTest(missing=missing):
                stripped = {k: v for k, v in ctx.items() if k != missing}
                applied, _ = self.attempt(led, stripped, action="OPS-4" + missing[0])
                self.assert_refused(led, applied)

    def test_a_non_quiescent_engine_cannot_rebase(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = execution_engine.equity_rebase_context(
            client, orders, pos, risk, equity=led, quiescent=False)
        applied, _ = self.attempt(led, ctx)
        self.assert_refused(led, applied)

    def test_positive_control_a_still_state_commits(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        self.assertIsNone(ctx["evidence_unstable"])
        applied, _ = self.attempt(led, ctx)
        self.assertTrue(applied, led.snapshot())
        self.assertAlmostEqual(led.risk_equity_reference(), 7.0, places=9)
        self.assertEqual(self.ledger_file()["capital_hold"]["reason"],
                         "post_rebase_validation")
        self.assertFalse(led.capital_eligible())

    def test_the_result_survives_a_restart(self):
        client, tlog, pos, led, orders, risk = self.build()
        ctx = self.ctx(client, orders, pos, risk, led)
        applied, prop = self.attempt(led, ctx)
        self.assertTrue(applied)
        led2, _, _ = self.reload()
        self.assertAlmostEqual(led2.risk_equity_reference(), 7.0, places=9)
        self.assertEqual(led2.state["capital_hold"]["reason"],
                         "post_rebase_validation")
        self.assertTrue(led2.token_consumed(prop["token"]))


if __name__ == "__main__":
    unittest.main()
