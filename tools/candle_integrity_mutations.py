#!/usr/bin/env python3
"""Targeted HIGH candle-gate mutations, isolated in disposable repo copies.

Run with the same Python environment as run_tests.py. No network, broker,
credentials, ledger or live runtime access. A mutant must make a permanent
regression test fail; timeout/process errors are not counted as kills.
"""
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = {
    "cadence_guard_removed": (
        'if previous is not None and ts - previous != KLINE_INTERVAL_S:',
        'if False:'),
    "minute_grid_guard_removed": (
        'if ts <= 0 or ts % KLINE_INTERVAL_S != 0:', 'if ts <= 0:'),
    "closed_guard_removed": (
        'if row.get("closed") is not True or row["close_ts"] > now:',
        'if row["close_ts"] > now:'),
    "close_time_guard_removed": (
        'if row["close_ts"] != ts + KLINE_INTERVAL_S:', 'if False:'),
    "freshness_guard_removed": (
        'if now - rows[-1]["ts"] > MAX_KLINE_AGE_S:', 'if False:'),
    "count_guard_removed": (
        'if require_min and len(rows) < MIN_KLINES:', 'if False:'),
    "provenance_product_binding_removed": (
        'or provenance.get("product") != product', 'or False'),
    "provenance_observation_bounds_removed": (
        'if not _finite_number(observed) or not row["close_ts"] <= observed <= now:',
        'if not _finite_number(observed):'),
    "provenance_provider_binding_removed": (
        'expected_provider is not None and provider != expected_provider', 'False'),
    "stale_label_accepted": (
        'if kl_src.startswith("stale_cache")', 'if False'),
    "context_cache_expiry_removed": (
        'and cached_ctx.generated_ts <= now <= cached_ctx.data_valid_until_ts', 'and True'),
    "kraken_uncommitted_tail_reused": (
        'if provider == "kraken" or out[-1]["ts"] == boundary:',
        'if out[-1]["ts"] == boundary:'),
    "kraken_envelope_completeness_removed": (
        'not isinstance(d, dict) or set(d) != {"error", "result"}',
        'not isinstance(d, dict)'),
    "transport_origin_binding_removed": (
        'if (actual.scheme, actual.netloc, actual.path) != (',
        'if False and (actual.scheme, actual.netloc, actual.path) != ('),
    "partial_transport_allowed": (
        'if r.status_code != 200:', 'if False:'),
    "transport_metadata_unknown_allowed": (
        'and set(meta).issubset({"http_status", "elapsed_ms", "error"})',
        'and True'),
    "duplicate_json_member_guard_removed": (
        'object_pairs_hook=_unique_json_object', 'object_pairs_hook=dict'),
    "nonstandard_json_constant_guard_removed": (
        'parse_constant=_reject_json_constant', 'parse_constant=float'),
    "wire_timestamp_type_guard_removed": (
        'if type(raw[0]) is not int:', 'if False:'),
    "wire_close_timestamp_type_guard_removed": (
        'if provider == "binance" and type(raw[6]) is not int:', 'if False:'),
    "json_decimal_precision_erased": (
        'parse_float=Decimal', 'parse_float=float'),
    "raw_decimal_bounds_guard_removed": (
        'or el > min(eo, ec) or max(eo, ec) > eh', 'or False'),
    "nonzero_underflow_guard_removed": (
        'or (exact != 0 and result == 0)', 'or False'),
    "http_content_range_guard_removed": (
        'if "Content-Range" in r.headers:', 'if False:'),
}


def run(repo):
    result = subprocess.run([sys.executable, '-B', '-m', 'pytest',
                             'tests/test_candle_integrity.py', '-q'],
                            cwd=repo, capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout + result.stderr


def classify_result(code, output):
    """Only a completed pytest assertion-failure run can kill a mutant.

    Keep the actual pytest summaries/node names so each kill is attributable.
    Collection, import, setup and process failures are evidence errors, not kills.
    """
    summaries = re.findall(
        r"^(\d+ failed(?:, [^\n]+)? in \d+(?:\.\d+)?s(?: \([^\n]*\))?)$",
        output, re.MULTILINE)
    failure_matches = re.findall(
        r"^(FAILED|SUBFAILED\([^\n]*\)) (tests/test_candle_integrity\.py::[^\n]+)$",
        output, re.MULTILINE)
    failed_lines = [f"{kind} {node}" for kind, node in failure_matches]
    error_outcome = bool(re.search(
        r"^(?:ERROR|SUBERROR)(?:\s|\(|$)|^_{2,}\s+ERROR|\b\d+ (?:subtests? )?errors?\b",
        output, re.MULTILINE))
    import_or_syntax_error = bool(re.search(
        r"\b(?:ImportError|ModuleNotFoundError|SyntaxError|IndentationError)\b", output))
    summary = summaries[-1] if summaries else None
    failed_count = int(summary.split()[0]) if summary else 0
    killed = (code == 1 and failed_count > 0 and bool(failed_lines)
              and not error_outcome and not import_or_syntax_error
              and 'AssertionError' in output)
    return {
        "status": "KILLED" if killed else "SURVIVED_OR_ERROR",
        "returncode": code,
        "pytest_summary": summary,
        "pytest_failed_count": failed_count,
        "failed_test_nodes": [node.split(' - ', 1)[0] for _, node in failure_matches],
        "failure_summaries": failed_lines,
        "error_outcome": error_outcome,
        "import_or_syntax_error": import_or_syntax_error,
    }


def _check_classifier():
    """Pinned pytest 9 subtest evidence shapes, including rejected error output."""
    node = 'tests/test_candle_integrity.py::Boundary::test_guard'
    subtest = (f"E AssertionError: boundary admitted bad input\n"
               f"SUBFAILED(field='close_ts') {node}\n"
               "1 failed, 30 passed, 96 subtests passed in 0.28s\n")
    classified = classify_result(1, subtest)
    assert classified['status'] == 'KILLED'
    assert classified['failed_test_nodes'] == [node]
    assert classified['failure_summaries'] == [f"SUBFAILED(field='close_ts') {node}"]
    assert classify_result(1, subtest + f"SUBERROR(field='other') {node}\n")[
        'status'] == 'SURVIVED_OR_ERROR'
    assert classify_result(1, subtest.replace('30 passed', '30 passed, 1 subtest error'))[
        'status'] == 'SURVIVED_OR_ERROR'


def main():
    _check_classifier()
    code, output = run(ROOT)
    if code:
        print(output)
        raise SystemExit('Baseline must pass before mutation execution')
    report = {"baseline": "PASS", "mutants": {},
              "execution_scope": "offline mocked provider tests; network counter not measured"}
    source = (ROOT / 'btc_context.py').read_text()
    with tempfile.TemporaryDirectory(prefix='atlas-candle-mutations-') as scratch:
        repo = Path(scratch) / 'repo'
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(
            '.git', '__pycache__', '.pytest_cache', 'test_report.json', 'logs', 'data'))
        for name, (old, new) in MUTANTS.items():
            if source.count(old) != 1:
                raise SystemExit(f'{name}: mutation anchor must occur exactly once')
            # mtime/size collisions between adjacent mutations must never
            # permit execution of bytecode from a preceding mutant.
            for cache in repo.rglob('__pycache__'):
                shutil.rmtree(cache)
            (repo / 'btc_context.py').write_text(source.replace(old, new))
            code, output = run(repo)
            result = classify_result(code, output)
            report['mutants'][name] = result
            print(f'{name}: {result["status"]}; {result["pytest_summary"]}', flush=True)
            for failure in result['failure_summaries']:
                print(f'  {failure}', flush=True)
            if result['status'] != 'KILLED':
                print(output)
    report['killed'] = sum(v['status'] == 'KILLED' for v in report['mutants'].values())
    report['total'] = len(MUTANTS)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['killed'] == report['total'] else 1)


if __name__ == '__main__':
    main()
