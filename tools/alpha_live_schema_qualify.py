#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qualify a CAPTURED live exchange payload against the candidate contract.

SHADOW ONLY. READ ONLY. This tool opens a file you give it and prints a
verdict. It makes no network call, holds no credential, touches no Railway
variable and cannot reach a broker: its entire input is a path on disk.

WHY IT EXISTS, AND WHAT IT DELIBERATELY WILL NOT DO
    The remediation cannot prove that a real Kalshi market carries the fields
    the contract requires -- `rules_primary`, `settlement_sources`,
    `expected_expiration_time` and all four quotes -- because obtaining that
    proof would mean either calling the exchange from here or changing
    production to capture it. Both are out of scope, and inventing a payload
    that "looks right" would be exactly the fabricated evidence this whole
    subsystem exists to prevent.

    So the candidate reports LIVE_SCHEMA_UNPROVEN, and this tool is how that
    status is discharged later, by an operator, from a payload they captured
    read-only. Until such a capture is supplied and passes, the status stands.

USAGE
    python tools/alpha_live_schema_qualify.py --input captured.json
    python tools/alpha_live_schema_qualify.py --input captured.json --json

    The input may be:
      * one market object,
      * a list of market objects, or
      * a Kalshi-shaped response `{"markets": [...]}`.

    Each object is treated as a RAW observation: quotes are read from it
    directly, and nothing is derived (AA-01).

EXIT CODES
    0  every market supplied satisfies the contract   -> LIVE_SCHEMA_PROVEN
    2  at least one did not                           -> LIVE_SCHEMA_UNPROVEN
    3  the input could not be read at all
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from candidate_contract import validate_record                 # noqa: E402
from research_feed import ResearchFeed, candidate_from_market   # noqa: E402

STATUS_PROVEN = "LIVE_SCHEMA_PROVEN"
STATUS_UNPROVEN = "LIVE_SCHEMA_UNPROVEN"


def _markets(payload):
    if isinstance(payload, dict) and isinstance(payload.get("markets"), list):
        return payload["markets"]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


def qualify(markets) -> dict:
    """Verdict for a captured payload. Pure: no I/O beyond what is passed in."""
    feed = ResearchFeed(start_writer=False)
    results = []
    for index, market in enumerate(markets, start=1):
        if not isinstance(market, dict):
            results.append({"index": index, "contract_id": None,
                            "qualifies": False,
                            "errors": ["market is not an object"]})
            continue
        # The captured object IS the raw observation. No execution-normalized
        # book is supplied, so nothing can be derived even by accident.
        candidate = candidate_from_market(market, {}, raw_book=market)
        record = feed._build(candidate)
        if record is None:
            results.append({
                "index": index,
                "contract_id": market.get("ticker"),
                "qualifies": False,
                "errors": list(feed.last_errors),
                "unavailable_fields": candidate.get("unavailable_fields", []),
                "quote_observation": candidate.get("quote_observation", {}),
            })
            continue
        errors = validate_record(record)
        results.append({
            "index": index,
            "contract_id": record["contract_id"],
            "qualifies": not errors,
            "errors": errors,
            "unavailable_fields": record["unavailable_fields"],
            "quote_observation": record["quote_observation"],
        })
    qualifying = [r for r in results if r["qualifies"]]
    return {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "capital_authority": False,
        "network_calls": 0,
        "markets_examined": len(results),
        "markets_qualifying": len(qualifying),
        "status": (STATUS_PROVEN if results and len(qualifying) == len(results)
                   else STATUS_UNPROVEN),
        "note": ("This verdict describes the payload supplied on disk. It says "
                 "nothing about whether that payload came from the exchange: "
                 "authenticity of the capture is the operator's to establish."),
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True,
                        help="path to a captured read-only market payload")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        with open(args.input, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[FATAL] could not read {args.input}: {exc}", file=sys.stderr)
        return 3

    markets = _markets(payload)
    if not markets:
        print(f"[FATAL] {args.input} contains no market object",
              file=sys.stderr)
        return 3

    verdict = qualify(markets)
    if args.json:
        print(json.dumps(verdict, indent=2, sort_keys=True))
    else:
        print(f"status: {verdict['status']}")
        print(f"markets: {verdict['markets_qualifying']}/"
              f"{verdict['markets_examined']} qualify")
        for row in verdict["results"]:
            if row["qualifies"]:
                print(f"  OK   {row['contract_id']}")
            else:
                print(f"  MISS {row['contract_id']}: {row['errors']}")
        print(f"\n{verdict['note']}")
    return 0 if verdict["status"] == STATUS_PROVEN else 2


if __name__ == "__main__":
    sys.exit(main())
