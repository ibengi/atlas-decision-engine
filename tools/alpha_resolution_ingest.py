#!/usr/bin/env python3
"""Ingest trusted Alpha settlement facts from JSONL. SHADOW ONLY.

Each input line must be a JSON object carrying `prediction_id`, `outcome`
(0/1), `source`, and the COMPLETE settlement binding -- `contract_id`,
`market_snapshot_id` and `source_record_sha256`. Optional `resolved_at` and
`settlement_evidence_id` are preserved. This tool never reaches a broker; it
only appends RESOLUTION rows to the existing AlphaLedger.

`--trusted-source` is REQUIRED, and that is the point of it (AA-15 re-audit).
No settlement authority has been qualified for this deployment, and an
unqualified authority is a reason to accept nothing rather than everything.
Naming one here is an operator STATING which feed they have verified; the
statement is recorded on every resolution the run appends, so a calibration
number can later be traced to the authority that produced its outcomes.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_ledger import AlphaLedger  # noqa: E402
from alpha_resolution_ingest import ingest_settlements  # noqa: E402
from config import CFG  # noqa: E402


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                rows.append({"__parse_error__": f"line {line_number}: {exc}"})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True,
                    help="JSONL settlement feed prepared by a trusted read-only source")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any row is rejected or conflicts")
    ap.add_argument("--trusted-source", action="append", required=True,
                    metavar="NAME", dest="trusted_sources",
                    help="a settlement source you have verified. Repeatable, "
                         "and required: without one, no authority has been "
                         "qualified and every row is refused.")
    args = ap.parse_args(argv)

    rows = _read_jsonl(args.input)
    parse_errors = [r.get("__parse_error__") for r in rows
                    if isinstance(r, dict) and r.get("__parse_error__")]
    clean_rows = [r for r in rows
                  if not (isinstance(r, dict) and r.get("__parse_error__"))]
    ledger = AlphaLedger()
    result = ingest_settlements(ledger, clean_rows,
                                trusted_sources=args.trusted_sources)
    for error in parse_errors:
        result["rejected"].append({"reason": error})
    result["received"] += len(parse_errors)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.strict and (result["rejected"] or result["conflicts"]
                        or result["quarantined"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
