"""Disposable, offline LI05 semantic mutations; no infrastructure failure kills.

Usage: python tools/li05_identity_mutations.py OUTPUT_DIRECTORY
Run under tools/audit_isolation/run_isolated.py with local dependencies.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

REPO = pathlib.Path(__file__).resolve().parents[1]
CASES = [
    ("LI05-M01", "alpha_schema.py", "        model=model,\n        model_version=str(data.get", "        model=str(data.get('model') or model),\n        model_version=str(data.get", "test_w04_generated_text_cannot_change_model_attribution"),
    ("LI05-M02", "alpha_providers.py", "or redact(canonical(body)) != canonical(body)\n                    or not secret_free(body, redactor=redact)", "or False", "test_unicode_escaped_key_echo_is_refused_even_inside_forecast_text"),
    ("LI05-M03", "alpha_learning.py", "if qualified_prediction_signal(row, key, signal)})", "if True})", "test_configured_astra_report_selector_cannot_bypass_qualification"),
    ("LI05-M04", "alpha_dispatcher.py", '    if meta.get("provider_identity_receipt") is not None:', "    if False:", "test_custom_provider_cannot_self_authenticate_receipt_metadata"),
    ("LI05-M05", "alpha_ledger.py", 'raise LedgerError("provider response/request identity reused across predictions")', "pass", "test_duplicate_response_across_requests_refused_after_restart"),
    ("LI05-M06", "alpha_identity.py", 'or receipt["environment"] != environment', "or False", "test_receipt_cannot_be_rebound_to_environment_contract_snapshot_or_output"),
    ("LI05-M07", "alpha_identity.py", 'return provider + "/" + model', "return model", "test_two_provider_envelopes_with_same_model_name_remain_distinguishable"),
    ("LI05-M08", "alpha_identity.py", 'or t["verified_tls"] is not True', "or False", "test_receipt_schema_strict_types_and_unknown_extensions_refuse"),
    ("LI05-M09", "alpha_identity.py", 'elif type(actual) not in (int, float) or actual != expected:', 'elif actual != expected:', "test_boolean_forecast_scalar_cannot_match_numeric_authenticated_value"),
    ("LI05-M10", "alpha_identity.py", "if key in out:", "if False:", "test_duplicate_json_keys_and_secret_bearing_envelopes_refused_before_retention"),
]


def run(repo, out, label, node):
    xml = out / (label + ".xml")
    with (out / (label + ".log")).open("w") as stream:
        process = subprocess.run([sys.executable, "-m", "pytest", "-q", node,
                                  "--junitxml", str(xml)], cwd=repo,
                                 stdout=stream, stderr=subprocess.STDOUT)
    try:
        suites = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return process.returncode, "INCONCLUSIVE_INFRASTRUCTURE"
    tests = suites.findall(".//testcase")
    if not tests:
        return process.returncode, "INCONCLUSIVE_COLLECTION"
    if suites.findall(".//error"):
        return process.returncode, "INCONCLUSIVE_SETUP"
    failures = suites.findall(".//failure")
    if process.returncode == 1 and failures and all(
            "AssertionError" in (item.text or "") or "Failed: DID NOT RAISE" in (item.text or "")
            for item in failures):
        return process.returncode, "KILLED_BEHAVIORALLY"
    if process.returncode == 0:
        return process.returncode, "SURVIVED"
    return process.returncode, "DIAGNOSTIC_ONLY"


def main():
    out = pathlib.Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    baseline_code, baseline = run(REPO, out, "BASELINE", "tests/test_alpha_provider_identity.py")
    if baseline_code != 0:
        (out / "results.json").write_text(json.dumps({"baseline": "FAILED", "exit_code": baseline_code}))
        return 2
    results = []
    for mutation_id, filename, before, after, test in CASES:
        with tempfile.TemporaryDirectory(prefix="atlas-li05-mutation-") as temporary:
            clone = pathlib.Path(temporary) / "repo"
            shutil.copytree(REPO, clone, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".pytest_cache", "test_report.json"))
            path = clone / filename
            source = path.read_text()
            if source.count(before) != 1:
                results.append({"id": mutation_id, "classification": "NOT_APPLIED"})
                continue
            path.write_text(source.replace(before, after, 1))
            node = "tests/test_alpha_provider_identity.py::ProviderIdentityTests::" + test
            code, classification = run(clone, out, mutation_id, node)
            results.append({"id": mutation_id, "file": filename,
                            "semantic_witness": node, "exit_code": code,
                            "classification": classification})
    report = {"baseline": "PASS", "mutations": results,
              "surviving_effective_safety_mutations": sum(r["classification"] == "SURVIVED" for r in results),
              "unresolved_classifications": sum(r["classification"] != "KILLED_BEHAVIORALLY" for r in results),
              "broker_writes": 0, "real_provider_requests": 0}
    (out / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["unresolved_classifications"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
