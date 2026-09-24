#!/usr/bin/env python3
"""Targeted safety mutations in isolated, network-disabled source copies.

Usage: python tools/mutate_positions_completeness.py --output /absolute/result.json
Never modifies the source checkout. Exit zero only when the clean control
passes and every mutant is killed by a test assertion (not collection error).
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
SUITE = "tests/test_positions_completeness.py"
MUTANTS = [
    ("ignore_partial_content_range", "test_http_200_content_range_cannot_establish_match", "kalshi_client.py", [
        ('if expected_status is not None and "Content-Range" in r.headers:', 'if False:')]),
    ("lossy_native_json_float", "test_raw_numeric_quantity_precision_cannot_round_or_underflow_to_match", "kalshi_client.py", [
        ('parse_float=Decimal', 'parse_float=float')]),
    ("last_json_member_wins", "test_raw_duplicate_json_members_fail_closed_before_decoding_loses_evidence", "kalshi_client.py", [
        ('if key in result:', 'if False:')]),
    ("accept_nonstandard_json_constant", "test_raw_nonstandard_json_constants_in_metadata_fail_closed", "kalshi_client.py", [
        ('raise PositionResponseIncomplete(0, "non-standard numeric constant in complete JSON response")',
         'return float(value)')]),
    ("follow_redirects_with_authentication", "test_complete_reads_disable_redirects_and_reject_redirect_status_before_json", "kalshi_client.py", [
        ('kw["allow_redirects"] = False', 'kw["allow_redirects"] = True')]),
    ("trust_bare_list", "test_bare_lists_iterators_dicts_and_duck_typed_proof_are_unknown", "position_manager.py", [
        ('return None, "position response completeness not proven"',
         'broker = PositionSnapshot(json.dumps(broker), ("",), (0,), (0,), 0)')]),
    ("unknown_envelope_as_empty", "test_unknown_missing_partial_and_malformed_envelopes_fail_closed", "kalshi_client.py", [
        ('            r = self._req("GET", "/portfolio/positions", params=params,\n                          expected_status=200)\n',
         '            r = self._req("GET", "/portfolio/positions", params=params,\n                          expected_status=200)\n            if type(r) is dict and "market_positions" not in r:\n                r = {"market_positions": [], "event_positions": [], "cursor": ""}\n')]),
    ("missing_cursor_as_terminal", "test_unknown_missing_partial_and_malformed_envelopes_fail_closed", "kalshi_client.py", [
        ('set(r) != {"market_positions", "event_positions", "cursor"}',
         'set(r) - {"market_positions", "event_positions", "cursor"}'),
        ('next_cursor = r["cursor"]', 'next_cursor = r.get("cursor", "")')]),
    ("discard_later_page_rows", "test_nonempty_later_page_is_not_lost", "kalshi_client.py", [
        ('            rows.extend(r["market_positions"])',
         '            if not cursor:\n                rows.extend(r["market_positions"])')]),
    ("ignore_repeated_cursor", "test_cursor_cycles_never_return_accumulated_rows", "kalshi_client.py", [
        ('or next_cursor in cursors', 'or False')]),
    ("accept_unsupported_scope", "test_unknown_or_unsupported_inventory_fails_closed", "kalshi_client.py", [
        ('if scopes != {0}:', 'if False:'),
        ('return tuple(sorted(scopes))', 'return (0,)')]),
    ("skip_closing_inventory", "test_scope_change_or_late_inventory_failure_discards_observation", "kalshi_client.py", [
        ('after = self._position_subaccounts()', 'after = before')]),
    ("late_error_as_complete_empty", "test_late_page_errors_never_match_or_release_prior_halt", "kalshi_client.py", [
        ('            r = self._req("GET", "/portfolio/positions", params=params,\n                          expected_status=200)',
         '            try:\n                r = self._req("GET", "/portfolio/positions", params=params, expected_status=200)\n            except KalshiAPIError:\n                return PositionSnapshot("[]", ("",), before, before, 0)')]),
    ("accept_partial_http", "test_transport_rejects_partial_http_206_even_with_valid_json", "kalshi_client.py", [
        ('if expected_status is not None and r.status_code != expected_status:', 'if False:')]),
    ("discard_event_evidence", "test_event_exposure_cannot_hide_behind_empty_or_flat_market_positions", "kalshi_client.py", [
        ('event_payload = json.dumps(events, allow_nan=False, sort_keys=True)',
         'event_payload = json.dumps([], allow_nan=False, sort_keys=True)')]),
    ("ignore_event_binding", "test_unrelated_event_exposure_cannot_match_nonflat_market", "position_manager.py", [
        ('and not any(bindings.get(ticker) == event["event_ticker"] for ticker in net)',
         'and False')]),
    ("ignore_zero_quantity_exposure", "test_zero_position_with_nonzero_market_exposure_is_unknown", "position_manager.py", [
        ('if qty == 0 and any(', 'if False and any(')]),
    ("mutable_snapshot", "test_snapshot_rows_are_immutable_and_iteration_is_detached", "kalshi_client.py", [
        ('@dataclass(frozen=True)', '@dataclass(frozen=False)')]),
    ("accept_missing_provider_fields", "test_missing_required_provider_fields_fail_at_adapter", "kalshi_client.py", [
        ('if require_provider_fields and not event:', 'if False:')]),
    ("truncate_fractional_quantity", "test_unknown_missing_partial_and_malformed_envelopes_fail_closed", "position_manager.py", [
        ('if fv != int(fv):', 'if False:')]),
]


def run_case(name, selector, filename=None, replacements=()):
    with tempfile.TemporaryDirectory(prefix="atlas_position_mutant_") as tmp:
        work = Path(tmp)
        (work / "tests").mkdir()
        for source in REPO.glob("*.py"):
            shutil.copy2(source, work / source.name)
        for name_to_copy in ("test_positions_completeness.py", "position_snapshot_fixture.py"):
            shutil.copy2(REPO / "tests" / name_to_copy, work / "tests" / name_to_copy)
        shutil.copy2(REPO / "pytest.ini", work / "pytest.ini")
        (work / "sitecustomize.py").write_text(
            'import socket\n'
            'def deny(*a, **kw):\n    raise AssertionError("REAL_NETWORK_FORBIDDEN")\n'
            'socket.socket.connect = deny\nsocket.socket.connect_ex = deny\n'
            'socket.create_connection = deny\n')
        if filename:
            target = work / filename
            source = target.read_text()
            for old, new in replacements:
                if source.count(old) != 1:
                    raise RuntimeError(f"{name}: mutation anchor count={source.count(old)}")
                source = source.replace(old, new, 1)
            target.write_text(source)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(work) + os.pathsep + env.get("PYTHONPATH", "")
        command = [sys.executable, "-B", "-m", "pytest", selector, "-q"]
        run = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=30)
        log = run.stdout + run.stderr
        expected = run.returncode == (1 if filename else 0)
        killed = bool(filename and expected and "AssertionError" in log
                      and "tests/test_positions_completeness.py::" in log
                      and ("FAILED tests/" in log or "SUBFAILED(" in log)
                      and "ERROR collecting" not in log and "REAL_NETWORK_FORBIDDEN" not in log)
        return {"name": name, "returncode": run.returncode,
                "outcome": "KILLED" if killed else ("CONTROL_PASS" if not filename and expected else "FAILED"),
                "selector": selector, "log": log}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [run_case("clean_control", SUITE)]
    if results[0]["outcome"] == "CONTROL_PASS":
        for name, method, filename, replacements in MUTANTS:
            result = run_case(name, SUITE + "::PositionCompletenessTests::" + method, filename, replacements)
            results.append(result)
            print(name + ": " + result["outcome"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"real_network_allowed": False, "isolated_copies": True,
                                       "results": results}, indent=2) + "\n")
    return 0 if len(results) == len(MUTANTS) + 1 and all(
        x["outcome"] in ("CONTROL_PASS", "KILLED") for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
