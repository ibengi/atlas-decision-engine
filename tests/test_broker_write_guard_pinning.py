# -*- coding: utf-8 -*-
"""RC-2 MEDIUM-1 and MEDIUM-2: the write guard, pinned where it was only true.

WHY THIS FILE EXISTS
    The independent RC-2 review found the transport backstop CORRECT but
    under-pinned. Two distinct problems:

    MEDIUM-1, a real escape. The backstop classified the verb with
    `method.upper() in MUTATING_HTTP_METHODS`. For a bytes method
    `b"POST".upper()` is `b"POST"`, which is not in a set of *strings*, so the
    request was classified as a read, skipped the guard entirely, and was then
    handed to `requests` — which sends it as a perfectly ordinary POST. The
    policy check and the send looked at different values.

    MEDIUM-2, four mutations that survived the whole suite. Each was correct
    behaviour that no test required:

      1. `if self.env != "prod": return` — deny-unless-demo becomes
         allow-unless-prod, so an unknown env string authorizes.
      2. dropping `.upper()` — `_req("post", ...)` reaches the network.
      3. `getattr(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True)` — fail-OPEN on
         a missing attribute.
      4. `if "demo" in self.base_url: return` — moving the trust anchor from
         the environment to a URL substring.

    Behaviour is NOT changed to satisfy these tests, except for MEDIUM-1 which
    was a genuine defect. The rest were already right; what was missing was
    anything that would notice if they stopped being right.

NO NETWORK
    The session raises on any send, and every assertion that a request was
    refused also asserts that zero sends were attempted. "Refused" and "sent
    but failed" are not the same outcome and this file never conflates them.
"""

import os
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401

import kalshi_client
from config import CFG
from kalshi_client import (BrokerWriteForbidden, KalshiAPIError,
                           MUTATING_HTTP_METHODS)


class _SendAttempted(Exception):
    """Raised by the fake session so a send is observable, never performed."""


class _CountingSession:
    def __init__(self):
        self.sends = []

    def request(self, method, url, **kw):
        self.sends.append((method, url))
        raise _SendAttempted(f"{method} {url}")


class _GuardCase(unittest.TestCase):
    """Shared harness: a real client, a fake transport, a scrubbed mode."""

    def setUp(self):
        self._saved = os.environ.get("PROD_ACCESS_MODE")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("PROD_ACCESS_MODE", None)
        else:
            os.environ["PROD_ACCESS_MODE"] = self._saved

    def _client(self, env="prod", base_url="https://api.example.invalid"):
        c = kalshi_client.KalshiClient.__new__(kalshi_client.KalshiClient)
        c.env = env
        c.base_url = base_url
        c.key_id = "unit-test-not-a-credential"
        c._raw_logged = set()
        c.session = _CountingSession()
        # A stub key, NOT a fabricated credential: `_sign_headers` is replaced
        # below, so nothing ever signs with it. It exists only so that
        # `/portfolio` paths get past the "RSA key not loaded" check and reach
        # the transport — otherwise the controls that must prove a request IS
        # sent would stop one line early and pass for the wrong reason.
        c._pk = object()
        c._sign_headers = lambda method, url: {}
        return c

    def _refused(self, client, method, path="/portfolio/orders", **kw):
        """Assert the call was refused BY POLICY with nothing sent."""
        with self.assertRaises(KalshiAPIError) as caught:
            client._req(method, path, **kw)
        self.assertEqual(
            client.session.sends, [],
            f"{method!r} was refused only after a network send was attempted")
        return caught.exception


class MutatingVerbsAreClassifiedBeforeTheyAreSent(_GuardCase):
    """MEDIUM-1: normalization happens before classification."""

    def setUp(self):
        super().setUp()
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"

    def test_a_bytes_post_does_not_escape_the_backstop(self):
        """The exact escape the reviewer found."""
        err = self._refused(self._client(), b"POST")
        self.assertIsInstance(err, BrokerWriteForbidden)

    def test_a_bytes_delete_does_not_escape_the_backstop(self):
        err = self._refused(self._client(), b"DELETE")
        self.assertIsInstance(err, BrokerWriteForbidden)

    def test_a_bytearray_method_does_not_escape(self):
        err = self._refused(self._client(), bytearray(b"PUT"))
        self.assertIsInstance(err, BrokerWriteForbidden)

    def test_lowercase_and_padded_verbs_are_refused(self):
        """MEDIUM-2 item 2, and the `.upper()` that must not be dropped."""
        for method in ("post", "Post", "  post  ", "delete", "patch", "put",
                       "\tPOST\n"):
            with self.subTest(method=method):
                err = self._refused(self._client(), method)
                self.assertIsInstance(
                    err, BrokerWriteForbidden,
                    f"{method!r} was not treated as a mutation")

    def test_unclassifiable_methods_fail_closed(self):
        """An exotic object is refused as a write, never waved through.

        A method nobody can classify is not evidence that the request is a
        read. On a production account the safe reading of "unknown" is
        "mutation".
        """
        class _Weird:
            def upper(self):
                return "GET"                # lies about being a read

        for method in (None, 42, object(), _Weird(), b"\xff\xfe", ""):
            with self.subTest(method=repr(method)):
                err = self._refused(self._client(), method)
                self.assertIsInstance(
                    err, BrokerWriteForbidden,
                    f"{method!r} escaped the write guard")

    # -- controls --------------------------------------------------------

    def test_CONTROL_a_read_still_reaches_the_transport(self):
        """Without this, refusing EVERYTHING would satisfy every test above.

        Read-only production must still read. A GET has to get past the
        policy guard and be attempted; the fake session proves it did.
        """
        client = self._client()
        with self.assertRaises(_SendAttempted):
            client._req("GET", "/markets")
        self.assertEqual(len(client.session.sends), 1)
        self.assertEqual(client.session.sends[0][0], "GET")

    def test_CONTROL_a_lowercase_read_is_normalized_not_refused(self):
        client = self._client()
        with self.assertRaises(_SendAttempted):
            client._req("get", "/markets")
        self.assertEqual(client.session.sends[0][0], "GET",
                         "the verb must reach the transport normalized")

    def test_CONTROL_demo_is_unaffected_and_normalizes_bytes(self):
        """DEMO must keep working, and must send a real string verb."""
        client = self._client(env="demo")
        with self.assertRaises(_SendAttempted):
            client._req(b"POST", "/portfolio/orders", json={})
        self.assertEqual(client.session.sends[0][0], "POST",
                         "bytes reached requests un-normalized")

    def test_the_two_verb_sets_do_not_overlap(self):
        """A verb counted as both a read and a mutation would be ambiguous."""
        self.assertEqual(
            MUTATING_HTTP_METHODS & kalshi_client.READ_HTTP_METHODS, frozenset())


class GuardPolarityAndTrustAnchor(_GuardCase):
    """MEDIUM-2 items 1, 3 and 4."""

    def setUp(self):
        super().setUp()
        os.environ.pop("PROD_ACCESS_MODE", None)

    def test_an_unknown_environment_is_not_authorized(self):
        """Item 1: deny-unless-demo, never allow-unless-prod.

        `KalshiClient("staging")` receives the PRODUCTION url, because the url
        is chosen by `env != "demo"`. A guard keyed on `env == "prod"` would
        hand it an open door to a real account.
        """
        for env in ("staging", "PROD", "production", "prod ", "live", "",
                    "demo-ish", "Demo"):
            with self.subTest(env=env):
                err = self._refused(self._client(env=env), "POST")
                self.assertIsInstance(
                    err, BrokerWriteForbidden,
                    f"env={env!r} authorized a broker write")

    def test_CONTROL_the_one_environment_that_is_exempt_is_demo(self):
        """Anti-vacuity for the test above: exactly `demo` passes."""
        client = self._client(env="demo")
        with self.assertRaises(_SendAttempted):
            client._req("POST", "/portfolio/orders", json={})
        self.assertEqual(len(client.session.sends), 1)

    def test_a_missing_authorization_attribute_does_not_authorize(self):
        """Item 3: fail-closed on `AttributeError`, never fail-open.

        `getattr(CFG, ..., True)` is the tempting defensive spelling and it is
        exactly backwards: it turns a configuration that lost its gate into a
        configuration that authorizes writes.
        """
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"      # past read-only
        client = self._client()
        cls = type(CFG)
        sentinel = object()
        saved = cls.__dict__.get("LIVE_BROKER_WRITES_AUTHORIZED", sentinel)
        try:
            if saved is not sentinel:
                delattr(cls, "LIVE_BROKER_WRITES_AUTHORIZED")
            with self.assertRaises((AttributeError, KalshiAPIError)):
                client._req("POST", "/portfolio/orders", json={})
            self.assertEqual(
                client.session.sends, [],
                "a missing authorization attribute allowed a broker write")
        finally:
            if saved is not sentinel:
                setattr(cls, "LIVE_BROKER_WRITES_AUTHORIZED", saved)

    def test_the_guard_keys_on_the_environment_not_on_the_url(self):
        """Item 4: a URL substring is a strictly weaker trust anchor.

        A production client pointed at a url containing "demo" — a staging
        mirror, a proxy, a typo — must still be refused. The environment is
        what was declared; the url is an implementation detail.
        """
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
        client = self._client(base_url="https://demo-api.example.invalid")
        err = self._refused(client, "POST")
        self.assertIsInstance(err, BrokerWriteForbidden)

    def test_CONTROL_a_demo_client_on_a_production_looking_url_still_passes(self):
        """The mirror image, so the test above is about `env` and not the url."""
        client = self._client(env="demo",
                              base_url="https://api.elections.example.invalid")
        with self.assertRaises(_SendAttempted):
            client._req("POST", "/portfolio/orders", json={})
        self.assertEqual(len(client.session.sends), 1)

    def test_read_only_dominates_an_armed_write_authorization(self):
        """READ_ONLY is checked before the write authorization, by behaviour."""
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True):
            err = self._refused(client, "POST")
        self.assertIsInstance(err, BrokerWriteForbidden)
        self.assertIn("LECTURE SEULE", str(err),
                      "the refusal must name read-only, not the write flag; "
                      "otherwise the ordering is not what was tested")

    def test_CONTROL_capital_plus_authorization_reaches_the_transport(self):
        """Both gates open must actually let a write through.

        Every refusal above would be satisfied by a guard that refuses
        unconditionally. This is the run that says it does not.
        """
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True):
            with self.assertRaises(_SendAttempted):
                client._req("POST", "/portfolio/orders", json={})
        self.assertEqual(len(client.session.sends), 1)
        self.assertEqual(client.session.sends[0][0], "POST")


class CancellationMatrixMatchesTheOperatorProcedure(_GuardCase):
    """RC-2 MEDIUM-3: the documented cancellation matrix, pinned.

    `docs/cancellation-operator-procedure.md` tells an operator what
    `cancel_order` does in each state. A runbook that drifts from the code is
    worse than none, because it is consulted under pressure. These tests are
    the matrix in that document, executed.
    """

    def _cancel(self, client):
        return client.cancel_order("order-1")

    def test_read_only_refuses_cancellation(self):
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
        client = self._client()
        with self.assertRaises(BrokerWriteForbidden):
            self._cancel(client)
        self.assertEqual(client.session.sends, [])

    def test_read_only_refuses_even_with_write_authorization_armed(self):
        """The row of the table that surprises people."""
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True):
            with self.assertRaises(BrokerWriteForbidden):
                self._cancel(client)
        self.assertEqual(client.session.sends, [])

    def test_capital_without_authorization_refuses_cancellation(self):
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", False):
            with self.assertRaises(BrokerWriteForbidden):
                self._cancel(client)
        self.assertEqual(client.session.sends, [])

    def test_CONTROL_capital_with_authorization_reaches_the_transport(self):
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True):
            with self.assertRaises(_SendAttempted):
                self._cancel(client)
        self.assertEqual(len(client.session.sends), 1)
        self.assertEqual(client.session.sends[0][0], "DELETE")

    def test_the_kill_switch_does_not_block_cancellation(self):
        """Documented asymmetry: the breaker must not trap an open order.

        `KILL_SWITCH` stops `create_order`. It deliberately does NOT stop
        `cancel_order`, because cancelling reduces exposure. If this ever
        changes, the runbook's emergency procedure becomes wrong.
        """
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True), \
                patch.object(CFG, "KILL_SWITCH", True):
            with self.assertRaises(_SendAttempted):
                self._cancel(client)
        self.assertEqual(len(client.session.sends), 1,
                         "the kill switch blocked a cancellation")

    def test_CONTROL_the_kill_switch_does_block_submission(self):
        """The asymmetry above is only meaningful if the switch works."""
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        client = self._client()
        with patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True), \
                patch.object(CFG, "ALLOW_ORDER_SUBMISSION", True), \
                patch.object(CFG, "KILL_SWITCH", True):
            with self.assertRaises(KalshiAPIError) as caught:
                client.create_order("KXBTC15M-26SEP0521-T30", "yes", 1, 43)
        self.assertIn("KILL_SWITCH", str(caught.exception))
        self.assertEqual(client.session.sends, [])

    def test_demo_cancellation_is_unaffected(self):
        client = self._client(env="demo")
        with self.assertRaises(_SendAttempted):
            self._cancel(client)
        self.assertEqual(len(client.session.sends), 1)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
