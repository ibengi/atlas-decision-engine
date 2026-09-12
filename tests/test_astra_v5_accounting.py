"""V4-RA-06/07: pre-dispatch accounting obligations and strict replay.

All providers below are synthetic labels. No transport or provider is invoked.
The original unreserved post-spend witness is retained as explicit API refusal:
accounting a call that lacked a durable pre-intent is intrinsically unsupported.
The reserved version exercises the same zero-byte/EIO failures across restart.
"""
import errno
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from alpha_cost import BudgetGuard, BudgetLedger
from config import CFG


class SyntheticPricing:
    version = "synthetic-v5"
    error = None

    def configured_models(self):
        return ["synthetic/synthetic"]

    def expired_models(self):
        return []

    def __init__(self, amount=.1):
        self.amount = amount

    def estimate(self, *args, **kwargs):
        return {"api_cost_usd": self.amount, "cost_priced": True}


def capped():
    stack = ExitStack()
    for name in ("ALPHA_MAX_COST_PER_DAY_USD", "ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD",
                 "ALPHA_MAX_COST_PER_ANALYSIS_USD"):
        stack.enter_context(patch.object(CFG, name, 1.0))
    return stack


def guard_at(path, amount=.1):
    return BudgetGuard(SyntheticPricing(amount), BudgetLedger(str(path)))


def fresh_verdict(path, reserve=False, fsync_fail=False):
    code = '''import json,sys
from alpha_cost import BudgetGuard,BudgetLedger
from config import CFG
class Pricing:
 def estimate(self,*a,**kw): return {"api_cost_usd":.1,"cost_priced":True}
for key in ("ALPHA_MAX_COST_PER_DAY_USD","ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD","ALPHA_MAX_COST_PER_ANALYSIS_USD"): setattr(CFG,key,1.)
g=BudgetGuard(Pricing(),BudgetLedger(sys.argv[1]))
if sys.argv[3]=="1":
 import os
 def unavailable(fd): raise OSError("persistent synthetic synchronization outage")
 os.fsync=unavailable
print(json.dumps(g.reserve("peer","synthetic",reservation_group="same-snapshot") if sys.argv[2]=="1" else g.check("synthetic","synthetic")))
'''
    proc = subprocess.run([sys.executable, "-c", code, str(path), str(int(reserve)), str(int(fsync_fail))],
                          capture_output=True, text=True, timeout=15)
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def admission_worker(path, ready, start, result):
    guard = guard_at(path, .6)
    ready.put(True)
    if not start.wait(10):
        raise RuntimeError("synthetic admission barrier expired")
    with capped():
        result.put(guard.reserve("synthetic", "synthetic", reservation_group="same-snapshot"))


class _BudgetFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-v5-budget-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "budget.jsonl"
        self.guard = guard_at(self.path)
        self.caps = capped()
        self.caps.__enter__()
        self.addCleanup(self.caps.close)

    def reserve(self, guard=None, **kw):
        verdict = (guard or self.guard).reserve("synthetic", "synthetic", **kw)
        self.assertTrue(verdict["allowed"], verdict)
        return verdict["reservation_id"]

    def usage(self, rid, amount=.2, guard=None, **kw):
        return (guard or self.guard).record_actual(
            {"provider": "synthetic", "model": "synthetic", "api_cost_usd": amount,
             "cost_priced": True, **kw}, reservation_id=rid)


class AccountingDurabilityV5(_BudgetFixture):
    def test_original_unreserved_completion_is_explicitly_refused(self):
        with patch("os.write", return_value=0):
            result = self.guard.record_actual({"provider": "synthetic", "model": "synthetic",
                                              "api_cost_usd": 2., "cost_priced": True})
        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "reservation_required")
        self.assertFalse(self.path.exists())

    def test_zero_write_after_reserved_spend_blocks_fresh_process(self):
        rid = self.reserve()
        with patch("os.write", return_value=0):
            self.assertFalse(self.usage(rid, 2.)["recorded"])
        self.assertFalse(self.guard.check("synthetic", "synthetic")["allowed"])
        self.assertFalse(fresh_verdict(self.path)["allowed"])
        self.assertEqual(len(self.guard.ledger.rows()), 1)

    def test_eio_after_reserved_spend_blocks_fresh_process(self):
        rid = self.reserve()
        with patch("os.write", side_effect=OSError(errno.EIO, "synthetic device failure")):
            self.assertFalse(self.usage(rid, 2.)["recorded"])
        self.assertFalse(fresh_verdict(self.path)["allowed"])
        self.assertEqual(self.guard.ledger.rows()[0]["event"], "RESERVATION")

    def test_six_failed_polls_and_restart_never_create_authority(self):
        rid = self.reserve()
        original = os.fsync
        def fail_usage(fd):
            # Permit the initial read barrier; fail fsync only after USAGE bytes exist.
            if b'"USAGE"' in self.path.read_bytes():
                raise OSError(errno.EIO, "persistent synthetic outage")
            return original(fd)
        with patch("os.fsync", side_effect=fail_usage):
            self.assertFalse(self.usage(rid)["recorded"])
            for _ in range(6):
                restarted = guard_at(self.path)
                self.assertFalse(restarted.check("synthetic", "synthetic")["allowed"])
                self.assertFalse(restarted.reserve("peer", "synthetic")["allowed"])
                self.assertFalse(fresh_verdict(self.path, fsync_fail=True)["allowed"])
        self.assertTrue(fresh_verdict(self.path)["allowed"])
        self.assertEqual(self.guard.ledger.spent_today(), .2)

    def test_short_write_and_eintr_completion_is_complete_and_once(self):
        rid = self.reserve()
        original = os.write
        calls = [0]
        def interrupted_short(fd, payload):
            calls[0] += 1
            if calls[0] < 3:
                raise InterruptedError(errno.EINTR, "synthetic interruption")
            return original(fd, payload[:7])
        with patch("os.write", side_effect=interrupted_short):
            self.assertTrue(self.usage(rid)["recorded"])
        self.assertGreater(calls[0], 3)
        self.assertEqual(len(self.guard.ledger.rows()), 2)
        self.assertEqual(self.guard.ledger.spent_today(), .2)
        self.assertTrue(fresh_verdict(self.path)["allowed"])

    def test_process_death_after_reservation_preserves_obligation(self):
        code = """import os,sys
from alpha_cost import BudgetGuard,BudgetLedger
class Pricing:
 def estimate(self,*a,**kw):return {"api_cost_usd":.001,"cost_priced":True}
g=BudgetGuard(Pricing(),BudgetLedger(sys.argv[1]))
r=g.reserve("synthetic","synthetic",reservation_group="same-snapshot")
os._exit(0 if r["allowed"] else 2)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(fresh_verdict(self.path, reserve=True)["allowed"])

    def test_pending_same_snapshot_different_provider_blocks_restart(self):
        self.reserve(reservation_group="same-snapshot")
        self.assertFalse(fresh_verdict(self.path, reserve=True)["allowed"])

    def test_health_reports_pending_and_unreadable_budget_as_unavailable(self):
        self.assertFalse(self.guard.snapshot()["exhausted"])
        self.reserve()
        report = guard_at(self.path).snapshot()
        self.assertTrue(report["exhausted"])
        self.assertTrue(report["accounting_uncertain"])
        self.assertEqual(report["unresolved_reservations"], 1)
        with patch("os.fsync", side_effect=OSError(errno.EIO, "storage unavailable")):
            report = self.guard.snapshot()
        self.assertTrue(report["exhausted"])
        self.assertTrue(report["accounting_uncertain"])
        self.assertIn("error", report)

    def test_failed_usage_blocks_even_same_group_peer_admission(self):
        rid = self.reserve(reservation_group="analysis")
        with patch("os.write", return_value=0):
            self.assertFalse(self.usage(rid)["recorded"])
        self.assertFalse(self.guard.reserve("peer", "model", reservation_group="analysis")["allowed"])

    def test_timeout_obligation_does_not_expire_with_budget_window(self):
        self.reserve(reservation_group="old-analysis")
        with patch("alpha_cost._now", return_value=time.time()+172800):
            self.assertFalse(self.guard.reserve("peer", "synthetic",
                                                reservation_group="new-analysis")["allowed"])

    def test_parallel_peers_of_same_live_analysis_are_reserved(self):
        self.reserve(reservation_group="analysis")
        peer = self.guard.reserve("peer", "model", reservation_group="analysis")
        self.assertTrue(peer["allowed"], peer)
        self.assertAlmostEqual(self.guard.ledger.spent_today(), .2)

    def test_analysis_cap_uses_ledger_liability_not_caller_counter(self):
        self.guard = guard_at(self.path, .6)
        self.reserve(reservation_group="analysis")
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 10.), \
             patch.object(CFG, "ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD", 10.):
            peer = self.guard.reserve("peer", "model", reservation_group="analysis",
                                      analysis_spent_usd=0.)
        self.assertFalse(peer["allowed"])
        self.assertIn("per-analysis", peer["detail"])

    def test_same_provider_cannot_duplicate_pending_reservation(self):
        self.reserve(reservation_group="analysis")
        again = self.guard.reserve("synthetic", "synthetic", reservation_group="analysis")
        self.assertFalse(again["allowed"])
        self.assertEqual(len(self.guard.ledger.rows()), 1)

    def test_two_consecutive_accounted_calls_are_possible(self):
        first = self.reserve()
        self.assertTrue(self.usage(first)["recorded"])
        second = self.reserve()
        self.assertNotEqual(first, second)
        self.assertTrue(self.usage(second, .3)["recorded"])
        self.assertEqual(self.guard.ledger.spent_today(), .5)
        self.assertTrue(fresh_verdict(self.path)["allowed"])

    def test_identical_usage_replay_is_idempotent(self):
        rid = self.reserve()
        self.assertTrue(self.usage(rid)["recorded"])
        before = self.path.read_bytes()
        self.assertTrue(self.usage(rid)["recorded"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.usage(rid, .3)["recorded"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_usage_identity_mismatch_keeps_obligation(self):
        rid = self.reserve()
        bad = self.guard.record_actual({"provider": "other", "model": "synthetic",
                                       "api_cost_usd": .2}, reservation_id=rid)
        self.assertFalse(bad["recorded"])
        self.assertFalse(fresh_verdict(self.path)["allowed"])

    def test_failed_reservation_barrier_permits_no_dispatch(self):
        for _ in range(6):
            with patch("os.fsync", side_effect=OSError(errno.EIO, "synthetic outage")):
                self.assertFalse(guard_at(self.path).reserve("synthetic", "synthetic")["allowed"])
        # A visible but never-admitted reservation is conservative orphan state.
        self.assertFalse(fresh_verdict(self.path)["allowed"])

    def test_concurrent_process_admission_serializes_cap(self):
        ctx = multiprocessing.get_context("fork")
        ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=admission_worker,
                                 args=(str(self.path), ready, start, output)) for _ in range(2)]
        for process in processes:
            process.start()
        try:
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            results = [output.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
            self.assertEqual([p.exitcode for p in processes], [0, 0])
            self.assertEqual(sum(r["allowed"] for r in results), 1, results)
            self.assertEqual(self.guard.ledger.spent_today(), .6)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
            ready.close()
            output.close()

    def test_forked_guard_cannot_inherit_reservation_authority(self):
        self.reserve(reservation_group="analysis")
        with patch("alpha_cost.os.getpid", return_value=os.getpid()+1):
            verdict = self.guard.reserve("peer", "model", reservation_group="analysis")
        self.assertFalse(verdict["allowed"])


class StrictBudgetHistoryV5(_BudgetFixture):
    def test_original_parseable_malformed_rows_refuse_after_restart(self):
        now = time.time()
        rows = [[], [dict(ts=now, api_cost_usd=2.)],
                {"ts": now, "provider": "synthetic"},
                {"ts": now, "provider": "synthetic", "api_cost_usd": None},
                {"provider": "synthetic", "api_cost_usd": 2.},
                {"ts": "invalid", "provider": "synthetic", "api_cost_usd": 2.},
                {"ts": now, "provider": "synthetic", "api_cost_usd": -2.}]
        for row in rows:
            with self.subTest(row=row):
                raw = (json.dumps(row)+"\n").encode()
                self.path.write_bytes(raw)
                self.assertFalse(fresh_verdict(self.path)["allowed"])
                self.assertEqual(self.path.read_bytes(), raw)

    def test_neighbor_types_huge_values_and_invalid_utf8_refuse(self):
        base = {"ts": time.time(), "provider": "synthetic", "api_cost_usd": .2}
        variants = [{**base, key: value} for key, values in (
            ("ts", [True, None, 10**500]),
            ("api_cost_usd", [True, "0", float("nan"), float("inf"), 10**500]),
            ("provider", [True, 1, "", []]),
            ("input_tokens", [True, "10", -1]),
            ("cost_priced", ["false", 1]),
            ("schema", ["wrong-version"])) for value in values]
        for row in variants:
            with self.subTest(row=row):
                self.path.write_text(json.dumps(row)+"\n")
                self.assertFalse(guard_at(self.path).check("synthetic", "synthetic")["allowed"])
        self.path.write_bytes(b"\xff\n")
        self.assertFalse(guard_at(self.path).check("synthetic", "synthetic")["allowed"])

    def test_valid_row_after_invalid_history_does_not_repair_spend(self):
        raw = b'{"provider":"synthetic","api_cost_usd":null}\n'
        self.path.write_bytes(raw)
        BudgetLedger(str(self.path)).record({"provider": "synthetic", "api_cost_usd": .2})
        self.assertTrue(self.path.read_bytes().startswith(raw))
        self.assertFalse(fresh_verdict(self.path)["allowed"])

    def test_completion_schema_and_identity_replay_are_strict(self):
        rid = self.reserve()
        self.assertTrue(self.usage(rid)["recorded"])
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        for key, bad in (("reservation_id", "other"), ("owner_id", "other"),
                         ("owner_pid", True), ("model", "other"),
                         ("reservation_group", "other"), ("event", "COMPLETE")):
            with self.subTest(key=key):
                altered = [rows[0], {**rows[1], key: bad}]
                self.path.write_text("".join(json.dumps(row)+"\n" for row in altered))
                self.assertFalse(fresh_verdict(self.path)["allowed"])

    def test_vendor_cost_and_effective_charge_must_agree(self):
        base = {"ts": time.time(), "provider": "synthetic", "api_cost_usd": 0.}
        for extra in ({"billed_cost_usd": 2., "cost_source": "vendor_billed"},
                      {"billed_cost_usd": 2.},
                      {"cost_source": "vendor_billed"},
                      {"billed_cost_usd": 0., "cost_source": "rate_card_estimate"},
                      {"cost_source": True}, {"cost_source": ["vendor_billed"]}):
            with self.subTest(extra=extra):
                self.path.write_text(json.dumps({**base, **extra})+"\n")
                before = self.path.read_bytes()
                self.assertFalse(guard_at(self.path).reserve("synthetic", "synthetic")["allowed"])
                self.assertFalse(fresh_verdict(self.path)["allowed"])
                self.assertEqual(self.path.read_bytes(), before)

    def test_untagged_event_members_cannot_downgrade_to_legacy_usage(self):
        base = {"ts": time.time(), "provider": "synthetic", "api_cost_usd": 0.}
        for key, value in (("reservation_id", "reservation-one"),
                           ("owner_id", "previous-process"), ("owner_pid", 123),
                           ("reservation_group", "analysis-one")):
            with self.subTest(key=key):
                self.path.write_text(json.dumps({**base, key: value})+"\n")
                self.assertFalse(guard_at(self.path).reserve("synthetic", "synthetic")["allowed"])
                self.assertFalse(fresh_verdict(self.path)["allowed"])

    def test_append_after_barrier_is_not_promoted_to_confirmed_usage(self):
        self.reserve()
        reserved = self.guard.ledger.rows()[0]
        completed = {**reserved, "event": "USAGE", "api_cost_usd": .2}
        import alpha_cost
        original = alpha_cost.sync_path
        appended = [False]
        def interleaved(target):
            result = original(target)
            if not appended[0]:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(completed)+"\n")
                appended[0] = True
            return result
        with patch("alpha_cost.sync_path", side_effect=interleaved):
            result = guard_at(self.path).check("synthetic", "synthetic")
        self.assertFalse(result["allowed"])
        self.assertIn("changed during", result["detail"])
        self.assertTrue(fresh_verdict(self.path)["allowed"])

    def test_changed_durable_reservation_cannot_be_acknowledged(self):
        rid = self.reserve()
        row = json.loads(self.path.read_text())
        row["api_cost_usd"] = .05
        self.path.write_text(json.dumps(row)+"\n")
        before = self.path.read_bytes()
        self.assertFalse(self.usage(rid)["recorded"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(fresh_verdict(self.path)["allowed"])

    def test_metadata_uncertainty_between_synchronization_and_read_refuses(self):
        self.guard.ledger.record({"provider": "synthetic", "api_cost_usd": .2})
        original = os.stat
        def inconsistent(path, *args, **kw):
            if os.fspath(path) == str(self.path):
                raise PermissionError(errno.EACCES, "synthetic metadata uncertainty")
            return original(path, *args, **kw)
        with patch("os.stat", side_effect=inconsistent):
            result = self.guard.check("synthetic", "synthetic")
        self.assertFalse(result["allowed"])
        self.assertTrue(fresh_verdict(self.path)["allowed"])


if __name__ == "__main__":
    unittest.main()
