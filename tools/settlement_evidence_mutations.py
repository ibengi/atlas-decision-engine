#!/usr/bin/env python3
"""Settlement evidence semantic mutations in disposable, synthetic copies.

Reuse the phase-aware historical classifier: setup, collection, import and
infrastructure errors never count as behavioral kills. Each target has an
invariant-specific assertion and an independently passing baseline.
"""
import sys

from tools import astra_mutation_probe as probe


TEST = "tests/test_settlement_evidence_binding.py::SettlementEvidenceBindingTests::"
CASES = {
    "SEB01": (
        "evidence identity must equal the recomputed complete object identity",
        "alpha_settlement_evidence.py",
        '    if _text(resolution.get("settlement_evidence_id"), "settlement_evidence_id") != expected_id or \\\n            _text(evidence["settlement_evidence_id"], "evidence identity") != expected_id:',
        '    if False:',
        "test_refuses_both_ids_equal_but_not_content_addressed"),
    "SEB02": (
        "digest must be independently recomputed from retained response bytes",
        "alpha_settlement_evidence.py",
        "    recomputed = hashlib.sha256(raw).hexdigest()",
        "    recomputed = supplied_digest",
        "test_refuses_altered_preimage_without_rehash"),
    "SEB03": (
        "confirmed resolution must persist its response preimage",
        "alpha_ledger.py",
        '                    raise LedgerError("settlement qualification failed: " + reason)\n            return self.log.append(row)',
        '                    raise LedgerError("settlement qualification failed: " + reason)\n            row.pop("settlement_response_preimage", None)\n            return self.log.append(row)',
        "test_persisted_preimage_is_required_for_learning"),
    "SEB04": (
        "response authority must agree even when both authorities are allowed",
        "alpha_settlement_evidence.py",
        '    if _text(response["settlement_authority"], "response authority") != authority:',
        '    if False:',
        "test_refuses_response_authority_rehashed_but_not_outer"),
    "SEB05": (
        "fully rehashed evidence cannot resolve before its prediction",
        "alpha_settlement_validation.py",
        "        if resolved_at <= predicted_at:",
        "        if False:",
        "test_refuses_resolution_before_prediction"),
    "SEB06": (
        "fully rehashed future outcome evidence cannot qualify",
        "alpha_settlement_validation.py",
        "        if resolved_at > current or predicted_at > current:",
        "        if False:",
        "test_refuses_resolution_in_future"),
    "SEB07": (
        "outcome must be supported by the retained response",
        "alpha_settlement_evidence.py",
        '    if type(response["outcome"]) is not int or response["outcome"] not in (0, 1) or \\\n            response["outcome"] != resolution["actual_outcome"]:',
        '    if False:',
        "test_refuses_response_outcome_rehashed_but_not_outer"),
    "SEB08": (
        "evidence must join the exact prediction source and snapshot",
        "alpha_settlement_evidence.py",
        '        if _text(evidence["binding"][key], key) != value:',
        '        if False:',
        "test_refuses_evidence_binding_wrong_snapshot_rehashed"),
    "SEB09": (
        "duplicate serialized evidence members cannot become a qualified receipt",
        "alpha_evidence_json.py",
        "        if key in value:",
        "        if False:",
        "tests/test_settlement_evidence_json_boundary.py::SettlementEvidenceJsonBoundaryTests::test_duplicate_evidence_id_cannot_enter_learning_from_public_cli_bytes"),
}


def register():
    # Replace only this process's registry; the historical tool stays intact.
    probe.MUTATIONS = {}
    probe.SEMANTIC_WITNESSES = {}
    for key, (description, filename, old, new, method) in CASES.items():
        selector = method if method.startswith("tests/") else TEST + method
        probe.MUTATIONS[key] = (description, filename, old, new, [selector])
        assertion = ("self.assertIn('settlement_response_preimage', resolution)"
                     if key == "SEB03" else "self.assertEqual(result['appended'], 0")
        probe.SEMANTIC_WITNESSES[key] = [{
            "node": selector, "assertion": assertion, "invariant": description,
            "exceptions": ["AssertionError"],
        }]


def main(argv=None):
    register()
    return probe.main(argv)


if __name__ == "__main__":
    sys.exit(main())
