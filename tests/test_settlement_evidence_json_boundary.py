"""Strict public JSON boundaries for synthetic settlement evidence.

These tests mutate serialized bytes BEFORE decoding, because dict fixtures
cannot represent the duplicate members that caused the original defect.
All files are disposable SHADOW_ONLY state; no external I/O occurs.
"""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from alpha_evidence_json import strict_json_loads
from alpha_ledger import AlphaLedger
from alpha_learning import learning_report, score_model
from alpha_resolution_ingest import ingest_settlements
from tests._settlement import qualified_fixture
from tools.alpha_resolution_ingest import _read_jsonl, main


AUTHORITY = "synthetic-json-boundary-authority"
MODEL = "synthetic-json-boundary-model"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def prepend_member(text, key, value):
    return "{" + json.dumps(key) + ":" + json.dumps(value) + "," + text[1:]


def nested_evidence_duplicate(text):
    return text.replace('"settlement_evidence":{',
                        '"settlement_evidence":{"settlement_response_sha256":"' + "e" * 64 + '",', 1)


def nested_binding_duplicate(text):
    return text.replace('"binding":{', '"binding":{"contract_id":"wrong-contract",', 1)


def escaped_duplicate(text):
    # JSON decodes this spelling to the same member name as the later one.
    return '{"settlement_\\u0065vidence_id":"wrong-but-well-formed",' + text[1:]


DUPLICATE_VARIANTS = {
    "evidence_id": lambda text: prepend_member(text, "settlement_evidence_id", "sha256:" + "e" * 64),
    "response_digest": lambda text: prepend_member(text, "settlement_response_sha256", "e" * 64),
    "response_preimage": lambda text: prepend_member(text, "settlement_response_preimage", "{}"),
    "evidence_id_equal_value": lambda text: prepend_member(text, "settlement_evidence_id", json.loads(text)["settlement_evidence_id"]),
    "unicode_escaped_evidence_id": escaped_duplicate,
    "nested_evidence_digest": nested_evidence_duplicate,
    "nested_contract_binding": nested_binding_duplicate,
}
NONFINITE_VARIANTS = {
    "nan": "NaN", "positive_infinity": "Infinity",
    "negative_infinity": "-Infinity", "positive_exponent_overflow": "1e999",
    "negative_exponent_overflow": "-1e999",
}


class SettlementEvidenceJsonBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-evidence-json-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "ledger.jsonl"
        self.cost = self.root / "cost.jsonl"
        self.ledger = AlphaLedger(path=str(self.path), cost_path=str(self.cost))
        _, _, self.prediction, self.settlement = qualified_fixture(
            source=AUTHORITY, prediction_id="synthetic-boundary-prediction",
            contract_id="KX-SYNTHETIC-JSON", p_meta=.75,
            per_model={MODEL: {"p_yes": .75}}, executed=False, authorized=False)
        self.ledger.record_prediction(deepcopy(self.prediction))
        self.before = self.path.read_bytes()

    def assert_samples(self, ledger, expected):
        rows = ledger.qualified_resolved()
        self.assertEqual(len(rows), expected)
        self.assertEqual(score_model(rows, MODEL)["samples"], expected)
        self.assertEqual((ledger.calibration(MODEL) or {"samples": 0})["samples"], expected)
        self.assertEqual(ledger.metrics()["ensemble_qualified"]["samples"], expected)
        self.assertEqual(learning_report(ledger)["astra"]["samples"], 0)

    def write_feed(self, text):
        path = self.root / "input.jsonl"
        path.write_text(text + "\n", encoding="utf-8")
        return path

    def check_cli_refusal(self, text):
        path = self.write_feed(text)
        parsed = _read_jsonl(str(path))
        self.assertEqual(len(parsed), 1)
        self.assertIn("__parse_error__", parsed[0])
        self.assertIn("line 1:", parsed[0]["__parse_error__"])
        out = io.StringIO()
        with patch("tools.alpha_resolution_ingest.AlphaLedger", return_value=self.ledger):
            with redirect_stdout(out):
                code = main(["--input", str(path), "--trusted-source", AUTHORITY, "--strict"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 2, report)
        self.assertEqual(report["received"], 1)
        self.assertEqual(report["appended"], 0)
        self.assertEqual(len(report["rejected"]), 1)
        self.assert_samples(self.ledger, 0)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(path.read_text(encoding="utf-8"), text + "\n")

    def check_historical_refusal(self, mutate):
        self.assertEqual(ingest_settlements(self.ledger, [self.settlement],
                         trusted_sources=[AUTHORITY])["appended"], 1)
        self.assert_samples(self.ledger, 1)
        original = self.path.read_bytes()
        lines = original.decode("utf-8").splitlines()
        altered = []
        for line in lines:
            row = json.loads(line)
            altered.append(mutate(line) if row.get("kind") == "RESOLUTION" else line)
        copied = self.root / "duplicate-disposable-history.jsonl"
        copied_bytes = ("\n".join(altered) + "\n").encode("utf-8")
        copied.write_bytes(copied_bytes)
        restarted = AlphaLedger(path=str(copied), cost_path=str(self.cost))
        with self.assertLogs("ALPHA", level="WARNING") as messages:
            rows, generation = restarted.log.rows(with_generation=True)
        self.assertFalse(any(row.get("kind") == "RESOLUTION" for row in rows))
        self.assertIsNotNone(generation)
        self.assertTrue(any("preserved" in line or "ignored" in line for line in messages.output))
        self.assert_samples(restarted, 0)
        self.assertEqual(copied.read_bytes(), copied_bytes)
        self.assertEqual(self.path.read_bytes(), original)

    def test_duplicate_evidence_id_cannot_enter_learning_from_public_cli_bytes(self):
        text = DUPLICATE_VARIANTS["evidence_id"](canonical(self.settlement))
        path = self.write_feed(text)
        decoded = _read_jsonl(str(path))
        result = ingest_settlements(self.ledger, decoded, trusted_sources=[AUTHORITY])
        # The first assertion is the externally meaningful safety invariant,
        # not whether a particular diagnostic key happened to be generated.
        self.assertEqual(result["appended"], 0, result)
        self.assert_samples(self.ledger, 0)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_valid_cli_positive_enters_generic_learning_and_calibration(self):
        path = self.write_feed(canonical(self.settlement))
        self.assertEqual(_read_jsonl(str(path)), [self.settlement])
        out = io.StringIO()
        with patch("tools.alpha_resolution_ingest.AlphaLedger", return_value=self.ledger):
            with redirect_stdout(out):
                code = main(["--input", str(path), "--trusted-source", AUTHORITY, "--strict"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["appended"], 1)
        self.assert_samples(self.ledger, 1)
        self.assertTrue(self.path.read_bytes().startswith(self.before))

    def test_invalid_first_line_does_not_hide_subsequent_valid_evidence(self):
        invalid = DUPLICATE_VARIANTS["evidence_id"](canonical(self.settlement))
        path = self.write_feed(invalid + "\n" + canonical(self.settlement))
        parsed = _read_jsonl(str(path))
        self.assertEqual(len(parsed), 2)
        self.assertIn("__parse_error__", parsed[0])
        self.assertEqual(parsed[1], self.settlement)
        out = io.StringIO()
        with patch("tools.alpha_resolution_ingest.AlphaLedger", return_value=self.ledger):
            with redirect_stdout(out):
                code = main(["--input", str(path), "--trusted-source", AUTHORITY, "--strict"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["received"], 2)
        self.assertEqual(report["appended"], 1)
        self.assertEqual(len(report["rejected"]), 1)
        self.assert_samples(self.ledger, 1)

    def test_duplicate_middle_history_row_is_reported_without_hiding_following_row(self):
        text = canonical({"kind": "OBSERVATION", "interval_s": 10})
        duplicate = '{"kind":"RESOLUTION",' + text[1:]
        following = canonical({"kind": "OBSERVATION", "interval_s": 20})
        copied = self.root / "middle-invalid-history.jsonl"
        data = self.before + (duplicate + "\n" + following + "\n").encode("utf-8")
        copied.write_bytes(data)
        ledger = AlphaLedger(path=str(copied), cost_path=str(self.cost))
        with self.assertLogs("ALPHA", level="ERROR"):
            rows = ledger.rows()
        self.assertEqual([row["interval_s"] for row in rows if row.get("kind") == "OBSERVATION"], [20])
        self.assertEqual(copied.read_bytes(), data)

    def test_nested_duplicate_inside_array_rejected_by_shared_boundary(self):
        with self.assertRaises(ValueError):
            strict_json_loads('{"items":[{"evidence":"first","evidence":"second"}]}')

    def test_json_escape_equivalent_duplicate_rejected_at_nested_boundary(self):
        with self.assertRaises(ValueError):
            strict_json_loads('{"items":[{"\\u0061":1,"a":1}]}')

    def test_finite_numbers_and_unicode_have_lossless_positive_parse(self):
        value = {"finite": 1.25, "integer": 10**100, "unicode": "é東京", "items": [False, None]}
        self.assertEqual(strict_json_loads(canonical(value)), value)


for _name, _mutate in DUPLICATE_VARIANTS.items():
    def _cli_test(self, mutate=_mutate):
        self.check_cli_refusal(mutate(canonical(self.settlement)))
    _cli_test.__name__ = "test_cli_rejects_duplicate_" + _name
    setattr(SettlementEvidenceJsonBoundaryTests, _cli_test.__name__, _cli_test)
    def _history_test(self, mutate=_mutate):
        self.check_historical_refusal(mutate)
    _history_test.__name__ = "test_replay_excludes_duplicate_" + _name
    setattr(SettlementEvidenceJsonBoundaryTests, _history_test.__name__, _history_test)

for _name, _token in NONFINITE_VARIANTS.items():
    def _cli_test(self, token=_token):
        text = canonical(self.settlement).replace('"outcome":1', '"outcome":' + token)
        self.check_cli_refusal(text)
    _cli_test.__name__ = "test_cli_rejects_nonfinite_" + _name
    setattr(SettlementEvidenceJsonBoundaryTests, _cli_test.__name__, _cli_test)
    def _history_test(self, token=_token):
        # Historical row extensions must not sneak a non-finite JSON fact
        # through a valid settlement proof. Bytes stay immutable but excluded.
        self.check_historical_refusal(lambda text: '{"historical_metric":' + token + ',' + text[1:])
    _history_test.__name__ = "test_replay_excludes_nonfinite_" + _name
    setattr(SettlementEvidenceJsonBoundaryTests, _history_test.__name__, _history_test)


if __name__ == "__main__":
    unittest.main()
