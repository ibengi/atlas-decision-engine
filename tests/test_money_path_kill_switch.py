# -*- coding: utf-8 -*-
"""Kill switch on the MONEY PATH, and the client-layer backstop (Workstream B).

Before this change `KILL_SWITCH` was read once per cycle in
`execution_engine`, upstream of the money path. Anything reaching
`OrderManager.place_and_track` by another route — a tool, the recovery path, an
integration script, a future caller — never consulted it, and once a cycle had
begun nothing re-read it. A circuit breaker that does not cover the money path
is not a circuit breaker.

Two independent layers are asserted here:
  1. `place_and_track` re-reads the switch on every order.
  2. `KalshiClient.create_order` refuses regardless of caller — the choke point
     no path can route around.

Anti-vacuity throughout: every refusal test is paired with a control in which
the same call SUCCEEDS once the condition is lifted, so a test cannot pass
because the order was blocked for some unrelated reason.
"""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

import _bootstrap  # noqa: F401

import config
from config import CFG
from kalshi_client import KalshiAPIError

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_OK_ORDER = {"order_id": "ord_1", "status": "executed",
             "fill_count": "1.00", "remaining_count": "0.00",
             "avg_price_cents": 40}


def _order_manager():
    import order_manager
    cli = MagicMock()
    cli.env = "demo"
    cli.create_order.return_value = dict(_OK_ORDER)
    cli.get_order.return_value = dict(_OK_ORDER)
    cli.get_fills.return_value = [{"count": 1, "price": 40}]
    om = order_manager.OrderManager(cli)
    om.session_submitted = {}
    om.open_orders = {}
    return om, cli


class MoneyPathReReadsTheKillSwitch(unittest.TestCase):
    """place_and_track must consult KILL_SWITCH on every single order."""

    def setUp(self):
        self._saved = (CFG.ALLOW_ORDER_SUBMISSION, CFG.KILL_SWITCH,
                       CFG.DAILY_RESEARCH_ORACLE_APPROVED)
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.KILL_SWITCH = False
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = True

    def tearDown(self):
        (CFG.ALLOW_ORDER_SUBMISSION, CFG.KILL_SWITCH,
         CFG.DAILY_RESEARCH_ORACLE_APPROVED) = self._saved

    def test_control_the_same_order_succeeds_with_the_switch_off(self):
        """Anti-vacuity: without this, every assertion below could pass
        because the order was refused for an unrelated reason."""
        om, cli = _order_manager()
        res = om.place_and_track("KXTEST-CTRL-T1", "yes", 1, 40)
        self.assertEqual(cli.create_order.call_count, 1)
        self.assertFalse(str(res.status).startswith("blocked:"), res.status)

    def test_kill_switch_blocks_before_create_order(self):
        om, cli = _order_manager()
        CFG.KILL_SWITCH = True
        res = om.place_and_track("KXTEST-CTRL-T1", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:kill_switch")
        self.assertEqual(res.state, "rejected")
        self.assertEqual(cli.create_order.call_count, 0,
                         "the broker was reached despite the kill switch")

    def test_kill_switch_is_re_read_per_order_not_cached(self):
        """Flipping it mid-session must take effect on the NEXT order."""
        om, cli = _order_manager()
        first = om.place_and_track("KXTEST-A-T1", "yes", 1, 40)
        self.assertFalse(str(first.status).startswith("blocked:"))
        CFG.KILL_SWITCH = True
        second = om.place_and_track("KXTEST-B-T1", "yes", 1, 40)
        self.assertEqual(second.status, "blocked:kill_switch")
        self.assertEqual(cli.create_order.call_count, 1,
                         "only the pre-flip order should have reached the broker")

    def test_gate_order_submission_guard_wins_over_kill_switch(self):
        """Required order: GLOBAL SUBMISSION GUARD, then KILL SWITCH."""
        om, cli = _order_manager()
        CFG.ALLOW_ORDER_SUBMISSION = False
        CFG.KILL_SWITCH = True
        res = om.place_and_track("KXTEST-CTRL-T1", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:submission_disabled")
        self.assertEqual(cli.create_order.call_count, 0)

    def test_gate_order_kill_switch_wins_over_daily_quarantine(self):
        """Required order: KILL SWITCH, then MARKET-TYPE / DAILY QUARANTINE."""
        om, cli = _order_manager()
        CFG.KILL_SWITCH = True
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = False      # quarantine also active
        res = om.place_and_track("KXBTCD-26SEP0517-T90749.99", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:kill_switch")
        self.assertEqual(cli.create_order.call_count, 0)

    def test_daily_quarantine_still_blocks_when_the_switch_is_off(self):
        """The ordering change must not have weakened B2."""
        om, cli = _order_manager()
        CFG.KILL_SWITCH = False
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = False
        res = om.place_and_track("KXBTCD-26SEP0517-T90749.99", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:daily_oracle_unapproved")
        self.assertEqual(cli.create_order.call_count, 0)


class ClientLayerBackstop(unittest.TestCase):
    """KalshiClient.create_order refuses regardless of who calls it.

    This is the layer that makes the broker-write inventory meaningful: a
    caller that skips OrderManager entirely still cannot reach the network.
    """

    def setUp(self):
        from kalshi_client import KalshiClient
        self._saved = (CFG.ALLOW_ORDER_SUBMISSION, CFG.KILL_SWITCH,
                       CFG.DAILY_RESEARCH_ORACLE_APPROVED)
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.KILL_SWITCH = False
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = True
        self.client = KalshiClient("demo")
        self.client.session = MagicMock()   # any network use would show here

    def tearDown(self):
        (CFG.ALLOW_ORDER_SUBMISSION, CFG.KILL_SWITCH,
         CFG.DAILY_RESEARCH_ORACLE_APPROVED) = self._saved

    def _assert_refused(self, needle, ticker="KXTEST-DIRECT-T1"):
        with self.assertRaises(KalshiAPIError) as cm:
            self.client.create_order(ticker, "yes", 1, 40)
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(self.client.session.request.call_count, 0,
                         "a network request was attempted despite the refusal")

    def test_refuses_when_submission_is_disabled(self):
        CFG.ALLOW_ORDER_SUBMISSION = False
        self._assert_refused("ALLOW_ORDER_SUBMISSION=false")

    def test_refuses_when_the_kill_switch_is_engaged(self):
        CFG.KILL_SWITCH = True
        self._assert_refused("KILL_SWITCH actif")

    def test_refuses_a_quarantined_daily_ticker(self):
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = False
        self._assert_refused("quotidien", ticker="KXBTCD-26SEP0517-T90749.99")

    def test_control_gates_open_reaches_the_request_layer(self):
        """Anti-vacuity: with every gate open the call proceeds past the
        guards. Without this, all three refusals above could be produced by a
        create_order that never works at all."""
        self.client.session.request.side_effect = RuntimeError("REACHED_NETWORK")
        with self.assertRaises(Exception) as cm:
            self.client.create_order("KXTEST-DIRECT-T1", "yes", 1, 40)
        self.assertNotIn("refuse au niveau client", str(cm.exception))

    def test_cancel_is_deliberately_not_blocked_by_the_kill_switch(self):
        """Cancelling REDUCES exposure. A breaker that blocked cancellation
        would trap an open order instead of protecting the account."""
        CFG.KILL_SWITCH = True
        self.client.session.request.side_effect = RuntimeError("REACHED_NETWORK")
        with self.assertRaises(Exception) as cm:
            self.client.cancel_order("ord_1")
        self.assertNotIn("refuse au niveau client", str(cm.exception))


class BrokerWriteInventoryStaysClosed(unittest.TestCase):
    """No new unprotected broker-write path may appear.

    The audit found five call sites of the two client write methods. This test
    fails when a new direct `create_order` call is introduced outside the
    sanctioned modules, so the inventory cannot silently regrow.
    """

    #: Modules permitted to call client.create_order directly, each with the
    #: reason it is safe. "Direct call" is not itself the hazard — bypassing
    #: the GATES is. Since the refusal now lives inside create_order, a direct
    #: caller is still gated; this list exists so that a NEW direct caller is
    #: a deliberate, reviewed decision rather than an accident.
    SANCTIONED = {
        "order_manager.py":              "owns the full gate ladder",
        "kalshi_client.py":              "defines the method and the backstop",
        "kalshi_demo_execution_check.py":
            "DEMO-only integration probe; refuses any LIVE context itself, "
            "and the client-layer backstop applies the submission gate, kill "
            "switch and daily quarantine to it like any other caller",
    }

    def test_the_backstop_that_makes_direct_calls_safe_exists(self):
        """The sanction above is only valid while create_order itself refuses.

        Without this, someone could delete the backstop and the sanctioned
        list would silently become a list of unprotected paths.
        """
        import inspect
        from kalshi_client import KalshiClient
        src = inspect.getsource(KalshiClient.create_order)
        body = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))
        for needle in ("CFG.ALLOW_ORDER_SUBMISSION", "CFG.KILL_SWITCH",
                       "daily_quarantine_blocks"):
            self.assertIn(needle, body,
                          f"create_order no longer consults {needle}; every "
                          f"sanctioned direct caller is now unprotected")

    def test_no_unsanctioned_direct_create_order_call(self):
        import pathlib
        offenders = []
        for path in sorted(pathlib.Path(_ROOT).glob("*.py")):
            if path.name in self.SANCTIONED:
                continue
            for i, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if ".create_order(" in stripped:
                    offenders.append(f"{path.name}:{i}")
        self.assertEqual(
            offenders, [],
            "direct broker-write path(s) outside the gate ladder: "
            f"{offenders}. Route through OrderManager.place_and_track, or add "
            f"the module to SANCTIONED with a written justification.")


class StrictSafetyConfigParsing(unittest.TestCase):
    """Workstream C: malformed safety configuration resolves the SAFE way.

    Run in scrubbed subprocesses because `config` freezes these as class
    attributes at import: mutating os.environ afterwards proves nothing.
    """

    def _read(self, assignments):
        probe = (
            "import json, config\n"
            "print(json.dumps({\n"
            "  'KILL_SWITCH': config.CFG.KILL_SWITCH,\n"
            "  'ALLOW_FRESH_STATE': config.CFG.ALLOW_FRESH_STATE,\n"
            "  'ALLOW_FALLBACK_CAPITAL': config.CFG.ALLOW_FALLBACK_CAPITAL,\n"
            "  'ALLOW_ORDER_SUBMISSION': config.CFG.ALLOW_ORDER_SUBMISSION,\n"
            "}))\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("KILL_SWITCH", "ALLOW_FRESH_STATE",
                            "ALLOW_FALLBACK_CAPITAL", "ALLOW_ORDER_SUBMISSION")}
        env.update({"PROBE_PROVIDERS_ON_START": "0",
                    "KALSHI_DEMO_KEY_ID": "test",
                    "KALSHI_DEMO_PRIVATE_KEY": "test"})
        env.update(assignments)
        out = subprocess.run([sys.executable, "-c", probe], cwd=_ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_allow_gates_read_garbage_as_closed(self):
        for bad in ("", "   ", "maybe", "flase", "2", "yes;drop"):
            seen = self._read({"ALLOW_ORDER_SUBMISSION": bad,
                               "ALLOW_FRESH_STATE": bad,
                               "ALLOW_FALLBACK_CAPITAL": bad})
            self.assertFalse(seen["ALLOW_ORDER_SUBMISSION"], bad)
            self.assertFalse(seen["ALLOW_FRESH_STATE"], bad)
            self.assertFalse(seen["ALLOW_FALLBACK_CAPITAL"], bad)

    def test_kill_switch_reads_garbage_as_ENGAGED(self):
        """Inverse polarity: an unreadable breaker must CUT, not pass.

        `_env_gate(default=False)` alone would have made a typo'd kill switch
        fail OPEN — the opposite of every other gate in this file.
        """
        for bad in ("", "   ", "maybe", "flase", "2"):
            seen = self._read({"KILL_SWITCH": bad})
            self.assertTrue(
                seen["KILL_SWITCH"],
                f"KILL_SWITCH={bad!r} was read as DISENGAGED — a mistyped "
                f"breaker would let orders through")

    def test_absent_kill_switch_is_not_engaged(self):
        """Absence is the normal running state; it cannot be required."""
        self.assertFalse(self._read({})["KILL_SWITCH"])

    def test_explicit_values_still_work_in_both_directions(self):
        self.assertTrue(self._read({"KILL_SWITCH": "true"})["KILL_SWITCH"])
        self.assertFalse(self._read({"KILL_SWITCH": "false"})["KILL_SWITCH"])
        self.assertTrue(
            self._read({"ALLOW_FRESH_STATE": "yes"})["ALLOW_FRESH_STATE"])
        self.assertFalse(
            self._read({"ALLOW_FRESH_STATE": "no"})["ALLOW_FRESH_STATE"])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
