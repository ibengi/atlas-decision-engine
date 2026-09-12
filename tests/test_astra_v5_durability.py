"""V4-RA-05/08/09/10: actual barriers, exact identity, append-only recovery.

All files are disposable, all providers are in-process counters. The original
v4 counterexamples are retained alongside neighboring faults and real process
restart/death. Readability is deliberately witnessed before every refusal.
"""
import copy
import errno
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests import _gates  # noqa: F401
from alpha_ledger import AlphaLedger, LedgerError, analysis_identity
import durable_append


class _Budget:
    pricing = None
    def check(self, *args, **kwargs):
        return {"allowed": True, "estimated_cost_usd": 0, "reason": "synthetic"}
    def reserve(self, *args, **kwargs):
        return {**self.check(), "reservation_id": "synthetic-reservation"}
    def record_actual(self, *args, **kwargs):
        pass
    def snapshot(self):
        return {}


class _Telemetry:
    def __init__(self):
        self.errors = []
    def record_error(self, error):
        self.errors.append(error)
    def incr(self, *args):
        pass
    def record_state(self, *args):
        pass
    def record_signal(self, *args):
        pass
    def flush(self, *args):
        pass


def _provider():
    from alpha_providers import AlphaProvider
    class LocalCounter(AlphaProvider):
        name = "v5-local-counter"
        env_key = None
        def __init__(self):
            self.calls = 0
            super().__init__()
        def default_model(self):
            return "synthetic"
        def configured(self):
            return True
        def analyze(self, snapshot, timeout):
            self.calls += 1
            return None, {"provider": self.name, "model": self.model,
                          "latency_ms": 0, "cost": {"api_cost_usd": 0,
                          "cost_priced": False}, "error": "synthetic exclusion"}
    return LocalCounter()


def _crash_with_lock(path):
    with durable_append.exclusive_lock(path):
        os._exit(73)


def _try_alias_lock(path, result):
    try:
        with durable_append.exclusive_lock(path, timeout=0.04):
            result.put("acquired")
    except (OSError, TimeoutError):
        result.put("refused")


class V5DurabilityCase(unittest.TestCase):
    def setUp(self):
        from config import CFG
        self.temp = tempfile.TemporaryDirectory(prefix="atlas-v5-durability-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(CFG, "DATA_DIR", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ledger = self.new_ledger()

    def new_ledger(self):
        return AlphaLedger(str(self.root / "predictions.jsonl"),
                           str(self.root / "costs.jsonl"))

    def prediction(self, pid="p-original", sid="snapshot-one", **extra):
        return {"prediction_id": pid, "market_snapshot_id": sid,
                "contract_id": "SYNTHETIC", "executed": False, **extra}

    def fail_ledger_sync(self):
        original = os.fsync
        def sync(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == self.ledger.log.path:
                raise OSError(errno.EIO, "synthetic ledger synchronization failure")
            return original(fd)
        return patch.object(os, "fsync", side_effect=sync)

    def service(self, provider, record=None):
        from tests._candidate import valid_record
        from alpha_consumer import SpoolConsumer, ProcessedStore
        from alpha_service import AlphaShadowService
        record = record or valid_record()
        class LocalSource:
            kind = "synthetic"
            def records(self):
                return [copy.deepcopy(record)]
            def describe(self):
                return {"transport": "synthetic"}
        consumer = SpoolConsumer(source=LocalSource(),
                                 store=ProcessedStore(str(self.root / "processed.jsonl")))
        service = AlphaShadowService(providers=[provider], ledger=self.ledger,
                                     consumer=consumer, budget=_Budget(), telemetry=_Telemetry())
        return service, record

    def fresh_failure_check(self, method, argument):
        script = '''
import json, os, sys
from unittest.mock import patch
from alpha_ledger import AlphaLedger, LedgerError
ledger = AlphaLedger(sys.argv[1], sys.argv[1] + '.cost')
original = os.fsync
def fail(fd):
    if os.readlink('/proc/self/fd/' + str(fd)) == sys.argv[1]:
        raise OSError(5, 'synthetic persistent failure after restart')
    return original(fd)
with patch.object(os, 'fsync', side_effect=fail):
    try:
        result = getattr(ledger, sys.argv[2])(sys.argv[3])
        print(json.dumps({'authorized': bool(result), 'refused': False}))
    except LedgerError:
        print(json.dumps({'authorized': False, 'refused': True}))
'''
        env = {key: os.environ[key] for key in
               ("PATH", "PYTHONPATH", "DATA_DIR", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE")
               if key in os.environ}
        child = subprocess.run([sys.executable, "-c", script, self.ledger.log.path,
                                method, argument], env=env, capture_output=True,
                               text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout)


class V4RA05DirectoryRetry(V5DurabilityCase):
    def test_original_persistent_directory_fsync_failure_never_acknowledges_retry(self):
        original = os.fsync
        def fail(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "synthetic directory barrier failure")
            return original(fd)
        path = str(self.root / "append.jsonl")
        with patch.object(os, "fsync", side_effect=fail):
            for number in range(7):
                with self.assertRaises(OSError):
                    with durable_append.serialized_append(path) as append:
                        append(json.dumps({"attempt": number}))
                self.assertEqual(len(Path(path).read_text().splitlines()), number + 1)

    def test_original_persistent_directory_open_failure_never_acknowledges_retry(self):
        original = os.open
        def fail(path, *args, **kwargs):
            if os.fspath(path) == str(self.root):
                raise OSError(errno.EIO, "synthetic directory open failure")
            return original(path, *args, **kwargs)
        path = str(self.root / "append.jsonl")
        with patch.object(os, "open", side_effect=fail):
            for number in range(6):
                with self.assertRaises(OSError):
                    with durable_append.serialized_append(path) as append:
                        append(json.dumps({"attempt": number}))

    def test_recovered_directory_barrier_preserves_all_prior_bytes(self):
        path = str(self.root / "append.jsonl")
        with patch.object(durable_append, "fsync_directory", side_effect=OSError(5, "synthetic")):
            with self.assertRaises(OSError):
                with durable_append.serialized_append(path) as append:
                    append('{"first":true}')
        before = Path(path).read_bytes()
        barriers = []
        original = os.fsync
        def watched(fd):
            barriers.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return original(fd)
        with patch.object(os, "fsync", side_effect=watched):
            with durable_append.serialized_append(path) as append:
                append('{"second":true}')
        self.assertEqual(barriers[0], "file")
        self.assertTrue(barriers[1:] and all(value == "directory" for value in barriers[1:]))
        self.assertTrue(Path(path).read_bytes().startswith(before))

    def test_sync_path_never_turns_unreadable_storage_into_absence(self):
        with patch.object(os, "open", side_effect=PermissionError(errno.EACCES, "synthetic")):
            with self.assertRaises(durable_append.DurabilityUnknown):
                durable_append.sync_path(str(self.root / "missing"))

    def test_new_nested_parent_names_require_ancestor_durability_on_every_retry(self):
        path = str(self.root / "new-parent" / "nested" / "rows.jsonl")
        original = durable_append.fsync_directory
        def fail(parent):
            if parent == str(self.root):
                raise OSError(errno.EIO, "synthetic unsynchronized parent name")
            return original(parent)
        with patch.object(durable_append, "fsync_directory", side_effect=fail):
            for number in range(6):
                with self.assertRaises(OSError):
                    with durable_append.serialized_append(path) as append:
                        append(json.dumps({"attempt": number}))
        self.assertEqual(len(Path(path).read_text().splitlines()), 6)

    def sibling_alias(self):
        target_dir = self.root / "actual-target-parent"
        alias_dir = self.root / "configured-alias-parent"
        target_dir.mkdir()
        alias_dir.mkdir()
        target = target_dir / "history.jsonl"
        target.write_text('{"prior":true}\n')
        alias = alias_dir / "history.jsonl"
        alias.symlink_to(target)
        return target_dir, alias

    def test_symlink_append_requires_actual_target_parent_barrier(self):
        target_dir, alias = self.sibling_alias()
        original = durable_append.fsync_directory
        def fail(parent):
            if parent == str(target_dir):
                raise OSError(errno.EIO, "synthetic target name uncertainty")
            return original(parent)
        with patch.object(durable_append, "fsync_directory", side_effect=fail):
            with self.assertRaises(OSError):
                with durable_append.serialized_append(str(alias)) as append:
                    append('{"next":true}')

    def test_symlink_recovery_requires_actual_target_parent_barrier(self):
        target_dir, alias = self.sibling_alias()
        original = durable_append.fsync_directory
        def fail(parent):
            if parent == str(target_dir):
                raise OSError(errno.EIO, "synthetic target name uncertainty")
            return original(parent)
        with patch.object(durable_append, "fsync_directory", side_effect=fail):
            with self.assertRaises(OSError):
                with durable_append.exclusive_lock(str(alias)):
                    durable_append.sync_path(str(alias))


class V4RA08CommitDurability(V5DurabilityCase):
    def seed_readable_failed_commit(self):
        with self.fail_ledger_sync():
            with self.assertRaises(LedgerError):
                self.ledger.record_prediction(self.prediction())
            with self.assertRaises(LedgerError):
                self.ledger.record_prediction(self.prediction(pid="never-written"))
        kinds = [row["kind"] for row in self.ledger.rows()]
        self.assertEqual(kinds, ["PREDICTION", "COMMIT"])

    def test_original_failed_prediction_and_failed_commit_remain_nonterminal(self):
        self.seed_readable_failed_commit()
        with self.fail_ledger_sync():
            for _ in range(7):
                with self.assertRaises(LedgerError):
                    self.ledger.committed_prediction("snapshot-one")
                with self.assertRaises(LedgerError):
                    self.new_ledger().committed_prediction("snapshot-one")
        self.assertEqual(len(self.ledger.predictions()), 1)
        self.assertEqual(len(self.ledger.rows()), 2, "recovery grew a receipt chain")

    def test_failed_commit_remains_nonterminal_after_real_process_restart(self):
        self.seed_readable_failed_commit()
        result = self.fresh_failure_check("committed_prediction", "snapshot-one")
        self.assertFalse(result["authorized"])
        self.assertTrue(result["refused"])

    def test_actual_successful_barrier_recovers_original_identity_without_append(self):
        self.seed_readable_failed_commit()
        before = Path(self.ledger.log.path).read_bytes()
        result = self.new_ledger().committed_prediction("snapshot-one")
        self.assertEqual(result["prediction_id"], "p-original")
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), before)

    def test_previously_confirmed_receipt_does_not_hide_new_storage_uncertainty(self):
        self.ledger.record_prediction(self.prediction())
        self.assertTrue(self.ledger.prediction_is_committed("snapshot-one"))
        with self.fail_ledger_sync():
            with self.assertRaises(LedgerError):
                self.ledger.prediction_is_committed("snapshot-one")

    def test_directory_barrier_required_when_recovering_existing_commit(self):
        self.seed_readable_failed_commit()
        with patch.object(durable_append, "fsync_directory", side_effect=OSError(5, "synthetic")):
            with self.assertRaises(LedgerError):
                self.ledger.committed_prediction("snapshot-one")

    def test_process_death_releases_lock_for_confirmed_restart(self):
        self.ledger.record_prediction(self.prediction())
        child = multiprocessing.get_context("fork").Process(target=_crash_with_lock,
                                                            args=(self.ledger.log.path,))
        child.start()
        child.join(5)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 73)
        self.assertEqual(self.new_ledger().committed_prediction("snapshot-one")["prediction_id"],
                         "p-original")


class V4RA09PrepareDurability(V5DurabilityCase):
    def test_original_third_poll_never_dispatches_with_persistent_failure(self):
        provider = _provider()
        service, record = self.service(provider)
        snapshot, _ = service.consumer.pending()[0]
        with self.fail_ledger_sync():
            for _ in range(7):
                result = service._analyze_one(snapshot, record)
                self.assertTrue(result["deferred"])
                self.assertEqual(provider.calls, 0)
        self.assertIsNotNone(self.ledger.find_prepare(analysis_identity(snapshot.market_snapshot_id)))
        self.assertFalse(service.consumer.store.seen(snapshot.market_snapshot_id))
        self.assertEqual(self.ledger.predictions(), [])

    def test_receipt_readability_never_skips_barrier_on_five_fresh_instances(self):
        aid = analysis_identity("snapshot-one")
        with self.fail_ledger_sync():
            for _ in range(7):
                with self.assertRaises(LedgerError):
                    self.new_ledger().prepare("snapshot-one", contract_id="SYNTHETIC")
        self.assertEqual([r["kind"] for r in self.ledger.rows()], ["PREPARE", "PREPARE_COMMIT"])
        self.assertFalse(self.fresh_failure_check("prepare_is_durable", aid)["authorized"])

    def test_recovery_requires_real_sync_without_adding_receipts(self):
        with self.fail_ledger_sync():
            for _ in range(5):
                with self.assertRaises(LedgerError):
                    self.ledger.prepare("snapshot-one")
        before = Path(self.ledger.log.path).read_bytes()
        result = self.new_ledger().prepare("snapshot-one")
        self.assertEqual(result["market_snapshot_id"], "snapshot-one")
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), before)
        self.assertTrue(self.ledger.prepare_is_durable(analysis_identity("snapshot-one")))

    def test_retry_cannot_change_prepare_contract_source_or_environment(self):
        self.ledger.prepare("snapshot-one", contract_id="A", source_record_sha256="a" * 64,
                            environment="shadow-demo")
        for kwargs in ({"contract_id": "B"}, {"source_record_sha256": "b" * 64},
                       {"environment": "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LedgerError):
                self.ledger.prepare("snapshot-one", **kwargs)


class V4RA10ReceiptBinding(V5DurabilityCase):
    def receipt_fixture(self, kind):
        if kind == "PREPARE_COMMIT":
            self.ledger.prepare("snapshot-one", contract_id="SYNTHETIC", environment="shadow")
        else:
            self.ledger.record_prediction(self.prediction())
        rows = self.ledger.rows()
        target, receipt = rows[-2:]
        # These are fresh disposable fixtures, not historical ledger edits.
        destination = self.root / ("malformed-" + kind + ".jsonl")
        return target, receipt, destination

    def assert_refused_receipt(self, kind, changes=None, missing=None):
        target, receipt, path = self.receipt_fixture(kind)
        receipt.update(changes or {})
        if missing:
            receipt.pop(missing, None)
        path.write_text(json.dumps(target) + "\n" + json.dumps(receipt) + "\n")
        candidate = AlphaLedger(str(path), str(path) + ".cost")
        with self.assertRaises(LedgerError):
            if kind == "PREPARE_COMMIT":
                candidate.prepare_is_durable(analysis_identity("snapshot-one"))
            else:
                candidate.committed_prediction("snapshot-one")

    def test_original_commit_cannot_name_another_prediction(self):
        self.assert_refused_receipt("COMMIT", {"prediction_id": "never-written"})

    def test_original_prepare_receipt_cannot_name_another_snapshot(self):
        self.assert_refused_receipt("PREPARE_COMMIT", {"market_snapshot_id": "snapshot-other"})

    def test_commit_receipt_requires_exact_schema(self):
        self.assert_refused_receipt("COMMIT", {"schema": "unknown"})

    def test_prepare_receipt_requires_exact_version(self):
        self.assert_refused_receipt("PREPARE_COMMIT", {"receipt_schema": "unknown"})

    def test_receipt_requires_complete_preimage_digest(self):
        self.assert_refused_receipt("COMMIT", missing="row_sha256")

    def test_receipt_cannot_bind_changed_economic_content(self):
        target, receipt, path = self.receipt_fixture("COMMIT")
        target["contract_id"] = "SYNTHETIC-OTHER"
        path.write_text(json.dumps(target) + "\n" + json.dumps(receipt) + "\n")
        with self.assertRaises(LedgerError):
            AlphaLedger(str(path), str(path) + ".cost").committed_prediction("snapshot-one")

    def test_receipt_unknown_extensions_are_not_ignored(self):
        self.assert_refused_receipt("COMMIT", {"extension": {"identity": "other"}})

    def test_receipt_requires_valid_typed_timestamp(self):
        self.assert_refused_receipt("PREPARE_COMMIT", {"at": True})

    def test_malformed_receipt_plus_otherwise_valid_prediction_stays_blocked(self):
        self.assert_refused_receipt("COMMIT", {"market_snapshot_id": []})

    def test_valid_duplicate_receipt_is_idempotent(self):
        self.ledger.record_prediction(self.prediction())
        receipt = self.ledger.rows()[-1]
        with self.ledger.log.lock():
            self.ledger.log.append(receipt)
        self.assertEqual(self.ledger.committed_prediction("snapshot-one")["prediction_id"], "p-original")

    def test_receipt_must_follow_target_in_history(self):
        target, receipt, path = self.receipt_fixture("COMMIT")
        path.write_text(json.dumps(receipt) + "\n" + json.dumps(target) + "\n")
        with self.assertRaises(LedgerError):
            AlphaLedger(str(path), str(path) + ".cost").committed_prediction("snapshot-one")

    def test_legacy_budget_attempt_is_superseded_append_only_exactly_once(self):
        old = self.ledger.record_prediction(self.prediction(state="BUDGET_EXHAUSTED"))
        before = Path(self.ledger.log.path).read_bytes()
        self.ledger.record_prediction(self.prediction(pid="p-completed", state="INSUFFICIENT_DATA"))
        current = self.new_ledger().committed_prediction("snapshot-one")
        self.assertEqual(current["prediction_id"], "p-completed")
        self.assertEqual(current["supersedes_prediction_id"], old["prediction_id"])
        self.assertTrue(Path(self.ledger.log.path).read_bytes().startswith(before))
        with self.assertRaises(LedgerError):
            self.ledger.record_prediction(self.prediction(pid="p-third", state="INSUFFICIENT_DATA"))
        self.assertEqual(len(self.ledger.predictions()), 2)


class V4LegacyReceiptRecovery(V5DurabilityCase):
    def legacy(self):
        fixture = Path(__file__).parent / "fixtures" / "astra_v4_refusal"
        manifest = json.loads((fixture / "manifest.json").read_text())
        self.assertEqual(manifest["source_commit"], "57d497566b9a218919c5934046edd67e61b9ff43")
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((fixture / name).read_bytes()).hexdigest(), digest)
        prefix = (fixture / "predictions.jsonl").read_bytes()
        Path(self.ledger.log.path).write_bytes(prefix)
        record = json.loads((fixture / "source-record.json").read_text())
        snapshot_id = next(r["market_snapshot_id"] for r in self.ledger.rows()
                           if r["kind"] == "PREDICTION")
        return prefix, record, snapshot_id

    def test_exact_frozen_v4_receipts_require_a_barrier_on_restart(self):
        prefix, record, sid = self.legacy()
        with self.fail_ledger_sync():
            with self.assertRaises(LedgerError):
                self.new_ledger().committed_prediction(sid)
        self.assertFalse(self.fresh_failure_check("committed_prediction", sid)["authorized"])
        self.assertEqual(self.new_ledger().committed_prediction(sid)["prediction_id"],
                         "legacy-v4-budget-refusal")
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), prefix)

    def test_exact_v4_budget_refusal_retries_once_after_barrier_recovery(self):
        prefix, record, sid = self.legacy()
        provider = _provider()
        service, _ = self.service(provider, record=record)
        service.now_fn = service.gateway.now_fn = lambda: datetime(2026, 9, 11, 12, 0, 2,
                                                                  tzinfo=timezone.utc)
        snapshot = service.consumer.mint(record)
        result = service._analyze_one(snapshot, record)
        self.assertFalse(result["deferred"], service.telemetry.errors)
        self.assertEqual(provider.calls, 1)
        current = self.new_ledger().committed_prediction(sid)
        self.assertNotEqual(current["prediction_id"], "legacy-v4-budget-refusal")
        self.assertEqual(current["supersedes_prediction_id"], "legacy-v4-budget-refusal")
        self.assertTrue(Path(self.ledger.log.path).read_bytes().startswith(prefix))
        service._analyze_one(snapshot, record)
        self.assertEqual(provider.calls, 1)

    def test_malformed_legacy_receipt_is_never_silently_upgraded(self):
        _, _, sid = self.legacy()
        rows = self.ledger.rows()
        rows[-1]["prediction_id"] = "wrong-original-v4-target"
        synthetic = self.root / "malformed-legacy.jsonl"
        synthetic.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaises(LedgerError):
            AlphaLedger(str(synthetic), str(synthetic) + ".cost").committed_prediction(sid)


class V5AdversarialGenerationAndAliases(V5DurabilityCase):
    def test_replaced_generation_between_read_and_confirmation_is_refused(self):
        self.ledger.record_prediction(self.prediction())
        other = self.root / "uncommitted-replacement.jsonl"
        replacement = copy.deepcopy(self.ledger.predictions()[0])
        replacement["prediction_id"] = "never-committed-replacement"
        other.write_text(json.dumps(replacement) + "\n")
        original_open = open
        swapped = []
        class WrappedRead:
            def __init__(self, inner):
                self.inner = inner
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.inner.close()
            def __getattr__(self, name):
                return getattr(self.inner, name)
            def read(self, *args):
                value = self.inner.read(*args)
                if not swapped:
                    swapped.append(True)
                    os.replace(other, self_path)
                return value
        self_path = self.ledger.log.path
        def read(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            return WrappedRead(handle) if os.fspath(path) == self_path else handle
        with patch("builtins.open", side_effect=read):
            with self.assertRaises((LedgerError, OSError)):
                self.ledger.committed_prediction("snapshot-one")
        self.assertTrue(swapped)

    def test_metadata_uncertainty_after_successful_file_read_is_not_absence(self):
        self.ledger.record_prediction(self.prediction())
        real_stat = os.stat
        count = []
        def fail(path, *args, **kwargs):
            if os.fspath(path) == self.ledger.log.path:
                count.append(True)
                # Lock topology stat is first; the next is the independently
                # observed identity after reading the ledger through its fd.
                if len(count) >= 2:
                    raise FileNotFoundError(errno.ENOENT, "synthetic transient metadata inconsistency")
            return real_stat(path, *args, **kwargs)
        with patch.object(os, "stat", side_effect=fail):
            with self.assertRaises((LedgerError, OSError)):
                self.ledger.committed_prediction("snapshot-one")

    def test_symlink_writer_alias_contends_on_the_same_process_lock(self):
        path = str(self.root / "rows.jsonl")
        Path(path).write_text('{"initial":true}\n')
        alias = self.root / "alias.jsonl"
        alias.symlink_to(path)
        context = multiprocessing.get_context("fork")
        result = context.Queue()
        with durable_append.exclusive_lock(path):
            child = context.Process(target=_try_alias_lock, args=(str(alias), result))
            child.start()
            child.join(3)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(result.get(timeout=1), "refused")
        result.close()
        result.join_thread()

    def test_hardlinked_authority_paths_refuse_append_without_changing_bytes(self):
        path = str(self.root / "rows.jsonl")
        Path(path).write_text('{"initial":true}\n')
        alias = self.root / "hardlink.jsonl"
        os.link(path, alias)
        before = Path(path).read_bytes()
        with self.assertRaises(OSError):
            with durable_append.serialized_append(str(alias)) as append:
                append('{"other":true}')
        self.assertEqual(Path(path).read_bytes(), before)
