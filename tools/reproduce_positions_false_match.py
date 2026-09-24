#!/usr/bin/env python3
"""Offline independent witness, compatible with the deployed baseline.

Usage: python tools/reproduce_positions_false_match.py /path/to/repo
No real network is permitted. The payloads are synthetic, never account evidence.
"""
import json
import logging
from pathlib import Path
import socket
import sys

repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
def deny(*args, **kwargs):
    raise AssertionError("REAL_NETWORK_FORBIDDEN")
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
logging.disable(logging.CRITICAL)
from kalshi_client import KalshiAPIError, KalshiClient
from position_manager import PositionManager

empty = {"market_positions": [], "event_positions": [], "cursor": ""}
inventory = {"subaccount_balances": [{"subaccount_number": 0, "exchange_index": 0,
                                      "balance": "0.00", "updated_ts": 1800748800}]}
results = {"real_network_requests": 0, "synthetic_only": True, "cases": {}}
for name in ("complete_empty_control", "unknown_envelope", "hidden_next_page",
             "late_page_error", "unsupported_subaccount"):
    client = object.__new__(KalshiClient)
    client._log_raw_once = lambda *a: None
    calls, position_reads = [], [0]
    def request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "kwargs": kwargs})
        if path == "/portfolio/subaccounts/balances":
            if name == "unsupported_subaccount":
                return {"subaccount_balances": inventory["subaccount_balances"] + [
                    {"subaccount_number": 1, "exchange_index": 0, "balance": "0.00", "updated_ts": 1800748800}]}
            return inventory
        position_reads[0] += 1
        if name == "unknown_envelope":
            return {"unexpected_positions": [{"ticker": "HIDDEN", "position": 1}]}
        if name in ("hidden_next_page", "late_page_error"):
            if position_reads[0] == 1:
                return {**empty, "cursor": "next"}
            if name == "late_page_error":
                raise KalshiAPIError(503, "synthetic late page failure")
            return {**empty, "market_positions": [{"ticker": "HIDDEN", "exchange_index": 0,
                "position_fp": "1.00", "total_traded_dollars": "0.00", "market_exposure_dollars": "0.00",
                "realized_pnl_dollars": "0.00", "fees_paid_dollars": "0.00", "last_updated_ts": "2026-09-24T00:00:00Z"}]}
        return empty
    client._req = request
    pm = object.__new__(PositionManager)
    pm.client, pm.positions, pm.reconcile_halt = client, {}, None
    result = pm.verify_against_broker()
    results["cases"][name] = {"status": result["status"], "halt": pm.reconcile_halt,
                               "synthetic_calls": calls, "local_after": pm.positions}
print(json.dumps(results, indent=2, sort_keys=True))
