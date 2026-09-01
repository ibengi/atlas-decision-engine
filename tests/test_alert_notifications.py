# -*- coding: utf-8 -*-
"""External alert delivery — loud outward, powerless inward.

A CRITICAL ambiguous-intent alert must be able to reach a human. The
channel that carries it must be unable to affect what the engine does:
a dead webhook, a hung one, a 500, a missing channel credential -- none
of them may cause an order, a cancellation, a closed intent or a
released guard. That asymmetry is the whole point of the module under
test, and every case below asserts both halves of it.

Delivery rules pinned here:

  * CRITICAL (MULTIPLE, MALFORMED, UNAVAILABLE streak) is always sent
  * WARNING (STALE) is sent only when explicitly enabled
  * one alert is delivered ONCE, not once per cycle
  * a failure is retried on later passes, bounded, and never retries
    anything at the broker
  * "sent / not sent / last error" survives a restart
  * no Kalshi credential, key or signature can appear in a payload

No real channel is configured anywhere: every notifier here is a mock or
a stub. No network, no DEMO order, no LIVE order.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
import alert_notifier as an  # noqa: E402
from config import CFG  # noqa: E402
from kalshi_client import KalshiAPIError  # noqa: E402
from persistence import JsonStore, PersistenceSentinel, _p  # noqa: E402

TICKER = "KXBTCD-CANARY-T1"
SIDE, COUNT, PRICE = "yes", 1, 40
CID = bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE)

SECRETS = [
    "-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
    "KALSHI_DEMO_PRIVATE_KEY", "KALSHI_PRIVATE_KEY", "KALSHI_KEY_ID",
    "ANTHROPIC_API_KEY", "RESEARCH_API_TOKEN", "KALSHI-ACCESS-SIGNATURE",
    "Authorization", "Bearer ", "super-secret-channel-token",
]


def order_row(order_id="ord-1"):
    return {"order_id": order_id, "client_order_id": CID, "ticker": TICKER,
            "side": SIDE, "status": "resting", "fill_count": 0,
            "remaining_count": COUNT}


class RecordingNotifier(an.AlertNotifier):
    """A channel that records instead of sending, and can be told to
    fail exactly like a real one would."""

    name = "recording"
    configured = True

    def __init__(self, error=None):
        self.sent = []
        self.error = error
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.sent.append(payload)


class _NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_notify_")
        self._tmps = getattr(self, "_tmps", [])
        self._tmps.append(self.tmp)
        self._saved = getattr(self, "_saved", None) or {
            k: getattr(CFG, k) for k in (
            "DATA_DIR", "REQUIRE_PERSISTENT_STATE", "ALLOW_ORDER_SUBMISSION",
            "MAX_CONTRACTS_PER_ORDER", "ALLOW_FRESH_STATE",
            "SUBMIT_DEDUP_TTL_S", "ORDER_TTL_SECONDS",
            "AMBIGUOUS_NOT_FOUND_CONFIRMATIONS",
            "AMBIGUOUS_NOT_FOUND_INTERVAL_S", "AMBIGUOUS_INTENT_STALE_S",
            "AMBIGUOUS_UNAVAILABLE_STREAK_ALERT", "ALERT_WEBHOOK_URL",
            "ALERT_WEBHOOK_TOKEN", "ALERT_WEBHOOK_REQUIRE_TOKEN",
            "ALERT_NOTIFY_MAX_ATTEMPTS", "ALERT_NOTIFY_WARNINGS",
            "ALERT_NOTIFY_TIMEOUT_S")}
        CFG.DATA_DIR = self.tmp
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"
        CFG.SUBMIT_DEDUP_TTL_S = 6 * 3600.0
        CFG.ORDER_TTL_SECONDS = 1
        CFG.AMBIGUOUS_NOT_FOUND_CONFIRMATIONS = 2
        CFG.AMBIGUOUS_NOT_FOUND_INTERVAL_S = 60.0
        CFG.AMBIGUOUS_INTENT_STALE_S = 900.0
        CFG.AMBIGUOUS_UNAVAILABLE_STREAK_ALERT = 3
        CFG.ALERT_WEBHOOK_URL = ""            # nothing real, ever
        CFG.ALERT_WEBHOOK_TOKEN = ""
        CFG.ALERT_WEBHOOK_REQUIRE_TOKEN = True
        CFG.ALERT_NOTIFY_MAX_ATTEMPTS = 5
        CFG.ALERT_NOTIFY_WARNINGS = False
        CFG.ALERT_NOTIFY_TIMEOUT_S = 5.0
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
        from datetime import datetime, timedelta, timezone
        born = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        om.pending_intents[TICKER]["at"] = born.isoformat(timespec="seconds")
        om._flush_pending_intents()

    def _multiple_om(self, notifier):
        om = bot.OrderManager(
            self._client(matches=[order_row("d1"), order_row("d2")]),
            notifier=notifier)
        self._submit(om)
        return om


class NotifierFactoryTest(_NotifyBase):
    """Configuration -> transport, with no real channel involved."""

    def test_no_url_gives_an_unconfigured_null_notifier(self):
        n = an.build_notifier(CFG)
        self.assertIsInstance(n, an.NullNotifier)
        self.assertFalse(n.configured)

    def test_url_without_the_required_token_is_misconfigured(self):
        CFG.ALERT_WEBHOOK_URL = "https://example.invalid/hook"
        n = an.build_notifier(CFG)
        self.assertIsInstance(n, an.MisconfiguredNotifier)
        self.assertTrue(n.configured, "a broken channel must not read as absent")
        with self.assertRaises(an.NotifierError):
            n.send({})

    def test_non_https_url_is_refused(self):
        CFG.ALERT_WEBHOOK_URL = "http://example.invalid/hook"
        CFG.ALERT_WEBHOOK_TOKEN = "t"
        self.assertIsInstance(an.build_notifier(CFG), an.MisconfiguredNotifier)

    def test_a_configured_webhook_posts_json_with_the_token_in_a_header(self):
        CFG.ALERT_WEBHOOK_URL = "https://example.invalid/hook"
        CFG.ALERT_WEBHOOK_TOKEN = "super-secret-channel-token"
        n = an.build_notifier(CFG)
        self.assertIsInstance(n, an.WebhookNotifier)

        with patch("requests.post") as post:
            post.return_value = MagicMock(status_code=204)
            n.send({"kind": "X", "ticker": TICKER})

        body = post.call_args.kwargs["data"]
        self.assertIn(TICKER, body)
        self.assertNotIn("super-secret-channel-token", body,
                         "the channel token leaked into the payload")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],
                         "Bearer super-secret-channel-token")

    def test_webhook_failures_become_notifier_errors_without_url_detail(self):
        n = an.WebhookNotifier("https://example.invalid/hook?token=leak", "t")
        with patch("requests.post", side_effect=OSError("connect to ?token=leak")):
            with self.assertRaises(an.NotifierError) as ctx:
                n.send({})
        self.assertNotIn("leak", str(ctx.exception),
                         "an error message leaked the channel URL")

        with patch("requests.post", return_value=MagicMock(status_code=500)):
            with self.assertRaises(an.NotifierError) as ctx:
                n.send({})
        self.assertIn("500", str(ctx.exception))


class CriticalDeliveryTest(_NotifyBase):

    def test_multiple_is_delivered(self):
        notifier = RecordingNotifier()
        om = self._multiple_om(notifier)

        om.evaluate_intent_alerts()

        self.assertEqual(len(notifier.sent), 1)
        payload = notifier.sent[0]
        self.assertEqual(payload["kind"], "AMBIGUOUS_RESOLUTION_MULTIPLE")
        self.assertEqual(payload["severity"], "CRITICAL")

    def test_malformed_is_delivered(self):
        notifier = RecordingNotifier()
        om = bot.OrderManager(self._client(
            lookup_error=KalshiAPIError(0, "listing d'ordres incoherent: x")),
            notifier=notifier)
        self._submit(om)

        om.evaluate_intent_alerts()

        self.assertEqual([p["kind"] for p in notifier.sent],
                         ["AMBIGUOUS_RESOLUTION_MALFORMED"])

    def test_unavailable_streak_is_delivered(self):
        notifier = RecordingNotifier()
        om = bot.OrderManager(self._client(
            lookup_error=KalshiAPIError(0, "reseau: timeout")),
            notifier=notifier)
        self._submit(om)
        for _ in range(2):
            om.pending_intents[TICKER]["last_not_found_at"] = 0.0
            om.resolve_pending_intents()

        om.evaluate_intent_alerts()

        self.assertEqual([p["kind"] for p in notifier.sent],
                         ["AMBIGUOUS_LOOKUP_UNAVAILABLE_STREAK"])

    def test_payload_carries_every_field_the_operator_needs(self):
        notifier = RecordingNotifier()
        om = self._multiple_om(notifier)
        om.evaluate_intent_alerts()

        p = notifier.sent[0]
        for field in ("ticker", "client_order_id", "status", "age_seconds",
                      "attempts", "timestamp", "kind", "severity"):
            self.assertIn(field, p, f"missing {field}")
        self.assertEqual(p["ticker"], TICKER)
        self.assertEqual(p["client_order_id"], CID)
        self.assertIsNotNone(p["timestamp"])

    def test_no_payload_can_carry_a_kalshi_credential(self):
        CFG.ALERT_WEBHOOK_TOKEN = "super-secret-channel-token"
        notifier = RecordingNotifier()
        om = self._multiple_om(notifier)
        om.evaluate_intent_alerts()

        blob = repr(notifier.sent)
        for secret in SECRETS:
            self.assertNotIn(secret, blob, f"payload leaked {secret}")


class SeverityFilterTest(_NotifyBase):

    def _stale_om(self, notifier):
        om = bot.OrderManager(self._client(), notifier=notifier)
        self._submit(om)
        self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)
        return om

    def test_warning_is_not_delivered_by_default(self):
        notifier = RecordingNotifier()
        om = self._stale_om(notifier)

        om.evaluate_intent_alerts()

        self.assertEqual(notifier.sent, [])
        self.assertEqual(self._alerts_on_disk()[f"STALE:{TICKER}"]
                         ["notify_state"], an.SKIPPED_SEVERITY)

    def test_warning_is_delivered_when_enabled(self):
        CFG.ALERT_NOTIFY_WARNINGS = True
        notifier = RecordingNotifier()
        om = self._stale_om(notifier)

        om.evaluate_intent_alerts()

        self.assertEqual([p["kind"] for p in notifier.sent],
                         ["AMBIGUOUS_INTENT_STALE"])
        self.assertEqual(notifier.sent[0]["severity"], "WARNING")

    def test_an_unconfigured_channel_skips_rather_than_fails(self):
        om = self._multiple_om(an.NullNotifier())
        om.evaluate_intent_alerts()

        alert = self._alerts_on_disk()[f"MULTIPLE:{TICKER}"]
        self.assertEqual(alert["notify_state"], an.SKIPPED_NO_CHANNEL)
        self.assertIsNone(alert.get("notify_last_error"))
        self.assertEqual(alert.get("notify_attempts"), None,
                         "an absent channel must not burn retry attempts")


class DeduplicationTest(_NotifyBase):

    def test_the_same_alert_is_delivered_once_not_once_per_cycle(self):
        notifier = RecordingNotifier()
        om = self._multiple_om(notifier)

        for _ in range(10):
            om.evaluate_intent_alerts()

        self.assertEqual(notifier.calls, 1, f"{notifier.calls} sends, expected 1")

    def test_dedup_survives_a_restart(self):
        notifier = RecordingNotifier()
        om = self._multiple_om(notifier)
        om.evaluate_intent_alerts()
        self.assertEqual(notifier.calls, 1)
        del om

        notifier2 = RecordingNotifier()
        om2 = bot.OrderManager(
            self._client(matches=[order_row("d1"), order_row("d2")]),
            notifier=notifier2)
        om2.evaluate_intent_alerts()

        self.assertEqual(notifier2.calls, 0,
                         "the alert was re-notified after a restart")

    def test_a_new_distinct_alert_is_still_delivered(self):
        notifier = RecordingNotifier()
        om = bot.OrderManager(self._client(), notifier=notifier)
        self._submit(om)
        self._age_intent(om, CFG.AMBIGUOUS_INTENT_STALE_S + 30)
        CFG.ALERT_NOTIFY_WARNINGS = True
        om.evaluate_intent_alerts()
        self.assertEqual(notifier.calls, 1)

        om.client.find_orders_by_client_order_id.return_value = [
            order_row("d1"), order_row("d2")]
        om.pending_intents[TICKER]["last_not_found_at"] = 0.0
        om.resolve_pending_intents()
        om.evaluate_intent_alerts()

        self.assertEqual(notifier.calls, 2)
        self.assertIn("AMBIGUOUS_RESOLUTION_MULTIPLE",
                      [p["kind"] for p in notifier.sent])


class NotificationFailureCannotTradeTest(_NotifyBase):
    """The asymmetry: loud outward, powerless inward."""

    FAILURES = [
        ("timeout", an.NotifierError("transport ReadTimeout")),
        ("http 500", an.NotifierError("HTTP 500")),
        ("credential absent", an.NotifierError("canal inutilisable: jeton")),
        ("erreur inattendue", RuntimeError("bug interne du notifier")),
    ]

    def test_no_failure_mode_can_touch_trading_state(self):
        for label, err in self.FAILURES:
            with self.subTest(case=label):
                self.setUp()
                notifier = RecordingNotifier(error=err)
                om = self._multiple_om(notifier)
                guard_before = dict(om.session_submitted)
                intents_before = {k: dict(v)
                                  for k, v in om.pending_intents.items()}
                om.client.reset_mock()

                om.evaluate_intent_alerts()      # must not raise

                self.assertEqual(om.client.create_order.call_count, 0)
                self.assertEqual(om.client.cancel_order.call_count, 0)
                self.assertEqual(om.client.method_calls, [],
                                 f"{label}: the broker was called")
                self.assertEqual(om.session_submitted, guard_before,
                                 f"{label}: the guard was modified")
                self.assertEqual(
                    {k: {kk: vv for kk, vv in v.items()}
                     for k, v in om.pending_intents.items()},
                    intents_before, f"{label}: an intent was modified")
                self.assertEqual(
                    self._alerts_on_disk()[f"MULTIPLE:{TICKER}"]
                    ["notify_state"], an.FAILED)
                self._cleanup()

    def test_a_failing_channel_still_leaves_the_ticker_blocked(self):
        notifier = RecordingNotifier(error=an.NotifierError("HTTP 500"))
        om = self._multiple_om(notifier)
        om.evaluate_intent_alerts()
        # reset: the one call so far is the ORIGINAL submission that created
        # the ambiguity. What must stay at zero is anything after it.
        om.client.reset_mock()
        om.client.create_order.side_effect = None
        om.client.create_order.return_value = order_row("ord-new")

        res = self._submit(om, price=PRICE + 5)

        self.assertEqual(res.status, "blocked:ambiguous_resolution_halt")
        self.assertEqual(om.client.create_order.call_count, 0)

    def test_delivery_is_retried_but_the_broker_is_not(self):
        notifier = RecordingNotifier(error=an.NotifierError("HTTP 500"))
        om = self._multiple_om(notifier)
        om.client.reset_mock()

        for _ in range(3):
            om.evaluate_intent_alerts()

        self.assertEqual(notifier.calls, 3, "delivery was not retried")
        self.assertEqual(om.client.method_calls, [],
                         "a notification retry reached the broker")

    def test_retries_are_bounded(self):
        CFG.ALERT_NOTIFY_MAX_ATTEMPTS = 2
        notifier = RecordingNotifier(error=an.NotifierError("HTTP 500"))
        om = self._multiple_om(notifier)

        for _ in range(10):
            om.evaluate_intent_alerts()

        self.assertEqual(notifier.calls, 2, "retries are not bounded")

    def test_a_later_success_marks_the_alert_sent(self):
        notifier = RecordingNotifier(error=an.NotifierError("HTTP 500"))
        om = self._multiple_om(notifier)
        om.evaluate_intent_alerts()
        self.assertEqual(self._alerts_on_disk()[f"MULTIPLE:{TICKER}"]
                         ["notify_state"], an.FAILED)

        notifier.error = None
        om.evaluate_intent_alerts()

        alert = self._alerts_on_disk()[f"MULTIPLE:{TICKER}"]
        self.assertEqual(alert["notify_state"], an.SENT)
        self.assertIsNone(alert["notify_last_error"])
        self.assertTrue(alert["notified_at"])


class RestartNotificationStateTest(_NotifyBase):

    def test_sent_state_survives_restart(self):
        om = self._multiple_om(RecordingNotifier())
        om.evaluate_intent_alerts()
        del om

        om2 = bot.OrderManager(self._client(), notifier=RecordingNotifier())
        alert = om2.intent_alerts[f"MULTIPLE:{TICKER}"]
        self.assertEqual(alert["notify_state"], an.SENT)
        self.assertTrue(alert["notified_at"])

    def test_failed_state_and_last_error_survive_restart(self):
        notifier = RecordingNotifier(error=an.NotifierError("HTTP 500"))
        om = self._multiple_om(notifier)
        om.evaluate_intent_alerts()
        del om

        om2 = bot.OrderManager(
            self._client(matches=[order_row("d1"), order_row("d2")]),
            notifier=RecordingNotifier())
        alert = om2.intent_alerts[f"MULTIPLE:{TICKER}"]
        self.assertEqual(alert["notify_state"], an.FAILED)
        self.assertEqual(alert["notify_last_error"], "HTTP 500")
        self.assertEqual(alert["notify_attempts"], 1)

    def test_attempt_count_keeps_growing_across_restarts(self):
        """A bounded retry budget that resets on every reboot is not
        bounded at all."""
        CFG.ALERT_NOTIFY_MAX_ATTEMPTS = 2
        for _ in range(4):
            om = bot.OrderManager(
                self._client(matches=[order_row("d1"), order_row("d2")]),
                notifier=RecordingNotifier(
                    error=an.NotifierError("HTTP 500")))
            self._submit(om)
            om.evaluate_intent_alerts()
            del om

        self.assertEqual(
            self._alerts_on_disk()[f"MULTIPLE:{TICKER}"]["notify_attempts"], 2,
            "the retry budget restarted on reboot")


if __name__ == "__main__":
    unittest.main()
