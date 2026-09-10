# -*- coding: utf-8 -*-
"""Crash semantics, stated and exercised.

The audit asked for failures injected at every step of the durable path and
for a restart after every interruption point. This file is that matrix, and
the contract it pins is:

DOCUMENTED CRASH SEMANTICS
    ledger commit (EquityLedger.save / _commit), in order:
      1. the watermark advances IN MEMORY only;
      2. the evidence is appended to the append-only continuity chain and
         fsynced, and the containing directory is fsynced;
      3. the ledger is written through a FENCED atomic replace: temp file
         written + fsynced, backups rotated, `os.replace`, directory fsync,
         then the `.sha256` sidecar, then a second directory fsync.

    Interruption points and their outcomes:
      before 2   nothing durable changed; the next load recomputes.
      between 2 and 3   the chain is AHEAD of the ledger. Safe: the chain
                 only ever asserts that history existed, so as long as the
                 journal still contains it the next load re-derives the
                 watermark. If the journal does NOT contain it, that is a
                 rollback and it blocks -- which is the correct verdict.
      inside 3, before os.replace   the old ledger is intact.
      after os.replace, before the sidecar   new data, stale checksum.
                 `JsonStore.load` answers from a rotation copy and RECORDS
                 that it did; a ledger recovered from backup that is behind
                 the continuity chain blocks instead of being believed.

    intent commit (OrderManager._record_intent): write, then READ BACK.
    Any failure at either step aborts before broker transport (A09).

    A write refused by the fencing generation is not an error to retry: it
    means another writer owns the state, and this one is stale.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import AstraCase, Client, trade                 # noqa: E402

import equity_ledger as EL                                  # noqa: E402
from config import _p                                       # noqa: E402
from continuity import ChainError, ContinuityChain          # noqa: E402
from equity_ledger import EquityLedger                      # noqa: E402
from order_manager import OrderManager                      # noqa: E402
from persistence import JsonStore, PersistenceSentinel      # noqa: E402
from trade_logger import TradeLogger                        # noqa: E402


class _Boom(OSError):
    pass


class LedgerCommitFailureInjection(AstraCase):

    def seeded(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        return client, tlog, pos, led

    def assert_intact_after_restart(self, expected_hwm=10.0):
        led2, _, _ = self.reload()
        self.assertAlmostEqual(led2.risk_equity_reference(), expected_hwm,
                               places=9)
        return led2

    def test_temp_file_write_failure(self):
        client, tlog, pos, led = self.seeded()
        before = open(_p(EL.LEDGER_FILE), "rb").read()
        real_open = open

        def boom(path, *a, **kw):
            if str(path).endswith(EL.LEDGER_FILE + ".tmp"):
                raise _Boom("temp write failed")
            return real_open(path, *a, **kw)
        with patch("state_authority.tempfile.mkstemp", side_effect=_Boom("temp create failed")):
            self.assertFalse(led.save())
        self.assertEqual(open(_p(EL.LEDGER_FILE), "rb").read(), before)
        self.assert_intact_after_restart()

    def test_fsync_failure(self):
        client, tlog, pos, led = self.seeded()
        before = open(_p(EL.LEDGER_FILE), "rb").read()
        with patch("persistence.os.fsync", side_effect=_Boom("fsync failed")):
            self.assertFalse(led.save())
        self.assertEqual(open(_p(EL.LEDGER_FILE), "rb").read(), before)
        self.assert_intact_after_restart()

    def test_atomic_replace_failure(self):
        client, tlog, pos, led = self.seeded()
        before = open(_p(EL.LEDGER_FILE), "rb").read()
        with patch("persistence.os.replace", side_effect=_Boom("replace failed")):
            self.assertFalse(led.save())
        self.assertEqual(open(_p(EL.LEDGER_FILE), "rb").read(), before)
        self.assertFalse(PersistenceSentinel.healthy())
        self.assert_intact_after_restart()

    def test_checksum_write_failure_leaves_recoverable_state(self):
        """The sidecar is written after the replace. A crash there leaves
        new data with a stale checksum -- the case that used to recover an
        older backup silently."""
        client, tlog, pos, led = self.seeded()
        self.lose(tlog, led)
        real_replace = os.replace
        calls = {"n": 0}

        def replace_then_die(src, dst):
            calls["n"] += 1
            if str(dst).endswith(".sha256"):
                raise _Boom("checksum replace interrupted")
            return real_replace(src, dst)
        with patch("persistence.os.replace", side_effect=replace_then_die):
            led.observe(7.0, cycle_n=50, quiet=True)
        led2, _, _ = self.reload()
        # whatever the load path answered with, no evidenced loss vanished
        self.assertGreaterEqual(led2.drawdown_pct(), 30.0 - 1e-6)
        self.assertIsNotNone(led2.from_backup)
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())
        # A second restart is not a recovery authorization.
        led3, _, _ = self.reload()
        self.assertIsNotNone(led3.from_backup)
        self.assertIn(EL.GUARD_CONTINUITY, led3.guards())
        self.assertFalse(led3.capital_eligible())

    def test_directory_fsync_failure_is_survivable(self):
        client, tlog, pos, led = self.seeded()
        with patch("persistence._fsync_dir", side_effect=_Boom("dir fsync")):
            self.assertFalse(led.save())
        self.assert_intact_after_restart()

    def test_backup_write_failure(self):
        client, tlog, pos, led = self.seeded()
        before = open(_p(EL.LEDGER_FILE), "rb").read()
        with patch("persistence.shutil.copy2", side_effect=_Boom("backup")):
            self.assertFalse(led.save())
        self.assertEqual(open(_p(EL.LEDGER_FILE), "rb").read(), before)
        self.assert_intact_after_restart()

    def test_continuity_append_failure_abandons_the_ledger_write(self):
        """A state whose evidence cannot be recorded must not become
        authoritative."""
        client, tlog, pos, led = self.seeded()
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)   # new evidence
        before = open(_p(EL.LEDGER_FILE), "rb").read()
        with patch.object(ContinuityChain, "append",
                          side_effect=ChainError("chain not durable")):
            self.assertFalse(led.save())
        self.assertEqual(open(_p(EL.LEDGER_FILE), "rb").read(), before)
        self.assertFalse(PersistenceSentinel.healthy())

    def test_a_fenced_write_is_refused_not_retried(self):
        client, tlog, pos, led = self.seeded()
        other = EquityLedger(tlog, pos, env="prod")
        self.assertTrue(other.save())
        self.assertFalse(led.save())
        self.assertEqual(self.ledger_file()["generation"], other.generation)

    def test_journal_commit_failure_keeps_the_ledger_consistent(self):
        client, tlog, pos, led = self.seeded()
        t = trade(tlog)
        with patch.object(JsonStore, "save", return_value=False):
            with self.assertRaises(RuntimeError):
                tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        led2, tlog2, _ = self.reload()
        # the settlement never became durable; nothing claims it did
        self.assertEqual(len(tlog2.settled_trades()), 0)
        self.assertAlmostEqual(led2.strategy_equity(), 10.0, places=6)

    def test_restart_after_every_interruption_point(self):
        """One matrix, one assertion: after any single injected failure the
        ledger reloads and never reports a SMALLER loss than was evidenced."""
        points = [
            ("temp write", lambda: patch("persistence.os.replace",
                                         side_effect=_Boom("replace"))),
            ("fsync", lambda: patch("persistence.os.fsync",
                                    side_effect=_Boom("fsync"))),
            ("backup", lambda: patch("persistence.shutil.copy2",
                                     side_effect=_Boom("backup"))),
            ("chain append", lambda: patch.object(
                ContinuityChain, "append", side_effect=ChainError("chain"))),
            ("save returns false", lambda: patch.object(
                JsonStore, "save", return_value=False)),
        ]
        for label, ctx in points:
            with self.subTest(point=label):
                self.setUp()                    # a fresh DATA_DIR per point
                client, tlog, pos, led = self.seeded()
                self.lose(tlog, led)
                with ctx():
                    led.observe(7.0, cycle_n=99, quiet=True)
                led2, _, _ = self.reload()
                self.assertGreaterEqual(led2.drawdown_pct(), 30.0 - 1e-6,
                                        f"{label} lost the evidenced loss")
                self.assertAlmostEqual(led2.risk_equity_reference(), 10.0,
                                       places=6)


class IntentCommitFailureInjection(AstraCase):
    """Every failure mode of the intent write must abort before transport."""

    def om(self):
        client = Client()
        client.env = "demo"
        client.create_calls = 0

        def create_order(*a, **kw):
            client.create_calls += 1
            raise AssertionError("broker transport reached")
        client.create_order = create_order
        TradeLogger()                   # materialises the journal file
        return client, OrderManager(client)

    def test_every_injected_failure_keeps_transport_at_zero(self):
        cases = [
            ("permission", lambda: patch.object(
                JsonStore, "save", side_effect=PermissionError("ro"))),
            ("disk full", lambda: patch.object(
                JsonStore, "save", side_effect=OSError("ENOSPC"))),
            ("fsync", lambda: patch("persistence.os.fsync",
                                    side_effect=OSError("fsync"))),
            ("atomic replace", lambda: patch("persistence.os.replace",
                                             side_effect=OSError("replace"))),
            ("save false", lambda: patch.object(
                JsonStore, "save", return_value=False)),
        ]
        for label, ctx in cases:
            with self.subTest(case=label):
                self.setUp()
                client, om = self.om()
                with ctx():
                    result = om.place_and_track("KXBTCD-X", "yes", 1, 40)
                self.assertEqual(client.create_calls, 0, label)
                self.assertEqual(result.state, "rejected")

    def test_a_directory_instead_of_the_file(self):
        client, om = self.om()
        os.unlink(_p(OrderManager.PENDING_FILE))
        os.makedirs(_p(OrderManager.PENDING_FILE), exist_ok=True)
        result = om.place_and_track("KXBTCD-X", "yes", 1, 40)
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(result.state, "rejected")

    def test_a_concurrent_writer_does_not_produce_a_phantom_intent(self):
        client, om = self.om()
        om2 = OrderManager(client)
        cid = OrderManager._client_order_id("KXBTCD-X", "yes", 1, 40)
        self.assertTrue(om._record_intent("KXBTCD-X", cid, 1, 40, side="yes"))
        om2.pending_intents.clear()
        self.assertFalse(om2._flush_pending_intents())   # stale writer must lose
        om3 = OrderManager(client)
        self.assertIn("KXBTCD-X", om3.pending_intents)
        self.assertEqual(om3.pending_intents["KXBTCD-X"]["client_order_id"], cid)
        self.assertFalse(PersistenceSentinel.healthy())
        self.assertIn("KXBTCD-X",
                      JsonStore.load(_p(OrderManager.PENDING_FILE), {}))


class ChainAppendFailureInjection(AstraCase):

    def test_an_unwritable_chain_is_an_error_not_a_silent_skip(self):
        chain = self.chain()
        os.makedirs(chain.path, exist_ok=True)          # a directory
        with self.assertRaises(ChainError):
            chain.append("evidence", {"settled_count": 0}, "2026-01-01T00:00:00Z")

    def test_concurrent_appenders_do_not_corrupt_the_chain(self):
        """Two ledger objects appending in turn: the chain stays valid."""
        client, tlog, pos = self.stack()
        led_a = self.reconciled_ledger(tlog, pos)
        led_b = EquityLedger(tlog, pos, env="prod")
        for i in range(5):
            led_a.chain.append("evidence", {"settled_count": i,
                                            "digest": "a" * 64},
                               "2026-01-01T00:00:0%dZ" % i)
            led_b.chain.append("evidence", {"settled_count": i,
                                            "digest": "b" * 64},
                               "2026-01-01T00:01:0%dZ" % i)
        ok, why = self.chain().healthy()
        self.assertTrue(ok, why)
        seqs = [r["seq"] for r in self.chain().records()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()


class RestoreCannotRollContinuityBackward(AstraCase):
    """A01 rule 7 at the entry point: `state_restore` refuses to write
    economic state onto a volume whose continuity chain already evidences
    history the restore does not carry."""

    def test_a_restore_onto_an_evidenced_volume_is_refused(self):
        import state_restore
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        for name in ("kalshi_trades.json", "orders_state.json",
                     "risk_state.json", "positions_state.json",
                     "submission_guard.json", EL.LEDGER_FILE):
            path = _p(name)
            if os.path.exists(path):
                os.remove(path)                  # the wipe a restore follows
        refusal = state_restore._continuity_rollback_refusal()
        self.assertTrue(refusal)
        self.assertIn("rembobiner", refusal)

    def test_a_virgin_volume_is_not_refused(self):
        import state_restore
        self.assertEqual(state_restore._continuity_rollback_refusal(), "")

    def test_an_unreadable_chain_refuses_the_restore(self):
        import state_restore
        with open(self.chain().path, "w") as fh:
            fh.write("not json\nstill not json\n")
        refusal = state_restore._continuity_rollback_refusal()
        self.assertTrue(refusal)
        self.assertIn("illisible", refusal)

    def test_the_new_evidence_files_are_restorable(self):
        import state_restore
        for name in ("equity_ledger.json", "pending_intents.json"):
            self.assertIn(name, state_restore.RESTORE_OPTIONAL_BASENAMES)
