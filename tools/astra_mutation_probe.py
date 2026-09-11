#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproducible negative-control mutation runner (Astra AA-17). SHADOW ONLY.

Astra's finding was not "two bugs"; it was that the suite could not TELL. Two
safety mutations passed all 1,670 tests, because the assertions were about
counters and diagnostics rather than about what ended up in the ledger.

This tool re-runs that experiment on demand. For each mutation it:

  1. copies the repository into a throwaway directory (the working tree is
     NEVER modified -- a mutation runner that edits the real source and
     restores it afterwards is one crash away from committing a mutation);
  2. applies one textual mutation that re-introduces a specific finding;
  3. runs the tests that are supposed to detect it;
  4. reports KILLED (the tests failed, as they should) or SURVIVED.

A SURVIVED line is a gap in the suite, not a bug in this tool.

    python tools/astra_mutation_probe.py            # all 11
    python tools/astra_mutation_probe.py --only M04
    python tools/astra_mutation_probe.py --json

Exit code 0 means every mutation was detected; 1 means at least one survived.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: id -> (description, file, old text, new text, detecting test selectors)
#:
#: Each `old` string is asserted to be present before the mutation is applied,
#: so a refactor that moves the code makes this tool fail loudly rather than
#: silently reporting KILLED for a mutation it never managed to apply.
MUTATIONS = {
    "M01": (
        "re-introduce the substituted settlement source (\"kalshi\")",
        "research_feed.py",
        '        if field == "resolution_source" and value is not None:\n'
        '            value = _settlement_source_name(value)\n'
        '            if value is None:\n'
        '                key = None\n',
        '        if field == "resolution_source":\n'
        '            value = _settlement_source_name(value) or "kalshi"\n'
        '            key = key or "settlement_sources"\n',
        ["tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts"],
    ),
    "M02": (
        "re-introduce the 0.0 volume default",
        "research_feed.py",
        '    for field in ("volume", "open_interest"):\n'
        '        if facts.get(field) is None:\n'
        '            continue\n',
        '    for field in ("volume", "open_interest"):\n'
        '        if facts.get(field) is None:\n'
        '            facts[field] = 0.0\n'
        '            provenance[field] = provenance_path(field, field)\n'
        '            unavailable = [f for f in unavailable if f != field]\n'
        '            continue\n',
        ["tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts"],
    ),
    "M03": (
        "use the close time as the expected resolution time",
        "candidate_contract.py",
        '    "expected_resolution_time_utc": ("market", ("expected_expiration_time",\n'
        '                                                "expiration_time")),',
        '    "expected_resolution_time_utc": ("market", ("expected_expiration_time",\n'
        '                                                "expiration_time",\n'
        '                                                "close_time")),',
        ["tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts",
         "tests/test_astra_aa01_aa18_remediation.py::AA03_ContradictoryAliasesSilentlyChosen"],
    ),
    "M04": (
        "ignore per-field provenance (SURVIVED in the rejected candidate)",
        "candidate_contract.py",
        "        for field in REQUIRED_FIELDS:\n"
        "            _check_field_provenance(field, record, provenance, unavailable,\n"
        "                                    errors)\n",
        "        for field in REQUIRED_FIELDS:\n"
        "            pass\n",
        ["tests/test_astra_mutation_regression.py::M04_IgnorePerFieldProvenance"],
    ),
    "M05": (
        "exclude provenance from the checksum",
        "candidate_contract.py",
        '    return {k: v for k, v in record.items() if k != "record_sha256"}',
        '    return {k: v for k, v in record.items()\n'
        '            if k not in ("record_sha256", "field_provenance",\n'
        '                         "quote_observation", "unavailable_fields")}',
        ["tests/test_astra_mutation_regression.py::M05_ProvenanceExcludedFromTheChecksum"],
    ),
    "M06": (
        "accept legacy v1 records",
        "candidate_contract.py",
        'LEGACY_FEED_SCHEMAS = ("atlas-research-candidate-v1",\n'
        '                       "atlas-research-candidate-v2")',
        'LEGACY_FEED_SCHEMAS = ()',
        ["tests/test_astra_mutation_regression.py::M11_AcceptLegacyWhileKeepingDiagnostics",
         "tests/test_alpha_feed_readiness.py"],
    ),
    "M07": (
        "allow a missing book side",
        "candidate_contract.py",
        "    for field in QUOTE_FIELDS:\n"
        "        kind = observation.get(field)\n",
        "    for field in ():\n"
        "        kind = observation.get(field)\n",
        ["tests/test_astra_aa01_aa18_remediation.py::AA01_DerivedQuotesArePresentedAsObserved",
         "tests/test_astra_mutation_regression.py::M01_M03_SubstitutedMarketFacts"],
    ),
    "M08": (
        "add a forbidden execution import to the producer",
        "research_feed.py",
        "from config import CFG\n",
        "from config import CFG\nimport order_manager  # noqa: F401\n",
        ["tests/test_research_feed_boundary.py",
         "tests/test_alpha_safety_boundary.py"],
    ),
    "M09": (
        "let a producer exception propagate into the decision cycle",
        "research_feed.py",
        "        except Exception as e:                                # noqa: BLE001\n"
        "            self.rejected += 1\n"
        '            log.warning(f"[RESEARCH_FEED] candidate dropped: "\n'
        '                        f"{type(e).__name__}: {e}")\n'
        "            return False\n",
        "        except Exception:                                     # noqa: BLE001\n"
        "            raise\n",
        ["tests/test_astra_mutation_regression.py::M09_ProducerExceptionPropagation",
         "tests/test_alpha_automatic_feed.py"],
    ),
    "M10": (
        "overwrite historical bytes instead of appending",
        "durable_append.py",
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)",
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)",
        ["tests/test_astra_mutation_regression.py::M10_OverwriteHistoricalBytes",
         "tests/test_astra_aa01_aa18_remediation.py::AA12_ShortWritesAndTornTails"],
    ),
    "M11": (
        "accept legacy while preserving the diagnostics "
        "(SURVIVED in the rejected candidate)",
        "alpha_consumer.py",
        '        if record.get("schema") in LEGACY_FEED_SCHEMAS:\n'
        '            self.stats["legacy_schema"] += 1\n'
        '            self.last_refusal = f"legacy schema {record.get(\'schema\')!r}"\n'
        '            log.warning("[ALPHA_CONSUMER] refusing legacy feed record "\n'
        '                        f"{record.get(\'schema\')!r}: it may carry substituted "\n'
        '                        f"market facts and must be re-observed")\n'
        "            return None\n",
        '        if record.get("schema") in LEGACY_FEED_SCHEMAS:\n'
        '            self.stats["legacy_schema"] += 1\n'
        '            log.warning("[ALPHA_CONSUMER] refusing legacy feed record "\n'
        '                        f"{record.get(\'schema\')!r}: it may carry substituted "\n'
        '                        f"market facts and must be re-observed")\n'
        '            record = dict(record, schema=FEED_SCHEMA)\n'
        '            record["record_sha256"] = __import__(\n'
        '                "candidate_contract").compute_checksum(record)\n',
        ["tests/test_astra_mutation_regression.py::M11_AcceptLegacyWhileKeepingDiagnostics"],
    ),
}


def _copy_repo(destination):
    def _ignore(directory, names):
        return {n for n in names
                if n in (".git", "__pycache__", ".pytest_cache", "venv",
                         ".venv", "node_modules")}
    shutil.copytree(REPO, destination, ignore=_ignore, symlinks=True)


def run_one(key, verbose=False):
    description, filename, old, new, selectors = MUTATIONS[key]
    workdir = tempfile.mkdtemp(prefix=f"astra-mut-{key}-")
    try:
        root = os.path.join(workdir, "repo")
        _copy_repo(root)
        target = os.path.join(root, filename)
        source = open(target, encoding="utf-8").read()
        if old not in source:
            return {"mutation": key, "description": description,
                    "status": "NOT_APPLIED",
                    "detail": f"anchor text absent from {filename}; the "
                              f"mutation could not be applied, so this run "
                              f"proves nothing"}
        open(target, "w", encoding="utf-8").write(source.replace(old, new, 1))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p",
             "no:cacheprovider", *selectors],
            cwd=root, capture_output=True, text=True, timeout=1800)
        killed = proc.returncode != 0
        return {"mutation": key, "description": description,
                "status": "KILLED" if killed else "SURVIVED",
                "detecting_tests": selectors,
                "detail": (proc.stdout or "")[-1500:] if verbose or not killed
                else (proc.stdout or "").strip().splitlines()[-1:]}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=None,
                        help="run just these mutation ids")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    keys = args.only or sorted(MUTATIONS)
    results = [run_one(key, verbose=args.verbose) for key in keys]
    survived = [r for r in results if r["status"] != "KILLED"]
    summary = {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "mutations_run": len(results),
        "killed": sum(1 for r in results if r["status"] == "KILLED"),
        "survivors": len(survived),
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for row in results:
            print(f"{row['mutation']}  {row['status']:<12} {row['description']}")
            if row["status"] != "KILLED":
                print(f"    {row['detail']}")
        print(f"\n{summary['killed']}/{summary['mutations_run']} killed, "
              f"{summary['survivors']} survivor(s)")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
