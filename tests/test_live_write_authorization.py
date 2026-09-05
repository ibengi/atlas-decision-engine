# -*- coding: utf-8 -*-
"""LIVE broker writes require explicit client-boundary authorization (E-2).

SECURITY INVARIANT
    "A broker write in PRODUCTION requires explicit client-boundary
     authorization. LIVE read-only observation requires none."

Before this, nothing refused a write *because the environment was production*.
Every control was a policy flag that a legitimate operation might open for its
own reasons — `ALLOW_ORDER_SUBMISSION` for a canary, `LIVE_TRADING` to reach
production at all, `MODEL_APPROVED` on promotion. Any of those being true would
have left a real account one bug away from a mutation. `LIVE_BROKER_WRITES_AUTHORIZED`
is deliberately distinct from all of them, so LIVE observation is read-only BY
CONSTRUCTION rather than by configuration.

The matrix below is the point of this file: for every write path, in every
authorization state, and — critically — with every higher-level flag set
permissive, a LIVE write must still be refused, with zero mutating HTTP calls.
"""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

import _bootstrap  # noqa: F401

from config import CFG
from kalshi_client import (BrokerWriteForbidden, KalshiAPIError, KalshiClient,
                           MUTATING_HTTP_METHODS)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every gate that could plausibly be mistaken for "you may write to LIVE".
_PERMISSIVE_EVERYTHING = {
    "ALLOW_ORDER_SUBMISSION": True,
    "KILL_SWITCH": False,
    "DAILY_RESEARCH_ORACLE_APPROVED": True,
}


def _live_client():
    """A production client whose transport is a mock.

    Any HTTP the guard fails to prevent shows up as a recorded call, so
    "blocked" cannot be confused with "the call silently did nothing".
    """
    c = KalshiClient.__new__(KalshiClient)      # no __init__: needs no creds
    c.env = "prod"
    c.base_url = CFG.PROD_URL
    c.key_id = "unused"
    c._pk = object()                            # non-None: key check passes
    # Signing is irrelevant here and needs a real RSA key; stub it so the
    # tests exercise the GUARD rather than the crypto.
    c._sign_headers = lambda method, url: {"Content-Type": "application/json"}
    c._raw_logged = set()
    c.cache_enabled = False
    c._balance_cache = c._markets_cache = None
    c.session = MagicMock()
    c.session.request.return_value = MagicMock(
        status_code=200, text="{}", json=lambda: {})
    return c


def _mutating_calls(client):
    """How many mutating HTTP requests actually reached the transport."""
    return [call for call in client.session.request.call_args_list
            if str(call.args[0]).upper() in MUTATING_HTTP_METHODS]


class GateIsStrictAndFailsClosed(unittest.TestCase):
    """Absent / blank / malformed / false must all read FALSE."""

    def _read(self, value):
        probe = "import json, config; print(json.dumps(config.CFG.LIVE_BROKER_WRITES_AUTHORIZED))"
        env = {k: v for k, v in os.environ.items()
               if k != "LIVE_BROKER_WRITES_AUTHORIZED"}
        env.update({"PROBE_PROVIDERS_ON_START": "0",
                    "KALSHI_DEMO_KEY_ID": "test",
                    "KALSHI_DEMO_PRIVATE_KEY": "test"})
        if value is not None:
            env["LIVE_BROKER_WRITES_AUTHORIZED"] = value
        out = subprocess.run([sys.executable, "-c", probe], cwd=_ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_absent_is_false(self):
        self.assertFalse(self._read(None))

    def test_blank_and_whitespace_are_false(self):
        for v in ("", " ", "\t", "\n"):
            self.assertFalse(self._read(v), repr(v))

    def test_malformed_is_false(self):
        for v in ("maybe", "ture", "TRUE!", "2", "-1", "yes;drop", "authorized"):
            self.assertFalse(self._read(v), repr(v))

    def test_explicit_false_words_are_false(self):
        for v in ("false", "0", "no", "n", "off", "non", "FALSE", " No "):
            self.assertFalse(self._read(v), repr(v))

    def test_only_explicit_true_words_arm_it(self):
        for v in ("true", "1", "yes", "y", "on", "TRUE", " Yes "):
            self.assertTrue(self._read(v), repr(v))


class LiveWritesAreRefusedAtTheClientBoundary(unittest.TestCase):

    def setUp(self):
        self._saved = {k: getattr(CFG, k) for k in
                       list(_PERMISSIVE_EVERYTHING) + ["LIVE_BROKER_WRITES_AUTHORIZED"]}
        for k, v in _PERMISSIVE_EVERYTHING.items():
            setattr(CFG, k, v)
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = False
        # RC-3 added PROD_ACCESS_MODE, a STRICTLY HIGHER-priority prohibition:
        # in READ_ONLY nothing can write, so this class's subject — the write
        # AUTHORIZATION gate below it — would never be reached. CAPITAL is set
        # so these tests still exercise the gate they were written for.
        # READ_ONLY dominance is asserted separately, in
        # tests/test_prod_access_mode.py.
        self._mode = os.environ.get("PROD_ACCESS_MODE")
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        self.client = _live_client()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(CFG, k, v)
        os.environ.pop("PROD_ACCESS_MODE", None)
        if self._mode is not None:
            os.environ["PROD_ACCESS_MODE"] = self._mode

    def test_create_order_is_refused_with_no_network_write(self):
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("LIVE_BROKER_WRITES_AUTHORIZED", str(cm.exception))
        self.assertEqual(_mutating_calls(self.client), [])

    def test_cancel_order_is_refused_with_no_network_write(self):
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.cancel_order("ord_1")
        self.assertIn("LIVE_BROKER_WRITES_AUTHORIZED", str(cm.exception))
        self.assertEqual(_mutating_calls(self.client), [])

    def test_every_mutating_verb_is_refused_at_the_transport(self):
        """A FUTURE write method is covered even if its author forgets the guard."""
        for verb in sorted(MUTATING_HTTP_METHODS):
            with self.subTest(verb=verb):
                with self.assertRaises(BrokerWriteForbidden):
                    self.client._req(verb, "/portfolio/some/future/endpoint")
        self.assertEqual(_mutating_calls(self.client), [])

    def test_zero_mutating_calls_of_each_verb(self):
        """Explicit per-verb counters, as the mission requires."""
        for op in (lambda: self.client.create_order("KXBTC15M-X", "yes", 1, 40),
                   lambda: self.client.cancel_order("ord_1")):
            with self.assertRaises(KalshiAPIError):
                op()
        counts = {v: 0 for v in MUTATING_HTTP_METHODS}
        for call in self.client.session.request.call_args_list:
            verb = str(call.args[0]).upper()
            if verb in counts:
                counts[verb] += 1
        self.assertEqual(counts, {"POST": 0, "PUT": 0, "PATCH": 0, "DELETE": 0})

    def test_permissive_higher_level_flags_do_NOT_bypass_the_guard(self):
        """THE critical case.

        Every higher-level flag is set as permissively as it can be — the
        engine's own submission gate open, the kill switch off, the daily
        oracle approved, and the process-level LIVE confirmations present.
        The client boundary must still refuse.
        """
        os.environ["LIVE_TRADING"] = "1"
        os.environ["LIVE_TRADING_CONFIRMED"] = "YES"
        os.environ["KALSHI_ENV_CONFIRM"] = "LIVE"
        os.environ["MODEL_APPROVED_FOR_LIVE"] = "YES"
        os.environ["NO_LIVE_PROMOTION"] = "0"
        try:
            self.assertTrue(CFG.ALLOW_ORDER_SUBMISSION)
            self.assertFalse(CFG.KILL_SWITCH)
            with self.assertRaises(BrokerWriteForbidden):
                self.client.create_order("KXBTC15M-X", "yes", 1, 40)
            with self.assertRaises(BrokerWriteForbidden):
                self.client.cancel_order("ord_1")
            self.assertEqual(_mutating_calls(self.client), [])
        finally:
            for k in ("LIVE_TRADING", "LIVE_TRADING_CONFIRMED",
                      "KALSHI_ENV_CONFIRM", "MODEL_APPROVED_FOR_LIVE",
                      "NO_LIVE_PROMOTION"):
                os.environ.pop(k, None)

    def test_create_order_refuses_WITHOUT_relying_on_the_transport_guard(self):
        """Each layer must hold ALONE.

        Mutation M3 (deleting create_order's own guard) initially SURVIVED:
        every test still passed because the transport backstop caught the
        write anyway. Two layers existed but only one was pinned, so the
        method-level guard could have been deleted silently. Here the
        transport is replaced by a recorder, so only the method's own guard
        can produce the refusal.
        """
        reached = []
        self.client._req = lambda *a, **k: reached.append(a) or {}
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("create_order", str(cm.exception))
        self.assertEqual(reached, [],
                         "create_order relied on _req to refuse; its own "
                         "guard is missing")

    def test_cancel_order_refuses_WITHOUT_relying_on_the_transport_guard(self):
        """Same for M4."""
        reached = []
        self.client._req = lambda *a, **k: reached.append(a) or {}
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.cancel_order("ord_1")
        self.assertIn("cancel_order", str(cm.exception))
        self.assertEqual(reached, [],
                         "cancel_order relied on _req to refuse; its own "
                         "guard is missing")

    def test_transport_guard_holds_without_any_method_guard(self):
        """The converse: the backstop alone must also refuse.

        Together with the two tests above, this pins BOTH layers
        independently, so neither can be removed without a named failure.
        """
        with self.assertRaises(BrokerWriteForbidden):
            self.client._req("POST", "/portfolio/events/orders", json={})
        self.assertEqual(_mutating_calls(self.client), [])

    def test_control_authorized_live_write_reaches_the_transport(self):
        """Anti-vacuity: with authorization the write proceeds.

        Without this, every refusal above could be produced by a client that
        simply never works in production.
        """
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = True
        self.client.session.request.side_effect = RuntimeError("REACHED_TRANSPORT")
        with self.assertRaises(Exception) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertNotIn("LIVE_BROKER_WRITES_AUTHORIZED", str(cm.exception))
        self.assertEqual(len(_mutating_calls(self.client)), 1)


class LiveReadsRemainAvailable(unittest.TestCase):
    """The control must block writes, not reads."""

    def setUp(self):
        self._saved = CFG.LIVE_BROKER_WRITES_AUTHORIZED
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = False     # writes forbidden
        self.client = _live_client()

    def tearDown(self):
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = self._saved

    def test_every_read_still_reaches_the_transport(self):
        reads = {
            "get_markets":   lambda: self.client.get_markets("KXBTC15M"),
            "get_market":    lambda: self.client.get_market("KXBTC15M-X"),
            "get_balance":   lambda: self.client._fetch_balance(),
            "get_order":     lambda: self.client.get_order("ord_1"),
            "get_fills":     lambda: self.client.get_fills("ord_1"),
            "list_orders":   lambda: self.client.list_orders(),
            "get_positions": lambda: self.client.get_positions(),
        }
        for name, call in reads.items():
            with self.subTest(read=name):
                self.client.session.request.reset_mock()
                try:
                    call()
                except BrokerWriteForbidden:            # pragma: no cover
                    self.fail(f"{name} was blocked by the WRITE guard")
                except Exception:
                    pass        # shape errors from the mock are irrelevant here
                verbs = [str(c.args[0]).upper()
                         for c in self.client.session.request.call_args_list]
                self.assertTrue(verbs, f"{name} issued no request at all")
                self.assertTrue(
                    all(v == "GET" for v in verbs),
                    f"{name} used a mutating verb: {verbs}")

    def test_reads_use_no_mutating_verb_anywhere_in_the_client(self):
        """Static companion: no read path may be built on a mutating verb."""
        import re
        src = open(os.path.join(_ROOT, "kalshi_client.py"), encoding="utf-8").read()
        calls = re.findall(r'self\._req\(\s*"([A-Z]+)"\s*,\s*([^\n]*)', src)
        mutating = [(verb, tail) for verb, tail in calls
                    if verb in MUTATING_HTTP_METHODS]
        self.assertEqual(
            len(mutating), 2,
            f"the set of mutating transport calls changed: {mutating}. Each "
            f"must be a deliberate, guarded broker write.")


class DemoBehaviourIsUnchanged(unittest.TestCase):
    """DEMO must be entirely unaffected by the LIVE write guard."""

    def setUp(self):
        self._saved = CFG.LIVE_BROKER_WRITES_AUTHORIZED
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = False   # irrelevant in DEMO

    def tearDown(self):
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = self._saved

    def test_demo_write_guard_is_a_noop(self):
        c = KalshiClient("demo")
        c.session = MagicMock()
        try:
            c._assert_broker_write_allowed("create_order")
        except BrokerWriteForbidden:                # pragma: no cover
            self.fail("the LIVE write guard fired in DEMO")

    def test_demo_create_order_still_governed_only_by_policy_gates(self):
        saved = CFG.ALLOW_ORDER_SUBMISSION
        try:
            c = KalshiClient("demo")
            c.session = MagicMock()
            c.session.request.side_effect = RuntimeError("REACHED_TRANSPORT")
            CFG.ALLOW_ORDER_SUBMISSION = True
            with self.assertRaises(Exception) as cm:
                c.create_order("KXBTC15M-X", "yes", 1, 40)
            self.assertNotIn("LIVE_BROKER_WRITES_AUTHORIZED", str(cm.exception))
            CFG.ALLOW_ORDER_SUBMISSION = False
            with self.assertRaises(KalshiAPIError) as cm2:
                c.create_order("KXBTC15M-X", "yes", 1, 40)
            self.assertIn("ALLOW_ORDER_SUBMISSION", str(cm2.exception))
        finally:
            CFG.ALLOW_ORDER_SUBMISSION = saved

    def test_demo_cancel_still_works(self):
        c = KalshiClient("demo")
        c.session = MagicMock()
        c.session.request.side_effect = RuntimeError("REACHED_TRANSPORT")
        with self.assertRaises(Exception) as cm:
            c.cancel_order("ord_1")
        self.assertNotIn("LIVE_BROKER_WRITES_AUTHORIZED", str(cm.exception))


class GuardIsIndependentOfEveryOtherFlag(unittest.TestCase):
    """The authorization must not be derived from another gate."""

    def test_the_gate_is_read_from_its_own_variable(self):
        import inspect
        import config
        src = inspect.getsource(config.Config)
        line = [ln for ln in src.splitlines()
                if "LIVE_BROKER_WRITES_AUTHORIZED" in ln and "_env_gate" in ln]
        self.assertTrue(line, "the gate is not declared with the strict parser")
        joined = " ".join(line)
        for other in ("ALLOW_ORDER_SUBMISSION", "LIVE_TRADING",
                      "MODEL_APPROVED", "DAILY_RESEARCH_ORACLE_APPROVED"):
            self.assertNotIn(other, joined,
                             f"the LIVE write authorization is derived from "
                             f"{other}; it must be independent")

    def test_the_guard_consults_only_env_and_its_own_gate(self):
        import inspect
        from kalshi_client import KalshiClient as KC
        import ast, textwrap
        body = inspect.getsource(KC._assert_broker_write_allowed)
        # Strip the docstring with ast, not by eye: the prose deliberately
        # NAMES the other gates to explain why it is independent of them, and
        # a naive filter would read that prose as a dependency.
        fn = ast.parse(textwrap.dedent(body)).body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)):
            fn.body = fn.body[1:]
        code = ast.unparse(fn)
        self.assertIn("LIVE_BROKER_WRITES_AUTHORIZED", code)
        self.assertIn("self.env", code)
        for other in ("ALLOW_ORDER_SUBMISSION", "KILL_SWITCH", "MODEL_APPROVED"):
            self.assertNotIn(other, code,
                             f"{other} can influence the LIVE write guard")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
