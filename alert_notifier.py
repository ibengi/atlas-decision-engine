"""External alert transport — deliberately powerless over trading.

An ambiguous-intent alert (see docs/ambiguous-intent-runbook.md) locks a
ticker fail-closed. Somebody has to hear about it. This module is how an
alert leaves the process, and its entire design constraint is the
opposite of the usual one: the channel must be UNABLE to influence
execution.

That is why the transport lives here rather than in OrderManager:

  * a notifier only ever receives a flat, explicitly-built payload -- it
    never sees the engine, the broker client, the guard or the intents
  * `send` may raise; the caller treats every failure as "not delivered"
    and nothing else. A dead webhook can never place, cancel, close or
    unlock anything.
  * swapping webhook for e-mail, Slack or a pager is implementing one
    method; no engine code changes

Nothing is configured by default: with no channel set, delivery is
SKIPPED, not failed, so an unconfigured deployment stays silent instead
of retrying forever.
"""

import json
import logging

from config import CFG

log = logging.getLogger("ALERT")

#: Delivery states persisted alongside each alert.
SENT = "SENT"
FAILED = "FAILED"
SKIPPED_NO_CHANNEL = "SKIPPED_NO_CHANNEL"
SKIPPED_SEVERITY = "SKIPPED_SEVERITY"


class NotifierError(Exception):
    """Delivery failed. Always non-fatal to the caller."""


class AlertNotifier:
    """Transport interface. Implement `send`; raise NotifierError to
    report a delivery failure. Never return a value the caller must
    interpret, and never expect the caller to retry inside `send`."""

    name = "base"
    configured = False

    def send(self, payload: dict) -> None:
        raise NotImplementedError


class NullNotifier(AlertNotifier):
    """No channel configured. Delivery is SKIPPED, never FAILED: an
    unconfigured deployment must not accumulate retries forever."""

    name = "null"
    configured = False

    def send(self, payload: dict) -> None:      # pragma: no cover - inert
        raise NotifierError("aucun canal de notification configure")


class MisconfiguredNotifier(AlertNotifier):
    """A channel was requested but cannot be used (e.g. its credential is
    missing). This is a REAL failure -- silence here would be the worst
    outcome, so it is reported as FAILED rather than skipped."""

    name = "misconfigured"
    configured = True

    def __init__(self, reason: str):
        self.reason = reason

    def send(self, payload: dict) -> None:
        raise NotifierError(f"canal inutilisable: {self.reason}")


class WebhookNotifier(AlertNotifier):
    """Generic HTTP POST of the alert payload as JSON.

    The channel credential travels in a header and NEVER in the payload
    or in a log line. Kalshi credentials are not visible from here at
    all: this module imports nothing that holds them.
    """

    name = "webhook"
    configured = True

    def __init__(self, url: str, token: str = "", timeout: float = 5.0):
        self.url, self._token, self.timeout = url, token, timeout

    def send(self, payload: dict) -> None:
        import requests
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            r = requests.post(self.url, data=json.dumps(payload),
                              headers=headers, timeout=self.timeout)
        except Exception as e:                              # noqa: BLE001
            # Le message d'erreur d'une lib HTTP peut contenir l'URL (donc
            # potentiellement un jeton en query): on ne garde que le type.
            raise NotifierError(f"transport {type(e).__name__}") from None
        if not (200 <= int(getattr(r, "status_code", 0)) < 300):
            raise NotifierError(f"HTTP {getattr(r, 'status_code', '?')}")


def build_notifier(cfg=CFG) -> AlertNotifier:
    """Factory driven by configuration. Unknown/empty -> NullNotifier."""
    url = str(getattr(cfg, "ALERT_WEBHOOK_URL", "") or "").strip()
    if not url:
        return NullNotifier()
    token = str(getattr(cfg, "ALERT_WEBHOOK_TOKEN", "") or "").strip()
    if getattr(cfg, "ALERT_WEBHOOK_REQUIRE_TOKEN", True) and not token:
        return MisconfiguredNotifier(
            "ALERT_WEBHOOK_TOKEN absent alors qu'un jeton est exige")
    return MisconfiguredNotifier("URL de webhook non https") \
        if not url.startswith("https://") else WebhookNotifier(
            url, token, float(getattr(cfg, "ALERT_NOTIFY_TIMEOUT_S", 5.0)))


def build_payload(alert: dict, key: str) -> dict:
    """The ONLY thing that leaves the process, built field by field.

    Explicitly constructed rather than dumped, so a future field added to
    an alert record cannot silently start being transmitted. Carries what
    an operator needs to act (runbook step 2): which ticker, which
    deterministic client_order_id to query at the broker, the resolution
    status, how long it has been open, how many attempts were made, and
    when. No credential, key, signature or token -- none of those are
    reachable from this module.
    """
    return {
        "source": "atlas-engine",
        "alert_key": key,
        "kind": alert.get("kind"),
        "severity": alert.get("severity"),
        "ticker": alert.get("ticker"),
        "client_order_id": alert.get("client_order_id"),
        "status": alert.get("status"),
        "age_seconds": alert.get("age_seconds"),
        "attempts": alert.get("attempts"),
        "first_raised_at": alert.get("first_raised_at"),
        "timestamp": alert.get("last_raised_at"),
        "detail": alert.get("detail"),
        "acknowledged_by": alert.get("acknowledged_by"),
    }
