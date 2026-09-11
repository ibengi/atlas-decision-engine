from authority_fixtures import corrupt_json
from authority_fixtures import freeze_for
# -*- coding: utf-8 -*-
"""A01 (CRITICAL) -- monotonic loss evidence cannot be rewound.

VIOLATED INVARIANT
    No economic loss and no consumed authorization that has been durably
    observed may silently disappear after a restore, a crash, a backup
    recovery, a restart or a concurrent write.

ROOT CAUSE on 508899b
    The only record of "how much history has been durably observed" was the
    `journal_watermark` field INSIDE equity_ledger.json -- exactly as
    rewindable as the state it protected. Restoring the ledger restored the
    watermark; deleting the field read as "no history"; a zero counter
    disabled the check; a stale second writer could overwrite it; a backup
    recovered after a checksum crash brought back an older one; and a
    consumed rebase token vanished with the snapshot that recorded it. The
    `.sha256` sidecar was no help: it is written beside the same bytes and
    rewinds with them.

ARCHITECTURAL CORRECTION
    `continuity.py`: an append-only, hash-chained, strictly validated
    evidence log in a SEPARATE file that the JsonStore backup/restore
    machinery does not touch. It is a FLOOR -- the journal, the ledger and
    the consumed-token set must all be at or above it. Anything below is a
    rollback, which opens a blocking recovery state (`continuity_block`,
    `GUARD_CONTINUITY`) that clears only when the evidenced history is
    present again, verified against that independent chain. Plus a fencing
    generation on the ledger so a stale writer cannot clobber newer state.

PRODUCTION PATHS
    EquityLedger._load / _check_continuity / save / _commit /
    _record_consumed_token / token_consumed / strategy_equity_conservative /
    guards, JsonStore.save(expect_generation=...), ExecutionEngine
    ._evaluate_global_guards.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import PRE_AT, AstraCase, trade  # noqa: E402

import equity_ledger as EL                            # noqa: E402
import execution_engine                               # noqa: E402
from config import CFG, _p                            # noqa: E402
from continuity import ChainError                     # noqa: E402
from equity_ledger import EquityLedger                # noqa: E402
from persistence import JsonStore                     # noqa: E402


class MissingEvidenceIsNeverNoHistory(AstraCase):
    """Rules 1-3: a missing, zeroed or malformed watermark on state the
    chain proves was reconciled must fail closed."""

    def loss_then(self, mutate):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        self.assertAlmostEqual(led.drawdown_pct(), 30.0, places=6)
        mutate()
        led2, _, _ = self.reload()
        return led2

    def test_watermark_deleted_with_a_rewound_journal(self):
        """Astra J_watermark_delete: the field is gone, so the ledger's own
        check is disabled -- but the chain still holds the evidence."""
        def mutate():
            state = self.ledger_file()
            state.pop("journal_watermark", None)
            JsonStore.save(_p(EL.LEDGER_FILE), state)
            corrupt_json(_p(CFG.TRADES_FILE), [])       # pre-loss journal
        led2 = self.loss_then(mutate)
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assert_loss_preserved(led2)

    def test_watermark_zeroed_with_a_rewound_journal(self):
        """Astra J_watermark_zero: `settled_count: 0` used to read as "no
        history evidenced yet", which is the same sentence as "nothing bad
        ever happened"."""
        def mutate():
            state = self.ledger_file()
            state["journal_watermark"]["settled_count"] = 0
            JsonStore.save(_p(EL.LEDGER_FILE), state)
            corrupt_json(_p(CFG.TRADES_FILE), [])
        led2 = self.loss_then(mutate)
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assert_loss_preserved(led2)

    def test_a_malformed_watermark_is_rejected_not_coerced(self):
        """Rule 3: strict schema. A digest that is not a sha256 refuses the
        whole persisted ledger rather than being read past."""
        def mutate():
            state = self.ledger_file()
            state["journal_watermark"]["digest"] = "not-a-digest"
            JsonStore.save(_p(EL.LEDGER_FILE), state)
        led2 = self.loss_then(mutate)
        self.assertIsNotNone(led2.schema_reject)
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())

    def test_a_non_integer_generation_is_rejected(self):
        def mutate():
            state = self.ledger_file()
            state["generation"] = "seven"
            JsonStore.save(_p(EL.LEDGER_FILE), state)
        led2 = self.loss_then(mutate)
        self.assertIsNotNone(led2.schema_reject)
        self.assertFalse(led2.capital_eligible())

    def test_positive_control_untouched_state_stays_reconciled(self):
        """Without this every refusal above could be a broken fixture."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        led.observe(10.0, cycle_n=1, quiet=True)
        led2, _, _ = self.reload()
        self.assertEqual(led2.derive_status(), EL.STATUS_RECONCILED)
        self.assertNotIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertTrue(led2.capital_eligible(), led2.snapshot())


class CoordinatedRestoreIsDetected(AstraCase):
    """Rules 7-8: restoring journal AND ledger together used to bring back a
    world where the loss never happened."""

    def test_restored_pair_cannot_erase_an_observed_loss(self):
        """Astra J_restore_both_journal_and_ledger."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        snap = self.snapshot_dir()                       # pre-loss, coherent
        self.lose(tlog, led)
        self.assertAlmostEqual(led.drawdown_pct(), 30.0, places=6)
        # restore everything EXCEPT the append-only chain, which the
        # JsonStore rotation does not produce and a file-level restore of
        # the state files does not carry
        self.restore_dir(snap, only=[n for n in snap if "continuity" not in n])
        led2, _, _ = self.reload()
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assert_loss_preserved(led2)

    def test_restored_pair_plus_deposit_does_not_pass_the_global_gates(self):
        """Astra J_restored_pair_plus_deposit_passes_global_guards: the
        balance reconciled at 10 again, drawdown read 0%, and
        `_evaluate_global_guards()` returned (True, None)."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        snap = self.snapshot_dir()
        self.lose(tlog, led)
        self.restore_dir(snap, only=[n for n in snap if "continuity" not in n])
        led2, tlog2, pos2 = self.reload()
        for cycle in range(20, 26):                       # +3 deposit observed
            led2.observe(10.0, cycle_n=cycle, quiet=True)
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())
        self.assertGreaterEqual(led2.drawdown_pct(), 30.0 - 1e-6)
        ok, guard = self.global_guards(client, tlog2, pos2, led2)
        self.assertFalse(ok, "restored history + deposit passed the gates")
        self.assertTrue(guard, "a refusal must NAME its guard")
        # whichever loss-derived gate fires first, the accounting layer is
        # independently refusing: that is the one Astra walked through
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())

    def global_guards(self, client, tlog, pos, led):
        """The real `ExecutionEngine._evaluate_global_guards`, borrowed onto
        a minimal holder exactly as the engine calls it."""
        from risk_manager import RiskManager

        class _Eng:
            pass
        eng = _Eng()
        eng.client = client
        eng.posmgr = pos
        eng.orders = None
        eng.equity = led
        eng.risk = RiskManager(tlog, pos, capital=10.0)
        eng.risk.equity = led
        eng._evaluate_global_guards = \
            execution_engine.ExecutionEngine._evaluate_global_guards.__get__(eng)
        return eng._evaluate_global_guards()


class ConcurrentAndCrashRewinds(AstraCase):
    """Rules 4-6: fencing, and a commit protocol whose crash windows are
    documented rather than hoped about."""

    def test_a_stale_second_writer_cannot_overwrite_newer_state(self):
        """Astra CRASH_stale_second_writer_overwrites_watermark.

        Two ledger objects load the same generation. One records the loss
        and commits. The other still holds the pre-loss state and the older
        generation: its write is FENCED OFF, not applied last-wins.
        """
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        stale = EquityLedger(tlog, pos, env="prod")        # loads generation N
        stale_gen = stale.generation
        self.lose(tlog, led)                               # advances to N+k
        self.assertGreater(led.generation, stale_gen)
        stale.state["journal_watermark"] = {"settled_count": 0,
                                            "digest": "0" * 64,
                                            "realized_pnl_cum": 0.0,
                                            "strategy_equity": 10.0,
                                            "at": PRE_AT}
        with self.assertLogs("PERSISTENCE", level="CRITICAL") as cm:
            self.assertFalse(stale.save())
        self.assertTrue(any("FENCE_REFUSED" in m for m in cm.output), cm.output)
        on_disk = self.ledger_file()
        self.assertEqual(on_disk["journal_watermark"]["settled_count"], 1)
        self.assertEqual(on_disk["generation"], led.generation)
        led2, _, _ = self.reload()
        self.assertAlmostEqual(led2.drawdown_pct(), 30.0, places=6)

    def test_crash_after_journal_commit_before_the_ledger_write(self):
        """Astra CRASH_after_journal_before_watermark_then_restore.

        The journal records the settlement; the process dies before the
        ledger write. The evidence is in the chain first, by construction,
        so a later restore of the pre-loss journal is still refused.
        """
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        pre_loss_journal = json.loads(json.dumps(
            JsonStore.load(_p(CFG.TRADES_FILE), [])))
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        # crash: the ledger never writes, but its evidence is appended first
        led._advance_journal_watermark()
        led._append_evidence()
        corrupt_json(_p(CFG.TRADES_FILE), pre_loss_journal)   # the restore
        led2, _, _ = self.reload()
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assert_loss_preserved(led2)

    def test_ledger_replaced_before_its_checksum_recovers_an_older_backup(self):
        """Astra CRASH_ledger_replace_before_checksum_replace.

        The ledger is replaced, the process dies before the `.sha256`
        sidecar is rewritten, and `JsonStore.load` answers from `.bak1` --
        which is OLDER state. It is not proven current, so continuity is
        blocked until it is re-established.
        """
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        ledger_path = _p(EL.LEDGER_FILE)
        pre_loss_ledger = open(ledger_path, "rb").read()
        self.lose(tlog, led)
        # the rotation copy the load path will answer from is OLDER state
        with open(ledger_path + ".bak1", "wb") as fh:
            fh.write(pre_loss_ledger)
        with open(ledger_path + ".sha256", "w") as fh:
            fh.write("0" * 64)                           # the torn window
        led2, _, _ = self.reload()
        self.assertIsNotNone(led2.from_backup)
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())
        self.assertGreaterEqual(led2.drawdown_pct(), 30.0 - 1e-6)

    def test_a_truncated_chain_is_a_rollback_not_a_fresh_start(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        open(self.chain().path, "w").close()               # emptied
        led2, _, _ = self.reload()
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())

    def test_an_edited_chain_record_breaks_the_hash_chain(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        path = self.chain().path
        lines = open(path).read().splitlines()
        rec = json.loads(lines[-1])
        rec["payload"]["strategy_equity"] = 10.0           # erase the loss
        lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        open(path, "w").write("\n".join(lines) + "\n")
        ok, why = self.chain().healthy()
        self.assertFalse(ok)
        self.assertIn("hash", why)
        led2, _, _ = self.reload()
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())

    def test_a_torn_last_append_requires_recovery_without_losing_the_chain(self):
        """A malformed tail cannot prove a harmless crash; recovery is required."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        path = self.chain().path
        with open(path, "a") as fh:
            fh.write('{"seq": 99, "prev": "')             # torn
        ok, _ = self.chain().healthy()
        self.assertFalse(ok)
        led2, _, _ = self.reload()
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        self.assertFalse(led2.capital_eligible())


class ConsumedAuthorizationCannotBeReplayed(AstraCase):
    """Rule 9: token history obeys the same anti-rollback guarantee."""

    def rebase_ctx(self, led):
        return {"drawdown_firing": True, "reconcile_status": "MATCH",
                "open_positions": 0, "in_flight_orders": 0, "quiescent": True,
                "evidence_unstable": None, "bound_state": led.bound_state(), "execution_freeze": freeze_for(led),
                "orders": {"local_open": [], "pending_intents": [],
                           "resolution_halt": False, "broker_open": 0,
                           "broker_open_ids": [], "broker_error": None,
                           "disagreement": False}}

    def test_a_consumed_token_stays_consumed_after_a_snapshot_restore(self):
        """Astra TOKEN_replay_after_snapshot_restore."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        before_consumption = self.snapshot_dir()
        prop = led.propose_rebase("losses acknowledged", "OPS-20")
        self.assertTrue(led.apply_rebase("losses acknowledged", "OPS-20",
                                         prop["token"], self.rebase_ctx(led)))
        self.assertTrue(led.token_consumed(prop["token"]))
        # restore the ledger to before the rebase; the chain records the burn
        self.restore_dir(before_consumption,
                         only=[n for n in before_consumption
                               if "continuity" not in n])
        led2, tlog2, pos2 = self.reload()
        self.assertTrue(led2.token_consumed(prop["token"]))
        hwm = led2.risk_equity_reference()
        self.assertFalse(led2.apply_rebase("losses acknowledged", "OPS-20",
                                           prop["token"],
                                           self.rebase_ctx(led2)))
        self.assertAlmostEqual(led2.risk_equity_reference(), hwm, places=9)

    def test_an_unreadable_chain_never_clears_a_token(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        with open(self.chain().path, "a") as fh:
            fh.write("garbage\nmore garbage\n")
        self.assertTrue(led.token_consumed("f" * 64),
                        "an unreadable chain must not clear a token")

    def test_positive_control_a_fresh_token_is_still_usable(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        prop = led.propose_rebase("losses acknowledged", "OPS-21")
        self.assertFalse(led.token_consumed(prop["token"]))
        self.assertTrue(led.apply_rebase("losses acknowledged", "OPS-21",
                                         prop["token"], self.rebase_ctx(led)))


class ChainSchemaIsStrict(AstraCase):
    """Rule 3 applied to the authority itself: the chain refuses records it
    does not fully understand rather than skipping them."""

    def append_raw(self, obj):
        with open(self.chain().path, "a") as fh:
            fh.write(json.dumps(obj) + "\n")

    def seeded(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        return led

    def test_unknown_record_kind_invalidates_the_chain(self):
        self.seeded()
        head = self.chain().head()
        self.append_raw({"version": 1, "seq": head["seq"] + 1,
                         "prev": head["hash"], "kind": "something_new",
                         "at": PRE_AT, "payload": {}, "hash": "0" * 64})
        ok, why = self.chain().healthy()
        self.assertFalse(ok)
        self.assertIn("kind", why)

    def test_a_sequence_gap_invalidates_the_chain(self):
        self.seeded()
        head = self.chain().head()
        self.append_raw({"version": 1, "seq": head["seq"] + 5,
                         "prev": head["hash"], "kind": "evidence",
                         "at": PRE_AT, "payload": {}, "hash": "0" * 64})
        ok, why = self.chain().healthy()
        self.assertFalse(ok)
        self.assertIn("sequence", why)

    def test_a_missing_field_is_never_a_default(self):
        self.seeded()
        head = self.chain().head()
        self.append_raw({"version": 1, "seq": head["seq"] + 1,
                         "prev": head["hash"], "kind": "evidence",
                         "payload": {}})                  # no `at`, no `hash`
        with self.assertRaises(ChainError):
            self.chain().records()

    def test_an_unknown_chain_version_is_refused(self):
        self.seeded()
        head = self.chain().head()
        self.append_raw({"version": 99, "seq": head["seq"] + 1,
                         "prev": head["hash"], "kind": "evidence",
                         "at": PRE_AT, "payload": {}, "hash": "0" * 64})
        ok, why = self.chain().healthy()
        self.assertFalse(ok)
        self.assertIn("version", why)


class ContinuityClearsOnlyWhenReconstructed(AstraCase):
    """Rule 8: the blocking recovery state is left by re-establishing the
    evidence, verified against the independent chain -- not by waiting."""

    def test_a_block_survives_restarts_and_clears_when_history_returns(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        full_journal = json.loads(json.dumps(
            JsonStore.load(_p(CFG.TRADES_FILE), [])))
        corrupt_json(_p(CFG.TRADES_FILE), [])
        for _ in range(3):                                 # survives restarts
            led2, _, _ = self.reload()
            self.assertIn(EL.GUARD_CONTINUITY, led2.guards())
        corrupt_json(_p(CFG.TRADES_FILE), full_journal)  # reconstructed
        led3, _, _ = self.reload()
        self.assertNotIn(EL.GUARD_CONTINUITY, led3.guards())
        self.assertAlmostEqual(led3.drawdown_pct(), 30.0, places=6)

    def test_an_attestation_cannot_paper_over_a_continuity_block(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        corrupt_json(_p(CFG.TRADES_FILE), [])
        led2, _, _ = self.reload()
        att = led2.propose_attestation("OPS-B", "b" * 64)
        self.assertFalse(led2.apply_attestation("OPS-B", "b" * 64, att["token"]))
        self.assertIn(EL.GUARD_CONTINUITY, led2.guards())


if __name__ == "__main__":
    import unittest
    unittest.main()
