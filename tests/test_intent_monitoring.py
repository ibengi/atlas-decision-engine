# -*- coding: utf-8 -*-
"""Supervision of ambiguous intents: nothing blocked may stay silent.

An open intent blocks a ticker fail-closed. That is the right behaviour
and the dangerous one: a lock nobody knows about looks exactly like a
market with no opportunities. This layer makes every open intent
observable and every stuck one loud.

What is asserted here:

  * a snapshot exposes the open count, the oldest age, and per intent the
    status, the NOT_FOUND count and the last attempt timestamp
  * an intent older than a configurable threshold raises an alert
  * MULTIPLE and MALFORMED alert IMMEDIATELY (severity CRITICAL)
  * consecutive UNAVAILABLE lookups alert once the streak is reached
  * no alert ever carries a credential or key material
  * an operator acknowledgement silences the noise and NOTHING else: the
    intent stays open, the ticker stays blocked, and no create_order can
    result from acknowledging
  * a restart preserves the alert and its AGE -- an alert that restarts
    its clock on every reboot would never cross a threshold

Mock broker throughout: no network, no DEMO order, no LIVE order.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from config import CFG  # noqa: E402
from kalshi_client import KalshiAPIError  # noqa: E402
from persistence import JsonStore, PersistenceSentinel, _p  # noqa: E402

TICKER = "KXBTCD-CANARY-T1"
SIDE, COUNT, PRICE = "yes", 1, 40
CID = bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE)

#: Things that must never appear in an alert or a health snapshot.
SECRETS = [
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "KALSHI_DEMO_PRIVATE_KEY", "KALSHI_PRIVATE_KEY", "KALSHI_KEY_ID",
    "ANTHROPIC_API_KEY", "RESEARCH_API_TOKEN",
    "KALSHI-ACCESS-SIGNATURE", "Authorization", "Bearer ",
]


def order_row(order_id="ord-1"):
    return {"order_id": order_id, "client_order_id": CID, "ticker": TICKER,
            "side": SIDE, "status": "resting", "fill_count": 0,
            "remaining_count": COUNT}


class _MonitorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_monitor_")
        self._tmps = getattr(self, "_tmps", [])
        self._tmps.append(self.tmp)
        self._saved = getattr(self, "_saved", None) or {
            k: getattr(CFG, k) for k in (
            "DATA_DIR", "REQUIRE_PERSISTENT_STATE", "ALLOW_ORDER_SUBMISSION",
            "MAX_CONTRACTS_PER_ORDER", "ALLOW_FRESH_STATE",
            "SUBMIT_DEDUP_TTL_S", "ORDER_TTL_SECONDS",
            "AMBIGUOUS_NOT_FOUND_CONFIRMATIONS",
            "AMBIGUOUS_NOT_FOUND_INTERVAL_S",
            "AMBIGUOUS_INTENT_STALE_S",
            "AMBIGUOUS_UNAVAILABLE_STREAK_ALERT")}
        CFG.DATA_DIR = self.tmp
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"
        CFG.SUBMIT_DEDUP_TTL_S = 6 * 3600.0
        CFG.ORDER_TTL_SECONDS = 1
        # 2x60s policy untouched (audit constraint); only the supervision
        # thresholds are tuned for the tests.
        CFG.AMBIGUOUS_NOT_FOUND_CONFIRMATIONS = 2
        CFG.AMBIGUOUS_NOT_FOUND_INTERVAL_S = 60.0
        CFG.AMBIGUOUS_INTENT_STALE_S = 900.0
        CFG.AMBIGUOUS_UNAVAILABLE_STREAK_ALERT = 3
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(CFG, k, v)
        for d in getattr(self, "_tmps", [self.tmp]):
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _client(matches=(), lookup_error=None):
        c = MagicMock()
        c.env = "demo"
        c.last_http_status = 201
        c.create_order.side_effect = KalshiAPIError(0, "reseau: ReadTimeout")
        c.get_positions.return_value = []
        if lookup_error is not None:
            c.find_orders_by_client_order_id.side_effect = lookup_error
        else:
            c.find_orders_by_client_order_id.return_value = list(matches)
        return c

    def _submit(self, om, price=PRICE):
        return om.place_and_track(TICKER, SIDE, COUNT, price)

    def _alerts_on_disk(self):
        return JsonStore.load(_p(bot.OrderManager.ALERTS_FILE), {})

    def _age_intent(self, om, seconds):
        """Make the intent look `seconds` old by backdating its birth."""
        from datetime import datetime, timedelta, timezone
        born = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        om.pending_intents[TICKER]["at"] = born.isoformat(timespec="seconds")
        om._flush_pending_intents()


class HealthSnapshotTest(_MonitorBase):
    """The observable state itself."""

    def test_snapshot_exposes_count_age_status_and_counters(self):
        om = bot.OrderManager(self._client())
        self.assertEqual(om.intent_health()["open_intents"], 0)

        self._submit(om)
        health = om.intent_health()

        self.assertEqual(health["open_intents"], 1)
        self.assertEqual(health["oldest_ticker"], TICKER)
        self.assertGreaterEqual(health["oldest_age_seconds"], 0.0)
        row = health["intents"][0]
        self.assertEqual(row["ticker"], TICKER)
        self.assertEqual(row["client_order_id"], CID)
        self.assertEqual(row["status"], "NOT_FOUND_PENDING")
        self.assertEqual(row["not_found_count"], 1)
        self.assertTrue(row["last_attempt_at"], "no last-attempt timestamp")
        self.assertIn("age_seconds", row)

    def test_every_resolution_status_is_reported(self):
        cases = {
            "NOT_FOUND_PENDING": ([], None),
            "MULTIPLE": ([order_row("d1"), order_row("d2")], None),
            "MALFORMED": (None, KalshiAPIError(0, "listing incoherent: x")),
            "UNAVAILABLE": (None, KalshiAPIError(0, "reseau: timeout")),
        }
        for expected, (matches, err) in cases.items():
            with self.subTest(status=expected):
                self.setUp()
                om = bot.OrderManager(
                    self._client(matches=matches or (), lookup_error=err))
                self._submit(om)
                self.assertEqual(om.intent_health()["intents"][0]["status"],
                                 expected)
                self._cleanup()

    def test_found_closes_the_intent_so_nothing_is_reported(self):
        om = bot.OrderManager(self._client(matches=[order_row()]))
        self._submit(om)
        health = om.intent_health()
        self.assertEqual(health["open_intents"], 0)
        self.assertEqual(health["alerts_open"], 0)

    def test_oldest_age_tracks_the_oldest_of_several_intents(self):
        from datetime import datetime, timezone
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, 400)
        om.pending_intents["KXOTHER"] = {
            "client_order_id": "alpha_other", "count": 1, "price": 10,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "resolution": None}
        om._flush_pending_intents()

        health = om.intent_health()
        self.assertEqual(health["open_intents"], 2)
        self.assertEqual(health["oldest_ticker"], TICKER)
        self.assertGreaterEqual(health["oldest_age_seconds"], 400)


class StaleIntentAlertTest(_MonitorBase):

    def test_no_alert_before_the_threshold(self):
        om = bot.OrderManager(self._client())
        self._submit(om)
        self.assertEqual(om.evaluate_intent_alerts(), [])
        self.assertEqual(self._alerts_on_disk(), {})

    def test_alert_once_the_threshold_is_crossed(self):
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)

        with self.assertLogs("API", level="WARNING") as logs:
            alerts = om.evaluate_intent_alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "AMBIGUOUS_INTENT_STALE")
        self.assertEqual(alerts[0]["severity"], "WARNING")
        self.assertEqual(alerts[0]["ticker"], TICKER)
        self.assertTrue(any("INTENT_ALERT" in m for m in logs.output))
        self.assertIn(f"STALE:{TICKER}", self._alerts_on_disk())

    def test_the_threshold_is_configurable(self):
        CFG.AMBIGUOUS_INTENT_STALE_S = 10.0
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, 15)

        self.assertEqual(len(om.evaluate_intent_alerts()), 1)

    def test_an_alert_clears_when_its_cause_disappears(self):
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)
        self.assertEqual(len(om.evaluate_intent_alerts()), 1)

        om.pending_intents.clear()            # resolved by the policy
        om._flush_pending_intents()

        self.assertEqual(om.evaluate_intent_alerts(), [])
        self.assertEqual(self._alerts_on_disk(), {})


class ImmediateAlertTest(_MonitorBase):
    """MULTIPLE and MALFORMED do not wait for any threshold."""

    def test_multiple_alerts_immediately_as_critical(self):
        om = bot.OrderManager(
            self._client(matches=[order_row("d1"), order_row("d2")]))
        self._submit(om)

        with self.assertLogs("API", level="CRITICAL") as logs:
            alerts = om.evaluate_intent_alerts()

        kinds = {a["kind"]: a for a in alerts}
        self.assertIn("AMBIGUOUS_RESOLUTION_MULTIPLE", kinds)
        self.assertEqual(kinds["AMBIGUOUS_RESOLUTION_MULTIPLE"]["severity"],
                         "CRITICAL")
        self.assertTrue(any("INTENT_ALERT" in m for m in logs.output))

    def test_malformed_alerts_immediately_as_critical(self):
        om = bot.OrderManager(self._client(
            lookup_error=KalshiAPIError(0, "listing d'ordres incoherent: x")))
        self._submit(om)

        alerts = om.evaluate_intent_alerts()

        kinds = {a["kind"] for a in alerts}
        self.assertIn("AMBIGUOUS_RESOLUTION_MALFORMED", kinds)

    def test_no_threshold_wait_for_these(self):
        """The intent is brand new: only the immediate alert fires."""
        om = bot.OrderManager(
            self._client(matches=[order_row("d1"), order_row("d2")]))
        self._submit(om)

        alerts = om.evaluate_intent_alerts()

        self.assertEqual([a["kind"] for a in alerts],
                         ["AMBIGUOUS_RESOLUTION_MULTIPLE"])


class UnavailableStreakAlertTest(_MonitorBase):

    def _unavailable_pass(self, om):
        om.pending_intents[TICKER]["last_not_found_at"] = 0.0
        om.resolve_pending_intents()

    def test_streak_alerts_only_at_the_configured_count(self):
        om = bot.OrderManager(self._client(
            lookup_error=KalshiAPIError(0, "reseau: timeout")))
        self._submit(om)                                   # streak = 1
        self.assertEqual(om.evaluate_intent_alerts(), [])

        self._unavailable_pass(om)                          # streak = 2
        self.assertEqual(om.evaluate_intent_alerts(), [])

        self._unavailable_pass(om)                          # streak = 3
        alerts = om.evaluate_intent_alerts()
        self.assertEqual([a["kind"] for a in alerts],
                         ["AMBIGUOUS_LOOKUP_UNAVAILABLE_STREAK"])
        self.assertEqual(alerts[0]["severity"], "CRITICAL")

    def test_a_successful_read_breaks_the_streak(self):
        client = self._client(lookup_error=KalshiAPIError(0, "reseau: timeout"))
        om = bot.OrderManager(client)
        self._submit(om)
        self._unavailable_pass(om)
        self.assertEqual(om.pending_intents[TICKER]["unavailable_streak"], 2)

        client.find_orders_by_client_order_id.side_effect = None
        client.find_orders_by_client_order_id.return_value = []
        self._unavailable_pass(om)

        self.assertEqual(om.pending_intents[TICKER]["unavailable_streak"], 0)
        self.assertEqual(om.evaluate_intent_alerts(), [])


class AlertsCarryNoSecretsTest(_MonitorBase):

    def test_no_alert_or_snapshot_contains_credentials(self):
        cases = [
            ("stale", dict(matches=()), True),
            ("multiple", dict(matches=[order_row("d1"), order_row("d2")]),
             False),
            ("malformed",
             dict(lookup_error=KalshiAPIError(0, "listing incoherent: x")),
             False),
        ]
        for label, kwargs, age_it in cases:
            with self.subTest(case=label):
                self.setUp()
                om = bot.OrderManager(self._client(**kwargs))
                self._submit(om)
                if age_it:
                    self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)
                blob = repr(om.evaluate_intent_alerts()) + repr(
                    om.intent_health()) + repr(self._alerts_on_disk())
                for secret in SECRETS:
                    self.assertNotIn(secret, blob, f"{label} leaked {secret}")
                self._cleanup()


class OperatorAckTest(_MonitorBase):
    """An acknowledgement says 'seen', never 'retry'."""

    def _stale_om(self):
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)
        om.evaluate_intent_alerts()
        return om

    def test_ack_records_who_and_when_without_touching_the_intent(self):
        om = self._stale_om()

        acked = om.ack_intent_alert(TICKER, "operator-1", "vu, enquete en cours")

        self.assertEqual(acked, [f"STALE:{TICKER}"])
        alert = self._alerts_on_disk()[f"STALE:{TICKER}"]
        self.assertEqual(alert["acknowledged_by"], "operator-1")
        self.assertTrue(alert["acknowledged_at"])
        self.assertIn(TICKER, om.pending_intents, "ack closed the intent")

    def test_ack_cannot_cause_a_submission(self):
        om = self._stale_om()
        om.client.create_order.reset_mock()
        om.client.create_order.side_effect = None
        om.client.create_order.return_value = order_row("ord-should-not-exist")

        om.ack_intent_alert(TICKER, "operator-1", "vu")
        res = self._submit(om, price=PRICE + 7)

        self.assertEqual(res.status, "blocked:ambiguous_intent_unresolved")
        self.assertEqual(om.client.create_order.call_count, 0,
                         "an acknowledgement produced an order")

    def test_ack_survives_restart_and_still_blocks(self):
        om = self._stale_om()
        om.ack_intent_alert(TICKER, "operator-1", "vu")
        del om

        client2 = self._client()
        client2.create_order.side_effect = None
        om2 = bot.OrderManager(client2)
        self.assertTrue(
            om2.intent_alerts[f"STALE:{TICKER}"]["acknowledged_at"])
        res = self._submit(om2, price=PRICE + 9)

        self.assertEqual(res.status, "blocked:ambiguous_intent_unresolved")
        self.assertEqual(client2.create_order.call_count, 0)

    def test_ack_does_not_silence_a_new_distinct_alert(self):
        om = self._stale_om()
        om.ack_intent_alert(TICKER, "operator-1", "vu")

        om.client.find_orders_by_client_order_id.return_value = [
            order_row("d1"), order_row("d2")]
        om.pending_intents[TICKER]["last_not_found_at"] = 0.0
        om.resolve_pending_intents()
        alerts = {a["kind"] for a in om.evaluate_intent_alerts()}

        self.assertIn("AMBIGUOUS_RESOLUTION_MULTIPLE", alerts)


class RestartPreservesAlertAgeTest(_MonitorBase):

    def test_first_raised_at_and_age_survive_a_restart(self):
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)
        first = om.evaluate_intent_alerts()[0]
        first_raised = first["first_raised_at"]
        del om

        time.sleep(1.1)
        om2 = bot.OrderManager(self._client())
        self.assertIn(f"STALE:{TICKER}", om2.intent_alerts,
                      "alert state lost on restart")
        again = om2.evaluate_intent_alerts()[0]

        self.assertEqual(again["first_raised_at"], first_raised,
                         "the alert clock restarted on reboot")
        self.assertGreater(again["age_seconds"], first["age_seconds"],
                           "alert age did not keep growing across restart")

    def test_intent_age_itself_survives_a_restart(self):
        om = bot.OrderManager(self._client())
        self._submit(om)
        self._age_intent(om, 500)
        del om

        om2 = bot.OrderManager(self._client())
        self.assertGreaterEqual(
            om2.intent_health()["oldest_age_seconds"], 500,
            "intent age restarted from zero")


class MonitoringIsObservatoryOnlyTest(_MonitorBase):
    """Supervision must never trade, close or clear anything."""

    def test_evaluating_alerts_performs_no_broker_call(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        client.reset_mock()

        om.evaluate_intent_alerts()
        om.intent_health()
        om.ack_intent_alert(TICKER, "operator-1", "vu")

        self.assertEqual(client.method_calls, [],
                         "supervision talked to the broker")
        self.assertIn(TICKER, om.pending_intents)


if __name__ == "__main__":
    unittest.main()
