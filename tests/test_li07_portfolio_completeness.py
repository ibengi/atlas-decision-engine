"""LI-07: complete synthetic portfolio enumeration, no engine construction.

These tests call real client/read/reconciliation methods with an in-memory
transport. No real credentials, endpoints, broker writes or ledger changes.
"""
import copy
import unittest
from unittest.mock import Mock, patch

from kalshi_client import KalshiClient, KalshiAPIError
from position_manager import PositionManager


def position(ticker="SYNTH-A", quantity="1.00"):
    return {"ticker": ticker, "position_fp": quantity}


def order(identity="synthetic-order-1"):
    return {"order_id": identity, "client_order_id": "synthetic-client-1",
            "ticker": "SYNTH-A", "status": "resting", "side": "yes",
            "fill_count": 0, "remaining_count": 1}


class PortfolioCompleteness(unittest.TestCase):
    def client(self, pages):
        client = KalshiClient.__new__(KalshiClient)
        iterator = iter(page if isinstance(page, Exception) else copy.deepcopy(page) for page in pages)
        calls = []

        def request(method, path, **kwargs):
            self.assertEqual(method, "GET", "synthetic transport forbids mutations")
            self.assertIn(path, ("/portfolio/positions", "/portfolio/orders"))
            calls.append((method, path, copy.deepcopy(kwargs)))
            page = next(iterator)
            if isinstance(page, Exception):
                raise page
            return page
        client._req = request
        client._log_raw_once = Mock()
        client.synthetic_calls = calls
        return client

    def manager(self, client, local=(), halt=None):
        manager = PositionManager.__new__(PositionManager)
        manager.client = client
        manager.reconcile_halt = copy.deepcopy(halt)
        manager.synthetic_local = copy.deepcopy(list(local))
        manager._active_positions = lambda: manager.synthetic_local
        return manager

    def refuse_position(self, page):
        client = self.client([page])
        with self.assertRaises(KalshiAPIError):
            client.get_positions()

    def test_original_known_empty_positive_control(self):
        manager = self.manager(self.client([{"market_positions": []}]))
        self.assertEqual(manager.verify_against_broker()["status"], "MATCH")

    def test_original_unknown_nonempty_envelope_cannot_become_MATCH(self):
        manager = self.manager(self.client([{"unexpected_schema": [position()]}]))
        self.assertEqual(manager.verify_against_broker()["status"], "BROKER_UNAVAILABLE")
        self.assertIsNotNone(manager.reconcile_halt)

    def test_original_unread_cursor_fetches_hidden_position(self):
        client = self.client([{"market_positions": [], "cursor": "synthetic-page-two"},
                              {"market_positions": [position()], "cursor": ""}])
        manager = self.manager(client)
        self.assertEqual(manager.verify_against_broker()["status"], "MISMATCH")
        self.assertEqual(len(client.synthetic_calls), 2)
        self.assertEqual(client.synthetic_calls[1][2]["params"]["cursor"], "synthetic-page-two")

    def test_original_visible_position_disagreement_refuses(self):
        manager = self.manager(self.client([{"market_positions": [position()]}]))
        self.assertEqual(manager.verify_against_broker()["status"], "MISMATCH")

    def test_known_envelopes_and_terminal_profiles(self):
        for key in ("market_positions", "positions"):
            for terminal in ({}, {"cursor": ""}):
                with self.subTest(key=key, terminal=terminal):
                    self.assertEqual(self.client([{key: [], **terminal}]).get_positions(), [])

    def test_nonempty_multipage_signed_quantities_reconcile_exactly(self):
        client = self.client([{"market_positions": [position("A", "+6.00")], "cursor": "page-two"},
                              {"market_positions": [position("B", "-2.00")], "cursor": ""}])
        manager = self.manager(client, [{"ticker": "A", "side": "yes", "count": 6},
                                        {"ticker": "B", "side": "no", "count": 2}])
        self.assertEqual(manager.verify_against_broker()["status"], "MATCH")

    def test_unknown_null_wrong_type_and_conflicting_envelopes_refuse(self):
        for payload in (None, [], "", {}, {"market_positions": None},
                        {"market_positions": {}}, {"market_positions": False},
                        {"market_positions": [], "positions": [position()]},
                        {"market_positions": [], "positions": []}):
            with self.subTest(payload=payload):
                self.refuse_position(payload)

    def test_any_unknown_pagination_indicator_refuses(self):
        for extra in ({"next_cursor": "p2"}, {"has_more": True}, {"pagination": {"next": "p2"}},
                      {"next": None}, {"total": 100}, {"cursor_state": "expired"}):
            for envelope in ("orders", "market_positions"):
                with self.subTest(extra=extra, envelope=envelope):
                    client = self.client([{envelope: [], "cursor": "", **extra}])
                    with self.assertRaises(KalshiAPIError):
                        (client.list_orders if envelope == "orders" else client.get_positions)()

    def test_wrong_cursor_types_and_whitespace_refuse_before_next_call(self):
        for cursor in (None, True, False, 0, 42, [], {}, " ", " p2", "p2\n", "x" * 4097):
            with self.subTest(cursor=str(cursor)[:30]):
                client = self.client([{"market_positions": [], "cursor": cursor}, {"market_positions": []}])
                with self.assertRaises(KalshiAPIError):
                    client.get_positions()
                self.assertEqual(len(client.synthetic_calls), 1)

    def test_cursor_cycle_more_than_two_pages_refuses(self):
        client = self.client([{"market_positions": [], "cursor": value} for value in ("A", "B", "A")])
        with self.assertRaisesRegex(KalshiAPIError, "progresse"):
            client.get_positions()
        self.assertEqual(len(client.synthetic_calls), 3)

    def test_page_limit_never_returns_partial_collection(self):
        client = self.client([{"market_positions": [position()], "cursor": "still-more"}])
        with self.assertRaisesRegex(KalshiAPIError, "tronque"):
            client.get_positions(max_pages=1)

    def test_terminal_page_exactly_at_bound_is_complete(self):
        client = self.client([{"market_positions": [], "cursor": "A"}, {"market_positions": [position()]}])
        self.assertEqual(client.get_positions(max_pages=2), [position()])

    def test_empty_intermediate_page_is_not_terminal(self):
        client = self.client([{"market_positions": [position("A")], "cursor": "A"},
                              {"market_positions": [], "cursor": "B"},
                              {"market_positions": [position("B")], "cursor": ""}])
        self.assertEqual(len(client.get_positions()), 2)
        self.assertEqual(len(client.synthetic_calls), 3)

    def test_late_transport_failure_preserves_halt_and_local_state(self):
        for halt in ({"status": "UNKNOWN", "at": "old"}, {"status": "MISMATCH", "at": "old"}):
            with self.subTest(halt=halt):
                client = self.client([{"market_positions": [position()], "cursor": "A"},
                                      KalshiAPIError(503, "synthetic late-page failure")])
                local = [{"ticker": "SYNTH-A", "side": "yes", "count": 1}]
                manager = self.manager(client, local, halt)
                self.assertEqual(manager.verify_against_broker()["status"], "BROKER_UNAVAILABLE")
                self.assertEqual(manager.reconcile_halt, halt)
                self.assertEqual(manager.synthetic_local, local)

    def test_invalid_later_page_cannot_clear_existing_halt(self):
        old = {"status": "MISMATCH", "detail": "retained"}
        manager = self.manager(self.client([{"market_positions": [], "cursor": "A"},
                                             {"unexpected_schema": []}]), halt=old)
        self.assertEqual(manager.verify_against_broker()["status"], "BROKER_UNAVAILABLE")
        self.assertEqual(manager.reconcile_halt, old)

    def test_only_complete_matching_recovery_clears_halt(self):
        client = self.client([{"market_positions": [], "cursor": "A"}, {"market_positions": []}])
        manager = self.manager(client, halt={"status": "MISMATCH"})
        self.assertEqual(manager.verify_against_broker()["status"], "MATCH")
        self.assertIsNone(manager.reconcile_halt)

    def test_envelope_cannot_change_between_pages(self):
        client = self.client([{"market_positions": [], "cursor": "A"}, {"positions": []}])
        with self.assertRaises(KalshiAPIError):
            client.get_positions()

    def test_duplicate_position_identity_refuses_even_opposite_or_zero(self):
        for second in ("-1.00", "1.00", "0.00"):
            with self.subTest(second=second):
                client = self.client([{"market_positions": [position()], "cursor": "A"},
                                      {"market_positions": [position(quantity=second)]}])
                with self.assertRaises(KalshiAPIError):
                    client.get_positions()
        self.refuse_position({"market_positions": [position(), position(quantity="-1.00")]})

    def test_malformed_quantity_never_coerces_to_zero_or_false_agreement(self):
        for value in (True, False, None, [], {}, "", "NaN", "inf", float("inf"), float("nan"),
                      10**500, "9007199254740993", "1.5", "1e3", " 1.00"):
            with self.subTest(value=str(value)[:40]):
                self.refuse_position({"market_positions": [position(quantity=value)]})
                qty, error = PositionManager.parse_broker_qty(position(quantity=value))
                self.assertIsNone(qty)
                self.assertTrue(error)
        self.refuse_position({"market_positions": [{"ticker": "A", "position": 2**53,
                                                    "position_fp": "9007199254740993"}]})

    def test_all_present_quantity_aliases_must_agree(self):
        row = {"ticker": "A", "position": 6.0, "position_fp": "+6.00", "quantity": "6", "count": 6}
        self.assertEqual(self.client([{"market_positions": [row]}]).get_positions(), [row])
        for field in ("position", "quantity", "count"):
            self.refuse_position({"market_positions": [{**row, field: None}]})
            self.refuse_position({"market_positions": [{**row, field: 7}]})

    def test_identity_validated_even_for_zero_quantity(self):
        for ticker in (None, True, 1, [], {}, "", " ", "A\n", "A B"):
            with self.subTest(ticker=ticker):
                self.refuse_position({"market_positions": [position(ticker, "0.00")]})

    def test_manager_defense_in_depth_rejects_unvalidated_mock_lists(self):
        for rows in ({}, [position(), position(quantity="-1.00")], [position(1, "0.00")], [position(quantity=True)]):
            with self.subTest(rows=rows):
                manager = self.manager(Mock(get_positions=Mock(return_value=rows)))
                self.assertEqual(manager.verify_against_broker()["status"], "UNKNOWN")
                self.assertIsNotNone(manager.reconcile_halt)

    def test_local_malformed_identity_does_not_become_MATCH(self):
        for local in ({"ticker": "A", "side": "unknown", "count": 0},
                      {"ticker": "A", "side": "yes", "count": True},
                      {"ticker": "A", "side": "yes", "count": None},
                      {"ticker": "A", "side": "yes", "count": -1},
                      {"ticker": 1, "side": "yes", "count": 0}):
            with self.subTest(local=local):
                manager = self.manager(self.client([{"market_positions": []}]), [local])
                self.assertEqual(manager.verify_against_broker()["status"], "UNKNOWN")
                self.assertIsNotNone(manager.reconcile_halt)

    def test_order_identity_status_and_quantity_malformed_refuse(self):
        for change in ({"order_id": 123}, {"order_id": ""}, {"ticker": False},
                       {"client_order_id": None}, {"client_id": "contradictory"},
                       {"status": "unknown"}, {"side": "unknown"},
                       {"remaining_count": True}, {"remaining_count": -1},
                       {"remaining_count_fp": "2.00"}, {"fill_count": "NaN"}):
            with self.subTest(change=change):
                with self.assertRaises(KalshiAPIError):
                    self.client([{"orders": [{**order(), **change}]}]).list_orders()

    def test_requested_order_filters_are_binding(self):
        for kwargs in ({"ticker": "OTHER"}, {"status": "executed"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(KalshiAPIError):
                self.client([{"orders": [order()], "cursor": ""}]).list_orders(**kwargs)
        with self.assertRaises(KalshiAPIError):
            self.client([{"orders": [{**order(), "id": "contradictory"}], "cursor": ""}]).list_orders()

    def test_unknown_local_state_is_not_silently_filtered_away(self):
        for state in ("opne", "", None, False, "closed"):
            with self.subTest(state=state):
                manager = self.manager(self.client([{"market_positions": []}]))
                manager.positions = {"synthetic-trade": {"ticker": "A", "side": "yes", "count": 1, "state": state}}
                manager._active_positions = lambda: (row for row in manager.positions.values() if row.get("state", "open") == "open")
                self.assertEqual(manager.verify_against_broker()["status"], "UNKNOWN")

    def test_raw_json_duplicate_fields_and_nonfinite_numbers_fail_before_coercion(self):
        import requests
        payloads = [b'{"market_positions":[{"ticker":"A","position":1}],"market_positions":[]}',
                    b'{"market_positions":[],"cursor":"p2","cursor":""}',
                    b'{"market_positions":[{"ticker":"A","position":1,"position":0}]}',
                    b'{"orders":[],"orders":[]}',
                    b'{"market_positions":[],"cursor":NaN}',
                    b'{"market_positions":[{"ticker":"A","position":1.0000000000000001}]}',
                    b'{"market_positions":[{"ticker":"A","position":9007199254740990.5}]}',
                    b'{"market_positions":[{"ticker":"A","position":1.0000000000000001,"position_fp":"1.00"}]}']
        for payload in payloads:
            with self.subTest(payload=payload):
                response = requests.Response()
                response.status_code = 200
                response._content = payload
                client = KalshiClient.__new__(KalshiClient)
                client._pk = object()  # In-memory sentinel, never a key.
                client.base_url = "https://synthetic.invalid"
                client._sign_headers = lambda *_: {}
                client.session = Mock(request=Mock(return_value=response))
                client._log_raw_once = Mock()
                with self.assertRaises(KalshiAPIError):
                    (client.list_orders if b'"orders"' in payload else client.get_positions)()
                self.assertEqual(client.session.request.call_count, 1)

    def test_valid_wire_decimals_normalize_after_validation_and_remain_json_serializable(self):
        import json
        import requests
        payloads = [b'{"market_positions":[{"ticker":"A","position":6.00,"market_exposure":2.45}],"cursor":""}',
                    b'{"orders":[{"ticker":"A","order_id":"O","client_order_id":"C","status":"resting","side":"yes","fill_count":0.0,"remaining_count":1.00,"yes_price":45.5}],"cursor":""}']
        for payload in payloads:
            with self.subTest(payload=payload):
                response = requests.Response()
                response.status_code, response._content = 200, payload
                client = KalshiClient.__new__(KalshiClient)
                client._pk = object()
                client.base_url = "https://synthetic.invalid"
                client._sign_headers = lambda *_: {}
                client.session = Mock(request=Mock(return_value=response))
                client._raw_logged = set()  # Exercise the real JSON logging function.
                rows = (client.list_orders if b'"orders"' in payload else client.get_positions)()
                self.assertEqual(json.loads(json.dumps(rows, allow_nan=False)), rows)
                if "position" in rows[0]:
                    self.assertIs(type(rows[0]["position"]), int)
                    self.assertEqual(rows[0]["position"], 6)
                else:
                    self.assertIs(type(rows[0]["remaining_count"]), int)
                    self.assertEqual(rows[0]["yes_price"], 45.5)

    def test_duplicate_order_id_cross_pages_refuses(self):
        client = self.client([{"orders": [order()], "cursor": "A"}, {"orders": [order()]}])
        with self.assertRaises(KalshiAPIError):
            client.list_orders()

    def test_same_client_identity_multiple_orders_is_visible_not_collapsed(self):
        client = self.client([{"orders": [order("one")], "cursor": "A"}, {"orders": [order("two")], "cursor": ""}])
        self.assertEqual(len(client.find_orders_by_client_order_id("synthetic-client-1")), 2)

    def test_empty_current_orders_cannot_prove_historical_absence(self):
        self.assertEqual(self.client([{"orders": [], "cursor": ""}]).list_orders(), [])
        with self.assertRaisesRegex(KalshiAPIError, "historical retention"):
            self.client([{"orders": [], "cursor": ""}]).find_orders_by_client_order_id("synthetic-client-1")

    def test_order_alias_and_current_status_quantities_are_consistent(self):
        for change in ({"outcome_side": "no"}, {"action": "unknown"},
                       {"book_side": "unknown"}, {"action": "buy", "book_side": "ask"},
                       {"status": "executed", "remaining_count": 1}, {"remaining_count": 0}):
            with self.subTest(change=change), self.assertRaises(KalshiAPIError):
                self.client([{"orders": [{**order(), **change}], "cursor": ""}]).list_orders()
        valid = {**order(), "side": "no", "outcome_side": "no", "action": "buy", "book_side": "ask",
                 "fill_count": 1, "remaining_count": 1, "initial_count": 1}
        self.assertEqual(self.client([{"orders": [valid], "cursor": ""}]).list_orders(), [valid])

    def test_missing_order_economic_fields_refuse_presence_qualification(self):
        for key in ("side", "fill_count", "remaining_count", "client_order_id", "ticker"):
            row = order()
            del row[key]
            with self.subTest(key=key), self.assertRaises(KalshiAPIError):
                self.client([{"orders": [row], "cursor": ""}]).list_orders()

    def test_documented_order_cursor_is_required_string(self):
        for payload in ({"orders": []}, {"orders": [], "cursor": None}):
            with self.subTest(payload=payload), self.assertRaises(KalshiAPIError):
                self.client([payload]).list_orders()

    def test_primary_subaccount_is_explicit_and_returned_scope_must_match(self):
        for key in ("orders", "market_positions"):
            client = self.client([{key: [], "cursor": ""}])
            (client.list_orders if key == "orders" else client.get_positions)()
            self.assertEqual(client.synthetic_calls[0][2]["params"]["subaccount"], 0)
        for subaccount in (1, True, "0", None):
            with self.subTest(subaccount=subaccount):
                self.refuse_position({"market_positions": [{**position(), "subaccount": subaccount}]})

    def test_auxiliary_unknown_schema_or_nested_pagination_refuses(self):
        for auxiliary in ([{}], [{"event_ticker": "EV", "pagination": {"next": "A"}}],
                          [{"event_ticker": "EV", "cursor": "A"}],
                          [{"event_ticker": "EV", "total_cost_dollars": {"cursor": "A"}}]):
            with self.subTest(auxiliary=auxiliary):
                self.refuse_position({"market_positions": [], "event_positions": auxiliary})
        self.assertEqual(self.client([{"market_positions": [], "event_positions": [
            {"event_ticker": "EV", "total_cost_dollars": "0.0000", "realized_pnl_dollars": "-1.2000"}]}]).get_positions(), [])

    def test_order_found_on_first_page_does_not_bypass_failed_later_page(self):
        client = self.client([{"orders": [order()], "cursor": "A"}, KalshiAPIError(500, "synthetic")])
        with self.assertRaises(KalshiAPIError):
            client.find_orders_by_client_order_id("synthetic-client-1")

    def test_bad_page_bounds_refuse_before_transport(self):
        for params in ({"limit": True}, {"limit": 0}, {"limit": 1001}, {"limit": "200"},
                       {"max_pages": False}, {"max_pages": -1}, {"max_pages": "2"}):
            for method in ("get_positions", "list_orders"):
                with self.subTest(params=params, method=method):
                    client = self.client([])
                    with self.assertRaises(KalshiAPIError):
                        getattr(client, method)(**params)
                    self.assertEqual(client.synthetic_calls, [])

    def test_startup_reconciliation_uncertainty_halts_without_rewriting_positions(self):
        manager = self.manager(self.client([{"market_positions": [], "next_cursor": "A"}]),
                               [{"ticker": "A", "side": "yes", "count": 1}])
        before = copy.deepcopy(manager.synthetic_local)
        with patch("position_manager.JsonStore.save") as save:
            report = manager.reconcile_with_broker()
        self.assertEqual(report["status"], "BROKER_UNAVAILABLE")
        self.assertEqual(manager.synthetic_local, before)
        self.assertIsNotNone(manager.reconcile_halt)
        self.assertEqual(save.call_count, 1)  # Derived reconciliation report only.


if __name__ == "__main__":
    unittest.main()
