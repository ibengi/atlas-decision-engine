#!/usr/bin/env python3
"""Run the AI Alpha Gateway over candidate markets. SHADOW ONLY.

    python tools/alpha_shadow_run.py analyze --input candidates.json
    python tools/alpha_shadow_run.py metrics
    python tools/alpha_shadow_run.py resolve --prediction-id pred-... --outcome 1

WHY THIS IS A TOOL AND NOT AN ENGINE HOOK
    Alpha Gateway v1 is deliberately NOT wired into `ExecutionEngine`'s
    cycle. Section 20 requires that no execution path exist between an Alpha
    signal and a broker write; the strongest available form of that is for
    the money path not to import this subsystem at all, which
    `tests/test_alpha_safety_boundary.py` enforces in both directions. So
    the gateway runs out of band, over a candidate file the scanner
    produces, and writes only its own append-only ledgers.

    Wiring it into the cycle is a later decision that requires the section 21
    evidence first. Until then the honest architecture is two processes that
    share a directory, not one process that shares a call stack.

    This tool opens outbound connections TO AI VENDORS when keys are set.
    It never contacts the broker: it holds no client and imports none.

INPUT FORMAT
    A JSON list of candidate markets, each carrying the fields
    `alpha_snapshot.build_snapshot` requires. `--dry-run` analyses without
    writing to the ledger.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_gateway import AlphaGateway                        # noqa: E402
from alpha_ledger import AlphaLedger                          # noqa: E402
from alpha_snapshot import build_snapshot                     # noqa: E402
from config import CFG                                        # noqa: E402
from logging_config import setup_logging                      # noqa: E402

SNAPSHOT_FIELDS = ("contract_id", "event_id", "question", "resolution_rules",
                   "resolution_source", "yes_bid", "yes_ask", "no_bid",
                   "no_ask", "volume", "open_interest",
                   "market_close_time_utc", "expected_resolution_time_utc")


def _snapshot_from_candidate(candidate: dict):
    missing = [f for f in SNAPSHOT_FIELDS if f not in candidate]
    if missing:
        raise ValueError(f"candidate is missing {missing}")
    return build_snapshot(
        **{f: candidate[f] for f in SNAPSHOT_FIELDS},
        snapshot_time_utc=candidate.get("snapshot_time_utc"),
        catalyst_name=candidate.get("catalyst_name", ""),
        catalyst_time_utc=candidate.get("catalyst_time_utc"))


def cmd_analyze(args) -> int:
    if not CFG.ALPHA_GATEWAY_ENABLED and not args.force:
        print("ALPHA_GATEWAY_ENABLED is not set. Analysis would call paid "
              "third-party APIs, so it is refused by default. Set the "
              "variable, or pass --force for a one-off run.", file=sys.stderr)
        return 78
    with open(args.input, encoding="utf-8") as fh:
        candidates = json.load(fh)
    if not isinstance(candidates, list):
        print("input must be a JSON list of candidate markets",
              file=sys.stderr)
        return 2
    gateway = AlphaGateway(ledger=AlphaLedger())
    out = []
    for candidate in candidates:
        try:
            snapshot = _snapshot_from_candidate(candidate)
        except Exception as e:                                # noqa: BLE001
            print(f"skipped {candidate.get('contract_id')!r}: {e}",
                  file=sys.stderr)
            continue
        opportunity = gateway.analyze(snapshot, record=not args.dry_run)
        out.append({k: opportunity[k] for k in
                    ("prediction_id", "contract_id", "state", "state_reason",
                     "p_meta", "confidence", "disagreement", "side",
                     "raw_edge", "shadow_net_edge", "cycle_cost_usd",
                     "executed")})
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_metrics(args) -> int:
    print(json.dumps(AlphaLedger().metrics(), indent=2, default=str))
    return 0


def cmd_resolve(args) -> int:
    ledger = AlphaLedger()
    row = ledger.resolve(args.prediction_id, int(args.outcome),
                         source=args.source or "")
    print(json.dumps(row, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("--input", required=True)
    a.add_argument("--dry-run", action="store_true",
                   help="analyse without writing to the calibration ledger")
    a.add_argument("--force", action="store_true",
                   help="run even though ALPHA_GATEWAY_ENABLED is unset")
    sub.add_parser("metrics")
    r = sub.add_parser("resolve")
    r.add_argument("--prediction-id", required=True)
    r.add_argument("--outcome", required=True, choices=["0", "1"])
    r.add_argument("--source", default="")
    args = ap.parse_args(argv)
    return {"analyze": cmd_analyze, "metrics": cmd_metrics,
            "resolve": cmd_resolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
