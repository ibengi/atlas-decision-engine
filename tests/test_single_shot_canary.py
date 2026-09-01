# -*- coding: utf-8 -*-
"""Single-shot canary semantics, proven on DEMO-shaped state only.

The canary envelope (docs/live-canary-design.md) promises ONE logical
order and no duplicate under network ambiguity. These tests pin that
promise against the real OrderManager, with a mock broker: no network,
no DEMO order, no LIVE order, zero real broker writes.

The property under test, stated precisely:

    A canary submission reserves a PERSISTED lock on the ticker BEFORE
    the POST leaves the process, and that lock is never released by a
    failure. Therefore, whatever the broker does or fails to say --
    HTTP 201, timeout, connection reset, unreadable body, failed
    read-back, or a restart mid-flight -- `create_order` is called AT
    MOST ONCE for that ticker, and never a second time with a different
    client_order_id.

The ordering matters and is the whole point: writing the lock AFTER the
POST returned left the ticker free whenever the POST timed out, and the
next cycle re-priced the order (new client_order_id), so the broker's
idempotency on client_order_id protected nothing. That is the shape of
the 2026-07-25 duplicate.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from config import CFG  # noqa: E402
from persistence import JsonStore, PersistenceSentinel, _p  # noqa: E402

TICKER = "KXBTCD-CANARY-T1"
SIDE, COUNT, PRICE = "yes", 1, 40

#: Every way a POST can leave the engine unable to know what happened.
#: status 0 is what KalshiClient._req raises for a timeout or a dropped
#: connection -- the request left, the answer never came back.
AMBIGUOUS_FAILURES = [
    ("timeout reseau", bot.KalshiAPIError(0, "reseau: ReadTimeout")),
    ("connexion coupee", bot.KalshiAPIError(0, "reseau: ConnectionError")),
    ("502 passerelle", bot.KalshiAPIError(502, "bad gateway")),
    ("503 maintenance", bot.KalshiAPIError(503, "service unavailable")),
    ("500 interne", bot.KalshiAPIError(500, "internal error")),
    ("429 debit", bot.KalshiAPIError(429, "rate limited")),
    ("400 corps illisible", bot.KalshiAPIError(400, "unparseable body")),
]


class _CanaryBase(unittest.TestCase):
    """Canary configuration: LIVE-capable persistence, cap of exactly 1,
    submissions enabled ONLY inside these in-process tests (the deployed
    service keeps ALLOW_ORDER_SUBMISSION=false; nothing here touches it)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_canary_")
        self._tmps = getattr(self, "_tmps", [])
        self._tmps.append(self.tmp)
        # Certains tests rappellent setUp() pour isoler un sous-cas: la
        # sauvegarde de CFG ne doit etre prise QU'UNE FOIS, sinon on
        # restaurerait des valeurs deja modifiees dans le CFG global et on
        # polluerait toute la suite (bug observe: kelly/pipeline en echec
        # uniquement quand ce fichier tourne avant eux).
        self._saved = getattr(self, "_saved", None) or {
            k: getattr(CFG, k) for k in (
            "DATA_DIR", "REQUIRE_PERSISTENT_STATE", "ALLOW_ORDER_SUBMISSION",
            "MAX_CONTRACTS_PER_ORDER", "ALLOW_FRESH_STATE",
            "SUBMIT_DEDUP_TTL_S", "ORDER_TTL_SECONDS")}
        CFG.DATA_DIR = self.tmp
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"      # canary envelope
        CFG.SUBMIT_DEDUP_TTL_S = 6 * 3600.0
        CFG.ORDER_TTL_SECONDS = 1              # keep fill-wait loops short
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(CFG, k, v)
        for d in getattr(self, "_tmps", [self.tmp]):
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _client(order_id="ord-canary-1"):
        c = MagicMock()
        c.env = "demo"
        c.last_http_status = 201
        c.create_order.return_value = {
            "order_id": order_id, "status": "executed",
            "fill_count": 1, "remaining_count": 0, "ts_ms": 1}
        c.get_order.return_value = {
            "order_id": order_id, "status": "executed",
            "fill_count": 1, "remaining_count": 0}
        c.get_fills.return_value = [{"fill_id": "f1", "count": 1,
                                     "price": PRICE, "yes_price": PRICE,
                                     "is_taker": True}]
        c.get_positions.return_value = [{"ticker": TICKER, "position": 1}]
        return c

    def _om(self, client):
        """A fresh OrderManager == a fresh process: it reloads the guard
        from disk exactly as a restarted container would."""
        return bot.OrderManager(client)

    def _submit(self, om, price=PRICE, count=COUNT):
        return om.place_and_track(TICKER, SIDE, count, price)

    def _guard_on_disk(self) -> dict:
        return JsonStore.load(_p("submission_guard.json"), {})


class DeterministicIdentityTest(_CanaryBase):
    """1. One deterministic, unique client_order_id."""

    def test_id_is_deterministic_and_distinct_per_order_shape(self):
        cid = bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE)
        self.assertEqual(
            cid, bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE),
            "the same order must always produce the same id")
        self.assertTrue(cid.startswith("alpha_"))
        others = {
            bot.OrderManager._client_order_id("KXOTHER", SIDE, COUNT, PRICE),
            bot.OrderManager._client_order_id(TICKER, "no", COUNT, PRICE),
            bot.OrderManager._client_order_id(TICKER, SIDE, 2, PRICE),
            bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE + 1),
        }
        self.assertNotIn(cid, others, "distinct orders share an id")
        self.assertEqual(len(others), 4, "id collision between order shapes")

    def test_the_submitted_id_is_that_deterministic_id(self):
        client = self._client()
        self._submit(self._om(client))
        sent = client.create_order.call_args.kwargs["client_order_id"]
        self.assertEqual(
            sent, bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE))


class GuardPersistedBeforeBrokerCallTest(_CanaryBase):
    """2. The lock is on disk before the POST leaves the process."""

    def test_guard_is_on_disk_at_the_moment_create_order_is_called(self):
        client = self._client()
        seen = {}

        def _capture(*a, **kw):
            # Read the FILE, not memory: this is what a restart would see.
            seen["disk"] = JsonStore.load(_p("submission_guard.json"), {})
            return {"order_id": "ord-canary-1", "status": "executed",
                    "fill_count": 1, "remaining_count": 0, "ts_ms": 1}

        client.create_order.side_effect = _capture
        self._submit(self._om(client))

        self.assertIn(TICKER, seen.get("disk", {}),
                      "the POST left the process before the lock was durable")

    def test_unwritable_guard_blocks_the_broker_call(self):
        """8. Guard cannot be persisted -> zero broker writes."""
        client = self._client()
        om = self._om(client)
        om._flush_submission_guard = MagicMock(return_value=False)

        res = self._submit(om)

        self.assertEqual(res.status, "blocked:submission_guard_unwritable")
        self.assertEqual(res.state, "rejected")
        self.assertEqual(client.create_order.call_count, 0)
        self.assertEqual(client.cancel_order.call_count, 0)

    def test_corrupted_guard_file_never_unlocks_a_second_order(self):
        """8. A corrupted guard file must not read as 'nothing submitted'
        in a way that lets a second order through: the engine either keeps
        the lock or refuses, but never submits twice."""
        client = self._client()
        self._submit(self._om(client))
        self.assertEqual(client.create_order.call_count, 1)

        with open(_p("submission_guard.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not json")

        client2 = self._client(order_id="ord-canary-2")
        res = self._submit(self._om(client2))

        self.assertEqual(client2.create_order.call_count, 0,
                         "a corrupted guard let a duplicate order through")
        self.assertEqual(res.state, "rejected")


class HttpCreatedIsAuthoritativeTest(_CanaryBase):
    """3. HTTP 201 means created; 5. read-back is attempted."""

    def test_201_is_treated_as_created_and_read_back(self):
        client = self._client()
        res = self._submit(self._om(client))

        self.assertEqual(client.create_order.call_count, 1)
        self.assertEqual(res.order_id, "ord-canary-1")
        self.assertEqual(res.state, "filled")
        client.get_order.assert_called()          # read-back attempted
        self.assertEqual(client.get_order.call_args[0][0], "ord-canary-1")

    def test_failed_read_back_never_triggers_a_repost(self):
        """4. Read-back fails -> the order stands as submitted; the engine
        does NOT create a second one, now or on the next cycle."""
        client = self._client()
        client.get_order.side_effect = bot.KalshiAPIError(500, "read-back down")
        client.get_fills.side_effect = bot.KalshiAPIError(500, "fills down")

        res = self._submit(self._om(client))
        self.assertEqual(client.create_order.call_count, 1)
        self.assertEqual(res.state, "rejected")
        self.assertEqual(res.status, "unverified")

        # next cycle, same ticker, re-priced (=> a DIFFERENT id if it ran)
        client2 = self._client(order_id="ord-canary-2")
        res2 = self._submit(self._om(client2), price=PRICE + 7)

        self.assertEqual(client2.create_order.call_count, 0,
                         "a failed read-back caused a second order")
        self.assertEqual(res2.status, "blocked:duplicate_submission_guard")


class NoRepostOnAmbiguousPostTest(_CanaryBase):
    """4 + 6. Every ambiguous POST outcome locks the ticker."""

    def test_ambiguous_post_keeps_the_lock_and_forbids_a_new_id(self):
        for label, exc in AMBIGUOUS_FAILURES:
            with self.subTest(case=label):
                self.setUp()                       # isolated state per case
                client = self._client()
                client.create_order.side_effect = exc

                res = self._submit(self._om(client))

                self.assertEqual(client.create_order.call_count, 1)
                self.assertEqual(res.state, "rejected")
                self.assertIn(TICKER, self._guard_on_disk(),
                              f"{label}: ticker left unlocked after an "
                              f"ambiguous POST")

                # the retry the engine would naturally make next cycle,
                # at a moved price => a different client_order_id
                client2 = self._client(order_id="ord-canary-2")
                res2 = self._submit(self._om(client2), price=PRICE + 11)

                self.assertEqual(client2.create_order.call_count, 0,
                                 f"{label}: repost with a NEW id")
                self.assertEqual(res2.status,
                                 "blocked:duplicate_submission_guard")
                self._cleanup()

    def test_ambiguous_status_is_reported_not_silently_rejected(self):
        client = self._client()
        client.create_order.side_effect = bot.KalshiAPIError(0, "reseau: timeout")

        with self.assertLogs("API", level="ERROR") as logs:
            res = self._submit(self._om(client))

        self.assertTrue(res.status.startswith("ambiguous:"),
                        f"ambiguity reported as {res.status!r}")
        blob = "\n".join(logs.output)
        self.assertIn("ORDER_SUBMIT_AMBIGUOUS", blob)
        self.assertIn("Verrou anti-doublon MAINTENU", blob)


class UnresolvedFirstOrderBlocksSecondTest(_CanaryBase):
    """6. While the first order is unknown/pending/open, a second is refused."""

    UNRESOLVED = [
        ("resting", {"order_id": "ord-canary-1", "status": "resting",
                     "fill_count": 0, "remaining_count": 1, "ts_ms": 1}),
        ("pending", {"order_id": "ord-canary-1", "status": "pending",
                     "fill_count": 0, "remaining_count": 1, "ts_ms": 1}),
        ("statut inconnu", {"order_id": "ord-canary-1", "status": "",
                            "fill_count": 0, "remaining_count": 1,
                            "ts_ms": 1}),
        ("partiellement rempli", {"order_id": "ord-canary-1",
                                  "status": "resting", "fill_count": 0,
                                  "remaining_count": 1, "ts_ms": 1}),
    ]

    def test_second_order_refused_while_first_is_unresolved(self):
        for label, response in self.UNRESOLVED:
            with self.subTest(case=label):
                self.setUp()
                client = self._client()
                client.create_order.return_value = response
                client.get_order.return_value = response
                client.get_fills.return_value = []
                # The order rests to TTL, so the engine cancels it -- the
                # designed lifecycle. Give the cancel a realistic reply so
                # the real path runs instead of a mock artefact.
                client.cancel_order.return_value = {
                    "order_id": "ord-canary-1", "status": "canceled",
                    "reduced_by": 1}

                self._submit(self._om(client))
                self.assertEqual(client.create_order.call_count, 1)

                client2 = self._client(order_id="ord-canary-2")
                res2 = self._submit(self._om(client2), price=PRICE + 3)

                self.assertEqual(client2.create_order.call_count, 0,
                                 f"{label}: second order while first "
                                 f"unresolved")
                self.assertEqual(res2.status,
                                 "blocked:duplicate_submission_guard")
                self._cleanup()


class RestartDuplicateGuardTest(_CanaryBase):
    """7. The lock survives a process restart."""

    def test_restart_after_success_blocks_a_second_order(self):
        client = self._client()
        self._submit(self._om(client))
        self.assertEqual(client.create_order.call_count, 1)

        # process dies; a brand-new OrderManager reloads from the volume
        client2 = self._client(order_id="ord-canary-2")
        om2 = self._om(client2)
        self.assertIn(TICKER, om2.session_submitted, "guard not reloaded")
        res2 = self._submit(om2, price=PRICE + 5)

        self.assertEqual(client2.create_order.call_count, 0)
        self.assertEqual(res2.status, "blocked:duplicate_submission_guard")

    def test_restart_after_an_ambiguous_post_blocks_a_second_order(self):
        """The worst case: the POST may have landed, the answer never came,
        and the container restarted before anything else was written."""
        client = self._client()
        client.create_order.side_effect = bot.KalshiAPIError(0, "reseau: timeout")
        self._submit(self._om(client))

        client2 = self._client(order_id="ord-canary-2")
        om2 = self._om(client2)
        self.assertIn(TICKER, om2.session_submitted)
        res2 = self._submit(om2, price=PRICE + 9)

        self.assertEqual(client2.create_order.call_count, 0,
                         "restart after an ambiguous POST allowed a duplicate")
        self.assertEqual(res2.status, "blocked:duplicate_submission_guard")

    def test_guard_survives_many_restarts_within_the_ttl(self):
        client = self._client()
        self._submit(self._om(client))
        for i in range(5):
            c = self._client(order_id=f"ord-restart-{i}")
            self.assertEqual(
                self._submit(self._om(c), price=PRICE + i).status,
                "blocked:duplicate_submission_guard")
            self.assertEqual(c.create_order.call_count, 0)


class ContractCapBehaviourTest(_CanaryBase):
    """9. MAX_CONTRACTS_PER_ORDER=1 is enforced behaviourally."""

    def test_one_contract_passes_and_two_are_blocked_without_a_call(self):
        client = self._client()
        res = self._submit(self._om(client), count=1)
        self.assertEqual(res.state, "filled")
        self.assertEqual(client.create_order.call_args[0][2], 1,
                         "count sent to the broker is not 1")

        self.setUp()
        client2 = self._client()
        res2 = self._submit(self._om(client2), count=2)
        self.assertEqual(res2.status, "blocked:contract_cap_exceeded")
        self.assertEqual(client2.create_order.call_count, 0,
                         "an over-cap order reached the broker")


class TotalSubmissionCountTest(_CanaryBase):
    """10. Across a whole canary lifecycle: create_order call count <= 1."""

    def test_a_full_canary_run_never_exceeds_one_create_order(self):
        """Ten cycles of the same signal, each a fresh process, through
        every outcome: at most ONE order ever reaches the broker."""
        clients = []
        first = self._client()
        clients.append(first)
        self._submit(self._om(first))

        for i in range(9):
            c = self._client(order_id=f"ord-cycle-{i}")
            # the book moves every cycle: prices, hence ids, keep changing
            self._submit(self._om(c), price=PRICE + i + 1)
            clients.append(c)

        total = sum(c.create_order.call_count for c in clients)
        self.assertEqual(total, 1, f"{total} orders submitted, expected 1")
        self.assertEqual(sum(c.cancel_order.call_count for c in clients), 0)

        ids = [c.create_order.call_args.kwargs["client_order_id"]
               for c in clients if c.create_order.call_count]
        self.assertEqual(len(set(ids)), 1, "more than one logical order")

    def test_ambiguous_then_nine_cycles_still_at_most_one_call(self):
        first = self._client()
        first.create_order.side_effect = bot.KalshiAPIError(0, "reseau: timeout")
        self._submit(self._om(first))
        self.assertEqual(first.create_order.call_count, 1)

        others = []
        for i in range(9):
            c = self._client(order_id=f"ord-after-ambig-{i}")
            self._submit(self._om(c), price=PRICE + i + 1)
            others.append(c)

        self.assertEqual(sum(c.create_order.call_count for c in others), 0,
                         "an ambiguous POST was followed by another order")


if __name__ == "__main__":
    unittest.main()
