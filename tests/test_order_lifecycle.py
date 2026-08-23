# -*- coding: utf-8 -*-
"""AIR-001 Wave 4 (DE-P0-005) — write-ahead order intent + recovery.

Fault-injection suite for the reproduced defect: nothing durable
existed BEFORE the order POST, so a crash or lost response could leave
a broker order with no local trace, and restart had nothing to
reconcile. Every scenario here is a crash/network fault around the
submission boundary.
"""
import fcntl
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from order_lifecycle import (OrderIntentJournal,  # noqa: E402
                             acquire_order_submission_lock,
                             release_order_submission_lock)
from position_manager import PositionManager  # noqa: E402
from trade_logger import TradeLogger  # noqa: E402

TICKER = "KXTEST-26AUG23-T1"


class FaultClient:
    """Scriptable broker double for submission-boundary faults."""
    env = "demo"
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def __init__(self, *, fail_network=False, orders_at_broker=None,
                 listing_error=False, verify_error=False,
                 assert_journal=None):
        self.last_http_status = 201
        self.fail_network = fail_network
        self.orders_at_broker = orders_at_broker or []
        self.listing_error = listing_error
        self.verify_error = verify_error
        self.assert_journal = assert_journal
        self.create_calls = 0
        self.cancel_calls = 0

    def create_order(self, ticker, side, count, price_cents,
                     client_order_id=None):
        self.create_calls += 1
        if self.assert_journal is not None:
            self.assert_journal(client_order_id)
        if self.fail_network:
            raise bot.KalshiAPIError(0, "reseau: timeout apres retries")
        self.last_http_status = 201
        if self.verify_error:
            # order_id known, but nothing that certifies a fill and the
            # GET will fail: the 'unverified' path.
            return {"order_id": "ord_unv_1",
                    "client_order_id": client_order_id,
                    "status": "resting", "taker_fill_count": 0,
                    "remaining_count": None, "raw": {}}
        return {"order_id": "ord_ok_1", "client_order_id": client_order_id,
                "status": "executed", "taker_fill_count": int(count),
                "remaining_count": 0, "ts_ms": 1787000000000,
                "raw": {"fill_count": f"{int(count)}.00",
                        "remaining_count": "0.00"}}

    def get_order(self, order_id):
        if self.verify_error:
            raise bot.KalshiAPIError(500, f"GET {order_id}", "boom")
        raise bot.KalshiAPIError(404, f"GET {order_id}", "")

    def get_orders(self, ticker=None):
        if self.listing_error:
            raise bot.KalshiAPIError(0, "reseau: listing indisponible")
        return list(self.orders_at_broker)

    def get_fills(self, order_id, *, strict=False):
        return [{"count": 1, "yes_price": 40, "fee_cost": "0.01"}]

    def cancel_order(self, order_id):
        self.cancel_calls += 1
        return {"order_id": order_id, "reduced_by": 0}

    def get_positions(self):
        return []


def fresh_dir(tag):
    tmp = tempfile.mkdtemp(prefix=f"olc_{tag}_")
    bot.CFG.DATA_DIR = tmp
    return tmp


def make_om(client):
    om = bot.OrderManager(client)
    om.open_orders = {}
    return om


class TestWriteAheadIntent(unittest.TestCase):
    def test_intent_fsynced_before_post(self):
        fresh_dir("wal")
        seen = {}

        def check(client_order_id):
            # At POST time the PREPARED record must already be durable.
            j = OrderIntentJournal()
            states = [s for s in j.states().values()
                      if s.get("client_order_id") == client_order_id]
            seen["found"] = bool(states)
            seen["state"] = states[0]["state"] if states else None

        om = make_om(FaultClient(assert_journal=check))
        res = om.place_and_track(TICKER, "yes", 1, 40,
                                 decision_id="dec-1",
                                 execution_intent_hash="h" * 64)
        self.assertTrue(seen["found"])
        self.assertEqual(seen["state"], "PREPARED")
        self.assertEqual(res.state, "filled")
        # Successful placement closes the intent (terminal).
        self.assertEqual(om.intents.unresolved(), [])
        st = list(om.intents.states().values())[0]
        self.assertEqual(st["decision_id"], "dec-1")
        self.assertEqual(st["validated_execution_intent_hash"], "h" * 64)
        # risk_proof_hash honestly absent (Wave 6), never invented
        self.assertIsNone(st["risk_proof_hash"])

    def test_network_ambiguous_never_reposts_and_blocks(self):
        fresh_dir("amb")
        client = FaultClient(fail_network=True)
        om = make_om(client)
        res = om.place_and_track(TICKER, "yes", 1, 40)
        self.assertEqual(res.state, "unknown")
        self.assertEqual(res.status, "ambiguous:network_result_unknown")
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(len(om.intents.unresolved()), 1)
        self.assertEqual(om.intents.unresolved()[0]["state"], "AMBIGUOUS")
        # No further submission is possible — on ANY ticker — until the
        # ambiguity is resolved against the broker.
        res2 = om.place_and_track("KXOTHER-1", "yes", 1, 40)
        self.assertEqual(res2.status, "blocked:unresolved_order_intents")
        self.assertEqual(client.create_calls, 1)   # never re-POSTed

    def test_restart_stays_blocked_then_confirms_not_submitted(self):
        tmp = fresh_dir("rst")
        om = make_om(FaultClient(fail_network=True))
        om.place_and_track(TICKER, "yes", 1, 40)
        # Restart: same data dir, new manager.
        bot.CFG.DATA_DIR = tmp
        om2 = make_om(FaultClient(orders_at_broker=[]))
        self.assertTrue(om2.blocked_reconciling)
        blocked = om2.place_and_track("KXOTHER-1", "yes", 1, 40)
        self.assertEqual(blocked.status,
                         "blocked:unresolved_order_intents")
        om2.reconcile_startup(TradeLogger(), PositionManager(
            om2.client, None))
        # Broker has no order under the client_order_id: confirmed
        # never submitted; trading unblocks.
        self.assertFalse(om2.blocked_reconciling)
        self.assertEqual(om2.intents.unresolved(), [])
        states = om2.intents.states()
        self.assertIn("NOT_SUBMITTED_CONFIRMED",
                      [s.get("outcome") for s in states.values()])

    def test_recovery_adopts_order_found_at_broker(self):
        """Crash after the POST reached the broker but before any local
        record: the intent's client_order_id is found at the broker and
        the order is adopted, resolved, and the fill recorded — never
        re-POSTed, never forgotten."""
        tmp = fresh_dir("adopt")
        journal = OrderIntentJournal()
        journal.prepare(ticker=TICKER, side="yes", count=1,
                        limit_cents=40, client_order_id="alpha_crash1",
                        decision_id="dec-9", engine_commit="c" * 8)
        bot.CFG.DATA_DIR = tmp
        client = FaultClient(orders_at_broker=[
            {"order_id": "ord_found_9", "client_order_id": "alpha_crash1",
             "status": "executed"}])
        om = make_om(client)
        self.assertTrue(om.blocked_reconciling)
        tlog, pm = TradeLogger(), PositionManager(client, None)
        om.reconcile_startup(tlog, pm)
        self.assertFalse(om.blocked_reconciling)
        self.assertEqual(om.intents.unresolved(), [])
        outcomes = [s.get("outcome")
                    for s in om.intents.states().values()]
        self.assertIn("FOUND_AT_BROKER", outcomes)
        self.assertEqual(client.create_calls, 0)    # NEVER re-POSTed
        # The real fill was recorded through the normal recovery path.
        self.assertTrue(tlog.has_open_on(TICKER))

    def test_broker_unreachable_stays_blocked(self):
        tmp = fresh_dir("closed")
        OrderIntentJournal().prepare(
            ticker=TICKER, side="yes", count=1, limit_cents=40,
            client_order_id="alpha_crash2")
        bot.CFG.DATA_DIR = tmp
        om = make_om(FaultClient(listing_error=True))
        om.reconcile_startup(TradeLogger(),
                             PositionManager(om.client, None))
        # Fail-closed: cannot prove either way -> still blocked.
        self.assertTrue(om.blocked_reconciling)
        res = om.place_and_track("KXOTHER-1", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:unresolved_order_intents")

    def test_unverified_order_stays_tracked(self):
        fresh_dir("unv")
        om = make_om(FaultClient(verify_error=True))
        res = om.place_and_track(TICKER, "yes", 1, 40)
        self.assertEqual(res.status, "unverified")
        # Regression: the acknowledged order used to vanish here.
        self.assertIn("ord_unv_1", om.open_orders)
        self.assertEqual(
            om.open_orders["ord_unv_1"]["ticker"], TICKER)
        outcomes = [s.get("outcome")
                    for s in om.intents.states().values()]
        self.assertIn("unverified_tracked_for_recovery", outcomes)

    def test_duplicate_process_is_refused(self):
        tmp = fresh_dir("lock")
        lock_path = os.path.join(tmp, "engine_orders.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # foreign holder
        try:
            om = make_om(FaultClient())
            res = om.place_and_track(TICKER, "yes", 1, 40)
            self.assertEqual(res.status, "blocked:duplicate_process")
            self.assertEqual(om.client.create_calls, 0)
        finally:
            os.close(fd)
        release_order_submission_lock(lock_path)

    def test_acknowledged_intent_closed_on_restart(self):
        """Crash after order_id was recorded: the intent is adopted for
        recovery (tracking belongs to orders_state) and the journal
        resolves without inventing anything."""
        tmp = fresh_dir("ack")
        j = OrderIntentJournal()
        iid = j.prepare(ticker=TICKER, side="yes", count=1,
                        limit_cents=40, client_order_id="alpha_ack")
        j.acknowledged(iid, "ord_ack_7", 201)
        bot.CFG.DATA_DIR = tmp
        om = make_om(FaultClient())
        self.assertTrue(om.blocked_reconciling)
        om.reconcile_startup(TradeLogger(),
                             PositionManager(om.client, None))
        self.assertFalse(om.blocked_reconciling)
        outcomes = [s.get("outcome")
                    for s in om.intents.states().values()]
        self.assertIn("ADOPTED_FOR_RECOVERY", outcomes)

    def test_torn_journal_line_does_not_hide_intents(self):
        tmp = fresh_dir("torn")
        j = OrderIntentJournal()
        j.prepare(ticker=TICKER, side="yes", count=1, limit_cents=40,
                  client_order_id="alpha_torn")
        with open(j.path, "a", encoding="utf-8") as fh:
            fh.write('{"event": "INTENT_PRE')      # crash mid-append
        bot.CFG.DATA_DIR = tmp
        om = make_om(FaultClient())
        # The intact intent is still seen and still blocks.
        self.assertTrue(om.blocked_reconciling)


if __name__ == "__main__":
    unittest.main()
