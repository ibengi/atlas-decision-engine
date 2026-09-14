"""Permanent end-to-end settlement evidence-integrity regressions.

Every authority/response in this module is fictional and used only in an
isolated temporary ledger. No provider or broker request is made. A real
learning sample requires a qualified settlement; a zero Astra score alone is
not an exclusion witness, so every case also checks generic learning and the
calibration interface consumed by Alpha's Meta engine.
"""
import base64
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401
from alpha_ledger import AlphaLedger, LedgerError
from alpha_learning import learning_report, score_model
from alpha_resolution_ingest import ingest_settlements
from alpha_settlement_validation import settlement_qualification
from tests._settlement import qualified_fixture


MODEL = "synthetic-evidence-model"
AUTHORITY = "synthetic-qualified-settlement-authority"
OTHER_AUTHORITY = "synthetic-other-qualified-authority"
TRUSTED = [AUTHORITY, OTHER_AUTHORITY]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fixture():
    return qualified_fixture(
        source=AUTHORITY, prediction_id="synthetic-binding-prediction",
        contract_id="KX-SYNTHETIC-EVIDENCE", p_meta=.7,
        per_model={MODEL: {"p_yes": .7, "model_version": "fixture-v1"}},
        market_class="synthetic", executed=False, authorized=False)


def reseal_object(row):
    """Independent test-side recomputation, without invoking the validator."""
    evidence = row["settlement_evidence"]
    evidence.pop("settlement_evidence_id", None)
    identity = "sha256:" + digest(canonical(evidence))
    evidence["settlement_evidence_id"] = identity
    row["settlement_evidence_id"] = identity


def change_response(row, key, value, *, rehash=True):
    response = json.loads(row["settlement_response_preimage"])
    response[key] = value
    text = canonical(response)
    row["settlement_response_preimage"] = text
    if rehash:
        row["settlement_response_sha256"] = digest(text)
        row["settlement_evidence"]["settlement_response_sha256"] = digest(text)
        reseal_object(row)


def set_value(key, value):
    return lambda row: row.__setitem__(key, deepcopy(value))


def remove(key):
    return lambda row: row.pop(key, None)


def alter_evidence(row, key, value):
    row["settlement_evidence"][key] = value


def alter_binding(row, key, value):
    row["settlement_evidence"]["binding"][key] = value
    reseal_object(row)


def relative_resolution(row, seconds):
    # Fixture prediction is one second before its original resolution.
    row["resolved_at"] = (datetime.fromisoformat(row["resolved_at"]) +
                          timedelta(seconds=seconds)).isoformat()
    change_response(row, "resolved_at", row["resolved_at"])


def duplicate_response_member(row):
    text = row["settlement_response_preimage"]
    # A duplicate whose value agrees is still non-canonical/unacceptable.
    text = '{"outcome":1,' + text[1:]
    row["settlement_response_preimage"] = text
    row["settlement_response_sha256"] = digest(text)
    row["settlement_evidence"]["settlement_response_sha256"] = digest(text)
    reseal_object(row)


def noncanonical_response(row):
    text = json.dumps(json.loads(row["settlement_response_preimage"]), indent=2)
    row["settlement_response_preimage"] = text
    row["settlement_response_sha256"] = digest(text)
    row["settlement_evidence"]["settlement_response_sha256"] = digest(text)
    reseal_object(row)


def nonfinite_response(row):
    text = row["settlement_response_preimage"].replace('"outcome":1', '"outcome":NaN')
    row["settlement_response_preimage"] = text
    row["settlement_response_sha256"] = digest(text)
    row["settlement_evidence"]["settlement_response_sha256"] = digest(text)
    reseal_object(row)


def consistent_but_uncomputed_id(row):
    row["settlement_evidence_id"] = "sha256:" + "a" * 64
    row["settlement_evidence"]["settlement_evidence_id"] = row["settlement_evidence_id"]


# Each entry changes precisely one independently meaningful claim. Rehashing
# a changed response is intentional: semantic disagreement must also fail
# when the attack-free synthetic input has internally consistent checksums.
INVALID_VARIANTS = {
    "different_well_formed_evidence_id": set_value("settlement_evidence_id", "sha256:" + "b" * 64),
    "both_ids_equal_but_not_content_addressed": consistent_but_uncomputed_id,
    "different_digest": set_value("settlement_response_sha256", "b" * 64),
    "altered_preimage_without_rehash": lambda row: change_response(row, "authority_record_id", "altered-record", rehash=False),
    "missing_digest": remove("settlement_response_sha256"),
    "missing_preimage": remove("settlement_response_preimage"),
    "missing_evidence_object": remove("settlement_evidence"),
    "missing_evidence_id": remove("settlement_evidence_id"),
    "digest_short": set_value("settlement_response_sha256", "a" * 63),
    "digest_long": set_value("settlement_response_sha256", "a" * 65),
    "digest_nonhex": set_value("settlement_response_sha256", "g" * 64),
    "digest_uppercase": set_value("settlement_response_sha256", "A" * 64),
    "digest_boolean": set_value("settlement_response_sha256", True),
    "digest_null": set_value("settlement_response_sha256", None),
    "preimage_object_instead_of_canonical_text": lambda row: row.__setitem__("settlement_response_preimage", json.loads(row["settlement_response_preimage"])),
    "preimage_empty": set_value("settlement_response_preimage", ""),
    "preimage_duplicate_json_key": duplicate_response_member,
    "preimage_noncanonical_json": noncanonical_response,
    "preimage_nonfinite_json": nonfinite_response,
    "unknown_authority": set_value("source", "unqualified-fictional-authority"),
    "wrong_authority_even_when_other_is_allowed": set_value("settlement_authority", OTHER_AUTHORITY),
    "wrong_source_even_when_other_is_allowed": set_value("source", OTHER_AUTHORITY),
    "wrong_contract": set_value("contract_id", "KX-OTHER-SYNTHETIC"),
    "wrong_prediction": set_value("prediction_id", "different-prediction"),
    "wrong_snapshot": set_value("market_snapshot_id", "snap-different-synthetic"),
    "wrong_source_digest": set_value("source_record_sha256", "d" * 64),
    "wrong_environment": set_value("environment", "another-environment"),
    "wrong_schema": set_value("contract_schema_version", "unrecognized-schema-v99"),
    "conflicting_schema_alias": set_value("contract_schema", "unrecognized-schema-v99"),
    "missing_resolution_timestamp": remove("resolved_at"),
    "resolution_before_prediction": lambda row: relative_resolution(row, -2),
    "resolution_equal_prediction": lambda row: relative_resolution(row, -1),
    "resolution_in_future": lambda row: relative_resolution(row, 86400),
    "outcome_altered": set_value("outcome", 0),
    "outcome_boolean": set_value("outcome", True),
    "outcome_float": set_value("outcome", 1.0),
    "outcome_string": set_value("outcome", "1"),
    "response_outcome_rehashed_but_not_outer": lambda row: change_response(row, "outcome", 0),
    "response_authority_rehashed_but_not_outer": lambda row: change_response(row, "settlement_authority", OTHER_AUTHORITY),
    "response_contract_rehashed_but_not_outer": lambda row: change_response(row, "contract_id", "KX-OTHER-SYNTHETIC"),
    "response_environment_rehashed_but_not_outer": lambda row: change_response(row, "environment", "other"),
    "response_schema_rehashed_but_not_outer": lambda row: change_response(row, "contract_schema_version", "other-v99"),
    "response_empty_authority_record": lambda row: change_response(row, "authority_record_id", ""),
    "response_boolean_authority_record": lambda row: change_response(row, "authority_record_id", True),
    "response_unknown_nested_extension": lambda row: change_response(row, "unsupported", {"nested": {"claim": True}}),
    "response_unknown_version": lambda row: change_response(row, "schema", "atlas-alpha-settlement-response-v999"),
    "evidence_schema_unknown": lambda row: alter_evidence(row, "schema", "atlas-alpha-settlement-evidence-v999"),
    "evidence_object_boolean": set_value("settlement_evidence", True),
    "evidence_authority_mismatch": lambda row: alter_evidence(row, "settlement_authority", OTHER_AUTHORITY),
    "evidence_digest_mismatch": lambda row: alter_evidence(row, "settlement_response_sha256", "e" * 64),
    "evidence_binding_wrong_prediction_rehashed": lambda row: alter_binding(row, "prediction_id", "other"),
    "evidence_binding_wrong_contract_rehashed": lambda row: alter_binding(row, "contract_id", "other"),
    "evidence_binding_wrong_snapshot_rehashed": lambda row: alter_binding(row, "market_snapshot_id", "other"),
    "evidence_binding_wrong_source_digest_rehashed": lambda row: alter_binding(row, "source_record_sha256", "e" * 64),
    "evidence_binding_wrong_environment_rehashed": lambda row: alter_binding(row, "environment", "other"),
    "evidence_binding_wrong_schema_rehashed": lambda row: alter_binding(row, "contract_schema_version", "other"),
    "evidence_unknown_extension": lambda row: alter_evidence(row, "extension", {"trusted": True}),
    "input_policy_override": set_value("settlement_authority_policy", {"authorities": [AUTHORITY]}),
    "input_trusted_sources_override": set_value("trusted_sources", [AUTHORITY]),
    "input_unknown_response_alias": set_value("response_preimage", "ignored evidence must not qualify"),
    "input_unrecognized_extension": set_value("unrecognized", {"nested": "value"}),
    "binding_verified_integer": set_value("binding_verified", 1),
    "source_trusted_integer": set_value("source_trusted", 1),
    "source_evidence_verified_string": set_value("source_evidence_verified", "true"),
    "quarantined_integer_zero": set_value("quarantined", 0),
    "quarantined_true": set_value("quarantined", True),
    "original_bytes_do_not_match_preimage": set_value("settlement_response_bytes_base64", base64.b64encode(b"{}").decode("ascii")),
    "original_bytes_malformed_base64": set_value("settlement_response_bytes_base64", "%%%"),
}


class SettlementEvidenceBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-settlement-evidence-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "predictions.jsonl"
        self.cost_path = Path(self.tmp.name) / "cost.jsonl"
        self.ledger = AlphaLedger(path=str(self.path), cost_path=str(self.cost_path))
        _, _, self.prediction, self.settlement = fixture()
        self.ledger.record_prediction(deepcopy(self.prediction))
        self.prediction_prefix = self.path.read_bytes()

    def counts(self, ledger=None):
        ledger = ledger or self.ledger
        rows = ledger.qualified_resolved()
        score = score_model(rows, MODEL)
        calibration = ledger.calibration(MODEL)
        metrics = ledger.metrics()
        return {
            "qualified_resolved": len(rows),
            "learning_samples": score["samples"],
            "calibration_samples": 0 if calibration is None else calibration["samples"],
            "ensemble_qualified_samples": metrics["ensemble_qualified"]["samples"],
            "astra_samples": learning_report(ledger)["astra"]["samples"],
        }

    def assert_excluded(self, ledger=None):
        self.assertEqual(self.counts(ledger), {
            "qualified_resolved": 0, "learning_samples": 0,
            "calibration_samples": 0, "ensemble_qualified_samples": 0,
            "astra_samples": 0})

    def ingest(self, row):
        return ingest_settlements(self.ledger, [row], trusted_sources=TRUSTED)

    def assert_invalid_variant(self, mutation):
        row = deepcopy(self.settlement)
        mutation(row)
        result = self.ingest(row)
        self.assertEqual(result["appended"], 0, result)
        self.assertEqual(result["idempotent"], 0, result)
        self.assertTrue(result["rejected"] or result["quarantined"], result)
        self.assert_excluded()
        # Refusal must not modify predictions or hide the invalid input in a
        # weaker resolution. The current ingestion path returns diagnostics.
        self.assertEqual(self.path.read_bytes(), self.prediction_prefix)
        self.assertEqual(self.ledger.resolutions(), {})

    def test_persisted_preimage_is_required_for_learning(self):
        self.assertEqual(self.ingest(self.settlement)["appended"], 1)
        resolution = self.ledger.find_resolution(self.prediction["prediction_id"])
        self.assertIn("settlement_response_preimage", resolution)
        self.assertEqual(resolution["settlement_response_preimage"],
                         self.settlement["settlement_response_preimage"])
        self.assertEqual(self.counts()["learning_samples"], 1)
        self.assertEqual(self.counts()["calibration_samples"], 1)

    def test_positive_exact_evidence_is_qualified_and_retained(self):
        original = deepcopy(self.settlement)
        self.assertEqual(self.ingest(self.settlement)["appended"], 1)
        self.assertEqual(self.settlement, original, "ingestion mutated caller evidence")
        resolution = self.ledger.find_resolution(self.prediction["prediction_id"])
        for field in ("settlement_evidence_id", "settlement_evidence",
                      "settlement_response_sha256", "settlement_response_preimage",
                      "settlement_authority"):
            self.assertEqual(resolution[field], original[field], field)
        self.assertEqual(digest(resolution["settlement_response_preimage"]),
                         resolution["settlement_response_sha256"])
        computed = deepcopy(resolution["settlement_evidence"])
        saved_id = computed.pop("settlement_evidence_id")
        self.assertEqual(saved_id, "sha256:" + digest(canonical(computed)))
        prediction = self.ledger.find_prediction(self.prediction["prediction_id"])
        self.assertEqual(settlement_qualification(prediction, resolution), (True, ""))
        self.assertTrue(self.path.read_bytes().startswith(self.prediction_prefix))
        self.assertEqual(self.counts(), {
            "qualified_resolved": 1, "learning_samples": 1,
            "calibration_samples": 1, "ensemble_qualified_samples": 1,
            "astra_samples": 0})

    def test_positive_matching_original_response_bytes_retained(self):
        self.settlement["settlement_response_bytes_base64"] = base64.b64encode(
            self.settlement["settlement_response_preimage"].encode("utf-8")).decode("ascii")
        self.assertEqual(self.ingest(self.settlement)["appended"], 1)
        resolution = self.ledger.find_resolution(self.prediction["prediction_id"])
        self.assertEqual(resolution["settlement_response_bytes_base64"],
                         self.settlement["settlement_response_bytes_base64"])
        self.assertEqual(self.counts()["learning_samples"], 1)

    def test_positive_utf8_preimage_is_hashed_as_exact_retained_bytes(self):
        row = deepcopy(self.settlement)
        change_response(row, "authority_record_id", "SYNTHETIC-résolution-東京")
        self.assertEqual(self.ingest(row)["appended"], 1)
        resolution = self.ledger.find_resolution(self.prediction["prediction_id"])
        self.assertEqual(resolution["settlement_response_preimage"], row["settlement_response_preimage"])
        self.assertEqual(self.counts()["learning_samples"], 1)

    def test_qualified_true_flags_still_require_complete_evidence(self):
        row = deepcopy(self.settlement)
        row.update(binding_verified=True, source_trusted=True,
                   source_evidence_verified=True, quarantined=False)
        row.pop("settlement_response_preimage")
        result = self.ingest(row)
        self.assertEqual(result["appended"], 0)
        self.assert_excluded()

    def test_new_process_replays_same_evidence_and_deduplicates(self):
        self.assertEqual(self.ingest(self.settlement)["appended"], 1)
        settled_prefix = self.path.read_bytes()
        input_path = Path(self.tmp.name) / "settlement-input.json"
        input_path.write_text(canonical(self.settlement), encoding="utf-8")
        code = r'''
import json, sys
from pathlib import Path
from alpha_ledger import AlphaLedger
from alpha_learning import score_model, learning_report
from alpha_resolution_ingest import ingest_settlements
path, cost, input_path = sys.argv[1:]
ledger = AlphaLedger(path=path, cost_path=cost)
settlement = json.loads(Path(input_path).read_text())
result = ingest_settlements(ledger, [settlement], trusted_sources=%r)
rows = ledger.qualified_resolved()
print(json.dumps({"qualified":len(rows), "learning":score_model(rows,%r)["samples"],
 "calibration":(ledger.calibration(%r) or {"samples":0})["samples"],
 "resolved_report":ledger.metrics()["settlements_qualified"],
 "astra":learning_report(ledger)["astra"]["samples"],
 "resolutions":len(ledger.resolutions()), "idempotent":result["idempotent"],
 "appended":result["appended"], "prediction_id":rows[0]["prediction_id"],
 "evidence":ledger.find_resolution(settlement["prediction_id"])}))
''' % (TRUSTED, MODEL, MODEL)
        completed = subprocess.run(
            [sys.executable, "-c", code, str(self.path), str(self.cost_path), str(input_path)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual({key: report[key] for key in
                          ("qualified", "learning", "calibration", "resolved_report",
                           "resolutions", "idempotent")},
                         dict.fromkeys(("qualified", "learning", "calibration", "resolved_report",
                                        "resolutions", "idempotent"), 1))
        self.assertEqual(report["appended"], 0)
        self.assertEqual(report["astra"], 0)
        self.assertEqual(report["prediction_id"], self.prediction["prediction_id"])
        self.assertEqual(report["evidence"], self.ledger.find_resolution(self.prediction["prediction_id"]))
        self.assertEqual(self.path.read_bytes(), settled_prefix)

    def test_direct_qualified_resolve_rechecks_under_writer_lock(self):
        bad = {
            "binding_verified": True, "source_trusted": True,
            "source_evidence_verified": True,
            "settlement_evidence_id": "sha256:" + "a" * 64,
        }
        with self.assertRaises((LedgerError, ValueError)):
            self.ledger.resolve(self.prediction["prediction_id"], 1,
                resolved_at=self.settlement["resolved_at"], source=AUTHORITY, binding=bad)
        self.assertEqual(self.path.read_bytes(), self.prediction_prefix)
        self.assert_excluded()

    def test_pre_remediation_history_is_preserved_but_not_qualified(self):
        binding = self.prediction["source_binding"]
        old_resolution = {
            "schema": "atlas-alpha-ledger-v1", "kind": "RESOLUTION",
            "prediction_id": self.prediction["prediction_id"],
            "actual_outcome": 1, "resolved_at": self.settlement["resolved_at"],
            "resolution_source": AUTHORITY,
            "settlement_binding": {"contract_id": binding["contract_id"],
                "market_snapshot_id": binding["market_snapshot_id"],
                "source_record_sha256": binding["record_sha256"],
                "environment": binding["environment"], "contract_schema": binding["contract_schema"]},
            "settlement_evidence_id": "previously-accepted-well-formed-id",
            "binding_verified": True, "source_trusted": True,
            "source_evidence_verified": True, "trusted_sources": TRUSTED,
        }
        self.ledger.log.append(old_resolution)  # Disposable historical fixture only.
        historical = self.path.read_bytes()
        restarted = AlphaLedger(path=str(self.path), cost_path=str(self.cost_path))
        self.assertEqual(len(restarted.resolved()), 1)
        self.assertEqual(len(restarted.unqualified_resolved()), 1)
        self.assert_excluded(restarted)
        self.assertEqual(learning_report(restarted)["settlements_excluded_unqualified"], 1)
        self.assertEqual(self.path.read_bytes(), historical)

    def test_altered_source_preimage_in_disposable_history_excluded(self):
        self.assertEqual(self.ingest(self.settlement)["appended"], 1)
        original = self.path.read_bytes()
        history = [json.loads(line) for line in original.splitlines()]
        prediction = next(row for row in history if row.get("kind") == "PREDICTION")
        prediction["source_binding"]["source_evidence"]["question"] = "Altered synthetic observation"
        copied_path = Path(self.tmp.name) / "altered-source-disposable.jsonl"
        copied_bytes = ("\n".join(canonical(row) for row in history) + "\n").encode("utf-8")
        copied_path.write_bytes(copied_bytes)
        restarted = AlphaLedger(path=str(copied_path), cost_path=str(self.cost_path))
        self.assert_excluded(restarted)
        self.assertEqual(len(restarted.unqualified_resolved()), 1)
        self.assertEqual(copied_path.read_bytes(), copied_bytes)
        self.assertEqual(self.path.read_bytes(), original)

    def assert_historical_tamper_excluded(self, mutate):
        self.assertEqual(self.ingest(self.settlement)["appended"], 1)
        original = self.path.read_bytes()
        history = [json.loads(line) for line in original.splitlines()]
        resolution = next(row for row in history if row.get("kind") == "RESOLUTION")
        mutate(resolution)
        # Explicit disposable copy: production/live history is never touched.
        copied_path = Path(self.tmp.name) / "tampered-disposable-copy.jsonl"
        copied_bytes = ("\n".join(canonical(row) for row in history) + "\n").encode("utf-8")
        copied_path.write_bytes(copied_bytes)
        restarted = AlphaLedger(path=str(copied_path), cost_path=str(self.cost_path))
        self.assert_excluded(restarted)
        self.assertEqual(len(restarted.unqualified_resolved()), 1)
        self.assertEqual(learning_report(restarted)["settlements_excluded_unqualified"], 1)
        self.assertEqual(copied_path.read_bytes(), copied_bytes)
        self.assertEqual(self.path.read_bytes(), original)


for _name, _mutation in INVALID_VARIANTS.items():
    def _test(self, mutate=_mutation):
        self.assert_invalid_variant(mutate)
    _test.__name__ = "test_refuses_" + _name
    _test.__doc__ = "Invalid settlement: %s; actual learning and calibration remain zero." % _name
    setattr(SettlementEvidenceBindingTests, _test.__name__, _test)


HISTORICAL_VARIANTS = {
    "evidence_id": set_value("settlement_evidence_id", "sha256:" + "c" * 64),
    "digest": set_value("settlement_response_sha256", "f" * 64),
    "preimage": lambda row: change_response(row, "authority_record_id", "tampered", rehash=False),
    "preimage_missing": remove("settlement_response_preimage"),
    "evidence_missing": remove("settlement_evidence"),
    "authority": set_value("settlement_authority", OTHER_AUTHORITY),
    "outcome": set_value("actual_outcome", 0),
    "true_flag_coerced_from_integer": set_value("binding_verified", 1),
    "trust_flag_coerced_from_integer": set_value("source_trusted", 1),
    "resolution_before_prediction": lambda row: relative_resolution(row, -2),
    "resolution_equal_prediction": lambda row: relative_resolution(row, -1),
    "resolution_in_future": lambda row: relative_resolution(row, 86400),
    "environment_binding": lambda row: row["settlement_binding"].__setitem__("environment", "other"),
    "schema_version_binding": lambda row: row["settlement_binding"].__setitem__("contract_schema_version", "other-v99"),
    "snapshot_binding": lambda row: row["settlement_binding"].__setitem__("market_snapshot_id", "other-snapshot"),
    "policy_missing": remove("settlement_authority_policy"),
    "policy_digest_altered": lambda row: row["settlement_authority_policy"].__setitem__("policy_id", "sha256:" + "9" * 64),
}
for _name, _mutation in HISTORICAL_VARIANTS.items():
    def _test(self, mutate=_mutation):
        self.assert_historical_tamper_excluded(mutate)
    _test.__name__ = "test_historical_replay_excludes_" + _name
    setattr(SettlementEvidenceBindingTests, _test.__name__, _test)


if __name__ == "__main__":
    unittest.main()
