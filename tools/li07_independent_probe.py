"""Independent LI-07 malformed-enumeration witnesses. Synthetic, no sockets.

Constructors for broker, engine and position manager are never called.
All transports are local functions returning synthetic dictionaries/JSON.
Run from the repository with python tools/li07_independent_probe.py.
The harness clears its inherited environment and denies sockets/subprocesses.
"""
import hashlib
import os
import sys
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace

# No runtime credentials/configuration or real transport may enter a witness.
os.environ.clear()
synthetic_data = tempfile.TemporaryDirectory(prefix="atlas-li07-witness-")
os.environ.update(DATA_DIR=synthetic_data.name, PROBE_PROVIDERS_ON_START="0")
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
def deny_external_actions(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        raise RuntimeError("LI07 witness forbids external actions")
sys.addaudithook(deny_external_actions)

import kalshi_client as kc
from position_manager import PositionManager


def position(ticker="SYNTHETIC-A", **extra):
    return {"ticker": ticker, "position_fp": "1.00", **extra}


def order(ticker="SYNTHETIC-A", oid="synthetic-order-1", **extra):
    return {"order_id": oid, "ticker": ticker, "client_order_id": "synthetic-client-1",
            "status": "resting", "side": "yes", "remaining_count_fp": "1.00",
            "fill_count_fp": "0.00", "initial_count_fp": "1.00", **extra}


def client(pages):
    instance = kc.KalshiClient.__new__(kc.KalshiClient)
    iterator = iter(pages)
    def request(*args, **kwargs):
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value
    instance._req = request
    instance._log_raw_once = lambda *_args: None
    return instance


def wire_client(raw):
    instance = kc.KalshiClient.__new__(kc.KalshiClient)
    response = SimpleNamespace(status_code=200, text=raw, headers={},
                               json=lambda **kwargs: json.loads(raw, **kwargs))
    instance._pk = object()  # opaque synthetic non-key; signing is not invoked
    instance.base_url = "https://synthetic.invalid"
    instance._sign_headers = lambda *_args: {}
    instance.session = SimpleNamespace(request=lambda *_args, **_kwargs: response)
    instance._log_raw_once = lambda *_args: None
    return instance


def reconcile(instance, local=None):
    manager = PositionManager.__new__(PositionManager)
    manager.positions = {} if local is None else local
    manager.client = instance
    manager.reconcile_halt = {"status": "UNKNOWN", "detail": "synthetic prior halt"}
    result = manager.verify_against_broker()
    return {"status": result["status"], "halt_present": manager.reconcile_halt is not None}


results = []
def probe(name, expected, action):
    try:
        value = action()
        outcome = "RETURNED"
    except kc.KalshiAPIError as exc:
        outcome, value = "REFUSED", type(exc).__name__
    except OSError as exc:
        # This case intentionally injects a specific late-page transport error.
        # Unrelated setup/import/type errors never count as semantic refusals.
        outcome = ("REFUSED" if name == "late_transport_failure_never_returns_prior_rows"
                   and str(exc) == "synthetic transport" else "INCONCLUSIVE_INFRASTRUCTURE")
        value = type(exc).__name__
    except Exception as exc:
        outcome, value = "INCONCLUSIVE_SETUP", type(exc).__name__
    correct = ((expected == "REFUSED" and outcome == "REFUSED")
               or (expected == "RETURNED" and outcome == "RETURNED")
               or (expected == "MATCH" and outcome == "RETURNED"
                   and value.get("status") == "MATCH" and not value.get("halt_present"))
               or (expected == "TWO_ROWS" and outcome == "RETURNED"
                   and isinstance(value, list) and len(value) == 2
                   and [row["ticker"] for row in value] == ["SYNTHETIC-A", "SYNTHETIC-B"])
               or (expected == "HALTED" and outcome == "RETURNED"
                   and value.get("status") != "MATCH" and value.get("halt_present")))
    results.append({"case": name, "expected": expected, "outcome": outcome,
                    "value": value, "invariant_satisfied": bool(correct)})


probe("recognized_two_page_positions", "TWO_ROWS", lambda: client([
    {"market_positions": [position()], "cursor": "next"},
    {"market_positions": [position("SYNTHETIC-B")], "cursor": ""},
]).get_positions(limit=1))
probe("unknown_envelope", "REFUSED", lambda: client([{"new_positions": []}]).get_positions())
probe("unknown_top_level_pagination", "REFUSED", lambda: client([
    {"market_positions": [], "next_cursor": "unread"}]).get_positions())
probe("conflicting_collection_aliases", "REFUSED", lambda: client([
    {"market_positions": [], "positions": [position()]}]).get_positions())
probe("alias_changes_between_pages", "REFUSED", lambda: client([
    {"market_positions": [], "cursor": "next"}, {"positions": [], "cursor": ""}]).get_positions())
probe("late_bad_page_never_returns_prior_rows", "REFUSED", lambda: client([
    {"market_positions": [position()], "cursor": "next"}, {"market_positions": None}]).get_positions())
probe("late_transport_failure_never_returns_prior_rows", "REFUSED", lambda: client([
    {"market_positions": [position()], "cursor": "next"}, OSError("synthetic transport")]).get_positions())
probe("long_cursor_cycle", "REFUSED", lambda: client([
    {"market_positions": [], "cursor": "one"}, {"market_positions": [], "cursor": "two"},
    {"market_positions": [], "cursor": "one"}]).get_positions())
probe("page_bound_with_live_cursor", "REFUSED", lambda: client([
    {"market_positions": [], "cursor": "next"}]).get_positions(max_pages=1))
probe("duplicate_ticker_late_page", "REFUSED", lambda: client([
    {"market_positions": [position()], "cursor": "next"},
    {"market_positions": [position()], "cursor": ""}]).get_positions())
probe("nested_unknown_pagination_in_auxiliary_envelope", "REFUSED", lambda: client([
    {"market_positions": [], "event_positions": [{"pagination": {"next_cursor": "unread"}}],
     "cursor": ""}]).get_positions())
probe("contradictory_quantity_aliases", "REFUSED", lambda: client([
    {"market_positions": [position(position=2)], "cursor": ""}]).get_positions())
probe("boolean_quantity", "REFUSED", lambda: client([
    {"market_positions": [position(position_fp=True)], "cursor": ""}]).get_positions())
probe("null_quantity_alias", "REFUSED", lambda: client([
    {"market_positions": [position(position=None)], "cursor": ""}]).get_positions())
probe("exact_fixed_point_aliases", "RETURNED", lambda: client([
    {"market_positions": [position(position=1)], "cursor": ""}]).get_positions())
probe("fractional_quantity", "REFUSED", lambda: client([
    {"market_positions": [position(position_fp="1.01")], "cursor": ""}]).get_positions())
probe("huge_quantity", "REFUSED", lambda: client([
    {"market_positions": [position(position_fp=str(10**500))], "cursor": ""}]).get_positions())
probe("malformed_cursor_boolean", "REFUSED", lambda: client([
    {"market_positions": [], "cursor": False}]).get_positions())
probe("malformed_cursor_object", "REFUSED", lambda: client([
    {"market_positions": [], "cursor": {"next": "other"}}]).get_positions())
probe("orders_missing_required_cursor", "REFUSED", lambda: client([
    {"orders": []}]).list_orders())
probe("orders_null_cursor_not_string", "REFUSED", lambda: client([
    {"orders": [], "cursor": None}]).list_orders())
probe("positions_null_cursor_not_string", "REFUSED", lambda: client([
    {"market_positions": [], "cursor": None}]).get_positions())
probe("filtered_order_returns_other_ticker", "REFUSED", lambda: client([
    {"orders": [order(ticker="SYNTHETIC-OTHER")], "cursor": ""}
]).find_orders_by_client_order_id("synthetic-client-1", ticker="SYNTHETIC-A"))
probe("filtered_order_returns_other_status", "REFUSED", lambda: client([
    {"orders": [order(status="executed")], "cursor": ""}
]).list_orders(status="resting"))
probe("duplicate_order_late_page", "REFUSED", lambda: client([
    {"orders": [order()], "cursor": "next"}, {"orders": [order()], "cursor": ""}
]).list_orders())
probe("contradictory_order_quantity_alias", "REFUSED", lambda: client([
    {"orders": [order(remaining_count=2)], "cursor": ""}]).list_orders())
probe("recognized_flat_reconciliation", "MATCH", lambda: reconcile(client([
    {"market_positions": [], "cursor": ""}])))
probe("unknown_local_state_must_not_disappear", "HALTED", lambda: reconcile(client([
    {"market_positions": [], "cursor": ""}]), {
        "synthetic-trade": {"ticker": "SYNTHETIC-A", "side": "yes", "count": 1,
                            "state": "opne"}}))
probe("duplicate_wire_collection_members", "HALTED", lambda: reconcile(wire_client(
    '{"market_positions":[{"ticker":"SYNTHETIC-A","position":1}],'
    '"market_positions":[],"cursor":""}')))
probe("duplicate_wire_cursor_members", "HALTED", lambda: reconcile(wire_client(
    '{"market_positions":[],"cursor":"unread-next-page","cursor":""}')))
probe("nested_duplicate_wire_quantity_members", "REFUSED", lambda: wire_client(
    '{"market_positions":[{"ticker":"SYNTHETIC-A","position":1,"position":0}],"cursor":""}'
).get_positions())
probe("wire_fractional_quantity_rounds_to_integer", "REFUSED", lambda: wire_client(
    '{"market_positions":[{"ticker":"SYNTHETIC-A","position":1.0000000000000001}],"cursor":""}'
).get_positions())
probe("wire_large_fractional_quantity_rounds_inside_allowed_bound", "REFUSED", lambda: wire_client(
    '{"market_positions":[{"ticker":"SYNTHETIC-A","position":9007199254740990.5}],"cursor":""}'
).get_positions())
probe("wire_rounded_alias_hides_contradiction", "REFUSED", lambda: wire_client(
    '{"market_positions":[{"ticker":"SYNTHETIC-A","position":1.0000000000000001,"position_fp":"1.00"}],"cursor":""}'
).get_positions())
probe("valid_wire_whole_decimal_position", "MATCH", lambda: reconcile(wire_client(
    '{"market_positions":[{"ticker":"SYNTHETIC-A","position":1.0}],"cursor":""}'), {
        "synthetic-trade": {"ticker": "SYNTHETIC-A", "side": "yes", "count": 1,
                            "state": "open"}}))
probe("valid_wire_order_decimal_quantity_and_price", "RETURNED", lambda: wire_client(
    '{"orders":[{"order_id":"synthetic-order-1","ticker":"SYNTHETIC-A",'
    '"client_order_id":"synthetic-client-1","status":"resting",'
    '"side":"yes","remaining_count":1.0,"fill_count":0.0,"initial_count":1.0,"yes_price":44.0}],"cursor":""}'
).list_orders())
probe("order_outcome_alias_contradiction", "REFUSED", lambda: client([
    {"orders": [order(side="yes", outcome_side="no")], "cursor": ""}]).list_orders())
probe("no_current_order_is_not_historical_absence_proof", "REFUSED", lambda: client([
    {"orders": [], "cursor": ""}]).find_orders_by_client_order_id("synthetic-client-1"))
probe("other_subaccount_is_not_primary_scope", "REFUSED", lambda: client([
    {"market_positions": [position(subaccount_number=1)], "cursor": ""}]).get_positions())

root = Path(kc.__file__).parent
print(json.dumps({"scope": "synthetic-only independent LI07 review",
                  "code_sha256": {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                                  for name in ("kalshi_client.py", "position_manager.py")},
                  "cases": results, "invariant_failures": [r["case"] for r in results
                                                          if not r["invariant_satisfied"]],
                  "external_requests": 0, "broker_writes": 0,
                  "production_service_changes": 0}, indent=2))
raise SystemExit(1 if any(not row["invariant_satisfied"] for row in results) else 0)
