"""HIGH regression: incomplete responses must never establish MATCH.

All transports are synthetic. Both startup and periodic paths must preserve
positions, existing halts, and broker-write prohibition on failed proof.
"""
import copy
import dataclasses
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
from kalshi_client import (KalshiAPIError, KalshiClient, PositionSnapshot,
                           PositionResponseIncomplete)
from position_manager import PositionManager
from position_snapshot_fixture import complete_positions


def inventory(*numbers):
    return {"subaccount_balances": [
        {"subaccount_number": n, "exchange_index": 0,
         "balance": "0.00", "updated_ts": 1800748800}
        for n in numbers or (0,)]}


def page(rows=None, cursor="", events=None):
    # Complete documented provider rows. Malformed rows deliberately keep
    # their malformed identity; schema-deletion tests edit this full shape.
    rows = [{"exchange_index": 0, "total_traded_dollars": "0.00",
             "position_fp": str(row.get("position", row.get("position_fp", 0))),
             "market_exposure_dollars": "0.00", "realized_pnl_dollars": "0.00",
             "fees_paid_dollars": "0.00", "last_updated_ts": "2026-09-24T00:00:00Z",
             **row} if isinstance(row, dict) else row for row in (rows or [])]
    return {"market_positions": rows,
            "event_positions": events if events is not None else [],
            "cursor": cursor}


def event_row(exposure="0.00"):
    return {"event_ticker": "EVENT", "total_cost_dollars": "4.00",
            "total_cost_shares_fp": "8.00", "event_exposure_dollars": exposure,
            "realized_pnl_dollars": "1.50", "fees_paid_dollars": "0.04"}


def client_for(pages, before=None, after=None):
    client = object.__new__(KalshiClient)
    client._log_raw_once = Mock()
    client._req = Mock(side_effect=[before if before is not None else inventory(),
                                   *pages, after if after is not None else inventory()])
    return client


def manager(client, positions=None):
    pm = object.__new__(PositionManager)
    pm.client = client
    pm.positions = copy.deepcopy(positions or {})
    pm.reconcile_halt = {"status": "UNKNOWN", "detail": "prior uncertainty", "at": "prior"}
    return pm


class PositionCompletenessTests(unittest.TestCase):
    def test_raw_duplicate_json_members_fail_closed_before_decoding_loses_evidence(self):
        """Use actual requests.Response decoding, never pre-parsed dictionaries."""
        scope = json.dumps(inventory())
        valid_page = json.dumps(page())
        duplicate_cases = [
            ("positions", '{"market_positions":[{"ticker":"HIDDEN","position_fp":"1.00"}],'
             '"market_positions":[],"event_positions":[],"cursor":""}'),
            ("positions", '{"market_positions":[],"event_positions":[],"cursor":"next","cursor":""}'),
            ("positions", '{"market_positions":[{"ticker":"HIDDEN"}],"market_\\u0070ositions":[],"event_positions":[],"cursor":""}'),
            ("positions", '{"market_positions":[],"event_positions":[{"event_ticker":"HIDDEN"}],'
             '"event_positions":[],"cursor":""}'),
            ("inventory", '{"subaccount_balances":[{"subaccount_number":1}],'
             '"subaccount_balances":[{"subaccount_number":0,"exchange_index":0,"balance":"0.00","updated_ts":1800748800}]}'),
            ("inventory", '{"subaccount_balances":[{"subaccount_number":1,"subaccount_number":0,'
             '"exchange_index":0,"balance":"0.00","updated_ts":1800748800}]}'),
            ("positions", json.dumps(page([{"ticker": "A", "position_fp": "0.00"}])).replace(
                '"position_fp": "0.00"', '"position_fp":"1.00","position_fp":"0.00"')),
        ]
        for target, body in duplicate_cases:
            for method in ("verify_against_broker", "reconcile_with_broker"):
                with self.subTest(target=target, body=body, method=method), patch("position_manager.JsonStore.save"):
                    c = self._raw_transport_client([body, valid_page, scope] if target == "inventory"
                                                   else [scope, body, scope])
                    pm = manager(c)
                    result = getattr(pm, method)()
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertIsNotNone(pm.reconcile_halt)
                    self.assertEqual(c.session.request.call_count, 1 if target == "inventory" else 2)
                    self.assertEqual(pm.positions, {})

    @staticmethod
    def _raw_transport_client(bodies):
        c = object.__new__(KalshiClient)
        c._pk = object()
        c.base_url = "https://synthetic.invalid"
        c._sign_headers = Mock(return_value={})
        c._raw_logged = set()
        responses = []
        for body in bodies:
            response = requests.Response()
            response.status_code = 200
            response._content = body.encode("utf-8")
            response.headers["Content-Type"] = "application/json"
            responses.append(response)
        c.session = Mock()
        c.session.request.side_effect = responses
        return c

    def test_raw_complete_json_control_matches(self):
        c = self._raw_transport_client([json.dumps(inventory()), json.dumps(page()),
                                        json.dumps(inventory())])
        pm = manager(c)
        self.assertEqual(pm.verify_against_broker()["status"], "MATCH")
        self.assertIsNone(pm.reconcile_halt)
        self.assertEqual(c.session.request.call_count, 3)
        self.assertTrue(all(call.kwargs.get("allow_redirects") is False
                            for call in c.session.request.call_args_list))

    def test_raw_duplicate_metadata_json_discards_complete_positions_observation(self):
        positions = page([{"ticker": "MARKET", "position_fp": "1.00"}], events=[event_row("0.50")])
        metadata = '{"market":{"ticker":"WRONG","ticker":"MARKET","event_ticker":"EVENT"}}'
        c = self._raw_transport_client([json.dumps(inventory()), json.dumps(positions), metadata,
                                        json.dumps(inventory())])
        pm = manager(c, {"trade": {"ticker": "MARKET", "side": "yes", "count": 1}})
        self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")
        self.assertIsNotNone(pm.reconcile_halt)
        self.assertEqual(c.session.request.call_count, 3)

    def test_raw_nonstandard_json_constants_in_metadata_fail_closed(self):
        positions = page([{"ticker": "MARKET", "position_fp": "1.00"}], events=[event_row("0.50")])
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                metadata = '{"market":{"ticker":"MARKET","event_ticker":"EVENT","title":' + constant + '}}'
                c = self._raw_transport_client([json.dumps(inventory()), json.dumps(positions), metadata,
                                                json.dumps(inventory())])
                pm = manager(c, {"trade": {"ticker": "MARKET", "side": "yes", "count": 1}})
                self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")
                self.assertIsNotNone(pm.reconcile_halt)
                self.assertEqual(c.session.request.call_count, 3)

    def test_raw_numeric_quantity_precision_cannot_round_or_underflow_to_match(self):
        for numeric, local_count in (("1e-400", 0), ("-1e-400", 0),
                                     ("1.00000000000000000001", 1)):
            for field in ("position_fp", "position"):
                with self.subTest(numeric=numeric, field=field):
                    row = {"ticker": "MARKET", "position_fp": str(local_count)}
                    if field == "position":
                        row[field] = "REPLACE_NATIVE_NUMBER"
                    else:
                        row["position_fp"] = "REPLACE_NATIVE_NUMBER"
                    body = json.dumps(page([row])).replace('"REPLACE_NATIVE_NUMBER"', numeric)
                    c = self._raw_transport_client([json.dumps(inventory()), body, json.dumps(inventory())])
                    positions = {} if local_count == 0 else {
                        "trade": {"ticker": "MARKET", "side": "yes", "count": local_count}}
                    pm = manager(c, positions)
                    self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")
                    self.assertIsNotNone(pm.reconcile_halt)
                    self.assertEqual(c.session.request.call_count, 2)

    def test_complete_reads_disable_redirects_and_reject_redirect_status_before_json(self):
        c = self._raw_transport_client([])
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = "https://untrusted.invalid/portfolio/positions"
        response._content = json.dumps(page()).encode("utf-8")
        response.json = Mock(wraps=response.json)
        c.session.request.side_effect = None
        c.session.request.return_value = response
        with self.assertRaises(PositionResponseIncomplete):
            c._req("GET", "/portfolio/positions", expected_status=200)
        self.assertIs(c.session.request.call_args.kwargs.get("allow_redirects"), False)
        response.json.assert_not_called()

    def test_http_200_content_range_cannot_establish_match(self):
        c = self._raw_transport_client([json.dumps(inventory()), json.dumps(page()),
                                        json.dumps(inventory())])
        responses = list(c.session.request.side_effect)
        responses[1].headers["Content-Range"] = "items 0-0/99"
        responses[1].json = Mock(wraps=responses[1].json)
        c.session.request.side_effect = responses
        pm = manager(c)
        self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")
        self.assertIsNotNone(pm.reconcile_halt)
        self.assertEqual(c.session.request.call_count, 2)
        responses[1].json.assert_not_called()

    def test_complete_empty_control_matches_both_paths(self):
        for method in ("verify_against_broker", "reconcile_with_broker"):
            with self.subTest(method=method), patch("position_manager.JsonStore.save"):
                pm = manager(client_for([page()]))
                self.assertEqual(getattr(pm, method)()["status"], "MATCH")
                self.assertIsNone(pm.reconcile_halt)
                calls = pm.client._req.call_args_list
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(c.args[0] == "GET" for c in calls))
                self.assertEqual(calls[1].kwargs["params"], {"limit": 1000, "subaccount": 0})
                self.assertTrue(all(c.kwargs["expected_status"] == 200 for c in calls))

    def test_nonempty_later_page_is_not_lost(self):
        client = client_for([page(cursor="next"),
                             page([{"ticker": "KX-LATE", "position_fp": "1.00"}])])
        pm = manager(client)
        self.assertEqual(pm.verify_against_broker()["status"], "MISMATCH")
        self.assertEqual(client._req.call_args_list[2].kwargs["params"]["cursor"], "next")
        self.assertEqual(pm.positions, {})

    def test_valid_multipage_snapshot_can_match(self):
        client = client_for([page([{"ticker": "A", "position": 2}], "next"),
                             page([{"ticker": "B", "position": -3}])])
        pm = manager(client, {"one": {"ticker": "A", "side": "yes", "count": 2},
                              "two": {"ticker": "B", "side": "no", "count": 3}})
        self.assertEqual(pm.verify_against_broker()["status"], "MATCH")
        self.assertIsNone(pm.reconcile_halt)

    def test_unknown_missing_partial_and_malformed_envelopes_fail_closed(self):
        cases = [None, [], {}, {"positions": [], "cursor": ""},
                 {"unexpected_positions": [{"ticker": "hidden", "position": 1}]},
                 {"market_positions": [], "cursor": ""},
                 {"market_positions": [], "event_positions": []},
                 {**page(), "market_positions": None},
                 {**page(), "market_positions": {}},
                 {**page(), "event_positions": None},
                 {**page(), "event_positions": {}},
                 {**page(), "partial": True}, {**page(), "complete": False},
                 {**page(), "has_more": True}, {**page(), "next_cursor": "hidden"},
                 {**page(), "errors": ["late shard error"]},
                 page([42]), page([{}]), page([{"ticker": ["A"]}]),
                 page([{"ticker": " ", "position": 0}]),
                 page([{"ticker": "A", "position": 0, "subaccount": 1}]),
                 page([{"ticker": "A", "position": 0, "error": "partial"}]),
                 page([{"ticker": "A", "position": False}]),
                 page([{"ticker": "A", "position_fp": "0.00000000000000001"}]),
                 page([{"ticker": "A", "position": 0, "market_exposure_dollars": "NaN"}]),
                 page(events=[None]), page(events=[{}])]
        for case in cases:
            for method in ("verify_against_broker", "reconcile_with_broker"):
                with self.subTest(case=case, method=method), patch("position_manager.JsonStore.save"):
                    pm = manager(client_for([case]))
                    result = getattr(pm, method)()
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertIsNotNone(pm.reconcile_halt)
                    self.assertEqual(pm.positions, {})

    def test_wrong_cursor_types_stop_before_followup(self):
        for value in (None, 0, False, [], {}, " ", " next "):
            with self.subTest(cursor=value):
                c = client_for([page(cursor=value), page()])
                self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")
                self.assertEqual(c._req.call_count, 2)

    def test_cursor_cycles_never_return_accumulated_rows(self):
        for cursors in (("A", "A"), ("A", "B", "A")):
            with self.subTest(cursors=cursors):
                c = client_for([page(cursor=x) for x in cursors])
                self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")
                self.assertEqual(c._req.call_count, len(cursors) + 1)

    def test_page_cap_fails_closed(self):
        c = client_for([page(cursor=f"next{i}") for i in range(100)])
        self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")
        self.assertEqual(c._req.call_count, 101)

    def test_duplicate_tickers_cannot_cancel_across_pages(self):
        c = client_for([page([{"ticker": "A", "position": 1}], "next"),
                        page([{"ticker": "A", "position": -1}])])
        self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")

    def test_late_page_errors_never_match_or_release_prior_halt(self):
        for error in (KalshiAPIError(503, "unavailable"), TimeoutError("late read")):
            with self.subTest(error=error):
                c = client_for([page(cursor="next"), error])
                pm = manager(c)
                self.assertEqual(pm.verify_against_broker()["status"], "BROKER_UNAVAILABLE")
                self.assertEqual(pm.reconcile_halt["status"], "UNKNOWN")
                self.assertEqual(c._req.call_count, 3)

    def test_malformed_late_page_cannot_match(self):
        pm = manager(client_for([page(cursor="next"), {}]))
        self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")

    def test_unknown_or_unsupported_inventory_fails_closed(self):
        cases = [{}, [], {"subaccount_balances": None}, {"subaccount_balances": []},
                 {"subaccount_balances": [None]},
                 {"subaccount_balances": [{}]},
                 {**inventory(), "cursor": "next"},
                 {**inventory(), "partial": True},
                 {**inventory(), "errors": ["late"]},
                 inventory(1), inventory(0, 1), inventory(0, 0),
                 inventory(True), inventory("0"), inventory(64)]
        cases += [{"subaccount_balances": [{"subaccount_number": 0}]},
                  {"subaccount_balances": [{**inventory()["subaccount_balances"][0], "balance": "NaN"}]}]
        for case in cases:
            with self.subTest(case=case):
                c = client_for([page()], before=case)
                self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")
                self.assertEqual(c._req.call_count, 1)

    def test_scope_change_or_late_inventory_failure_discards_observation(self):
        for after, status in ((inventory(0, 1), "UNKNOWN"), ({}, "UNKNOWN"),
                              (KalshiAPIError(503, "late inventory"), "BROKER_UNAVAILABLE")):
            with self.subTest(after=after):
                pm = manager(client_for([page()], after=after))
                self.assertEqual(pm.verify_against_broker()["status"], status)
                self.assertIsNotNone(pm.reconcile_halt)

    def test_bare_lists_iterators_dicts_and_duck_typed_proof_are_unknown(self):
        for result in ([], [{"ticker": "A", "position": 0}], iter([]), {},
                       {"complete": True, "positions": []}, Mock(complete=True)):
            with self.subTest(result=type(result).__name__):
                c = Mock()
                c.get_positions.return_value = result
                pm = manager(c)
                self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")
                c.create_order.assert_not_called()
                c.cancel_order.assert_not_called()

    def test_corrupt_snapshot_proofs_fail_closed(self):
        good = complete_positions([])
        cases = [dataclasses.replace(good, page_cursors=()),
                 dataclasses.replace(good, page_cursors=("next",)),
                 dataclasses.replace(good, page_cursors=("next", "next", "")),
                 dataclasses.replace(good, page_cursors=(None, "")),
                 dataclasses.replace(good, subaccounts_before=()),
                 dataclasses.replace(good, subaccounts_after=(0, 1)),
                 dataclasses.replace(good, subaccounts_after=(False,)),
                 dataclasses.replace(good, requested_subaccount=False),
                 dataclasses.replace(good, requested_subaccount=1),
                 dataclasses.replace(good, positions_json="{}"),
                 dataclasses.replace(good, positions_json="not JSON")]
        cases += [dataclasses.replace(good, events_json="{}"),
                  dataclasses.replace(good, events_json=json.dumps([event_row("1.00")]))]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                c = Mock()
                c.get_positions.return_value = snapshot
                self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")

    def test_snapshot_rows_are_immutable_and_iteration_is_detached(self):
        payload = page([{"ticker": "A", "position": 2}])
        c = client_for([payload])
        snapshot = c.get_positions()
        payload["market_positions"][0]["position"] = 0
        first = next(iter(snapshot))
        first["position"] = 0
        self.assertEqual(next(iter(snapshot))["position"], 2)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.positions_json = "[]"

    def test_event_exposure_cannot_hide_behind_empty_or_flat_market_positions(self):
        for rows in ([], [{"ticker": "A", "position": 0}]):
            for exposure in ("1.00", "-1.00", "0.00000001"):
                for method in ("verify_against_broker", "reconcile_with_broker"):
                    with self.subTest(rows=rows, exposure=exposure, method=method), patch("position_manager.JsonStore.save"):
                        pm = manager(client_for([page(rows, events=[event_row(exposure)])]))
                        self.assertEqual(getattr(pm, method)()["status"], "UNKNOWN")
                        self.assertIsNotNone(pm.reconcile_halt)

    def test_historical_event_costs_with_zero_current_exposure_can_match(self):
        c = client_for([page(events=[event_row()])])
        self.assertEqual(manager(c).verify_against_broker()["status"], "MATCH")

    def test_event_exposure_on_later_page_is_preserved(self):
        c = client_for([page(cursor="next"), page(events=[event_row("1.00")])])
        self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")

    def test_complete_nonflat_response_matches_with_authoritative_event_binding(self):
        market = {"ticker": "MARKET", "event_ticker": "EVENT"}
        response = page([{"ticker": "MARKET", "position_fp": "1.00",
                          "market_exposure_dollars": "0.50"}], events=[event_row("0.50")])
        c = client_for([response, {"market": market}])
        pm = manager(c, {"trade": {"ticker": "MARKET", "side": "yes", "count": 1}})
        self.assertEqual(pm.verify_against_broker()["status"], "MATCH")
        self.assertEqual(c._req.call_args_list[2].args, ("GET", "/markets/MARKET"))
        self.assertEqual(c._req.call_args_list[2].kwargs["expected_status"], 200)
        self.assertEqual(c._req.call_count, 4)

    def test_unrelated_event_exposure_cannot_match_nonflat_market(self):
        response = page([{"ticker": "MARKET", "position": 1}], events=[event_row("999.00")])
        c = client_for([response, {"market": {"ticker": "MARKET", "event_ticker": "OTHER"}}])
        pm = manager(c, {"trade": {"ticker": "MARKET", "side": "yes", "count": 1}})
        self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")

    def test_metadata_identity_partial_errors_or_late_failure_discard_observation(self):
        cases = [{}, {"market": {}}, {"market": {"ticker": "WRONG", "event_ticker": "EVENT"}},
                 {"market": {"ticker": "MARKET", "event_ticker": " "}},
                 {"market": {"ticker": "MARKET", "event_ticker": "EVENT"}, "partial": True},
                 {"market": {"ticker": "MARKET", "event_ticker": "EVENT", "error": "partial"}},
                 KalshiAPIError(503, "metadata unavailable")]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                c = client_for([page([{"ticker": "MARKET", "position": 1}],
                                     events=[event_row("0.50")]), metadata])
                pm = manager(c, {"trade": {"ticker": "MARKET", "side": "yes", "count": 1}})
                self.assertEqual(pm.verify_against_broker()["status"], "UNKNOWN")
                self.assertIsNotNone(pm.reconcile_halt)
                self.assertEqual(c._req.call_count, 3)

    def test_zero_position_with_nonzero_market_exposure_is_unknown(self):
        for amount in ("0.50", "-0.50", "0.00000001"):
            with self.subTest(amount=amount):
                c = client_for([page([{"ticker": "A", "position": 0,
                                       "market_exposure_dollars": amount}])])
                self.assertEqual(manager(c).verify_against_broker()["status"], "UNKNOWN")

    def test_missing_required_provider_fields_fail_at_adapter(self):
        required_market = ("ticker", "exchange_index", "position_fp", "total_traded_dollars",
                           "market_exposure_dollars", "realized_pnl_dollars",
                           "fees_paid_dollars", "last_updated_ts")
        for key in required_market:
            with self.subTest(market_field=key):
                response = page([{"ticker": "A", "position": 0}])
                del response["market_positions"][0][key]
                c = client_for([response])
                with self.assertRaises(PositionResponseIncomplete):
                    c.get_positions()
                self.assertEqual(c._req.call_count, 2)
        for key in ("subaccount_number", "exchange_index", "balance", "updated_ts"):
            with self.subTest(inventory_field=key):
                scope = inventory()
                del scope["subaccount_balances"][0][key]
                c = client_for([page()], before=scope)
                with self.assertRaises(PositionResponseIncomplete):
                    c.get_positions()
                self.assertEqual(c._req.call_count, 1)

    def test_malformed_event_financial_values_and_control_fields_fail_closed(self):
        for bad in ({"event_ticker": "EVENT"},
                    {**event_row(), "error": "late error"},
                    {**event_row(), "partial": True},
                    {**event_row(), "event_exposure_dollars": "not a number"},
                    {**event_row(), "event_exposure_dollars": "NaN"},
                    {**event_row(), "fees_paid_dollars": True}):
            with self.subTest(event=bad):
                self.assertEqual(manager(client_for([page(events=[bad])])).verify_against_broker()["status"], "UNKNOWN")

    def test_failures_never_mutate_local_state_or_journal(self):
        positions = {"trade": {"ticker": "A", "side": "yes", "count": 5}}
        for method in ("verify_against_broker", "reconcile_with_broker"):
            with self.subTest(method=method), patch("position_manager.JsonStore.save") as save:
                pm = manager(client_for([page(cursor="next"), {}]), positions)
                pm.flush = Mock()
                self.assertEqual(getattr(pm, method)()["status"], "UNKNOWN")
                self.assertEqual(pm.positions, positions)
                pm.flush.assert_not_called()
                for call in save.call_args_list:
                    self.assertTrue(call.args[0].endswith("reconciliation_report.json"))
                self.assertTrue(all(c.args[0] == "GET" for c in pm.client._req.call_args_list))

    def test_transport_rejects_partial_http_206_even_with_valid_json(self):
        c = object.__new__(KalshiClient)
        c._pk = object()
        c.base_url = "https://synthetic.invalid"
        c._sign_headers = Mock(return_value={})
        response = requests.Response()
        response.status_code = 206
        response._content = json.dumps(page()).encode("utf-8")
        response.json = Mock(wraps=response.json)
        c.session = Mock()
        c.session.request.return_value = response
        with self.assertRaises(PositionResponseIncomplete):
            c._req("GET", "/portfolio/positions", expected_status=200)
        self.assertEqual(c.session.request.call_args.args[0], "GET")
        response.json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
