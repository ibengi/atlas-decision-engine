"""V5 recovery regressions: real appenders, synthetic providers, disposable state.

Original witnesses retained: wrong processed prediction identity despite a
legitimate committed row (V4-RA-11), historical budget refusal that never
retries (12), metadata denial mistaken for absence (13), and independently
valid source A paired with economic snapshot B (17).
"""
import copy
import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider, write_pricing
from _candidate import valid_record, raw_market
from alpha_consumer import ProcessedStore, SpoolConsumer
from alpha_cost import BudgetGuard, BudgetLedger, PricingTable
from alpha_ledger import AlphaLedger
from alpha_service import AlphaShadowService, source_binding_for
from alpha_telemetry import Telemetry
from config import CFG


class _Source:
    kind = "isolated-synthetic"
    directory = None

    def __init__(self, record):
        self.record = record

    def records(self):
        return [copy.deepcopy(self.record)]

    def describe(self):
        return {"transport": self.kind}


class V5Recovery(AlphaCase):
    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "ALPHA_GATEWAY_ENABLED", True))
        self._patches[-1].start()
        self.record = valid_record()
        self.snapshot = object.__new__(SpoolConsumer).mint(self.record)
        self.pricing = PricingTable(write_pricing(
            os.path.join(self._tmp, "rates.json"), models=["grok"]))

    def service(self, provider=None):
        root = Path(self._tmp)
        return AlphaShadowService(
            providers=[] if provider is None else [provider],
            consumer=SpoolConsumer(source=_Source(self.record),
                store=ProcessedStore(str(root / "processed.jsonl"))),
            ledger=AlphaLedger(str(root / "predictions.jsonl"),
                               str(root / "costs.jsonl")),
            budget=BudgetGuard(pricing=self.pricing,
                ledger=BudgetLedger(str(root / "budget.jsonl"))),
            telemetry=Telemetry(str(root / "telemetry.json")))

    def test_wrong_prediction_id_is_automatically_repaired_without_redispatch(self):
        provider = FakeProvider("grok")
        service = self.service(provider)
        first = service.cycle()
        self.assertEqual(provider.calls, 1)
        actual = first["analyzed"][0]["prediction_id"]
        sid = self.snapshot.market_snapshot_id
        service.consumer.store.mark(sid, "ANALYZED", contract_id=self.snapshot.contract_id,
                                    prediction_id="never-committed-id")
        before = Path(service.consumer.store.path).read_bytes()
        other = FakeProvider("grok")
        resumed = self.service(other)
        outcome = resumed.cycle()
        self.assertEqual(other.calls, 0)
        self.assertEqual(resumed.consumer.store.entry(sid)["prediction_id"], actual)
        self.assertTrue(outcome["reconciliation"]["analyzed_without_prediction"])
        self.assertTrue(Path(service.consumer.store.path).read_bytes().startswith(before))
        self.assertEqual(len(resumed.ledger.predictions()), 1)

    def test_wrong_contract_identity_is_repaired_from_exact_committed_row(self):
        service = self.service()
        first = service.cycle()["analyzed"][0]
        service.consumer.store.mark(self.snapshot.market_snapshot_id, "ANALYZED",
                                    contract_id="wrong-contract", prediction_id=first["prediction_id"])
        self.service().cycle()
        self.assertEqual(service.consumer.store.entry(self.snapshot.market_snapshot_id)["contract_id"],
                         self.snapshot.contract_id)

    def test_orphan_acknowledgement_stays_quarantined_across_restarts(self):
        service = self.service()
        service.consumer.store.mark(self.snapshot.market_snapshot_id, "ANALYZED",
                                    contract_id=self.snapshot.contract_id, prediction_id="lost")
        before = Path(service.consumer.store.path).read_bytes()
        for _ in range(6):
            provider = FakeProvider("grok")
            resumed = self.service(provider)
            outcome = resumed.cycle()
            self.assertTrue(outcome["reconciliation"]["blocked"])
            self.assertEqual(provider.calls, 0)
            self.assertFalse(resumed.consumer.store.seen(self.snapshot.market_snapshot_id))
            self.assertEqual(resumed.ledger.predictions(), [])
        self.assertTrue(Path(service.consumer.store.path).read_bytes().startswith(before))

    def test_legacy_budget_refusal_gets_one_append_only_real_successor(self):
        seed = self.service()
        seed.ledger.record_prediction({
            "prediction_id": "historical-budget-refusal",
            "market_snapshot_id": self.snapshot.market_snapshot_id,
            "contract_id": self.snapshot.contract_id,
            "state": "BUDGET_EXHAUSTED", "p_meta": None})
        before = Path(seed.ledger.log.path).read_bytes()
        calls = []
        for _ in range(4):
            provider = FakeProvider("grok")
            resumed = self.service(provider)
            resumed.cycle()
            calls.append(provider.calls)
        self.assertEqual(calls, [1, 0, 0, 0])
        predictions = resumed.ledger.predictions()
        self.assertEqual(len(predictions), 2)
        self.assertEqual(predictions[0]["prediction_id"], "historical-budget-refusal")
        self.assertEqual(predictions[1]["supersedes_prediction_id"], "historical-budget-refusal")
        self.assertNotEqual(predictions[1]["state"], "BUDGET_EXHAUSTED")
        self.assertTrue(Path(seed.ledger.log.path).read_bytes().startswith(before))
        self.assertEqual(resumed.consumer.store.entry(self.snapshot.market_snapshot_id)["prediction_id"],
                         predictions[1]["prediction_id"])

    def test_processed_metadata_permission_failure_is_not_absence(self):
        path = str(Path(self._tmp) / "processed.jsonl")
        mine, theirs = ProcessedStore(path), ProcessedStore(path)
        self.assertFalse(mine.seen("other"))
        theirs.mark("other", "ANALYZED")
        mine.mark("mine", "ANALYZED")
        original = os.stat

        def unavailable(candidate, *args, **kwargs):
            if os.fspath(candidate) == path:
                raise PermissionError(errno.EACCES, "synthetic metadata denial")
            return original(candidate, *args, **kwargs)

        with patch("os.stat", side_effect=unavailable):
            with self.assertRaises(RuntimeError):
                mine.seen("other")
        self.assertTrue(ProcessedStore(path).seen("other"))

    def test_transient_missing_stat_cannot_hide_readable_processed_history(self):
        service = self.service()
        path = service.consumer.store.path
        service.consumer.store.mark("already", "ANALYZED")
        original = os.stat

        def missing(candidate, *args, **kwargs):
            if os.fspath(candidate) == path:
                raise FileNotFoundError(errno.ENOENT, "synthetic transient lookup")
            return original(candidate, *args, **kwargs)

        with patch("os.stat", side_effect=missing):
            with self.assertRaises(RuntimeError):
                ProcessedStore(path).seen("already")

    def test_storage_failure_between_reconciliation_reads_blocks_cycle(self):
        provider = FakeProvider("grok")
        service = self.service(provider)
        original = service.consumer.store._load
        reads = []

        def intermittent():
            reads.append(1)
            if len(reads) > 1:
                raise RuntimeError("synthetic storage changed between reconciliation reads")
            return original()

        with patch.object(service.consumer.store, "_load", side_effect=intermittent):
            result = service.cycle()
        self.assertEqual(provider.calls, 0)
        self.assertTrue(result["reconciliation"]["blocked"])
        self.assertEqual(service.ledger.predictions(), [])

    def test_later_terminal_skip_rechecks_prediction_storage(self):
        service = self.service()
        service.cycle()
        resumed = self.service(FakeProvider("grok"))
        with patch.object(resumed.ledger, "committed_prediction",
                          side_effect=RuntimeError("synthetic later read uncertainty")) as read:
            result = resumed.cycle()
        self.assertTrue(read.called)
        self.assertEqual(resumed.providers[0].calls, 0)
        self.assertEqual(result["analyzed"], [])
        self.assertIn("consume", resumed.telemetry.last_error["detail"])

    def test_snapshot_b_with_same_labels_and_other_quotes_is_refused(self):
        # Matching contract/question/source labels are insufficient: B quotes
        # a different observation while A still has a valid independent hash.
        altered = copy.deepcopy(self.record)
        altered.update(yes_bid=.40, yes_ask=.42, no_bid=.58, no_ask=.60)
        from candidate_contract import compute_checksum
        altered["record_sha256"] = compute_checksum(altered)
        snapshot_b = object.__new__(SpoolConsumer).mint(altered)
        self.assertEqual(snapshot_b.contract_id, self.snapshot.contract_id)
        self.assertNotEqual(snapshot_b.market_snapshot_id, self.snapshot.market_snapshot_id)
        provider = FakeProvider("grok")
        service = self.service(provider)
        service.consumer._valid(self.record)
        result = service._analyze_one(snapshot_b, self.record)
        self.assertTrue(result["deferred"])
        self.assertEqual(result["prediction_id"], "")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.ledger.predictions(), [])

    def test_binding_builder_with_explicit_snapshot_rejects_false_identity(self):
        with self.assertRaises(ValueError):
            source_binding_for(self.record, contract_id="wrong-contract",
                market_snapshot_id=self.snapshot.market_snapshot_id,
                digest_verified=True, snapshot=self.snapshot)

    def test_repeat_processed_fsync_failure_never_confers_terminal_authority(self):
        service = self.service()
        sid = self.snapshot.market_snapshot_id
        service.consumer.store.mark(sid, "DEFERRED")
        original = os.fsync
        path = service.consumer.store.path

        def failed(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == path:
                raise OSError(errno.EIO, "synthetic repeated processed sync failure")
            return original(fd)

        with patch("os.fsync", side_effect=failed):
            with self.assertRaises(RuntimeError):
                service.consumer.store.mark(sid, "ANALYZED")
            for _ in range(6):
                with self.assertRaises(RuntimeError):
                    ProcessedStore(path).seen(sid)
        # A successful actual barrier, never mere readability, recovers it.
        self.assertTrue(ProcessedStore(path).seen(sid))

    def test_persistent_prepare_failure_seven_polls_and_recreated_service_never_dispatches(self):
        provider = FakeProvider("grok")
        service = self.service(provider)
        original = os.fsync
        path = service.ledger.log.path

        def failed(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == path:
                raise OSError(errno.EIO, "synthetic repeated prediction sync failure")
            return original(fd)

        with patch("os.fsync", side_effect=failed):
            for _ in range(7):
                service.cycle()
                service = self.service(provider)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.ledger.predictions(), [])
        service.cycle()
        self.assertEqual(provider.calls, 1)

    def test_recovery_read_failure_is_a_dispatch_refusal(self):
        provider = FakeProvider("grok")
        service = self.service(provider)
        service.consumer._valid(self.record)
        with patch.object(service.ledger, "committed_prediction",
                          side_effect=RuntimeError("synthetic recovery uncertainty")):
            result = service._analyze_one(self.snapshot, self.record)
        self.assertTrue(result["deferred"])
        self.assertEqual(result["prediction_id"], "")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.ledger.predictions(), [])

    def test_legacy_budget_retry_and_recovery_in_fresh_python_processes(self):
        seed = self.service()
        seed.ledger.record_prediction({
            "prediction_id": "legacy-process-refusal",
            "market_snapshot_id": self.snapshot.market_snapshot_id,
            "contract_id": self.snapshot.contract_id,
            "state": "BUDGET_EXHAUSTED", "p_meta": None})
        Path(self._tmp, "input.json").write_text(json.dumps(self.record))
        program = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_astra_v5_recovery import (_Source, FakeProvider, ProcessedStore,
    SpoolConsumer, AlphaLedger, BudgetGuard, BudgetLedger, PricingTable,
    AlphaShadowService, Telemetry, CFG)
p = Path(sys.argv[1]); CFG.DATA_DIR = str(p)
provider = FakeProvider("grok")
s = AlphaShadowService(providers=[provider],
    consumer=SpoolConsumer(source=_Source(json.loads((p/"input.json").read_text())),
        store=ProcessedStore(str(p/"processed.jsonl"))),
    ledger=AlphaLedger(str(p/"predictions.jsonl"),str(p/"costs.jsonl")),
    budget=BudgetGuard(pricing=PricingTable(str(p/"rates.json")),
        ledger=BudgetLedger(str(p/"budget.jsonl"))),
    telemetry=Telemetry(str(p/"telemetry.json")))
result=s.cycle()
print(json.dumps({"provider_calls":provider.calls,"predictions":len(s.ledger.predictions()),
                  "analyzed":result["analyzed"]}))
"""
        results = []
        for _ in range(2):
            child = subprocess.run([sys.executable, "-c", program, self._tmp],
                capture_output=True, text=True, timeout=20,
                env={k: v for k, v in os.environ.items()
                     if k in ("PATH", "PYTHONPATH", "LANG", "LC_ALL", "DATA_DIR")})
            self.assertEqual(child.returncode, 0, child.stderr)
            results.append(json.loads(child.stdout))
        self.assertEqual([r["provider_calls"] for r in results], [1, 0])
        self.assertEqual([r["predictions"] for r in results], [2, 2])

    def test_new_process_cannot_believe_visible_processed_row_without_sync(self):
        store = self.service().consumer.store
        store.mark("snapshot", "ANALYZED")
        program = r"""
import errno, json, os, sys
from unittest.mock import patch
from alpha_consumer import ProcessedStore
path=sys.argv[1]; original=os.fsync
refusals=0
def failed(fd):
    if os.readlink(f"/proc/self/fd/{fd}")==path:
        raise OSError(errno.EIO,"synthetic restart barrier failure")
    return original(fd)
with patch("os.fsync",side_effect=failed):
    for _ in range(6):
        try: ProcessedStore(path).seen("snapshot")
        except RuntimeError: refusals+=1
print(json.dumps({"refusals":refusals}))
"""
        child = subprocess.run([sys.executable, "-c", program, store.path],
            capture_output=True, text=True, timeout=20,
            env={k: v for k, v in os.environ.items()
                 if k in ("PATH", "PYTHONPATH", "LANG", "LC_ALL", "DATA_DIR")})
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)["refusals"], 6)

    def test_returned_processed_rows_cannot_mutate_terminal_cache(self):
        store = self.service().consumer.store
        store.mark("snapshot", "DEFERRED")
        exposed = store._load()
        exposed["snapshot"]["status"] = "ANALYZED"
        self.assertFalse(store.seen("snapshot"))

    def test_coherent_direct_analysis_retains_independently_verified_source_digest(self):
        service = self.service()
        self.assertEqual(service.consumer.verified_digests, {})
        result = service._analyze_one(self.snapshot, self.record)
        prediction = service.ledger.find_prediction(result["prediction_id"])
        self.assertEqual(prediction["source_binding"]["record_sha256"], self.record["record_sha256"])
        self.assertIs(prediction["source_binding"]["digest_verified"], True)

    def test_two_services_cannot_redispatch_between_usage_and_prediction_commit(self):
        """New v5 witness: accounting completed, publication temporarily stalled."""
        import threading
        from concurrent.futures import ThreadPoolExecutor
        first_provider, second_provider = FakeProvider("grok"), FakeProvider("grok")
        first, second = self.service(first_provider), self.service(second_provider)
        reached_commit, release_commit = threading.Event(), threading.Event()
        second_started = threading.Event()
        original = first.ledger.record_prediction

        def stalled(opportunity):
            reached_commit.set()
            if not release_commit.wait(5):
                raise RuntimeError("synthetic coordinator timed out")
            return original(opportunity)

        def second_cycle():
            second_started.set()
            return second.cycle()

        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(first.ledger, "record_prediction", side_effect=stalled):
                run_first = pool.submit(first.cycle)
                self.assertTrue(reached_commit.wait(3))
                run_second = pool.submit(second_cycle)
                self.assertTrue(second_started.wait(3))
                try:
                    # On the old code this completes its duplicate dispatch.
                    # On the fixed code it is waiting for the analysis lock.
                    try:
                        run_second.result(timeout=.15)
                    except TimeoutError:
                        pass
                finally:
                    release_commit.set()
                run_first.result(timeout=4)
                run_second.result(timeout=4)
        self.assertEqual(first_provider.calls, 1)
        self.assertEqual(second_provider.calls, 0)
        self.assertEqual(len(first.ledger.predictions()), 1)

    def test_analysis_lock_timeout_is_structured_deferral_without_dispatch(self):
        import durable_append
        provider = FakeProvider("grok")
        service = self.service(provider)
        original = durable_append.exclusive_lock

        def busy(path, **kwargs):
            if path.endswith(".analysis"):
                raise TimeoutError("synthetic other analysis writer")
            return original(path, **kwargs)

        with patch("durable_append.exclusive_lock", side_effect=busy):
            result = service._analyze_one(self.snapshot, self.record)
        self.assertTrue(result["deferred"])
        self.assertEqual(result["state_reason"], "analysis_writer_unavailable")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.ledger.predictions(), [])

    def test_analysis_writer_process_death_releases_kernel_lock(self):
        service = self.service()
        program = r"""
import os, sys
from durable_append import exclusive_lock
with exclusive_lock(sys.argv[1]):
    os._exit(77)
"""
        child = subprocess.run([sys.executable, "-c", program,
            os.path.realpath(service.ledger.log.path) + ".analysis"],
            capture_output=True, text=True, timeout=10,
            env={k: v for k, v in os.environ.items()
                 if k in ("PATH", "PYTHONPATH", "LANG", "LC_ALL", "DATA_DIR")})
        self.assertEqual(child.returncode, 77, child.stderr)
        result = service._analyze_one(self.snapshot, self.record)
        self.assertFalse(result["deferred"])
        self.assertEqual(len(service.ledger.predictions()), 1)

    def test_failed_processed_ack_does_not_publish_terminal_result_or_observation(self):
        service = self.service()
        path = service.consumer.store.path
        original = os.fsync

        def failed(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == path:
                raise OSError(errno.EIO, "synthetic acknowledgement fsync failure")
            return original(fd)

        with patch("os.fsync", side_effect=failed):
            result = service._analyze_one(self.snapshot, self.record)
        self.assertTrue(result["deferred"])
        self.assertEqual(service._pending_observations, [])
        self.assertIsNotNone(service.ledger.find_prediction(result["prediction_id"]))
        # A later successful barrier and exact reconciliation resumes without
        # manufacturing another prediction or another provider request.
        resumed = self.service(FakeProvider("grok"))
        resumed.cycle()
        self.assertEqual(resumed.providers[0].calls, 0)
        self.assertEqual(len(resumed.ledger.predictions()), 1)

    def test_disappearance_after_successful_mark_is_storage_uncertainty(self):
        store = self.service().consumer.store
        store.mark("snapshot", "ANALYZED")
        os.unlink(store.path)  # Deliberate fault in new disposable test state.
        with self.assertRaises(RuntimeError):
            store.seen("snapshot")

    def test_startup_does_not_report_uncertain_custom_budget_available(self):
        service = self.service()
        for budget_state in ({"error": "unreadable", "exhausted": False},
                             {"accounting_uncertain": True, "exhausted": False}):
            with self.subTest(budget_state=budget_state):
                with patch.object(service.budget, "snapshot", return_value=budget_state), \
                     patch("alpha_service.assert_no_broker_credentials", return_value=[]):
                    self.assertFalse(service.startup_report()["budget_available"])
