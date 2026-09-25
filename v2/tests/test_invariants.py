"""Offline adversarial witnesses. Synthetic receipts are never production proof."""
import base64
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from atlas_v2.domain import Refused, Scope, Page, reconcile, strict_json, now, digest
from atlas_v2.store import Store
from atlas_v2.execution import Quote, Limits, reprice, reserve_shadow, deny_financial_mutation
from atlas_v2.accounting import CashEntry, cash_bridge, atlas_equity, lifecycle
from atlas_v2.data import capture_scan, PublicReader, ORIGIN
from atlas_v2.validation import register_hypothesis, lock_candidate, record_prediction, evaluate_future, CONSUMED_DATASETS

H = "a" * 64
SCOPE = Scope("synthetic-account", 0)
T = "2026-09-25T18:00:00+00:00"


def after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def quote(**updates):
    return replace(Quote("SYNTHETIC", "yes", "0.28", "0.30", "10.00", now(), after(600), H), **updates)


def declaration():
    return {"hypothesis": "synthetic preregistration", "universe": "synthetic only",
            "inclusion_rule": "all", "split_rule": "event disjoint chronological",
            "baseline_rule": "same row ask", "cost_rule": "synthetic explicit bounds",
            "stopping_rule": "fixed preregistered end", "multiplicity_rule": "one hypothesis",
            "min_periods": 2, "min_markets": 2}


class WithStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "v2.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def lock(self):
        register_hypothesis(self.store, "hyp", declaration())
        return lock_candidate(self.store, "hyp", {k: H for k in ("source", "parameters", "features", "thresholds", "config")},
              {"dataset_hash": "b" * 64, "events": ["train"], "label_available_through": T},
              {"dataset_hash": "c" * 64, "events": ["validation"], "label_available_through": T})

    def control(self, **updates):
        payload = {"scope": asdict(SCOPE), "dedicated_scope": True, "reconciliation": "MATCH",
                   "open_orders_complete": True, "manual_positions": [], "kill_switch": False,
                   "mode": "READ_ONLY", "capital": "OFF", "drawdown": "0.10",
                   "allocation_receipt": H, "available_budget": "10"}
        payload.update(updates)
        return self.store.append("control:" + digest(payload), "CONTROL", payload)

    def reserve(self, refresh=None, **updates):
        self.lock()
        control = self.control(**updates)
        return reserve_shadow(self.store, SCOPE, "lock:hyp", control["hash"],
                              refresh or (lambda: quote()), "0.65", "0.01", "0.01")


class CompletenessTests(unittest.TestCase):
    def page(self, **updates):
        return replace(Page("", "", (), SCOPE, H, True, True), **updates)

    def test_positive_empty_and_fractional_mismatch(self):
        self.assertEqual(reconcile([self.page()], SCOPE, {}), "MATCH")
        fractional = self.page(rows=({"ticker": "manual", "position_fp": "17.06"},))
        self.assertEqual(reconcile([fractional], SCOPE, {}), "MISMATCH")
        self.assertEqual(reconcile([fractional], SCOPE, {"manual": "17.06"}), "MATCH")

    def test_every_unknown_completeness_fails_closed(self):
        bad = [[], [self.page(response_cursor="next")],
               [self.page(transport_complete=False)], [self.page(envelope_complete=False)],
               [self.page(scope=Scope("other", 0))], [self.page(scope=Scope("synthetic-account", 1))],
               [self.page(error="late error")], [self.page(response_cursor=None)],
               [self.page(), self.page(error="late")],
               [self.page(response_cursor="x"), self.page(request_cursor="x", response_cursor="x")],
               [self.page(rows=({"ticker": "x"},))]]
        for pages in bad:
            with self.subTest(pages=pages), self.assertRaises(Refused):
                reconcile(pages, SCOPE, {})

    def test_malformed_quantities_and_duplicate_json(self):
        for value in (True, "NaN", "Infinity", "17.061", 17.06):
            with self.subTest(value=value), self.assertRaises(Refused):
                reconcile([self.page(rows=({"ticker": "x", "position_fp": value},))], SCOPE, {})
        for raw in (b'{"cursor":"","cursor":"next"}', b'{"x":NaN}'):
            with self.assertRaises(Refused):
                strict_json(raw)


class DurabilityTests(WithStore):
    def test_immutable_history_and_collision(self):
        event = self.store.append("one", "RAW", {"value": "17.06"})
        self.assertEqual(event, self.store.append("one", "RAW", {"value": "17.06"}))
        with self.assertRaises(Refused):
            self.store.append("one", "RAW", {"value": "17"})
        for sql in ("DELETE FROM events", "UPDATE events SET payload='{}'"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.db.execute(sql)
        self.assertEqual(self.store.verify()["seq"], 1)

    def test_transaction_rollback_and_restart(self):
        self.store.append("kept", "RAW", {})
        anchor = self.store.anchor()
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.append("lost", "RAW", {})
                raise RuntimeError("crash before commit")
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.get("lost"))
        self.assertEqual(self.store.verify(anchor), anchor)
        with self.assertRaises(Refused):
            self.store.verify({"schema": 1, "seq": 2, "hash": H})

    def test_thread_cannot_join_another_threads_rollback(self):
        opened, attempted, release, returned = threading.Event(), threading.Event(), threading.Event(), threading.Event()
        results = []
        def writer_a():
            try:
                with self.store.transaction():
                    self.store.append("a", "RAW", {})
                    opened.set()
                    release.wait(3)
                    raise RuntimeError("rollback A")
            except RuntimeError:
                pass
        def writer_b():
            opened.wait(3)
            attempted.set()
            results.append(self.store.append("b", "RAW", {}))
            returned.set()
        a, b = threading.Thread(target=writer_a), threading.Thread(target=writer_b)
        a.start(); b.start()
        self.assertTrue(attempted.wait(3))
        returned_before_release = returned.wait(0.1)
        release.set(); a.join(3); b.join(3)
        self.assertFalse(returned_before_release, "B must remain blocked while A owns transaction")
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertIsNone(self.store.get("a"))
        self.assertEqual(self.store.get("b"), results[0])


class EconomicsTests(unittest.TestCase):
    def priced(self, q=None, **kwargs):
        return reprice(q or quote(), kwargs.get("p", "0.65"), kwargs.get("budget", "10"),
                       kwargs.get("fee", "0.01"), kwargs.get("slip", "0.01"),
                       kwargs.get("limits", Limits()), now())

    def test_valid_control(self):
        self.assertEqual(self.priced()["count"], 1)
        self.assertEqual(self.priced()["net_edge"], "0.32")

    def test_refresh_price_cap(self):
        with self.assertRaisesRegex(Refused, "price exceeds"):
            self.priced(quote(bid="0.88", ask="0.90"), p="0.999", fee="0", slip="0")

    def test_refresh_spread(self):
        with self.assertRaisesRegex(Refused, "spread"):
            self.priced(quote(bid="0.10"))

    def test_refresh_gross_edge(self):
        with self.assertRaisesRegex(Refused, "gross edge"):
            self.priced(quote(bid="0.59", ask="0.61"), fee="0", slip="0")

    def test_refresh_net_edge(self):
        with self.assertRaisesRegex(Refused, "net edge"):
            self.priced(quote(bid="0.57", ask="0.59"), fee="0.03", slip="0")

    def test_ev_independent_stricter_threshold(self):
        with self.assertRaisesRegex(Refused, "expected value"):
            self.priced(limits=replace(Limits(), min_ev="0.33"))

    def test_stale_future_closed_and_no_liquidity(self):
        for q in (quote(observed_at=after(-6)), quote(observed_at=after(1)),
                  quote(closes_at=after(-1)), quote(available="0")):
            with self.subTest(q=q), self.assertRaises(Refused):
                self.priced(q)

    def test_fee_inclusive_size(self):
        with self.assertRaises(Refused):
            self.priced(budget="0.31")
        with self.assertRaises(Refused):
            self.priced(budget="0.311", fee="0.001", slip="0.01")

    def test_rounded_cost_bound_also_drives_net_edge(self):
        with self.assertRaisesRegex(Refused, "net edge"):
            self.priced(quote(bid="0.49", ask="0.50"), p="0.55", fee="0.0001", slip="0.005")

    def test_limits_cannot_be_relaxed(self):
        for updates in ({"min_gross_edge": "0"}, {"min_net_edge": "0"}, {"min_ev": "0"},
                        {"uncertainty": "0"}, {"max_price": "0.99"}, {"max_spread": "0.1"},
                        {"max_contracts": 2}, {"max_age_seconds": 60}):
            with self.subTest(updates=updates), self.assertRaises(Refused):
                self.priced(limits=replace(Limits(), **updates))

    def test_no_financial_mutation_even_if_flags_claim_live(self):
        with patch.dict("os.environ", {"CAPITAL": "ON", "ALLOW_ORDER_SUBMISSION": "1", "PROD_ACCESS_MODE": "LIVE"}):
            for action in ("create_order", "cancel_order", "transfer", "adjust_position"):
                with self.assertRaises(Refused):
                    deny_financial_mutation(action)


class IntentTests(WithStore):
    def test_duplicate_is_durable_across_restart(self):
        intent = self.reserve()
        self.assertFalse(intent["payload"]["would_submit"])
        self.store.close(); self.store = Store(self.path)
        with self.assertRaisesRegex(Refused, "already reserved"):
            self.reserve()

    def test_failed_intent_persistence_has_no_success(self):
        self.lock(); c = self.control()
        self.store.db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON events WHEN NEW.kind='SHADOW_INTENT' BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            reserve_shadow(self.store, SCOPE, "lock:hyp", c["hash"], lambda: quote(), "0.65", "0.01", "0.01")
        self.assertEqual(self.store.events("SHADOW_INTENT"), [])

    def test_control_change_during_refresh_aborts(self):
        self.lock(); c = self.control()
        def refresh():
            self.control(available_budget="11")
            return quote()
        with self.assertRaisesRegex(Refused, "changed during refresh"):
            reserve_shadow(self.store, SCOPE, "lock:hyp", c["hash"], refresh, "0.65", "0.01", "0.01")

    def test_manual_unknown_scope_kill_and_drawdown_abort(self):
        for changes in ({"manual_positions": ["manual"]}, {"dedicated_scope": False},
                        {"reconciliation": "UNKNOWN"}, {"open_orders_complete": False},
                        {"kill_switch": True}, {"drawdown": "0.20"}, {"capital": "ON"}, {"mode": "LIVE"}):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                self.reserve(**changes)

    def test_ambiguous_partial_fill_restart_and_unknown_settlement(self):
        intent = self.reserve()["event_id"]
        lifecycle(self.store, intent, "ambiguous", "AMBIGUOUS", "0", "1", H)
        lifecycle(self.store, intent, "partial", "PARTIAL", "0.50", "1", H)
        self.store.close(); self.store = Store(self.path)
        self.assertTrue(self.store.latest("LIFECYCLE")["payload"]["inventory_unresolved"])
        for settlement in (None, {"status": "finalized", "outcome": "?"}):
            with self.assertRaises(Refused):
                lifecycle(self.store, intent, "fake", "SETTLED", "0.50", "1", H, settlement)
        self.assertEqual(len(self.store.events("LIFECYCLE")), 2)

    def test_fill_and_payout_conservation(self):
        intent = self.reserve()["event_id"]
        with self.assertRaises(Refused):
            lifecycle(self.store, intent, "overflow", "FILLED", "100", "100", H)
        bad = {"status": "finalized", "outcome": "yes", "payout": "1000", "settled_at": after(700), "rules_hash": H}
        with self.assertRaises(Refused):
            lifecycle(self.store, intent, "payout", "SETTLED", "1", "1", H, bad)


class AccountingTests(unittest.TestCase):
    def event(self, event_id, kind, amount, corrects=None):
        return dict(id=event_id, kind=kind, amount=amount, receipt=H, at=T, corrects=corrects)

    def test_withdrawal_does_not_amplify_drawdown_or_erase_loss(self):
        events = [self.event("loss", "PNL", "-10"), self.event("withdrawal", "FLOW", "-45")]
        result = atlas_equity("100", H, events)
        self.assertEqual(Decimal(result["equity"]), Decimal("45"))
        self.assertEqual(Decimal(result["drawdown"]), Decimal("0.1"))
        self.assertEqual(result["realized_pnl"], "-10")
        self.assertEqual(atlas_equity("100", H, [self.event("loss", "PNL", "-20")])["risk_blocked"], True)

    def test_corrections_append_without_changing_original(self):
        original = self.event("pnl", "PNL", "-10")
        result = atlas_equity("100", H, [original, self.event("fix", "CORRECTION", "2", "pnl")])
        self.assertEqual(original["amount"], "-10")
        self.assertEqual(result["realized_pnl"], "-8")
        with self.assertRaises(Refused):
            atlas_equity("100", H, [self.event("deposit", "FLOW", "5"), self.event("fake", "CORRECTION", "50", "deposit")])

    def test_no_fake_opening_equity_or_manual_pnl(self):
        for opening, receipt in (("0", H), ("100", "")):
            with self.assertRaises(Refused):
                atlas_equity(opening, receipt, [])
        with self.assertRaises(Refused):
            atlas_equity("100", H, [self.event("manual", "MANUAL", "20")])

    def test_cash_residual_is_not_inferred_deposit(self):
        args = ("100", "80", [], SCOPE, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", H, H, H)
        self.assertEqual(cash_bridge(*args)["status"], "UNEXPLAINED_RESIDUAL")
        entry = CashEntry("withdrawal", SCOPE, "WITHDRAWAL", "-20", "EXTERNAL", T, H)
        self.assertEqual(cash_bridge("100", "80", [entry], *args[3:])["status"], "RECONCILED")
        with self.assertRaises(Refused):
            cash_bridge("100", "80", [entry, entry], *args[3:])


class DataTests(WithStore):
    def response(self, cursor="", **updates):
        raw = {"markets": [{"ticker": "KXBTC15M-SYNTHETIC", "event_ticker": "SYNTHETIC-EVENT",
                "yes_bid_dollars": "0.49", "yes_ask_dollars": "0.51", "close_time": after(600), "status": "active"}], "cursor": cursor}
        url = ORIGIN + "/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=200"
        response = {"url": url, "response_url": url, "method": "GET", "status": 200,
                    "started_at": now(), "received_at": now(), "raw": json.dumps(raw).encode(),
                    "transport_complete": True, "content_type": "application/json", "request_cursor": "", "content_range": None}
        response.update(updates)
        return response

    def reader(self, responses):
        class Reader:
            def get_markets(self, series, cursor):
                result = responses.pop(0)
                if isinstance(result, Exception): raise result
                return result
        return Reader()

    def test_complete_scan_keeps_raw_and_no_model(self):
        result = capture_scan(self.store, self.reader([self.response()]))
        self.assertTrue(result["payload"]["complete"])
        raw = self.store.latest("RAW_HTTP")["payload"]
        self.assertEqual(hashlib.sha256(base64.b64decode(raw["body_base64"])).hexdigest(), raw["body_sha256"])
        obs = self.store.latest("OBSERVATION")["payload"]
        self.assertIsNone(obs["model_id"])
        self.assertFalse(obs["execution_eligible"])

    def test_late_page_error_preserves_raw_never_complete(self):
        with self.assertRaises(Refused):
            capture_scan(self.store, self.reader([self.response("next"), Refused("late failure")]))
        self.assertEqual(len(self.store.events("RAW_HTTP")), 1)
        self.assertEqual(self.store.events("SCAN"), [])
        self.assertEqual(self.store.events("OBSERVATION"), [])

    def test_partial_redirect_and_unknown_envelope_fail_closed(self):
        for update in ({"content_range": "bytes 0-99/200"}, {"transport_complete": False},
                       {"status": 206}, {"response_url": "https://example.com"},
                       {"raw": b'{"markets":[]}'}, {"raw": b'{"markets":[],"cursor":null}'}):
            with self.subTest(update=update), self.assertRaises(Refused):
                capture_scan(self.store, self.reader([self.response(**update)]))
        self.assertEqual(self.store.events("OBSERVATION"), [])


class ValidationTests(WithStore):
    def observation(self, event="future", **updates):
        payload = {"observed_at": now(), "close_at": after(600), "event_id": event,
                   "ticker": "SYNTHETIC-" + event, "baseline_probability": "0.5"}
        payload.update(updates)
        return self.store.append("obs:" + event, "OBSERVATION", payload)

    def test_lock_is_immutable_and_prelock_data_fails(self):
        old = self.observation()
        lock = self.lock()
        with self.assertRaises(Refused):
            record_prediction(self.store, lock["event_id"], old["event_id"], "0.6", "0.5")
        with self.assertRaises(Refused):
            self.store.append(lock["event_id"], "LOCK", {"changed": True})

    def test_future_accepted_but_stale_and_event_leakage_refused(self):
        lock = self.lock()
        future = self.observation()
        self.assertEqual(record_prediction(self.store, lock["event_id"], future["event_id"], "0.6", "0.5")["kind"], "PREDICTION")
        for event, updates in (("train", {}), ("late", {"close_at": after(-1)})):
            obs = self.observation(event, **updates)
            with self.assertRaises(Refused):
                record_prediction(self.store, lock["event_id"], obs["event_id"], "0.6", "0.5")

    def test_baseline_and_consumed_dataset_binding(self):
        lock = self.lock(); obs = self.observation()
        with self.assertRaises(Refused):
            record_prediction(self.store, lock["event_id"], obs["event_id"], "0.6", "0.4")
        for consumed in CONSUMED_DATASETS:
            with self.assertRaises(Refused):
                evaluate_future(self.store, lock["event_id"], ["x"], {}, consumed)


if __name__ == "__main__":
    unittest.main()
