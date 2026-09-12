#!/usr/bin/env python3
"""Generate Atlas Alpha Learning v1 scorecards from the immutable shadow ledger."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_consumer import ProcessedStore
from alpha_cost import BudgetLedger
from alpha_learning_runtime import memory_context, write_learning_report
from alpha_ledger import AlphaLedger
from alpha_telemetry import Telemetry
from config import CFG


def _cost():
    try:
        return float(os.getenv("ASTRA_MONTHLY_SUBSCRIPTION_USD", "100") or 100)
    except ValueError:
        return 100.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-class", default=None)
    ap.add_argument("--memory-only", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)

    ledger = AlphaLedger()
    selector = os.getenv("ASTRA_MODEL_SELECTOR", "astra") or "astra"
    if args.memory_only:
        print(memory_context(ledger, astra_selector=selector,
                             market_class=args.market_class,
                             limit=args.limit))
        return 0

    report = write_learning_report(
        ledger,
        CFG.DATA_DIR,
        # RA-14: hand the guard the objects, so it protects the paths these
        # actually use rather than only the configured defaults.
        processed_store=ProcessedStore(),
        budget_ledger=BudgetLedger(),
        telemetry=Telemetry(),
        astra_selector=selector,
        baseline_selector=os.getenv("ASTRA_BASELINE_SELECTOR", "atlas_quant") or "atlas_quant",
        subscription_cost_usd=_cost(),
        market_class=args.market_class,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
