"""V4-RA-18: retained real pytest counterexamples and hostile neighbors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import astra_mutation_probe as probe

ROOT = Path(__file__).resolve().parents[1]


class V4RA18TruthfulMutationEvidence(unittest.TestCase):
    def run_case(self, source):
        with tempfile.TemporaryDirectory(prefix="astra-v5-classifier-") as tmp:
            test = Path(tmp, "test_witness.py")
            test.write_text(source, encoding="utf-8")
            receipt = Path(tmp, "phases.json")
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "-p", "tools.astra_mutation_pytest", "--astra-phase-file",
                 str(receipt), str(test)], cwd=tmp, env=env,
                capture_output=True, text=True, timeout=30)
            evidence = json.loads(receipt.read_text()) if receipt.exists() else None
            return proc, evidence

    def classify(self, source, assertion="assert actual_minted == 0"):
        proc, evidence = self.run_case(source)
        witness = {"node": "test_witness.py::", "assertion": assertion,
                   "invariant": "zero snapshots from inadmissible evidence"}
        return probe._classify(proc, evidence, [witness])[0]

    def test_original_unittest_setup_failure_is_not_a_behavioral_kill(self):
        self.assertEqual(self.classify('''import unittest
class BrokenFixture(unittest.TestCase):
    def setUp(self):
        raise RuntimeError("synthetic unavailable test fixture")
    def test_body(self):
        raise AssertionError("TEST BODY MUST NOT RUN")
'''), "INCONCLUSIVE_SETUP")

    def test_original_diagnostic_only_assertion_is_not_a_behavioral_kill(self):
        self.assertEqual(self.classify('''def test_refusal_message():
    baseline_appended = mutated_appended = 0
    assert baseline_appended == mutated_appended
    assert "authority not in allow-list" == "unqualified authority"
'''), "DIAGNOSTIC_ONLY")

    def test_pytest_fixture_failure_is_not_a_behavioral_kill(self):
        self.assertEqual(self.classify('''import pytest
@pytest.fixture
def broken():
    raise AssertionError("actual_minted == 0")
def test_body(broken):
    actual_minted = 1
    assert actual_minted == 0
'''), "INCONCLUSIVE_SETUP")

    def test_unittest_teardown_after_semantic_failure_invalidates_experiment(self):
        self.assertEqual(self.classify('''import unittest
class BrokenCleanup(unittest.TestCase):
    def tearDown(self):
        raise RuntimeError("synthetic cleanup fault")
    def test_body(self):
        actual_minted = 1
        assert actual_minted == 0
'''), "INCONCLUSIVE_SETUP")

    def test_collection_failure_is_not_a_behavioral_kill(self):
        self.assertEqual(self.classify('''raise RuntimeError("cannot collect")
'''), "INCONCLUSIVE_COLLECTION")

    def test_missing_import_is_not_a_behavioral_kill(self):
        self.assertEqual(self.classify('''import astra_nonexistent_synthetic_module
'''), "INCONCLUSIVE_IMPORT")

    def test_real_semantic_assertion_with_reviewed_witness_is_killed(self):
        self.assertEqual(self.classify('''def test_body():
    actual_minted = 1
    assert actual_minted == 0
'''), "KILLED_BEHAVIORALLY")

    def test_semantic_looking_message_is_not_the_reviewed_assertion(self):
        self.assertEqual(self.classify('''def test_diagnostic():
    assert "actual_minted == 0" == "some other wording"
'''), "DIAGNOSTIC_ONLY")

    def test_missing_phase_receipt_cannot_be_rescued_by_failed_summary(self):
        proc = subprocess.CompletedProcess([], 1, stdout="1 failed in 0.01s", stderr="")
        self.assertEqual(probe._classify(proc)[0], "INCONCLUSIVE_INFRASTRUCTURE")

    def test_no_executed_body_is_collection_inconclusive(self):
        self.assertEqual(self.classify("value = 1\n"), "INCONCLUSIVE_COLLECTION")

    def test_successful_semantic_control_survives(self):
        self.assertEqual(self.classify('''def test_body():
    actual_minted = 0
    assert actual_minted == 0
'''), "SURVIVED")

    def test_summary_does_not_count_inconclusive_or_diagnostics_as_kills(self):
        rows = [{"status": status, "effective_safety_mutation": True}
                for status in probe.STATUSES]
        summary = probe.summarize(rows)
        self.assertEqual(summary["behavioural_kills"], 1)
        self.assertEqual(summary["kills_with_setup_errors"], 0)
        self.assertEqual(summary["unresolved_effective_safety_mutations"], 7)
        self.assertFalse(summary["gate_passed"])

    def test_declared_diagnostic_control_is_honest_without_requiring_false_kill(self):
        summary = probe.summarize([
            {"status": "KILLED_BEHAVIORALLY", "effective_safety_mutation": True},
            {"status": "DIAGNOSTIC_ONLY", "effective_safety_mutation": False}])
        self.assertTrue(summary["gate_passed"])
        self.assertEqual(summary["behavioural_kills"], 1)
        self.assertEqual(summary["diagnostic_only"], 1)
        self.assertEqual(summary["surviving_effective_safety_mutations"], 0)


if __name__ == "__main__":
    unittest.main()
