# -*- coding: utf-8 -*-
"""RC-3 HIGH-1: the read-only shadow claim, proved by EXECUTION.

WHY THIS FILE EXISTS
    RC-3 claims `SHADOW_INVOKES_WRITE_LAYER=NO`. That claim was defended by
    assertions over the SOURCE TEXT of `_execute_decision` — that the string
    `prod_is_read_only()` appears before the string `place_and_track`. The
    independent reviewer showed the weakness directly: source-order assertions
    survive mutations that change behaviour, because they never run the code.
    A test that reads the program cannot fail for the reason the program is
    wrong.

WHAT IS ASSERTED INSTEAD
    The real `ExecutionEngine._execute_decision` is executed against a real
    `OrderManager` and a real `KalshiClient`, with the WRITE BOUNDARY
    instrumented at three depths:

        place_and_track   the order manager's entry point
        create_order      the client's write method
        _req              the transport, split into mutating and read verbs

    Under `PROD_ACCESS_MODE=READ_ONLY` the decision must run to completion —
    book, risk gates, sizing, WOULD_SUBMIT telemetry — while all three
    recorders stay at zero.

THE ANTI-VACUITY CONTROL
    Zero crossings proves nothing unless the recorders CAN fire. Every
    read-only assertion is therefore paired with a `CAPITAL` run that is
    identical except for the mode, and that run must drive the very same
    recorders above zero, including a `POST` at the transport. That is what
    makes the zero meaningful: the instrumentation demonstrably observes the
    boundary when the path reaches it.

NO NETWORK, NO CREDENTIALS
    `_req` is replaced by a recorder that returns canned responses, and the
    session object raises if anything ever tries to send. No Kalshi host is
    contacted, no production credential is read, and every environment
    variable this module touches is restored.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401

import config
from config import CFG
import execution_engine
import kalshi_client
import order_manager

TICKER = "KXBTC15M-26SEP0521-T30"
MARKET_TYPE = "btc_15m_above_strike"


class _ExplodingSession:
    """Any real network call is a test failure, not a skipped assertion."""

    def request(self, method, url, **kw):        # pragma: no cover - guard
        raise AssertionError(
            f"a real HTTP request escaped the test harness: {method} {url}")


class _Boundary:
    """Every crossing of the broker write boundary, at three depths."""

    def __init__(self):
        self.place_and_track = []
        self.create_order = []
        self.cancel_order = []
        self.http = []

    @property
    def mutating_http(self):
        return [(m, p) for m, p in self.http
                if isinstance(m, str) and m.upper()
                in kalshi_client.MUTATING_HTTP_METHODS]

    def summary(self):
        return (f"place_and_track={len(self.place_and_track)} "
                f"create_order={len(self.create_order)} "
                f"cancel_order={len(self.cancel_order)} "
                f"mutating_http={len(self.mutating_http)}")


class _Decision:
    """A decision that clears every upstream gate, so the read-only branch is
    genuinely the thing that stops it — not an earlier refusal."""

    ticker = TICKER
    market_type = MARKET_TYPE
    strategy = "btc15m"
    side = "yes"
    confidence = 8
    taille = "2%"
    model_probability = 0.62
    market_probability = 0.43
    net_edge = 0.19
    net_ev = 0.17
    category = "Crypto"


class _IsolatedState:
    """Each test gets its own DATA_DIR and its own environment.

    Without this the order manager's durable session state — the duplicate
    submission guard above all — leaks between tests: the CAPITAL control
    submitted once, wrote a 6-hour lock on the ticker, and every later run
    was refused by that lock rather than by the code under test. A control
    that silently stops working is worse than no control, because the zeros
    it is supposed to justify keep passing.
    """

    ENV_KEYS = ("PROD_ACCESS_MODE", "KALSHI_ENV_CONFIRM", "LIVE_TRADING",
                "LIVE_TRADING_CONFIRMED", "LIVE_BROKER_WRITES_AUTHORIZED")

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self._tmp = tempfile.mkdtemp(prefix="atlas-shadow-")
        self._data_dir = patch.object(CFG, "DATA_DIR", self._tmp)
        self._data_dir.start()
        self.boundary = _Boundary()

    def tearDown(self):
        self._data_dir.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ReadOnlyShadowNeverEntersTheWriteLayer(_IsolatedState, unittest.TestCase):
    """RC-3 §4, by execution rather than by reading the source."""

    # -- harness ---------------------------------------------------------

    def _client(self):
        """A real KalshiClient in a production environment, with the
        transport replaced by a recorder. Its own guards remain real."""
        c = kalshi_client.KalshiClient.__new__(kalshi_client.KalshiClient)
        c.env = "prod"
        c.base_url = "https://example.invalid"
        c.key_id = "unit-test-not-a-credential"
        c._pk = None
        c._raw_logged = set()
        c.session = _ExplodingSession()
        boundary = self.boundary

        def _req(method, path, **kw):
            boundary.http.append((method, path))
            if path.endswith("/orders") or "/orders" in path:
                return {"order": {"order_id": "test-order-1",
                                  "client_order_id": kw.get("json", {})
                                  .get("client_order_id", "cid"),
                                  "ticker": TICKER, "status": "resting",
                                  "fill_count": 0, "remaining_count": 1,
                                  "reduced_by": 1}}
            return {}

        c._req = _req
        return c

    def _engine(self, client):
        """The real `_execute_decision`, with only its collaborators stubbed.

        A real `OrderManager` sits under the engine so that `place_and_track`
        and `create_order` are the genuine articles rather than mocks that
        would agree with any claim made about them.
        """
        eng = execution_engine.ExecutionEngine.__new__(
            execution_engine.ExecutionEngine)
        eng.client = client
        eng.capital = 500.0
        eng.configured_capital = 500.0
        eng.fresh_book = lambda t: (
            {"ticker": t},
            {"yes_ask": 43, "yes_bid": 41, "no_ask": 57, "no_bid": 55})

        class _Risk:
            def portfolio_check(self, *a, **k):
                return (True, "")

            def rolling_drawdown(self):
                return 0.0

            def drawdown_size_factor(self):
                return 1.0

            def claim_half_open_attempt(self, ticker):
                return (True, "")

            def release_half_open_attempt(self, *a, **k):
                return None

        class _Pos:
            def open_risk_by_category(self):
                return {}

            def open_risk_on(self, ticker):
                return 0.0

            def open_risk(self):
                return 0.0

        eng.risk = _Risk()
        eng.posmgr = _Pos()

        om = order_manager.OrderManager(client, notifier=_NullNotifier())
        boundary = self.boundary
        real_place = om.place_and_track

        def place_and_track(*a, **k):
            boundary.place_and_track.append((a, k))
            return real_place(*a, **k)

        om.place_and_track = place_and_track

        real_create = client.create_order
        real_cancel = client.cancel_order

        def create_order(*a, **k):
            boundary.create_order.append((a, k))
            return real_create(*a, **k)

        def cancel_order(*a, **k):
            boundary.cancel_order.append((a, k))
            return real_cancel(*a, **k)

        client.create_order = create_order
        client.cancel_order = cancel_order

        eng.orders = om
        return eng

    def _run(self, mode, *, shadow, allow_submission, write_auth):
        os.environ["PROD_ACCESS_MODE"] = mode
        report = {"rejections": {}}
        client = self._client()
        eng = self._engine(client)
        with patch.object(CFG, "SHADOW_MODE", shadow), \
                patch.object(CFG, "ALLOW_ORDER_SUBMISSION", allow_submission), \
                patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", write_auth), \
                patch.object(CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
                patch.object(CFG, "KILL_SWITCH", False):
            placed = eng._execute_decision(_Decision(), report)
        return placed, report

    # -- the claim -------------------------------------------------------

    def test_read_only_shadow_runs_the_decision_and_touches_no_write_layer(self):
        """SHADOW_WRITE_LAYER_CALLS=0, established by running the code."""
        placed, report = self._run("READ_ONLY", shadow=True,
                                   allow_submission=True, write_auth=True)

        # The decision really ran: it passed the risk gates and was sized.
        # Without this the zeros below could mean "refused at line one".
        self.assertEqual(report.get("risk_passed"), 1,
                         "the decision did not reach sizing, so the zeros "
                         "below would prove nothing about the shadow path")
        self.assertEqual(report.get("would_submit"), 1,
                         "WOULD_SUBMIT telemetry was not produced")
        self.assertEqual(report["rejections"].get("prod_read_only"), 1)
        self.assertEqual(placed, 0)
        self.assertNotIn("orders_submitted", report)

        self.assertEqual(self.boundary.place_and_track, [],
                         f"write layer entered: {self.boundary.summary()}")
        self.assertEqual(self.boundary.create_order, [],
                         f"write method called: {self.boundary.summary()}")
        self.assertEqual(self.boundary.cancel_order, [],
                         f"cancel called: {self.boundary.summary()}")
        self.assertEqual(self.boundary.mutating_http, [],
                         f"mutating HTTP emitted: {self.boundary.http}")

    def test_read_only_without_shadow_mode_is_equally_isolated(self):
        """READ_ONLY must dominate on its own, not only alongside SHADOW.

        If the isolation came from `SHADOW_MODE` rather than from the mode,
        turning shadow off would reach the write layer — and an operator who
        cleared `SHADOW_MODE` on a read-only deployment would be submitting.
        """
        placed, report = self._run("READ_ONLY", shadow=False,
                                   allow_submission=True, write_auth=True)
        self.assertEqual(report.get("would_submit"), 1)
        self.assertEqual(placed, 0)
        self.assertEqual(self.boundary.place_and_track, [])
        self.assertEqual(self.boundary.create_order, [])
        self.assertEqual(self.boundary.mutating_http, [])

    def test_an_unreadable_mode_is_read_only_too(self):
        """Fail-closed: a typo in the mode must not become CAPITAL."""
        placed, report = self._run("CAPITOL", shadow=False,
                                   allow_submission=True, write_auth=True)
        self.assertEqual(report.get("would_submit"), 1)
        self.assertEqual(self.boundary.create_order, [])
        self.assertEqual(self.boundary.mutating_http, [])

    def test_an_environment_that_is_neither_demo_nor_prod_is_still_read_only(self):
        """RC-3 MED-4: the branch keys on "not demo", never on "is prod".

        `KalshiClient("staging")` receives the PRODUCTION url, because the url
        is chosen by `env != "demo"`. A read-only branch written as
        `env == "prod"` would therefore be SKIPPED for exactly the
        environments that reach a real account under a name nobody
        anticipated, and the engine would walk into the write layer.

        Not reachable today — the entrypoint computes `env` as exactly
        `"demo"` or `"prod"` — which is precisely why nothing noticed. This
        mutant survived the full 947-test suite before this test existed.
        """
        for env in ("staging", "PROD", "production", "live", "", "sandbox"):
            with self.subTest(env=env):
                self.boundary = _Boundary()
                os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
                report = {"rejections": {}}
                client = self._client()
                client.env = env
                eng = self._engine(client)
                with patch.object(CFG, "SHADOW_MODE", False), \
                        patch.object(CFG, "ALLOW_ORDER_SUBMISSION", True), \
                        patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True), \
                        patch.object(CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
                        patch.object(CFG, "KILL_SWITCH", False):
                    eng._execute_decision(_Decision(), report)
                self.assertEqual(
                    report.get("would_submit"), 1,
                    f"env={env!r} skipped the read-only branch")
                self.assertEqual(
                    self.boundary.place_and_track, [],
                    f"env={env!r} entered the write layer: "
                    f"{self.boundary.summary()}")
                self.assertEqual(self.boundary.create_order, [])

    # -- the anti-vacuity control ---------------------------------------

    def test_CONTROL_capital_drives_the_same_recorders_above_zero(self):
        """CAPITAL_CONTROL_NON_VACUOUS.

        Identical execution, identical instrumentation, one variable changed:
        the access mode. If this run did not reach the write layer, every
        zero asserted above would be satisfied by a broken harness rather
        than by the read-only branch.
        """
        placed, report = self._run("CAPITAL", shadow=False,
                                   allow_submission=True, write_auth=True)

        self.assertEqual(report.get("risk_passed"), 1)
        self.assertNotIn("would_submit", report,
                         "CAPITAL must submit, not merely observe")
        self.assertEqual(report.get("orders_submitted"), 1)

        self.assertEqual(len(self.boundary.place_and_track), 1,
                         f"control failed to enter the write layer: "
                         f"{self.boundary.summary()}")
        self.assertEqual(len(self.boundary.create_order), 1,
                         f"control failed to reach create_order: "
                         f"{self.boundary.summary()}")
        self.assertTrue(
            self.boundary.mutating_http,
            f"control emitted no mutating HTTP, so 'mutating_http == 0' "
            f"under READ_ONLY is not evidence: {self.boundary.http}")
        self.assertEqual(self.boundary.mutating_http[0][0].upper(), "POST")

    def test_CONTROL_the_boundary_recorder_reports_zero_only_when_true(self):
        """The recorder itself is not a constant zero."""
        self.assertEqual(self.boundary.mutating_http, [])
        self.boundary.http.append(("POST", "/trade-api/v2/portfolio/orders"))
        self.assertEqual(len(self.boundary.mutating_http), 1)
        self.boundary.http.append(("GET", "/trade-api/v2/portfolio/orders"))
        self.assertEqual(len(self.boundary.mutating_http), 1,
                         "a read verb must not be counted as a mutation")


class _NullNotifier:
    """Alert transport that cannot reach anything outside the process."""

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return True

    def notify(self, *a, **k):
        self.sent.append((a, k))
        return True


class _ReconcileTlog:
    def __init__(self):
        self.rows = []

    def log_trade(self, *a, **k):
        self.rows.append((a, k))

    def append(self, *a, **k):
        self.rows.append((a, k))


class _ReconcilePos:
    def sync_from_broker(self, *a, **k):
        return None

    def refresh(self, *a, **k):
        return None


class ReconciliationDoesNotCancelUnderReadOnly(_IsolatedState,
                                               unittest.TestCase):
    """RC-3 §5, by execution: recovery observes, it does not repair."""

    def _manager(self):
        c = kalshi_client.KalshiClient.__new__(kalshi_client.KalshiClient)
        c.env = "prod"
        c.base_url = "https://example.invalid"
        c.key_id = "unit-test-not-a-credential"
        c._pk = None
        c._raw_logged = set()
        c.session = _ExplodingSession()
        boundary = self.boundary

        def _req(method, path, **kw):
            boundary.http.append((method, path))
            return {"order": {"order_id": "stuck-1", "reduced_by": 1}}

        c._req = _req
        c.get_order = lambda oid: {"order_id": oid, "status": "resting",
                                   "fill_count": 0, "remaining_count": 1}
        c.get_fills = lambda oid, **k: []

        real_cancel = c.cancel_order

        def cancel_order(*a, **k):
            boundary.cancel_order.append((a, k))
            return real_cancel(*a, **k)

        c.cancel_order = cancel_order

        om = order_manager.OrderManager(c, notifier=_NullNotifier())
        om.pending_intents = {}
        om.open_orders = {"stuck-1": {"ticker": TICKER, "side": "yes",
                                      "count": 1, "price": 43,
                                      "ts": 0.0}}
        return om

    def _reconcile(self, mode):
        os.environ["PROD_ACCESS_MODE"] = mode
        om = self._manager()
        _Tlog, _Pos = _ReconcileTlog, _ReconcilePos
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True), \
                patch.object(CFG, "KILL_SWITCH", False):
            try:
                om.reconcile_startup(_Tlog(), _Pos())
            except Exception as exc:               # pragma: no cover - noise
                # Reconciliation may fail for reasons unrelated to the mode
                # (journal shape, position manager). What is under test is
                # whether a CANCEL was attempted, and that is recorded either
                # way. Re-raise only if nothing was recorded to look at.
                if not self.boundary.http and not self.boundary.cancel_order:
                    raise
                self._error = exc
        return om

    def test_read_only_recovery_attempts_no_cancellation(self):
        self._reconcile("READ_ONLY")
        self.assertEqual(self.boundary.cancel_order, [],
                         "recovery cancelled an order in READ_ONLY")
        self.assertEqual(self.boundary.mutating_http, [],
                         f"recovery emitted a mutation: {self.boundary.http}")

    def test_recovery_in_an_unnamed_environment_is_also_read_only(self):
        """RC-3 MED-4, reconciliation side: same predicate, same risk."""
        for env in ("staging", "PROD", "production", ""):
            with self.subTest(env=env):
                self.boundary = _Boundary()
                os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
                om = self._manager()
                om.client.env = env
                with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True), \
                        patch.object(CFG, "KILL_SWITCH", False):
                    try:
                        om.reconcile_startup(_ReconcileTlog(), _ReconcilePos())
                    except Exception:
                        if not self.boundary.http and \
                                not self.boundary.cancel_order:
                            raise
                self.assertEqual(
                    self.boundary.cancel_order, [],
                    f"env={env!r} cancelled during read-only recovery")
                self.assertEqual(self.boundary.mutating_http, [])

    def test_CONTROL_capital_recovery_does_attempt_cancellation(self):
        """Without this, the zero above could mean recovery never ran."""
        self._reconcile("CAPITAL")
        self.assertTrue(
            self.boundary.cancel_order or self.boundary.mutating_http,
            f"control never reached the cancel path, so the READ_ONLY zero "
            f"is not evidence: {self.boundary.summary()} {self.boundary.http}")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
