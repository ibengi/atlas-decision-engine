#!/usr/bin/env python3
"""Offline preflight for Alpha candidate-feed compatibility.

SHADOW ONLY. This tool reads an exported JSON/JSONL file, checks whether each
record contains the immutable facts required by ``alpha_snapshot``, and emits a
machine-readable verdict. It does not contact a broker, provider, or engine and
it never reconstructs missing market facts.

Exit codes:
    0  every supplied record is directly usable
    2  one or more records is incomplete (or the input is empty)
    64 unreadable/unsupported input
"""

import argparse
import json
import os
import sys

# Running a file named alpha_feed_readiness.py from tools/ puts that directory
# first on sys.path; without this explicit repository-root precedence Python
# imports this CLI again instead of the pure readiness module. This is import
# hygiene only and adds no runtime authority or network access.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
else:
    sys.path.remove(_REPO)
    sys.path.insert(0, _REPO)

from alpha_feed_readiness import assess_records


def _rows_from_json(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        rows = value.get("rows")
        if isinstance(rows, list):
            return rows
        return [value]
    raise ValueError("JSON input must be an object, list, or object containing rows")


def load_rows(path: str) -> list[dict]:
    text = open(path, encoding="utf-8").read()
    if not text.strip():
        return []
    try:
        return _rows_from_json(json.loads(text))
    except json.JSONDecodeError:
        rows = []
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {lineno}: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL line {lineno} is not an object")
            rows.append(row)
        return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True,
                        help="exported JSON page/list/object or JSONL file")
    args = parser.parse_args(argv)
    try:
        rows = load_rows(args.input)
    except (OSError, ValueError) as exc:
        print(json.dumps({
            "mode": "SHADOW_ONLY",
            "broker_authority": False,
            "all_records_ready": False,
            "error": str(exc),
        }, sort_keys=True))
        return 64

    result = assess_records(rows)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["all_records_ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
