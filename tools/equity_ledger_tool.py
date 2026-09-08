#!/usr/bin/env python3
"""Dry-run proposals for the F2 risk-equity ledger. Writes NOTHING.

Every operator action on equity_ledger.json is declarative: this tool prints
the proposal the engine will recompute at boot and the hash or token the
operator must set for it to apply. If anything in the journal, the ledger or
the positions changes in between, the recomputed value differs and the boot
refuses with a logged no-op.

  python tools/equity_ledger_tool.py status
  python tools/equity_ledger_tool.py seed --pre-flow-cash 0.04 \\
      --pre-flow-at 2026-09-07T18:01:19Z --evidence "<ref>" --cash-now 9.84
  python tools/equity_ledger_tool.py rebase --reason "..." --action-id OPS-42
  python tools/equity_ledger_tool.py hold-release --action-id OPS-43 --validation "<ref>"
  python tools/equity_ledger_tool.py attest --action-id OPS-44 --funding-records-sha256 <hex>

Run with DATA_DIR pointing at the state directory (the same one the engine
uses). Requires no broker credentials and opens no network connection.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CFG, _p  # noqa: E402
from equity_ledger import EquityLedger  # noqa: E402
from persistence import JsonStore  # noqa: E402
from trade_logger import TradeLogger, fold_corrections, is_correction  # noqa: E402


class _JournalView:
    """Read-only view of kalshi_trades.json. TradeLogger's constructor
    migrates legacy rows on load (a write); a dry-run must not."""

    def __init__(self):
        raw = JsonStore.load(_p(CFG.TRADES_FILE), []) or []
        self.trades = [t for t in raw if isinstance(t, dict)
                       and t.get("schema") == TradeLogger.SCHEMA]

    def correction_rows(self):
        return [t for t in self.trades if is_correction(t)]

    def open_trades(self):
        return [t for t in fold_corrections(self.trades) if t.get("state") == "open"]

    def settled_trades(self):
        return [t for t in fold_corrections(self.trades) if t.get("state") == "settled"]


class _PositionsView:
    """Read-only view of positions_state.json for the ledger's cost basis."""

    def __init__(self):
        raw = JsonStore.load(_p(CFG.POSITIONS_FILE), {}) or {}
        rows = raw.get("positions", raw) if isinstance(raw, dict) else raw
        self.rows = [p for p in (rows.values() if isinstance(rows, dict) else rows)
                     if isinstance(p, dict) and p.get("count")]
        self.reconcile_halt = None

    def open_risk(self):
        return sum(float(p.get("count", 0)) * float(p.get("avg_price", 0)) / 100.0
                   for p in self.rows)

    def open_count(self):
        return len(self.rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("seed")
    s.add_argument("--pre-flow-cash", required=True, type=float)
    s.add_argument("--pre-flow-at", required=True)
    s.add_argument("--evidence", required=True)
    s.add_argument("--cash-now", required=True, type=float)
    r = sub.add_parser("rebase")
    r.add_argument("--reason", required=True)
    r.add_argument("--action-id", required=True)
    h = sub.add_parser("hold-release")
    h.add_argument("--action-id", required=True)
    h.add_argument("--validation", required=True)
    a = sub.add_parser("attest")
    a.add_argument("--action-id", required=True)
    a.add_argument("--funding-records-sha256", required=True)
    args = ap.parse_args(argv)

    ledger = EquityLedger(_JournalView(), _PositionsView(), env="prod")
    if args.cmd == "status":
        out = ledger.snapshot()
    elif args.cmd == "seed":
        out = ledger.propose_seed(args.pre_flow_cash, args.pre_flow_at, args.evidence,
                                  args.cash_now)
        out["set_to_apply"] = {
            "EQUITY_LEDGER_SEED_PRE_FLOW_CASH": str(args.pre_flow_cash),
            "EQUITY_LEDGER_SEED_PRE_FLOW_AT": args.pre_flow_at,
            "EQUITY_LEDGER_SEED_EVIDENCE": args.evidence,
            "EQUITY_LEDGER_SEED_SHA256": out["sha256"]}
    elif args.cmd == "rebase":
        out = ledger.propose_rebase(args.reason, args.action_id)
        out["set_to_apply"] = {"EQUITY_LEDGER_REBASE_REASON": args.reason,
                               "EQUITY_LEDGER_REBASE_ACTION_ID": args.action_id,
                               "EQUITY_LEDGER_REBASE_TOKEN": out["token"]}
    elif args.cmd == "hold-release":
        out = ledger.propose_hold_release(args.action_id, args.validation)
        out["set_to_apply"] = {"EQUITY_LEDGER_HOLD_RELEASE_ACTION_ID": args.action_id,
                               "EQUITY_LEDGER_HOLD_RELEASE_VALIDATION": args.validation,
                               "EQUITY_LEDGER_HOLD_RELEASE_TOKEN": out["token"]}
    else:
        out = ledger.propose_attestation(args.action_id, args.funding_records_sha256)
        out["set_to_apply"] = {"EQUITY_LEDGER_ATTEST_ACTION_ID": args.action_id,
                               "EQUITY_LEDGER_ATTEST_FUNDING_RECORDS_SHA256": args.funding_records_sha256,
                               "EQUITY_LEDGER_ATTEST_TOKEN": out["token"]}
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
