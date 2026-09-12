# -*- coding: utf-8 -*-
"""Astra v4 counter-audit V4-RA-05..V4-RA-07, reproduced before they closed.

SHADOW ONLY. Nothing here contacts a provider, a broker or a real credential,
nothing touches CAPITAL or deployment, and every byte written lands under a
throwaway `DATA_DIR` supplied by `AlphaCase`. The provider calls are replaced
by direct `record_actual` invocations with synthetic figures, because the
subject is the ACCOUNTING protocol, not the vendor.

WHY THESE TESTS LOOK LIKE THIS
    Each case below was written from the counter-audit's counterexample and
    run against the ACCEPTED PARENT (`30715b9`) first. The reproductions are
    kept rather than replaced by tests of the fix: a test that only describes
    the fix cannot tell you whether the fix addressed the defect.

    The three findings are one protocol -- see
    `docs/design/budget-durability-protocol.md` -- so the cross-finding cases
    at the bottom assert the interactions rather than leaving them to be
    inferred from three passing halves.

THE SHARED RULE, WHICH EVERY CASE HERE IS AN INSTANCE OF
    UNKNOWN never becomes ZERO, and readable never means durable.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

import alpha_cost                                             # noqa: E402
import durable_append                                         # noqa: E402
from _alpha import AlphaCase                                  # noqa: E402
from alpha_cost import (REASON_BUDGET, BudgetGuard,           # noqa: E402
                        BudgetLedger, PricingTable)
from config import CFG                                        # noqa: E402
from durable_append import (DurabilityUnknown, append_line,   # noqa: E402
                            serialized_append)

# The names this batch INTRODUCES are resolved defensively, with the value the
# protocol defines as the fallback. That is not indirection for its own sake:
# run against the ACCEPTED PARENT, an `ImportError` at collection time would
# fail all of these cases at once and tell an auditor nothing about WHICH
# invariant each one pins. Resolved this way, every case below fails on its
# own assertion against the parent and names the defect it reproduces.
BUDGET_SCHEMA = getattr(alpha_cost, "BUDGET_SCHEMA",
                        "atlas-alpha-budget-v1")
KIND_RESERVATION = getattr(alpha_cost, "KIND_RESERVATION", "reservation")
KIND_ACTUAL = getattr(alpha_cost, "KIND_ACTUAL", "actual")
KIND_VOID = getattr(alpha_cost, "KIND_VOID", "void")
KIND_RECONCILED = getattr(alpha_cost, "KIND_RECONCILED", "reconciled")
BudgetLedgerInvalid = getattr(alpha_cost, "BudgetLedgerInvalid", RuntimeError)


def _forget_durability(path=None):
    hook = getattr(durable_append, "forget_pathname_durability", None)
    if hook is not None:
        hook(path)


def _durability_proven(path):
    hook = getattr(durable_append, "pathname_durability_proven", None)
    return False if hook is None else hook(path)


# ════════════════════════════════════════════════════════════════════════
# V4-RA-05 — existence was treated as proof the name had been persisted
# ════════════════════════════════════════════════════════════════════════
class V4RA05_ExistenceWasMistakenForDirectoryDurability(AlphaCase):
    """`append_line` ran the directory barrier only when `created` was true.

        created = not os.path.exists(path)
        ... write bytes, fsync the file fd ...
        if created and parent:
            fsync_directory(parent)          # raises on failure

    The bytes reach the disk BEFORE the barrier, so a barrier failure leaves
    the file existing. On the next append `os.path.exists(path)` is True,
    `created` is False, and the barrier is not merely skipped -- it is never
    ATTEMPTED. Measured on the accepted parent, with the same fault still
    present:

        first append : raised DurabilityUnknown (correctly)
        file exists  : True
        retry append : ACKNOWLEDGED SUCCESS
        barrier attempts during the retry: 0

    The file existing was evidence that the first append wrote BYTES. It was
    never evidence that the first append's directory ENTRY survived, and the
    retry published a terminal "durable" on the strength of it.
    """

    def setUp(self):
        super().setUp()
        # Every pathname starts UNPROVEN, which is also its state after any
        # real restart. Without this the proof registry could carry over from
        # another case in the same process and mask the defect.
        _forget_durability()
        self.addCleanup(_forget_durability)

    def ledger_path(self, name="ra05"):
        return os.path.join(self._tmp, name, "ledger.jsonl")

    # ── fault injectors ─────────────────────────────────────────────────
    def dir_open_fault(self, directory):
        """Fail ONLY the read-only open of a directory, never a file open."""
        real_open = os.open
        calls = {"n": 0}

        def hostile(path, flags, *a, **kw):
            if flags == os.O_RDONLY and isinstance(path, str) \
                    and os.path.isdir(path) \
                    and os.path.abspath(directory) == os.path.abspath(path):
                calls["n"] += 1
                raise OSError(5, "EIO synthetic: cannot open directory")
            return real_open(path, flags, *a, **kw)

        return patch("os.open", side_effect=hostile), calls

    def dir_fsync_fault(self):
        """Fail ONLY an fsync whose fd is a directory."""
        real_fsync = os.fsync
        calls = {"n": 0}

        def hostile(fd):
            import stat as stat_module
            try:
                is_dir = stat_module.S_ISDIR(os.fstat(fd).st_mode)
            except OSError:                              # pragma: no cover
                is_dir = False
            if is_dir:
                calls["n"] += 1
                raise OSError(5, "EIO synthetic: cannot fsync directory")
            return real_fsync(fd)

        return patch("os.fsync", side_effect=hostile), calls

    # ── the witnesses ───────────────────────────────────────────────────
    def test_a_retry_after_a_directory_open_failure_attempts_the_barrier(self):
        path = self.ledger_path("open")
        parent = os.path.dirname(path)
        fault, calls = self.dir_open_fault(parent)
        with fault:
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 1}')
            self.assertTrue(os.path.exists(path),
                            "this case is only meaningful if the file is on "
                            "disk after the failure")
            before = calls["n"]
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 2}')
            self.assertGreater(
                calls["n"], before,
                "the retry acknowledged success without attempting the "
                "directory barrier, because the file it had already created "
                "was read as proof the name was durable")

    def test_a_retry_after_a_directory_fsync_failure_attempts_the_barrier(self):
        path = self.ledger_path("fsync")
        fault, calls = self.dir_fsync_fault()
        with fault:
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 1}')
            self.assertTrue(os.path.exists(path))
            before = calls["n"]
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 2}')
            self.assertGreater(calls["n"], before)

    def test_repeated_retries_stay_fail_closed(self):
        """Not one retry: the refusal must not decay into acceptance."""
        for label, make in (("open", lambda: self.dir_open_fault(
                os.path.dirname(self.ledger_path("rep_open")))),
                ("fsync", lambda: self.dir_fsync_fault())):
            with self.subTest(fault=label):
                path = self.ledger_path(f"rep_{label}")
                fault, calls = make()
                with fault:
                    for attempt in range(6):
                        with self.assertRaises(DurabilityUnknown):
                            append_line(path, '{"row": %d}' % attempt)
                    self.assertGreaterEqual(
                        calls["n"], 6,
                        f"{calls['n']} barrier attempts across 6 appends; a "
                        f"retry stopped trying and started assuming")

    def test_the_append_is_acknowledged_once_the_fault_clears(self):
        """Fail-closed must not mean fail-forever."""
        path = self.ledger_path("recover")
        fault, _calls = self.dir_fsync_fault()
        with fault:
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 1}')
        # Fault cleared. The very next append must succeed AND establish the
        # barrier that was owed.
        append_line(path, '{"row": 2}')
        self.assertTrue(_durability_proven(path),
                        "the append was acknowledged without the pathname "
                        "ever being proven")
        self.assertEqual(len(open(path, encoding="utf-8").read()
                             .splitlines()), 2)

    def test_an_existing_file_whose_durability_cannot_be_proven_owes_a_barrier(self):
        """The restart case, and the heart of the finding.

        A file created by a process that is gone is exactly a file whose
        directory durability this process cannot prove. It must be treated as
        UNPROVEN -- not as durable because it is readable.
        """
        path = self.ledger_path("restart")
        append_line(path, '{"row": 1}')                # clean, proven
        self.assertTrue(_durability_proven(path))

        _forget_durability()    # the restart
        self.assertFalse(_durability_proven(path))
        fault, calls = self.dir_fsync_fault()
        with fault:
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 2}')
            self.assertGreater(
                calls["n"], 0,
                "an existing file was assumed durable after a restart, which "
                "is the inference this finding forbids")

    def test_a_proven_pathname_does_not_pay_for_a_barrier_every_append(self):
        """The control. Correct is not the same as unconditional.

        A barrier is owed once per NAME, because an append to an existing
        name creates no new directory entry. Re-running it on every append
        would be defensible and slow; asserting it here pins the intended
        design rather than leaving the cost unstated.
        """
        path = self.ledger_path("proven")
        append_line(path, '{"row": 1}')
        fault, calls = self.dir_fsync_fault()
        with fault:
            append_line(path, '{"row": 2}')            # must NOT raise
        self.assertEqual(calls["n"], 0)

    def test_a_new_name_in_a_proven_directory_still_owes_a_barrier(self):
        """Proof is per PATHNAME, not per directory.

        A directory fsync persists the entries that exist when it runs. A
        second file created in that directory afterwards is a new entry and a
        new obligation, and keying the proof by directory would have skipped
        it.
        """
        first = os.path.join(self._tmp, "shared", "a.jsonl")
        second = os.path.join(self._tmp, "shared", "b.jsonl")
        append_line(first, '{"row": 1}')
        self.assertTrue(_durability_proven(first))
        fault, calls = self.dir_fsync_fault()
        with fault:
            with self.assertRaises(DurabilityUnknown):
                append_line(second, '{"row": 1}')
            self.assertGreater(calls["n"], 0,
                               "a new name inherited another file's proof")

    def test_history_is_never_rewritten_by_a_failed_barrier(self):
        """Append-only survives the fix (AA-12's guarantee, still true)."""
        path = self.ledger_path("history")
        append_line(path, '{"row": 1}')
        _forget_durability()
        fault, _calls = self.dir_fsync_fault()
        with fault:
            with self.assertRaises(DurabilityUnknown):
                append_line(path, '{"row": 2}')
        lines = open(path, encoding="utf-8").read().splitlines()
        self.assertEqual([json.loads(x)["row"] for x in lines], [1, 2],
                         "a barrier failure removed or rewrote a row; the "
                         "bytes were already durable and must stay")

    def test_a_torn_tail_is_still_separated_not_truncated(self):
        """AA-12's control, re-asserted through the new barrier logic."""
        path = self.ledger_path("torn")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"row": 1}\n{"row": 2, "tor')      # no trailing newline
        append_line(path, '{"row": 3}')
        lines = open(path, encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('"tor', lines[1], "the damaged fragment was destroyed")

    def test_the_serialized_writer_inherits_the_same_refusal(self):
        """`serialized_append` is what every real writer calls."""
        path = self.ledger_path("serialized")
        fault, calls = self.dir_fsync_fault()
        with fault:
            with self.assertRaises(DurabilityUnknown):
                with serialized_append(path) as append:
                    append('{"row": 1}')
            before = calls["n"]
            with self.assertRaises(DurabilityUnknown):
                with serialized_append(path) as append:
                    append('{"row": 2}')
            self.assertGreater(calls["n"], before)

    def test_concurrent_appenders_each_confirm_before_acknowledging(self):
        """Two threads, one unproven pathname, one fault.

        The proof registry is deliberately lock-free: the only interleaving a
        reader can see is "not yet proven", which costs a redundant barrier.
        What must never happen is an acknowledgement without one, so both
        threads must raise while the fault is present.
        """
        import threading
        path = self.ledger_path("concurrent")
        fault, calls = self.dir_fsync_fault()
        outcomes = []

        def writer():
            try:
                with serialized_append(path) as append:
                    append('{"row": 1}')
                outcomes.append("acknowledged")
            except DurabilityUnknown:
                outcomes.append("refused")
            except Exception as exc:                     # pragma: no cover
                outcomes.append(f"{type(exc).__name__}")

        with fault:
            threads = [threading.Thread(target=writer) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
        self.assertEqual(sorted(outcomes), ["refused", "refused"],
                         f"outcomes were {outcomes}; an append was "
                         f"acknowledged with no confirmed barrier")
        self.assertGreaterEqual(calls["n"], 2)


# ════════════════════════════════════════════════════════════════════════
# V4-RA-06 — the accounting latch died with the process
# ════════════════════════════════════════════════════════════════════════
class V4RA06_UnaccountedSpendVanishedAcrossRestart(AlphaCase):
    """`accounting_uncertain` was a memory attribute, and memory restarts.

    The protection was also on the WRONG SIDE of the irreversible act: it was
    set in `record_actual`, i.e. after the provider had already been paid.
    So the window was:

        1. provider called, $2 of usage really incurred
        2. `record_actual` -> ledger write fails (zero-byte / EIO)
        3. no cost row exists anywhere on disk
        4. `accounting_uncertain` blocks further calls -- in THIS process
        5. process restarts
        6. fresh guard: latch is "", ledger is empty, and an empty ledger
           reads as ZERO SPEND

    Measured on the accepted parent with a $1 daily cap:

        SAME process   : allowed=False  reason=budget_exhausted
        AFTER restart  : allowed=True   reason=None
        spent_today()  : $0.00   (the real incurred spend was $2.00)

    A completed cost disappeared from admission logic because persistence
    failed and the process died.
    """

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS",
                                          True))
        self._patches[-1].start()

    def path(self):
        return os.path.join(self._tmp, "budget.jsonl")

    def guard(self):
        return BudgetGuard(ledger=BudgetLedger(self.path()))

    def restart(self):
        """A fresh process: a new guard AND a new instance identity.

        The instance token is what makes an unresolved reservation orphaned,
        and a real restart always produces a new one. Patching it is how a
        single test process simulates two.
        """
        return patch.object(alpha_cost, "INSTANCE_ID", "instance-after-restart")

    def kinds(self):
        """The `kind` of every row on disk. A legacy row reports None.

        `.get` rather than `[...]`: a row written by the plain
        `ledger.record({...})` path carries no `schema` and no `kind`, which
        is exactly the legacy shape the validator must still accept.
        """
        return [json.loads(line).get("kind")
                for line in open(self.path(), encoding="utf-8")
                if line.strip()]

    # ── the witness ─────────────────────────────────────────────────────
    def test_an_unrecorded_spend_still_blocks_after_a_restart(self):
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1.0):
            guard = self.guard()
            verdict = guard.check("synthetic", "m")
            self.assertTrue(verdict["allowed"])
            self.assertTrue(verdict.get("intent_id"),
                            "the call was allowed without being announced")
            # The provider is "called" and $2 is really incurred; the row
            # recording it cannot be written.
            with patch.object(BudgetLedger, "record",
                              side_effect=OSError(5, "EIO synthetic")):
                guard.record_actual({"provider": "synthetic", "model": "m",
                                     "api_cost_usd": 2.0,
                                     "billed_cost_usd": 2.0,
                                     "cost_priced": True,
                                     "intent_id": verdict["intent_id"]})
            self.assertEqual(self.kinds(), [KIND_RESERVATION],
                             "the announcement did not survive as the only "
                             "durable trace of the spend")

            with self.restart():
                fresh = self.guard()
                self.assertEqual(fresh.accounting_uncertain, "",
                                 "this case is only meaningful if the "
                                 "memory latch is genuinely gone")
                after = fresh.check("synthetic", "m")
                self.assertFalse(
                    after["allowed"],
                    "admission was restored after a restart even though $2 "
                    "of provider usage was never accounted for")
                self.assertEqual(after["reason"], REASON_BUDGET)
                self.assertIn("never accounted for", after["detail"])
                self.assertIn("Restarting does NOT clear this",
                              after["detail"])

    def test_the_spend_is_announced_before_the_provider_can_be_called(self):
        """The protection moved to BEFORE the irreversible act."""
        guard = self.guard()
        self.assertFalse(os.path.exists(self.path()))
        verdict = guard.check("synthetic", "m")
        self.assertTrue(verdict["allowed"])
        rows = [json.loads(x) for x in open(self.path(), encoding="utf-8")
                if x.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], KIND_RESERVATION)
        self.assertEqual(rows[0]["schema"], BUDGET_SCHEMA)
        self.assertEqual(rows[0]["owner_instance"], alpha_cost.INSTANCE_ID)
        self.assertEqual(rows[0]["intent_id"], verdict["intent_id"])

    def test_a_refused_call_announces_nothing(self):
        """A reservation is an announcement, not a log line."""
        guard = self.guard()
        guard.ledger.record({"provider": "synthetic", "api_cost_usd": 50.0})
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1.0):
            self.assertFalse(guard.check("synthetic", "m")["allowed"])
        self.assertNotIn(KIND_RESERVATION, self.kinds())

    def test_a_failure_before_dispatch_refuses_rather_than_dispatching(self):
        """Failure BEFORE the provider call: nothing spent, nothing lost.

        The cross-finding contract with V4-RA-05: if the announcement cannot
        be made durable, the call is not made. A provider contacted against
        an unwritable ledger is money that cannot be accounted for afterwards.
        """
        guard = self.guard()
        with patch.object(BudgetLedger, "record",
                          side_effect=OSError(5, "EIO synthetic")):
            verdict = guard.check("synthetic", "m")
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], REASON_BUDGET)
        self.assertIn("could not be ANNOUNCED", verdict["detail"])

    def test_a_failure_after_the_usage_is_known_leaves_an_open_intent(self):
        guard = self.guard()
        verdict = guard.check("synthetic", "m")
        with patch.object(BudgetLedger, "record",
                          side_effect=OSError(5, "EIO synthetic")):
            guard.record_actual({"provider": "synthetic", "model": "m",
                                 "api_cost_usd": 2.0, "cost_priced": True,
                                 "intent_id": verdict["intent_id"]})
        state = guard.accounting_state()
        self.assertFalse(state["certain"])
        self.assertEqual(state["reason"], "record_not_durable")
        with self.restart():
            self.assertFalse(self.guard().accounting_state()["certain"])

    def test_a_settled_intent_is_charged_once_not_twice(self):
        """Reservation + actual for one intent is ONE charge."""
        guard = self.guard()
        verdict = guard.check("synthetic", "m")
        guard.record_actual({"provider": "synthetic", "model": "m",
                             "api_cost_usd": 0.25, "cost_priced": True,
                             "intent_id": verdict["intent_id"]})
        self.assertEqual(self.kinds(), [KIND_RESERVATION, KIND_ACTUAL])
        self.assertAlmostEqual(guard.ledger.spent_today(), 0.25, places=6)
        self.assertTrue(guard.accounting_state()["certain"])

    def test_a_duplicate_actual_for_one_intent_is_not_a_second_charge(self):
        """Idempotency by identity, which is what makes RA-05's retry safe.

        A barrier failure leaves the row on disk and raises, so a retrying
        caller can append the same logical row twice. Summing rows would
        double-charge; resolving INTENTS does not.
        """
        guard = self.guard()
        verdict = guard.check("synthetic", "m")
        for _ in range(3):
            guard.record_actual({"provider": "synthetic", "model": "m",
                                 "api_cost_usd": 0.25, "cost_priced": True,
                                 "intent_id": verdict["intent_id"]})
        self.assertEqual(self.kinds().count(KIND_ACTUAL), 3)
        self.assertAlmostEqual(guard.ledger.spent_today(), 0.25, places=6,
                              msg="three appends of one charge became three "
                                  "charges")

    def test_multiple_unresolved_intents_are_all_reported(self):
        guard = self.guard()
        ids = [guard.check("synthetic", f"m{i}")["intent_id"]
               for i in range(3)]
        self.assertEqual(len(set(ids)), 3)
        with self.restart():
            fresh = self.guard()
            state = fresh.accounting_state()
            self.assertFalse(state["certain"])
            self.assertEqual(sorted(state["orphaned"]), sorted(ids))
            self.assertIn("3 announced", fresh.check("synthetic", "m")["detail"])

    def test_an_open_intent_of_this_instance_is_not_uncertainty(self):
        """The control that keeps the fix from blocking normal operation.

        An in-flight reservation this process made is PROVISIONAL, not
        uncertain: something is going to settle it. Treating it as uncertainty
        would refuse the second provider of every ordinary analysis.
        """
        guard = self.guard()
        first = guard.check("synthetic", "a")
        self.assertTrue(first["allowed"])
        second = guard.check("synthetic", "b")
        self.assertTrue(second["allowed"],
                        "an in-flight announcement by this very process was "
                        "treated as unaccounted spend")
        self.assertTrue(guard.accounting_state()["certain"])

    def test_an_open_intent_still_counts_toward_the_caps(self):
        """Provisional is not free: the reserved amount binds."""
        pricing = PricingTable(self.write_rates())
        guard = BudgetGuard(pricing=pricing,
                            ledger=BudgetLedger(self.path()))
        worst = pricing.estimate("grok", "grok")["api_cost_usd"]
        self.assertGreater(worst, 0.0)
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", worst * 1.5):
            self.assertTrue(guard.check("grok", "grok")["allowed"])
            self.assertFalse(
                guard.check("grok", "grok")["allowed"],
                "the first announcement reserved nothing, so the daily cap "
                "could be spent twice over before either call returned")

    def test_a_void_releases_a_reservation_and_says_why(self):
        guard = self.guard()
        verdict = guard.check("synthetic", "m")
        self.assertTrue(guard.void_reservation(verdict["intent_id"],
                                               "provider never contacted"))
        self.assertEqual(self.kinds(), [KIND_RESERVATION, KIND_VOID])
        self.assertEqual(guard.ledger.spent_today(), 0.0)
        self.assertTrue(guard.accounting_state()["certain"])

    def test_reconciliation_is_the_documented_exit_from_uncertainty(self):
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 100.0):
            guard = self.guard()
            verdict = guard.check("synthetic", "m")
            with patch.object(BudgetLedger, "record",
                              side_effect=OSError(5, "EIO synthetic")):
                guard.record_actual({"provider": "synthetic", "model": "m",
                                     "api_cost_usd": 2.0,
                                     "intent_id": verdict["intent_id"]})
            with self.restart():
                fresh = self.guard()
                self.assertFalse(fresh.check("synthetic", "m")["allowed"])
                orphans = fresh.ledger.orphaned_intents()
                self.assertEqual(len(orphans), 1)
                self.assertTrue(fresh.reconcile(orphans[0]["intent_id"], 2.0,
                                                note="vendor invoice"))
                self.assertEqual(self.kinds()[-1], KIND_RECONCILED)
                # The real figure is now on the books, and admission is
                # decided by the CAP rather than by uncertainty.
                self.assertAlmostEqual(fresh.ledger.spent_today(), 2.0,
                                       places=6)
                self.assertTrue(fresh.accounting_state()["certain"])
                self.assertTrue(fresh.check("synthetic", "m")["allowed"])

    def test_reconciliation_does_not_rewrite_the_orphan(self):
        guard = self.guard()
        verdict = guard.check("synthetic", "m")
        before = open(self.path(), encoding="utf-8").read()
        with self.restart():
            self.guard().reconcile(verdict["intent_id"], 1.0)
        after = open(self.path(), encoding="utf-8").read()
        self.assertTrue(after.startswith(before),
                        "reconciliation edited history instead of appending")

    def test_an_orphan_is_not_retired_by_the_daily_boundary(self):
        """"Restart clears the latch" must not come back wearing a calendar.

        `spent_today()` looks back to midnight. If uncertainty were computed
        over the same window, an abandoned announcement from yesterday would
        stop blocking at midnight -- the same defect on a timer.
        """
        guard = self.guard()
        verdict = guard.check("synthetic", "m")
        del verdict
        # Re-stamp the reservation far in the past, then restart.
        lines = open(self.path(), encoding="utf-8").read().splitlines()
        row = json.loads(lines[0])
        row["ts"] = alpha_cost._now() - 86400.0 * 10
        with open(self.path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        with self.restart():
            fresh = self.guard()
            self.assertEqual(fresh.ledger.spent_today(), 0.0,
                             "a ten-day-old row is outside today's window")
            self.assertFalse(
                fresh.check("synthetic", "m")["allowed"],
                "an announcement abandoned ten days ago stopped blocking "
                "because it fell out of the daily spend window")

    def write_rates(self):
        from _alpha import write_pricing
        return write_pricing(os.path.join(self._tmp, "pricing.json"),
                             rates=(3.0, 15.0), models=("grok",),
                             version="ra06-1")


# ════════════════════════════════════════════════════════════════════════
# V4-RA-07 — parseable was treated as valid
# ════════════════════════════════════════════════════════════════════════
class V4RA07_MalformedRowsSilentlyReducedSpend(AlphaCase):
    """`rows()` validated JSON syntax and almost nothing else.

        if isinstance(row, dict) and (since_ts is None
                                      or float(row.get("ts") or 0) >= since_ts):
            out.append(row)

    Two holes in one expression. A non-object was FILTERED OUT -- a silent
    skip, and a skipped charge and a charge of zero are the same number. And
    `float(row.get("ts") or 0)` raised `ValueError` on a string timestamp and
    `TypeError` on a list or mapping one, out of `spent_today()` and past
    `BudgetGuard.check`'s `except RuntimeError`.

    `spent()` then summed only values `_finite()` liked, so a missing, null,
    boolean, NaN or Infinity cost contributed nothing at all.

    Measured on the accepted parent, with a real $2.00 row followed by one
    malformed row, all ten shapes:

        list / scalar / {} / missing cost / null cost / bool-as-number /
        NaN / Infinity / missing timestamp / string cost
            -> spent_today() == $2.00 in EVERY case (the row vanished)

        {"ts": "not-a-timestamp", ...} -> check() raised ValueError
        {"ts": [1, 2], ...}            -> check() raised TypeError
    """

    #: Every shape must make spend UNKNOWN. None may make it SMALLER.
    MALFORMED = (
        ("a list instead of an object", '[1, 2, 3]'),
        ("a bare scalar", '42'),
        ("a bare string", '"nope"'),
        ("json null", 'null'),
        ("an empty object", '{}'),
        ("missing cost", '{"ts": 1789000000.0, "provider": "x"}'),
        ("null cost", '{"ts": 1789000000.0, "api_cost_usd": null}'),
        ("bool-as-number", '{"ts": 1789000000.0, "api_cost_usd": true}'),
        ("NaN cost", '{"ts": 1789000000.0, "api_cost_usd": NaN}'),
        ("Infinity cost", '{"ts": 1789000000.0, "api_cost_usd": Infinity}'),
        ("-Infinity cost", '{"ts": 1789000000.0, "api_cost_usd": -Infinity}'),
        ("string cost", '{"ts": 1789000000.0, "api_cost_usd": "2.0"}'),
        ("list cost", '{"ts": 1789000000.0, "api_cost_usd": [2.0]}'),
        ("missing timestamp", '{"api_cost_usd": 2.0}'),
        ("string timestamp", '{"ts": "not-a-timestamp", "api_cost_usd": 2.0}'),
        ("list timestamp", '{"ts": [1, 2], "api_cost_usd": 2.0}'),
        ("mapping timestamp", '{"ts": {"a": 1}, "api_cost_usd": 2.0}'),
        ("bool timestamp", '{"ts": true, "api_cost_usd": 2.0}'),
        ("null timestamp", '{"ts": null, "api_cost_usd": 2.0}'),
        ("NaN timestamp", '{"ts": NaN, "api_cost_usd": 2.0}'),
        ("negative timestamp", '{"ts": -5.0, "api_cost_usd": 2.0}'),
        ("zero timestamp", '{"ts": 0, "api_cost_usd": 2.0}'),
        ("timestamp in the far future",
         '{"ts": 99999999999.0, "api_cost_usd": 2.0}'),
        ("a negative cost outside the refund policy",
         '{"ts": 1789000000.0, "api_cost_usd": -500.0}'),
        ("a cost beyond the policy bound",
         '{"ts": 1789000000.0, "api_cost_usd": 1e12}'),
        ("an unknown schema version",
         '{"schema": "atlas-alpha-budget-v99", "kind": "actual", '
         '"ts": 1789000000.0, "intent_id": "i", "api_cost_usd": 1.0}'),
        ("an unknown kind",
         '{"schema": "atlas-alpha-budget-v1", "kind": "invented", '
         '"ts": 1789000000.0, "intent_id": "i", "api_cost_usd": 1.0}'),
        ("a v1 row with no intent_id",
         '{"schema": "atlas-alpha-budget-v1", "kind": "actual", '
         '"ts": 1789000000.0, "api_cost_usd": 1.0}'),
        ("a v1 row whose intent_id is blank",
         '{"schema": "atlas-alpha-budget-v1", "kind": "actual", '
         '"ts": 1789000000.0, "intent_id": "   ", "api_cost_usd": 1.0}'),
        ("a reservation with no owner_instance",
         '{"schema": "atlas-alpha-budget-v1", "kind": "reservation", '
         '"ts": 1789000000.0, "intent_id": "i", "reserved_usd": 1.0}'),
        ("a reservation with no reserved_usd",
         '{"schema": "atlas-alpha-budget-v1", "kind": "reservation", '
         '"ts": 1789000000.0, "intent_id": "i", "owner_instance": "o"}'),
        ("a reservation reserving a negative amount",
         '{"schema": "atlas-alpha-budget-v1", "kind": "reservation", '
         '"ts": 1789000000.0, "intent_id": "i", "owner_instance": "o", '
         '"reserved_usd": -9.0}'),
        ("a void with no reason",
         '{"schema": "atlas-alpha-budget-v1", "kind": "void", '
         '"ts": 1789000000.0, "intent_id": "i"}'),
        ("unparseable JSON", '{"ts": 1789000000.0, "api_cost'),
    )

    def ledger_with(self, raw):
        """A real $2.00 charge, then one malformed row."""
        path = os.path.join(self._tmp, f"m{abs(hash(raw)) % 10 ** 9}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": alpha_cost._now(),
                                 "api_cost_usd": 2.0,
                                 "provider": "synthetic"}) + "\n")
            fh.write(raw + "\n")
        return BudgetLedger(path)

    def test_every_malformed_row_makes_spend_unknown_not_smaller(self):
        for label, raw in self.MALFORMED:
            with self.subTest(row=label):
                ledger = self.ledger_with(raw)
                with self.assertRaises(RuntimeError, msg=label) as caught:
                    ledger.spent_today()
                self.assertNotIsInstance(
                    caught.exception, (ValueError, TypeError),
                    f"{label} escaped as a {type(caught.exception).__name__} "
                    f"rather than the structured accounting refusal")

    def test_every_malformed_row_becomes_a_structured_guard_refusal(self):
        """It must reach `check()` as a refusal, never as an exception."""
        for label, raw in self.MALFORMED:
            with self.subTest(row=label):
                guard = BudgetGuard(ledger=self.ledger_with(raw))
                with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
                    try:
                        verdict = guard.check("synthetic", "m")
                    except Exception as exc:              # noqa: BLE001
                        self.fail(f"{label}: check() raised "
                                  f"{type(exc).__name__}: {exc}")
                self.assertFalse(verdict["allowed"], label)
                self.assertEqual(verdict["reason"], REASON_BUDGET)

    def test_a_malformed_row_is_preserved_not_deleted(self):
        for label, raw in self.MALFORMED[:8]:
            with self.subTest(row=label):
                ledger = self.ledger_with(raw)
                before = open(ledger.path, encoding="utf-8").read()
                with self.assertRaises(RuntimeError):
                    ledger.spent_today()
                self.assertEqual(open(ledger.path, encoding="utf-8").read(),
                                 before, "the reader edited history")

    def test_the_refusal_names_the_row(self):
        ledger = self.ledger_with('[1, 2, 3]')
        with self.assertRaises(BudgetLedgerInvalid) as caught:
            ledger.spent_today()
        self.assertIn("row 2", str(caught.exception))
        self.assertIn(ledger.path, str(caught.exception))

    def test_a_refund_is_admissible_only_on_a_reconciled_row(self):
        """The documented policy, stated as a pair so it is falsifiable.

        Without the restriction, appending a large negative cost would be the
        cheapest way to defeat every cap. With it, a correction is still
        possible -- deliberately, on the row kind an operator writes.
        """
        good = ('{"schema": "atlas-alpha-budget-v1", "kind": "reservation", '
                '"ts": %f, "intent_id": "i1", "owner_instance": "o", '
                '"reserved_usd": 5.0}\n'
                '{"schema": "atlas-alpha-budget-v1", "kind": "reconciled", '
                '"ts": %f, "intent_id": "i1", "api_cost_usd": -1.5}\n'
                % (alpha_cost._now(), alpha_cost._now()))
        path = os.path.join(self._tmp, "refund.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(good)
        self.assertAlmostEqual(BudgetLedger(path).spent_today(), -1.5,
                               places=6)

        bad = ('{"schema": "atlas-alpha-budget-v1", "kind": "actual", '
               '"ts": %f, "intent_id": "i2", "api_cost_usd": -1.5}\n'
               % alpha_cost._now())
        path2 = os.path.join(self._tmp, "refund_bad.jsonl")
        with open(path2, "w", encoding="utf-8") as fh:
            fh.write(bad)
        with self.assertRaises(BudgetLedgerInvalid):
            BudgetLedger(path2).spent_today()

    def test_a_void_for_an_unreserved_intent_is_a_lifecycle_violation(self):
        """The one settlement whose reservation must exist.

        A void reduces a charge to nothing. Voiding an intent this ledger
        never announced cannot be told from losing the announcement, so the
        total is not defensible.
        """
        path = os.path.join(self._tmp, "stray_void.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema": "atlas-alpha-budget-v1", "kind": "void", '
                     '"ts": %f, "intent_id": "never-reserved", '
                     '"reason": "r"}\n' % alpha_cost._now())
        with self.assertRaises(BudgetLedgerInvalid):
            BudgetLedger(path).spent_today()

    def test_a_standalone_actual_is_a_charge_not_a_violation(self):
        """The control. Over-refusal is a defect too.

        Every row written before this protocol existed is a standalone
        charge, and `record_actual` writes one when the gate was bypassed.
        Refusing those would make every existing ledger unverifiable -- which
        is the failure the first draft of `resolve()` actually had.
        """
        path = os.path.join(self._tmp, "standalone.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": alpha_cost._now(), "provider": "p",
                                 "api_cost_usd": 1.25}) + "\n")
            fh.write(json.dumps({"schema": BUDGET_SCHEMA, "kind": KIND_ACTUAL,
                                 "ts": alpha_cost._now(), "intent_id": "x",
                                 "provider": "p",
                                 "api_cost_usd": 0.75}) + "\n")
        self.assertAlmostEqual(BudgetLedger(path).spent_today(), 2.0,
                               places=6)

    def test_a_legacy_ledger_still_totals_correctly(self):
        """Back-compatibility, asserted rather than hoped for."""
        path = os.path.join(self._tmp, "legacy.jsonl")
        ledger = BudgetLedger(path)
        for index in range(5):
            ledger.record({"provider": "grok", "model": "grok",
                           "api_cost_usd": 0.10, "outcome": "VALID"})
        self.assertAlmostEqual(ledger.spent_today(), 0.50, places=6)
        self.assertAlmostEqual(ledger.spent(window_s=3600.0, provider="grok"),
                               0.50, places=6)
        self.assertEqual(ledger.spent(window_s=3600.0, provider="gemini"),
                         0.0)
        self.assertEqual(len(ledger.rows()), 5)

    def test_a_duplicated_key_inside_one_row_is_ambiguous(self):
        """Found by probing after the implementation, not by the witness list.

        `{"api_cost_usd": 1.0, "api_cost_usd": 99.0}` is valid JSON and
        `json.loads` keeps the last value silently -- so this row read as
        $99.00 with nothing recording that $1.00 had equal claim. "Pick the
        last" is an arbitrary rule standing in for a fact we do not have.

        The refusal is the UNPARSEABLE-row one rather than a schema one, and
        deliberately so: a row with two answers for one field and a row with
        no readable answer are the same fact, which is that the row does not
        have a single meaning. What matters is that it reaches `check()` as a
        structured refusal, which `test_every_malformed_row_becomes_a_
        structured_guard_refusal` covers for this shape too.
        """
        path = os.path.join(self._tmp, "dupkey.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"ts": %f, "api_cost_usd": 1.0, "api_cost_usd": 99.0}\n'
                     % alpha_cost._now())
        with self.assertRaises(RuntimeError) as caught:
            BudgetLedger(path).spent_today()
        self.assertNotIsInstance(caught.exception, (ValueError, TypeError))
        self.assertIn("PRESERVED", str(caught.exception))

    def test_one_intent_reserved_twice_for_different_amounts_is_refused(self):
        """An identical duplicate is expected; a conflicting one is not.

        RA-05's append-only retry produces byte-identical duplicate rows, and
        those must resolve to one charge. Two announcements of the SAME intent
        for DIFFERENT amounts cannot both be true, and `intent_id` carries a
        uuid4 so this is corruption rather than coincidence. Picking either
        figure would be inventing one.
        """
        now = alpha_cost._now()

        def reservation(amount):
            return json.dumps({"schema": BUDGET_SCHEMA,
                               "kind": KIND_RESERVATION, "ts": now,
                               "intent_id": "one-intent",
                               "owner_instance": "o", "provider": "p",
                               "model": "m", "reserved_usd": amount})

        same = os.path.join(self._tmp, "same.jsonl")
        with open(same, "w", encoding="utf-8") as fh:
            fh.write(reservation(1.0) + "\n" + reservation(1.0) + "\n")
        self.assertAlmostEqual(BudgetLedger(same).spent_today(), 1.0, places=6,
                               msg="an identical retry became two charges")

        conflicting = os.path.join(self._tmp, "conflict.jsonl")
        with open(conflicting, "w", encoding="utf-8") as fh:
            fh.write(reservation(1.0) + "\n" + reservation(9.0) + "\n")
        with self.assertRaises(BudgetLedgerInvalid):
            BudgetLedger(conflicting).spent_today()

    def test_a_reconciliation_is_terminal(self):
        """A late automatic row must not overturn a human decision.

        An operator reconciles an orphan; a slow response then lands for the
        same intent. Resolving by file order alone let the `actual` win purely
        by arriving second, silently replacing the figure a person chose.
        """
        now = alpha_cost._now()
        path = os.path.join(self._tmp, "terminal.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for row in ({"schema": BUDGET_SCHEMA, "kind": KIND_RESERVATION,
                         "ts": now, "intent_id": "i", "owner_instance": "o",
                         "provider": "p", "model": "m", "reserved_usd": 1.0},
                        {"schema": BUDGET_SCHEMA, "kind": KIND_RECONCILED,
                         "ts": now, "intent_id": "i", "api_cost_usd": 7.0},
                        {"schema": BUDGET_SCHEMA, "kind": KIND_ACTUAL,
                         "ts": now, "intent_id": "i", "api_cost_usd": 0.5}):
                fh.write(json.dumps(row) + "\n")
        self.assertAlmostEqual(
            BudgetLedger(path).spent_today(), 7.0, places=6,
            msg="an actual row arriving after a reconciliation replaced the "
                "figure an operator had deliberately chosen")

    def test_an_out_of_order_settlement_still_resolves(self):
        """Resolution is by identity, not by position in the file."""
        now = alpha_cost._now()
        path = os.path.join(self._tmp, "ooo.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for row in ({"schema": BUDGET_SCHEMA, "kind": KIND_ACTUAL,
                         "ts": now, "intent_id": "i", "api_cost_usd": 0.5},
                        {"schema": BUDGET_SCHEMA, "kind": KIND_RESERVATION,
                         "ts": now, "intent_id": "i", "owner_instance": "o",
                         "provider": "p", "model": "m", "reserved_usd": 9.0}):
                fh.write(json.dumps(row) + "\n")
        self.assertAlmostEqual(BudgetLedger(path).spent_today(), 0.5,
                               places=6)

    def test_the_policy_bound_is_inclusive(self):
        """A stated bound has to be decidable at its edge."""
        for amount, ok in ((alpha_cost.MAX_ROW_USD, True),
                           (alpha_cost.MAX_ROW_USD + 0.01, False)):
            with self.subTest(amount=amount):
                path = os.path.join(self._tmp, f"b{ok}.jsonl")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": alpha_cost._now(),
                                         "api_cost_usd": amount}) + "\n")
                if ok:
                    self.assertAlmostEqual(BudgetLedger(path).spent_today(),
                                           amount, places=2)
                else:
                    with self.assertRaises(BudgetLedgerInvalid):
                        BudgetLedger(path).spent_today()

    def test_a_torn_last_line_is_refused_not_believed(self):
        """AA-12's torn tail, now inside the strict reader."""
        path = os.path.join(self._tmp, "torntail.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": alpha_cost._now(),
                                 "api_cost_usd": 1.0}) + "\n")
            fh.write('{"ts": 1.0, "api_cost')
        with self.assertRaises(RuntimeError):
            BudgetLedger(path).spent_today()

    def test_a_hostile_vendor_figure_cannot_brick_the_ledger(self):
        """Found by probing the IMPLEMENTATION, not by the witness list.

        The reader's strictness can be turned against the ledger.
        `budgeted_cost` prefers the vendor's own `billed_cost_usd`, which a
        third party supplies. A negative or absurd one was written faithfully
        and then REFUSED by the next read -- so every later total was
        unverifiable and the only remedy would have been editing history,
        which this protocol forbids everywhere else. The strict reader would
        have created a denial-of-accounting the lenient one did not have.

        The row is therefore not written at all: the reservation stays OPEN,
        which blocks conservatively, and the real figure goes through
        `reconcile()`.
        """
        for label, billed in (("negative", -5.0),
                              ("absurd", alpha_cost.MAX_ROW_USD * 1000),
                              ("a string", "free"),
                              ("a boolean", True)):
            with self.subTest(billed=label):
                path = os.path.join(self._tmp, f"vendor_{label[:4]}.jsonl")
                with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
                    guard = BudgetGuard(ledger=BudgetLedger(path))
                    verdict = guard.check("synthetic", "m")
                    guard.record_actual({"provider": "synthetic", "model": "m",
                                         "api_cost_usd": 0.01,
                                         "billed_cost_usd": billed,
                                         "intent_id": verdict["intent_id"]})
                    # The ledger must still be readable...
                    try:
                        BudgetLedger(path).spent_today()
                    except RuntimeError as exc:
                        self.fail(f"a {label} vendor figure made the ledger "
                                  f"permanently unverifiable: {exc}")

    def test_an_inadmissible_estimate_refuses_the_call(self):
        """The same hole on the OTHER writer, found by probing.

        `_reserve` wrote whatever `estimate` produced. A NaN figure -- which
        a broken rate card can yield -- was serialized as `NaN`, the call was
        ALLOWED, and the very next read of the ledger was unverifiable for
        good. Refusing is the only safe answer: a call whose cost cannot even
        be announced must not be made, and then nothing is spent.
        """
        path = os.path.join(self._tmp, "nan_estimate.jsonl")
        with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
            guard = BudgetGuard(ledger=BudgetLedger(path))
            hostile = {"api_cost_usd": float("nan"), "cost_priced": True,
                       "pricing_missing_reason": ""}
            with patch.object(type(guard.pricing), "estimate",
                              return_value=hostile):
                verdict = guard.check("synthetic", "m")
            self.assertFalse(verdict["allowed"],
                             "a call was allowed whose cost could not be "
                             "announced in a form the ledger can read back")
            if os.path.exists(path):
                BudgetLedger(path).spent_today()   # must not raise

    def test_an_inadmissible_cost_blocks_rather_than_being_silently_dropped(self):
        """Refusing to WRITE must not become refusing to NOTICE."""
        path = os.path.join(self._tmp, "inadmissible.jsonl")
        with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
            guard = BudgetGuard(ledger=BudgetLedger(path))
            verdict = guard.check("synthetic", "m")
            guard.record_actual({"provider": "synthetic", "model": "m",
                                 "api_cost_usd": 0.01,
                                 "billed_cost_usd": -5.0,
                                 "intent_id": verdict["intent_id"]})
            state = guard.accounting_state()
            self.assertFalse(state["certain"],
                             "an unrecordable charge was dropped quietly")
            self.assertFalse(guard.check("synthetic", "m")["allowed"])
            # And it survives the restart, because the reservation is open.
            with patch.object(alpha_cost, "INSTANCE_ID", "later-instance"):
                fresh = BudgetGuard(ledger=BudgetLedger(path))
                self.assertFalse(fresh.accounting_state()["certain"])

    def test_no_writer_can_append_a_row_this_reader_would_refuse(self):
        """The general form, swept across EVERY writer after implementation.

        The strict reader can be turned against the ledger: one inadmissible
        row makes every later total unverifiable, and the only remedy --
        editing history -- is forbidden everywhere else in this protocol. The
        first version guarded `record_actual` and `_reserve` individually and
        still left `void_reservation("")` and `reconcile(NaN)` able to brick
        the file permanently, which is why the guard now sits at the one
        choke point every writer goes through.

        Each refusal fails closed in the right direction at its own caller:
        the call is not made, the latch is set, or the reservation simply
        stays open.
        """
        hostile = (
            ("void with a blank reason", lambda g, i: g.void_reservation(i, "")),
            ("void with no reason", lambda g, i: g.void_reservation(i, None)),
            ("reconcile to NaN", lambda g, i: g.reconcile(i, float("nan"))),
            ("reconcile past the bound",
             lambda g, i: g.reconcile(i, alpha_cost.MAX_ROW_USD * 1000)),
        )
        for label, act in hostile:
            with self.subTest(writer=label):
                path = os.path.join(self._tmp, f"w{abs(hash(label)) % 10**6}.jsonl")
                with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
                    guard = BudgetGuard(ledger=BudgetLedger(path))
                    verdict = guard.check("synthetic", "m")
                    self.assertFalse(act(guard, verdict["intent_id"]),
                                     f"{label} reported success")
                try:
                    BudgetLedger(path).spent_today()
                except RuntimeError as exc:
                    self.fail(f"{label} made the ledger permanently "
                              f"unverifiable: {exc}")

        # The control: the legitimate forms of both writers still work.
        path = os.path.join(self._tmp, "legit.jsonl")
        with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
            guard = BudgetGuard(ledger=BudgetLedger(path))
            first = guard.check("synthetic", "a")
            self.assertTrue(guard.void_reservation(first["intent_id"],
                                                   "provider not contacted"))
            second = guard.check("synthetic", "b")
            self.assertTrue(guard.reconcile(second["intent_id"], 1.5))
        self.assertAlmostEqual(BudgetLedger(path).spent_today(), 1.5,
                               places=6)

    def test_a_zero_cost_row_is_valid(self):
        """Zero is a price. Only ABSENT is unknown."""
        path = os.path.join(self._tmp, "zero.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": alpha_cost._now(),
                                 "api_cost_usd": 0.0}) + "\n")
        self.assertEqual(BudgetLedger(path).spent_today(), 0.0)

    def test_an_empty_or_absent_ledger_is_genuinely_zero(self):
        """The one case where zero is the honest answer."""
        absent = BudgetLedger(os.path.join(self._tmp, "nothing.jsonl"))
        self.assertEqual(absent.spent_today(), 0.0)
        self.assertEqual(absent.rows(), [])
        path = os.path.join(self._tmp, "empty.jsonl")
        open(path, "w", encoding="utf-8").close()
        self.assertEqual(BudgetLedger(path).spent_today(), 0.0)


# ════════════════════════════════════════════════════════════════════════
# The three findings are one protocol
# ════════════════════════════════════════════════════════════════════════
class V4RA05to07_TheProtocolsAgree(AlphaCase):
    """Patched separately, these three produce contradictory semantics.

    The cases here are the interactions, asserted rather than inferred from
    three passing halves. See `docs/design/budget-durability-protocol.md`.
    """

    def setUp(self):
        super().setUp()
        _forget_durability()
        self.addCleanup(_forget_durability)
        self._patches.append(patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS",
                                          True))
        self._patches[-1].start()

    def test_an_unprovable_announcement_does_not_permit_a_call(self):
        """RA-05 meets RA-06.

        The reservation goes through `serialized_append`, so a pathname whose
        directory barrier cannot be confirmed makes the announcement raise
        `DurabilityUnknown`. An announcement we cannot prove we made is not an
        announcement, and `check` must refuse rather than dispatch against it.
        """
        real_fsync = os.fsync

        def dir_fsync_fails(fd):
            import stat as stat_module
            try:
                if stat_module.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError(5, "EIO synthetic: cannot fsync directory")
            except OSError as exc:
                if "synthetic" in str(exc):
                    raise
            return real_fsync(fd)

        guard = BudgetGuard(ledger=BudgetLedger(
            os.path.join(self._tmp, "unprovable", "budget.jsonl")))
        with patch("os.fsync", side_effect=dir_fsync_fails):
            verdict = guard.check("synthetic", "m")
        self.assertFalse(
            verdict["allowed"],
            "a provider call was permitted against an announcement whose "
            "pathname durability was never established")
        self.assertIn("could not be ANNOUNCED", verdict["detail"])

    def test_an_unverifiable_ledger_blocks_before_any_reservation(self):
        """RA-07 meets RA-06: uncertainty is decided before announcing."""
        path = os.path.join(self._tmp, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('[1, 2, 3]\n')
        guard = BudgetGuard(ledger=BudgetLedger(path))
        verdict = guard.check("synthetic", "m")
        self.assertFalse(verdict["allowed"])
        # And nothing was appended to a file we cannot read.
        self.assertEqual(open(path, encoding="utf-8").read(), '[1, 2, 3]\n')

    def test_a_retried_announcement_is_one_charge_not_two(self):
        """RA-05's append-only retry meets RA-06's identity resolution.

        A barrier failure leaves the row on disk and raises, so the retry
        appends again. Summing rows would double-charge; resolving intents
        does not.
        """
        path = os.path.join(self._tmp, "retry", "budget.jsonl")
        row = {"schema": BUDGET_SCHEMA, "kind": KIND_RESERVATION,
               "intent_id": "duplicated-intent",
               "owner_instance": alpha_cost.INSTANCE_ID,
               "provider": "synthetic", "model": "m", "reserved_usd": 0.40,
               "ts": alpha_cost._now(), "at": alpha_cost._iso()}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for _ in range(3):                      # the same row, three times
                fh.write(json.dumps(row) + "\n")
        ledger = BudgetLedger(path)
        self.assertAlmostEqual(ledger.spent_today(), 0.40, places=6,
                               msg="one announcement appended three times by "
                                   "a durability retry became three charges")
        self.assertEqual(len(ledger.resolve()["open"]), 1)

    def test_no_state_turns_unknown_into_zero(self):
        """The rule all three findings share, as one assertion.

        Money incurred, cost row unwritable, process restarted, and a NEW DAY
        begun. Not one of those transitions may produce a spend of zero with
        admission restored.
        """
        path = os.path.join(self._tmp, "invariant.jsonl")
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1.0):
            guard = BudgetGuard(ledger=BudgetLedger(path))
            verdict = guard.check("synthetic", "m")
            self.assertTrue(verdict["allowed"])
            with patch.object(BudgetLedger, "record",
                              side_effect=OSError(5, "EIO synthetic")):
                guard.record_actual({"provider": "synthetic", "model": "m",
                                     "api_cost_usd": 2.0,
                                     "intent_id": verdict["intent_id"]})
            # Age the announcement into a previous day, then restart.
            lines = open(path, encoding="utf-8").read().splitlines()
            row = json.loads(lines[0])
            row["ts"] = alpha_cost._now() - 86400.0 * 3
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            with patch.object(alpha_cost, "INSTANCE_ID", "another-instance"):
                fresh = BudgetGuard(ledger=BudgetLedger(path))
                self.assertEqual(fresh.ledger.spent_today(), 0.0)
                state = fresh.accounting_state()
                self.assertFalse(
                    state["certain"],
                    "spend read as zero and accounting read as certain after "
                    "a restart and a day boundary, for money that really was "
                    "spent")
                self.assertFalse(fresh.check("synthetic", "m")["allowed"])


# ════════════════════════════════════════════════════════════════════════
# Batch A (V4-RA-01..04) and the SHADOW_ONLY boundary are untouched
# ════════════════════════════════════════════════════════════════════════
class V4_BatchAAndTheBoundaryAreIntact(unittest.TestCase):

    def test_the_producer_import_boundary_still_holds(self):
        from test_research_feed_boundary import \
            TheProducerKnowsNothingAboutResearch as Pinned
        suite = unittest.TestLoader().loadTestsFromTestCase(Pinned)
        with open(os.devnull, "w") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
        self.assertEqual((len(result.failures), len(result.errors)), (0, 0),
                         f"{result.failures + result.errors}")

    def test_durable_append_grew_no_import(self):
        """The pinned allow-list for this module, checked directly.

        V4-RA-05 was implemented WITHOUT importing `threading`: the proof
        registry needs no lock, because losing a race costs a redundant
        barrier and never a skipped one. Widening a pinned boundary to buy
        nothing is not a trade worth making, and this asserts it was not made.
        """
        import ast
        from test_research_feed_boundary import NEUTRAL_MODULES, _imported_roots
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "durable_append.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        allowed = NEUTRAL_MODULES["durable_append.py"]
        self.assertEqual(sorted(_imported_roots(tree) - allowed), [])

    def test_no_execution_or_capital_symbol_reached_the_accounting_path(self):
        import ast
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        forbidden = {"create_order", "place_and_track", "submit_order",
                     "set_capital", "enable_capital", "capital_eligible",
                     "OrderManager", "RiskManager", "KalshiClient"}
        for name in ("alpha_cost.py", "durable_append.py"):
            with self.subTest(module=name):
                with open(os.path.join(repo, name), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                used = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        used.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        used.add(node.attr)
                self.assertEqual(sorted(used & forbidden), [])


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
