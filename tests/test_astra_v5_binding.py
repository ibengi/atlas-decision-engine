"""Permanent V4-RA-14..17 witnesses and independent neighboring cases.

Every record is synthetic. These tests never instantiate a provider or broker.
Invalid historical rows are appended to disposable ledgers and never edited.
"""

import copy
import errno
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tests import _gates  # noqa: F401
from tests._candidate import raw_market, valid_record
from alpha_consumer import SpoolConsumer
from alpha_ledger import AlphaLedger, settlement_qualification, verify_source_evidence
from alpha_learning import learning_report
from alpha_learning_runtime import write_learning_report
from alpha_resolution_ingest import ingest_settlements
from alpha_settlement_validation import validate_source_snapshot
from alpha_telemetry import Telemetry
from candidate_contract import canonical_content, compute_checksum
from config import CFG


OBSERVED = datetime(2025, 1, 1, 10, tzinfo=timezone.utc)
PREDICTED = OBSERVED + timedelta(minutes=1)
RESOLVED = OBSERVED + timedelta(hours=1)
AUTHORITY = "synthetic-qualified-settlement-authority"


def source_record(**changes):
    return valid_record(raw_market(
        close_time=(OBSERVED + timedelta(minutes=30)).isoformat(),
        expiration_time=RESOLVED.isoformat(), **changes),
        observed_at_utc=OBSERVED.isoformat())


def prediction_fixture(ledger):
    record = source_record()
    snapshot = SpoolConsumer.mint(None, record)
    row = ledger.record_prediction({
        "prediction_id": "pred-v5-synthetic", "contract_id": snapshot.contract_id,
        "market_snapshot_id": snapshot.market_snapshot_id,
        "snapshot": snapshot.as_dict(), "prediction_time": PREDICTED.isoformat(),
        "source_binding": {
            "contract_id": snapshot.contract_id, "market_snapshot_id": snapshot.market_snapshot_id,
            "record_sha256": record["record_sha256"], "source_evidence": canonical_content(record),
            "contract_schema": record["schema"], "environment": CFG.ALPHA_ENVIRONMENT,
            "digest_verified": True,
        },
        "p_meta": 0.6, "confidence": 0.7, "per_model": {"astra": {"p_yes": 0.6}},
        "state": "INSUFFICIENT_DATA", "market_class": "MEDIUM",
    })
    return record, row


def incoming(prediction, **changes):
    binding = prediction["source_binding"]
    row = {
        "prediction_id": prediction["prediction_id"], "outcome": 1,
        "source": AUTHORITY, "resolved_at": RESOLVED.isoformat(),
        "settlement_evidence_id": "synthetic-evidence-1",
        "contract_id": binding["contract_id"], "market_snapshot_id": binding["market_snapshot_id"],
        "source_record_sha256": binding["record_sha256"],
        "environment": binding["environment"], "contract_schema": binding["contract_schema"],
    }
    row.update(changes)
    return row


def resolution_fixture(prediction):
    row = incoming(prediction)
    return {
        "schema": "atlas-alpha-ledger-v1", "kind": "RESOLUTION",
        "prediction_id": row["prediction_id"], "actual_outcome": row["outcome"],
        "resolved_at": row["resolved_at"], "resolution_source": row["source"],
        "settlement_evidence_id": row["settlement_evidence_id"],
        "settlement_binding": {key: row[key] for key in (
            "contract_id", "market_snapshot_id", "source_record_sha256", "environment", "contract_schema")},
        "binding_verified": True, "source_trusted": True,
        "source_evidence_verified": True, "trusted_sources": [AUTHORITY],
    }


class SettlementQualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-v5-binding-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = AlphaLedger(str(self.root / "prediction.jsonl"),
                                  str(self.root / "cost.jsonl"))
        self.record, self.prediction = prediction_fixture(self.ledger)

    def assert_refused(self, row):
        before = Path(self.ledger.log.path).read_bytes()
        result = ingest_settlements(self.ledger, [row], trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(self.ledger.qualified_resolved(), [])
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), before)
        return result

    def assert_history_excluded(self, prediction, resolution):
        ok, reason = settlement_qualification(prediction, resolution)
        self.assertFalse(ok, reason)
        self.assertTrue(reason)

    def test_complete_source_survives_pruning_and_restart(self):
        self.assertTrue(verify_source_evidence(self.prediction)["verified"])
        result = ingest_settlements(self.ledger, [incoming(self.prediction)], trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 1)
        original = Path(self.ledger.log.path).read_bytes()
        restarted = AlphaLedger(self.ledger.log.path, self.ledger.cost_log.path)
        self.assertEqual(len(restarted.qualified_resolved()), 1)
        self.assertEqual(restarted.calibration("astra")["samples"], 1)
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), original)

    def test_resolution_just_before_prediction_is_refused(self):
        self.assert_refused(incoming(self.prediction, resolved_at=(PREDICTED - timedelta(microseconds=1)).isoformat()))

    def test_resolution_equal_prediction_is_refused(self):
        self.assert_refused(incoming(self.prediction, resolved_at=PREDICTED.isoformat()))

    def test_resolution_far_future_is_refused(self):
        self.assert_refused(incoming(self.prediction, resolved_at="2999-01-01T00:00:00+00:00"))

    def test_original_year_1900_counterexample_is_refused(self):
        self.assert_refused(incoming(self.prediction, resolved_at="1900-01-01T00:00:00+00:00"))

    def test_original_year_1900_historical_counterexample_is_excluded(self):
        resolution = resolution_fixture(self.prediction)
        resolution["resolved_at"] = "1900-01-01T00:00:00+00:00"
        self.assert_history_excluded(self.prediction, resolution)

    def test_replay_rejects_unsupported_ledger_schema_and_row_kind(self):
        for field in ("schema", "kind"):
            with self.subTest(field=field):
                resolution = resolution_fixture(self.prediction)
                resolution[field] = "unknown-v999"
                self.assert_history_excluded(self.prediction, resolution)

    def test_replay_rejects_malformed_resolution_timestamp(self):
        for value in ("not-a-time", 123, True, None, "2025-01-01T11:00:00"):
            with self.subTest(value=value):
                resolution = resolution_fixture(self.prediction)
                resolution["resolved_at"] = value
                self.assert_history_excluded(self.prediction, resolution)

    def test_unverifiable_source_preimage_remains_excluded_after_restart(self):
        prediction = copy.deepcopy(self.prediction)
        prediction["source_binding"]["source_evidence"]["volume"] += 1
        self.assertFalse(verify_source_evidence(prediction)["verified"])
        self.assert_history_excluded(prediction, resolution_fixture(prediction))
        history = AlphaLedger(str(self.root / "unverifiable-history.jsonl"),
                              str(self.root / "unverifiable-cost.jsonl"))
        history.log.append(prediction)
        history.log.append(resolution_fixture(prediction))
        original = Path(history.log.path).read_bytes()
        restarted = AlphaLedger(history.log.path, history.cost_log.path)
        self.assertEqual(len(restarted.resolved()), 1)
        self.assertEqual(restarted.qualified_resolved(), [])
        self.assertEqual(Path(history.log.path).read_bytes(), original)

    def test_resolution_microsecond_after_prediction_is_qualified(self):
        result = ingest_settlements(self.ledger, [incoming(self.prediction,
            resolved_at=(PREDICTED + timedelta(microseconds=1)).isoformat())], trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 1)

    def test_missing_required_identifiers_share_quarantine_classification(self):
        for field in ("prediction_id", "source", "contract_id", "market_snapshot_id",
                      "source_record_sha256", "environment", "contract_schema",
                      "resolved_at", "settlement_evidence_id"):
            for absence in ("missing", None, "", "  "):
                with self.subTest(field=field, absence=absence):
                    row = incoming(self.prediction)
                    if absence == "missing":
                        row.pop(field)
                    else:
                        row[field] = absence
                    result = self.assert_refused(row)
                    self.assertEqual(len(result["quarantined"]), 1)

    def test_replay_rejects_exact_binding_mismatches(self):
        for field in ("contract_id", "market_snapshot_id", "source_record_sha256", "environment", "contract_schema"):
            for value in ("different", None, True, 123, [], {}):
                with self.subTest(field=field, value=value):
                    resolution = resolution_fixture(self.prediction)
                    resolution["settlement_binding"][field] = value
                    self.assert_history_excluded(self.prediction, resolution)

    def test_replay_requires_exact_prediction_identity(self):
        resolution = resolution_fixture(self.prediction)
        resolution["prediction_id"] = "pred-another"
        self.assert_history_excluded(self.prediction, resolution)

    def test_replay_requires_boolean_qualification_fields(self):
        for field in ("binding_verified", "source_trusted", "source_evidence_verified"):
            for value in ("false", "true", 1, [], {}, None, False):
                with self.subTest(field=field, value=value):
                    resolution = resolution_fixture(self.prediction)
                    resolution[field] = value
                    self.assert_history_excluded(self.prediction, resolution)

    def test_replay_requires_recorded_authority_membership(self):
        for allowed in (None, [], "synthetic-qualified-settlement-authority", ["different"], [AUTHORITY, 123]):
            with self.subTest(allowed=allowed):
                resolution = resolution_fixture(self.prediction)
                resolution["trusted_sources"] = allowed
                self.assert_history_excluded(self.prediction, resolution)

    def test_replay_requires_well_typed_evidence_identity(self):
        for value in (None, "", " ", 123, True, [], {}, "\x00", "evidence\nidentity"):
            with self.subTest(value=value):
                resolution = resolution_fixture(self.prediction)
                resolution["settlement_evidence_id"] = value
                self.assert_history_excluded(self.prediction, resolution)

    def test_prediction_identity_is_exact_without_hidden_normalization(self):
        prediction = copy.deepcopy(self.prediction)
        prediction["prediction_id"] = " " + prediction["prediction_id"]
        resolution = resolution_fixture(self.prediction)
        self.assert_history_excluded(prediction, resolution)

    def test_replay_requires_prediction_time_and_chronology(self):
        for value in (None, "not-a-time", "2025-01-01T10:01:00", True,
                      "1900-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"):
            with self.subTest(value=value):
                prediction = copy.deepcopy(self.prediction)
                prediction["prediction_time"] = value
                self.assert_history_excluded(prediction, resolution_fixture(prediction))

    def test_direct_malformed_history_is_immutable_and_excluded_from_learning(self):
        resolution = resolution_fixture(self.prediction)
        resolution["binding_verified"] = "false"
        resolution["settlement_binding"] = ["invalid", "shape"]
        self.ledger.log.append(resolution)
        original = Path(self.ledger.log.path).read_bytes()
        self.assertEqual(len(self.ledger.resolved()), 1)
        self.assertFalse(self.ledger.resolved()[0]["settlement_qualified"])
        self.assertEqual(self.ledger.qualified_resolved(), [])
        self.assertIsNone(self.ledger.calibration("astra"))
        self.assertEqual(learning_report(self.ledger)["memory"], [])
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), original)

    def test_malformed_historical_outcome_is_auditable_without_scoring(self):
        resolution = resolution_fixture(self.prediction)
        resolution["actual_outcome"] = {"unqualified": 1}
        self.ledger.log.append(resolution)
        original = Path(self.ledger.log.path).read_bytes()
        row = self.ledger.resolved()[0]
        self.assertEqual(row["actual_outcome"], {"unqualified": 1})
        self.assertFalse(row["settlement_qualified"])
        self.assertIsNone(row["brier_score"])
        self.assertEqual(self.ledger.metrics()["ensemble"]["samples"], 0)
        self.assertEqual(learning_report(self.ledger)["memory"], [])
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), original)

    def test_malformed_existing_outcome_is_quarantined_without_aborting_batch(self):
        resolution = resolution_fixture(self.prediction)
        resolution["actual_outcome"] = {"unqualified": 1}
        self.ledger.log.append(resolution)
        next_prediction = copy.deepcopy(self.prediction)
        next_prediction["prediction_id"] = "pred-second-synthetic-observation"
        next_record = source_record(yes_bid=40, yes_ask=42)
        next_snapshot = SpoolConsumer.mint(None, next_record)
        next_prediction["market_snapshot_id"] = next_snapshot.market_snapshot_id
        next_prediction["snapshot"] = next_snapshot.as_dict()
        next_prediction["source_binding"]["market_snapshot_id"] = next_snapshot.market_snapshot_id
        next_prediction["source_binding"]["source_evidence"] = canonical_content(next_record)
        next_prediction["source_binding"]["record_sha256"] = next_record["record_sha256"]
        # A second independent immutable prediction makes batch continuation
        # observable. Appending a synthetic historical fixture does not edit
        # the malformed resolution or create a provider request.
        self.ledger.log.append(next_prediction)
        before = Path(self.ledger.log.path).read_bytes()
        result = ingest_settlements(self.ledger,
            [incoming(self.prediction), incoming(next_prediction)],
            trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 1)
        self.assertEqual(result["resolved_prediction_ids"], [next_prediction["prediction_id"]])
        self.assertEqual(result["idempotent"], 0)
        self.assertEqual(len(result["quarantined"]), 1)
        self.assertTrue(Path(self.ledger.log.path).read_bytes().startswith(before))
        self.assertEqual(self.ledger.find_resolution(self.prediction["prediction_id"]), resolution)
        self.assertEqual([row["prediction_id"] for row in self.ledger.qualified_resolved()],
                         [next_prediction["prediction_id"]])

    def test_existing_outcome_types_never_coerce_into_idempotence(self):
        values = (None, "1", "not-a-number", 1.0, True, False, [], {}, -1, 2, 10**500)
        for index, value in enumerate(values):
            with self.subTest(value=value):
                history = AlphaLedger(str(self.root / f"malformed-outcome-{index}.jsonl"),
                                      str(self.root / f"malformed-outcome-cost-{index}.jsonl"))
                history.log.append(self.prediction)
                resolution = resolution_fixture(self.prediction)
                resolution["actual_outcome"] = value
                history.log.append(resolution)
                before = Path(history.log.path).read_bytes()
                result = ingest_settlements(history, [incoming(self.prediction)], trusted_sources=[AUTHORITY])
                self.assertEqual(result["appended"], 0)
                self.assertEqual(result["idempotent"], 0)
                self.assertEqual(len(result["quarantined"]), 1)
                self.assertEqual(Path(history.log.path).read_bytes(), before)

    def test_existing_same_outcome_with_unqualified_binding_is_not_idempotent(self):
        resolution = resolution_fixture(self.prediction)
        resolution["binding_verified"] = "true"
        self.ledger.log.append(resolution)
        before = Path(self.ledger.log.path).read_bytes()
        result = ingest_settlements(self.ledger, [incoming(self.prediction)], trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(result["idempotent"], 0)
        self.assertEqual(len(result["quarantined"]), 1)
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), before)

    def test_existing_resolution_read_uncertainty_is_structured_refusal(self):
        before = Path(self.ledger.log.path).read_bytes()
        with patch.object(self.ledger, "find_resolution", side_effect=OSError("synthetic resolution read uncertainty")):
            result = ingest_settlements(self.ledger, [incoming(self.prediction)], trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(result["idempotent"], 0)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), before)

    def test_existing_qualified_resolution_remains_idempotent_or_conflicting(self):
        self.ledger.log.append(resolution_fixture(self.prediction))
        before = Path(self.ledger.log.path).read_bytes()
        repeated = ingest_settlements(self.ledger, [incoming(self.prediction)], trusted_sources=[AUTHORITY])
        conflict = ingest_settlements(self.ledger, [incoming(self.prediction, outcome=0)], trusted_sources=[AUTHORITY])
        self.assertEqual(repeated["idempotent"], 1)
        self.assertEqual(repeated["quarantined"], [])
        self.assertEqual(len(conflict["conflicts"]), 1)
        self.assertEqual(conflict["appended"], 0)
        self.assertEqual(Path(self.ledger.log.path).read_bytes(), before)

    def test_source_a_snapshot_b_same_labels_different_prices_is_refused(self):
        other = source_record(yes_bid=40, yes_ask=42)
        snapshot_b = SpoolConsumer.mint(None, other)
        prediction = copy.deepcopy(self.prediction)
        prediction["snapshot"] = snapshot_b.as_dict()
        prediction["market_snapshot_id"] = snapshot_b.market_snapshot_id
        prediction["source_binding"]["market_snapshot_id"] = snapshot_b.market_snapshot_id
        self.assertFalse(validate_source_snapshot(self.record, snapshot_b)[0])
        self.assertFalse(verify_source_evidence(prediction)["verified"])
        self.assert_history_excluded(prediction, resolution_fixture(prediction))

    def test_source_a_snapshot_b_same_labels_different_sizes_is_refused(self):
        other = source_record(volume=1201)
        self.assertFalse(validate_source_snapshot(self.record, SpoolConsumer.mint(None, other))[0])

    def _assert_optional_event_cycle(self, *, missing):
        from alpha_consumer import LocalSpoolSource, ProcessedStore
        from alpha_service import AlphaShadowService
        record = source_record(event_ticker=None)
        self.assertIsNone(record.get("event_id"))
        if missing:
            record.pop("event_id", None)
            record["record_sha256"] = compute_checksum(record)
        spool = self.root / "optional-event-spool"
        spool.mkdir()
        (spool / "source.json").write_text(json.dumps(record))
        consumer = SpoolConsumer(source=LocalSpoolSource(str(spool)),
                                  store=ProcessedStore(str(self.root / "processed.jsonl")))
        service = AlphaShadowService(providers=[], consumer=consumer,
            ledger=self.ledger, now_fn=lambda: OBSERVED,
            telemetry=Telemetry(str(self.root / "optional-event-telemetry.json")))
        result = service.cycle()
        self.assertEqual(len(result["analyzed"]), 1)
        prediction = self.ledger.predictions()[-1]
        self.assertEqual(prediction["snapshot"]["event_id"], "")
        self.assertTrue(verify_source_evidence(prediction)["verified"])

    def test_explicitly_optional_null_event_still_completes_shadow_cycle(self):
        self._assert_optional_event_cycle(missing=False)

    def test_explicitly_optional_missing_event_still_completes_shadow_cycle(self):
        self._assert_optional_event_cycle(missing=True)

    def test_snapshot_boolean_price_cannot_equal_numeric_source(self):
        from alpha_snapshot import MarketSnapshot
        record = source_record(yes_bid=0)
        snapshot = SpoolConsumer.mint(None, record).as_dict()
        snapshot["yes_bid"] = False
        snapshot["market_snapshot_id"] = MarketSnapshot.derive_id({k: v for k, v in snapshot.items() if k != "market_snapshot_id"})
        self.assertFalse(validate_source_snapshot(record, snapshot)[0])

    def test_valid_checksum_cannot_excuse_derived_source_quotes(self):
        record = copy.deepcopy(self.record)
        record["quote_observation"] = {field: "derived" for field in ("yes_bid", "yes_ask", "no_bid", "no_ask")}
        record["record_sha256"] = compute_checksum(record)
        self.assertFalse(validate_source_snapshot(record, self.prediction["snapshot"])[0])

    def test_no_authority_policy_is_coerced_from_non_text(self):
        for allowed in (AUTHORITY, [123], [True], {"authority": AUTHORITY}):
            with self.subTest(allowed=allowed):
                result = ingest_settlements(self.ledger, [incoming(self.prediction)], trusted_sources=allowed)
                self.assertEqual(result["appended"], 0)

    def test_historical_lossy_source_without_preimage_stays_immutable_unqualified(self):
        prediction = copy.deepcopy(self.prediction)
        evidence = prediction["source_binding"]["source_evidence"]
        evidence.pop("settlement_source_evidence", None)
        prediction["source_binding"]["record_sha256"] = compute_checksum(evidence)
        # Legacy records can still recover within SHADOW; they cannot acquire
        # retrospective source proof that their producer did not retain.
        self.assertTrue(verify_source_evidence(prediction)["verified"])
        history = AlphaLedger(str(self.root / "legacy-lossy.jsonl"), str(self.root / "legacy-lossy-cost.jsonl"))
        history.log.append(prediction)
        before = Path(history.log.path).read_bytes()
        result = ingest_settlements(history, [incoming(prediction)], trusted_sources=[AUTHORITY])
        self.assertEqual(result["appended"], 0)
        self.assertTrue(result["quarantined"])
        self.assertEqual(Path(history.log.path).read_bytes(), before)
        history.log.append(resolution_fixture(prediction))
        before = Path(history.log.path).read_bytes()
        restarted = AlphaLedger(history.log.path, history.cost_log.path)
        self.assertEqual(len(restarted.resolved()), 1)
        self.assertEqual(restarted.qualified_resolved(), [])
        self.assertEqual(Path(history.log.path).read_bytes(), before)

    def test_checksummed_source_identity_extensions_cannot_be_ignored(self):
        for container in (
                [[{"name": "CF Benchmarks RTI"}]],
                [{"name": "CF Benchmarks RTI", "extension": {"authority_id": "different"}}],
                [{"name": "CF Benchmarks RTI"}, []]):
            with self.subTest(container=container):
                prediction = copy.deepcopy(self.prediction)
                evidence = prediction["source_binding"]["source_evidence"]
                evidence["settlement_source_evidence"] = {
                    "schema": "atlas-settlement-source-v1",
                    "aliases": {"settlement_sources": container},
                }
                prediction["source_binding"]["record_sha256"] = compute_checksum(evidence)
                self.assert_history_excluded(prediction, resolution_fixture(prediction))

    def test_source_identity_preimage_must_match_rendered_source(self):
        prediction = copy.deepcopy(self.prediction)
        evidence = prediction["source_binding"]["source_evidence"]
        evidence["settlement_source_evidence"] = {
            "schema": "atlas-settlement-source-v1",
            "aliases": {"settlement_sources": [{"name": "other authority"}]},
        }
        prediction["source_binding"]["record_sha256"] = compute_checksum(evidence)
        self.assert_history_excluded(prediction, resolution_fixture(prediction))

    def test_source_identity_version_is_verified_independently(self):
        prediction = copy.deepcopy(self.prediction)
        evidence = prediction["source_binding"]["source_evidence"]
        evidence["settlement_source_evidence"] = {
            "schema": "unknown-source-v999",
            "aliases": {"settlement_sources": [{"name": "CF Benchmarks RTI"}]},
        }
        prediction["source_binding"]["record_sha256"] = compute_checksum(evidence)
        self.assert_history_excluded(prediction, resolution_fixture(prediction))


class RuntimePathProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-v5-report-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = AlphaLedger(str(self.root / "predictions.jsonl"), str(self.root / "cost.jsonl"))
        self.telemetry = Telemetry(str(self.root / "runtime-only-custom.json"))
        self.telemetry.incr("cycles", 7)
        self.telemetry.flush()

    def assert_protected(self, target):
        before = Path(self.telemetry.path).read_bytes()
        with self.assertRaises((ValueError, OSError)):
            write_learning_report(self.ledger, str(self.root), filename=str(target))
        self.assertEqual(Path(self.telemetry.path).read_bytes(), before)

    def test_runtime_telemetry_path_is_protected_without_configuration(self):
        self.assert_protected(self.telemetry.path)

    def test_runtime_telemetry_in_progress_path_is_also_protected(self):
        self.assert_protected(self.telemetry.path + ".tmp")

    def test_runtime_telemetry_relative_alias_is_protected(self):
        self.assert_protected(os.path.relpath(self.telemetry.path, self.root))

    def test_runtime_telemetry_parent_alias_is_protected(self):
        directory = self.root / "child"
        directory.mkdir()
        self.assert_protected(directory / ".." / Path(self.telemetry.path).name)

    def test_runtime_telemetry_symlink_alias_is_protected(self):
        target = self.root / "symlink.json"
        target.symlink_to(self.telemetry.path)
        self.assert_protected(target)
        self.assertTrue(target.is_symlink())

    def test_runtime_telemetry_hardlink_alias_is_protected(self):
        target = self.root / "hardlink.json"
        os.link(self.telemetry.path, target)
        self.assert_protected(target)
        self.assertTrue(os.path.samefile(target, self.telemetry.path))

    def test_runtime_reconfiguration_preserves_both_actual_paths(self):
        prior = self.telemetry.path
        self.telemetry.path = str(self.root / "runtime-reassigned.json")
        self.telemetry.flush()
        self.assert_protected(self.telemetry.path)
        self.assert_protected(prior)

    def test_registration_during_publication_cannot_lose_new_runtime_state(self):
        target = self.root / "concurrently-configured.json"
        at_replace, release = threading.Event(), threading.Event()
        registered, completed = threading.Event(), threading.Event()
        errors = []
        original_replace = os.replace
        def delayed_replace(source, destination):
            if threading.current_thread().name == "v5-report-publisher":
                at_replace.set()
                if not release.wait(3):
                    raise TimeoutError("synthetic report release was not signaled")
            return original_replace(source, destination)
        def publisher():
            try:
                write_learning_report(self.ledger, str(self.root), filename=target.name)
            except Exception as exc:
                errors.append(exc)
        def runtime_writer():
            try:
                registered.set()
                telemetry = Telemetry(str(target))
                telemetry.incr("cycles", 9)
                telemetry.flush()
                completed.set()
            except Exception as exc:
                errors.append(exc)
        self.addCleanup(release.set)
        with patch("alpha_learning_runtime.os.replace", side_effect=delayed_replace):
            report_thread = threading.Thread(target=publisher, name="v5-report-publisher", daemon=True)
            report_thread.start()
            self.assertTrue(at_replace.wait(2))
            writer_thread = threading.Thread(target=runtime_writer, daemon=True)
            writer_thread.start()
            self.assertTrue(registered.wait(2))
            completed_before_publication = completed.wait(0.05)
            release.set()
            report_thread.join(2)
            writer_thread.join(2)
        self.assertFalse(report_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(target.read_text()).get("cycles"), 9)
        self.assertFalse(completed_before_publication)

    def test_metadata_uncertainty_cannot_skip_alias_protection(self):
        target = self.root / "hardlink.json"
        os.link(self.telemetry.path, target)
        stat = os.stat
        def uncertain(path, *args, **kwargs):
            if os.fspath(path) == str(target):
                raise PermissionError(errno.EACCES, "synthetic metadata uncertainty")
            return stat(path, *args, **kwargs)
        with patch("alpha_learning_runtime.os.stat", side_effect=uncertain):
            self.assert_protected(target)

    def test_custom_nontelemetry_runtime_object_path_is_protected(self):
        from types import SimpleNamespace
        path = self.root / "runtime-custom-state.jsonl"
        path.write_text('{"immutable":true}\n')
        with self.assertRaises(ValueError):
            write_learning_report(self.ledger, str(self.root), filename=str(path),
                                  persistence_objects=[SimpleNamespace(path=str(path))])
        self.assertEqual(path.read_text(), '{"immutable":true}\n')

    def test_normal_report_does_not_modify_runtime_state(self):
        before = Path(self.telemetry.path).read_bytes()
        write_learning_report(self.ledger, str(self.root))
        self.assertEqual(Path(self.telemetry.path).read_bytes(), before)
        self.assertTrue((self.root / "alpha_learning_report.json").is_file())


if __name__ == "__main__":
    unittest.main()
