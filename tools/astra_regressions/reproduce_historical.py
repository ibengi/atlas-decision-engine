"""Re-execute all 59 historical findings against local Git trees, offline.

The inventory contains identifiers and assertions, never computed test results.
Both phases execute complete suites in fresh isolated subprocesses. An expected
baseline assertion failure demonstrates the old defect; exceptions and missing
cases never count as reproduction. No repository or network mutation is needed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
BOOTSTRAP = """
import importlib.util, pathlib, runpy, sys
repo, script, output = map(pathlib.Path, sys.argv[1:4])
sys.path.insert(0, str(script.parent))
for module in ('config', 'persistence', 'continuity', 'equity_ledger',
               'execution_engine', 'order_manager', 'position_manager',
               'trade_logger', 'risk_manager', 'kalshi_client'):
    spec = importlib.util.find_spec(module)
    if spec is None or pathlib.Path(spec.origin).resolve().parent != repo:
        raise RuntimeError('Wrong production module source: ' + module)
sys.argv = [str(script), str(output)]
runpy.run_path(str(script), run_name='__main__')
"""


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   text=True, env=env).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_receipt(repo: Path) -> dict:
    names = git(repo, "ls-files", "-z").split("\0")
    names += git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    source_hashes = {name: digest(repo / name) for name in names
                     if name.endswith(".py") and (repo / name).is_file()}
    encoded = json.dumps(source_hashes, sort_keys=True, separators=(",", ":"))
    return {"commit": git(repo, "rev-parse", "HEAD"),
            "tree": git(repo, "rev-parse", "HEAD^{tree}"),
            "working_tree_changes": git(repo, "status", "--porcelain", "--untracked-files=all"),
            "python_source_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "python_files": source_hashes}


def run_phase(phase: str, repo: Path, output: Path, inventory: dict,
              allow_dirty: bool) -> dict:
    start = source_receipt(repo)
    if phase == "before" and start["commit"] != inventory["baseline_sha"]:
        raise ValueError("BEFORE must be the exact rejected 3af848e commit")
    if start["working_tree_changes"] and (phase == "before" or not allow_dirty):
        raise ValueError("Source changes require a clean tree; exploratory AFTER "
                         "runs alone may explicitly use --allow-dirty-after")
    dest = output / phase
    dest.mkdir(parents=True, exist_ok=True)
    suites = {}
    for label, spec in inventory["suites"].items():
        script = (HERE / spec[phase + "_script"]).resolve()
        report, log = dest / (label + ".json"), dest / (label + ".log")
        # Remove only output files owned by this invocation. A stale report must
        # never substitute for a crashed or otherwise incomplete subprocess.
        report.unlink(missing_ok=True)
        command = [sys.executable, str(HERE / "run_isolated.py"), str(repo),
                   str(log), "-c", BOOTSTRAP, str(repo), str(script), str(report)]
        print(f"{phase}: executing {label}", flush=True)
        try:
            process = subprocess.run(command, timeout=180, check=False)
            payload = json.loads(report.read_text())
            rows = payload.get("results")
            if not isinstance(rows, list) or len(rows) != spec["expected_case_count"]:
                raise ValueError("Incomplete result collection")
            index = {row["id"]: row for row in rows}
            if len(index) != len(rows):
                raise ValueError("Duplicate case identity")
            embedded = payload.get("candidate", payload.get("target", payload.get("commit")))
            if embedded is not None and embedded != start["commit"]:
                raise ValueError("Harness reported a different commit")
            suites[label] = {"exit_code": process.returncode, "case_count": len(rows),
                             "counts": dict(Counter(row["status"] for row in rows)),
                             "results": index, "script_sha256": digest(script),
                             "report_sha256": digest(report), "log_sha256": digest(log)}
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            suites[label] = {"execution_error": repr(exc), "results": {}}
    end = source_receipt(repo)
    stable = start == end
    cases = []
    for expected in inventory["cases"]:
        suite = suites[expected["suite"]]
        actual = suite["results"].get(expected["id"], {})
        if phase == "before":
            passed = (actual.get("status") == "FAIL" and
                      actual.get("reason") == expected["baseline_assertion"])
        else:
            passed = actual.get("status") == "PASS"
        cases.append({"suite": expected["suite"], "id": expected["id"],
                      "observed_status": actual.get("status", "NOT_EXECUTED"),
                      "status": "PASS" if passed and stable else "NEEDS_REVIEW",
                      "observed_assertion": actual.get("reason")})
    complete = all("execution_error" not in suite for suite in suites.values())
    # A passing AFTER run requires every control, including the seven formerly
    # inconclusive rows, to pass. They are not silently relabelled as old bugs.
    if phase == "after":
        complete = complete and all(suite.get("exit_code") == 0 and
                                    set(suite.get("counts", {})) == {"PASS"}
                                    for suite in suites.values())
    inconclusive = []
    for expected in inventory["baseline_inconclusive"]:
        actual = suites[expected["suite"]]["results"].get(expected["id"], {})
        inconclusive.append({**expected, "observed_status": actual.get("status", "NOT_EXECUTED"),
                             "reason": actual.get("reason")})
    return {"status": "PASS" if complete and stable and
            all(row["status"] == "PASS" for row in cases) else "NEEDS_REVIEW",
            "source": start, "source_unchanged_during_execution": stable,
            "exact_committed_source": not bool(start["working_tree_changes"]),
            "reproduced_or_safe_cases": sum(row["status"] == "PASS" for row in cases),
            "claimed_unsafe_cases": len(cases), "cases": cases,
            "historically_inconclusive": inconclusive, "suites": suites}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, help="Local clean checkout at 3af848e")
    parser.add_argument("--after", type=Path, help="Local checkout under final review")
    parser.add_argument("--output", type=Path, required=True,
                        help="Evidence directory outside the reviewed checkouts")
    parser.add_argument("--phase", choices=("before", "after", "both"), default="both")
    parser.add_argument("--allow-dirty-after", action="store_true",
                        help="Exploratory execution only; marks result as uncommitted")
    args = parser.parse_args()
    inventory = json.loads((HERE / "historical_cases.json").read_text())
    if len(inventory["cases"]) != 59 or len({(r["suite"], r["id"])
                                            for r in inventory["cases"]}) != 59:
        parser.error("Historical inventory must contain 59 distinct cases")
    for name, expected in inventory["frozen_source_sha256"].items():
        if digest(HERE / name) != expected:
            parser.error("Preserved baseline harness changed: " + name)
    phases = ("before", "after") if args.phase == "both" else (args.phase,)
    output = args.output.resolve()
    report = {"schema": 1, "inventory_sha256": digest(HERE / "historical_cases.json"),
              "capital_enabled": False, "real_broker_writes": 0,
              "external_sockets_permitted": False, "phases": {}}
    for phase in phases:
        repo = getattr(args, phase)
        if repo is None:
            parser.error("--" + phase + " is required for the selected phase")
        repo = repo.resolve()
        if output == repo or repo in output.parents:
            parser.error("Evidence output must be outside each reviewed checkout")
        report["phases"][phase] = run_phase(phase, repo, output, inventory,
                                            args.allow_dirty_after)
    report["status"] = "PASS" if all(r["status"] == "PASS" for r in
                                     report["phases"].values()) else "NEEDS_REVIEW"
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / ("reproduction-" + args.phase + ".json")
    result_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({"status": report["status"], "report": str(result_path),
                      "cases": {name: row["reproduced_or_safe_cases"]
                                for name, row in report["phases"].items()}}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
