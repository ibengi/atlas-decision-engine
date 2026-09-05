# -*- coding: utf-8 -*-
"""RC-3 MED-3: the suite cannot reach a real production account.

WHY THIS FILE EXISTS
    `tests/_netblock.py` makes the entrypoint subprocesses in
    `test_prod_access_mode.StartupMatrix` structurally incapable of network
    access. A block nobody tests is a block that quietly stops working, and
    its failure mode is silent: the tests keep passing, and the only symptom
    is a developer's laptop polling the real account.

    So this module tests the block itself, and — with the block in place, so
    nothing is contacted — demonstrates the reach that made the block
    necessary in the first place.

NOTHING HERE CONTACTS ANYTHING
    Every child process in this file runs with the block installed. The one
    test that shows the engine reaching for Kalshi observes a BLOCKED
    attempt: the hostname is recorded, the connection never happens.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401

try:
    from tests import _netblock
except ImportError:                     # pragma: no cover - depends on runner
    import _netblock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _ChildCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="atlas-netblock-test-")
        self.network_log = os.path.join(self.tmpdir, "attempts.log")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _child(self, code, env_extra=None, argv=None, timeout=180):
        env = _netblock.install(self.tmpdir, dict(os.environ),
                               self.network_log)
        env.update({"PROBE_PROVIDERS_ON_START": "0",
                    "KALSHI_DEMO_KEY_ID": "test",
                    "KALSHI_DEMO_PRIVATE_KEY": "test",
                    "DATA_DIR": os.environ.get("DATA_DIR", ".")})
        env.update(env_extra or {})
        cmd = ([sys.executable] + (argv if argv else ["-c", code]))
        out = subprocess.run(cmd, cwd=_ROOT, env=env, capture_output=True,
                             text=True, timeout=timeout)
        return out.returncode, out.stdout + out.stderr


class TheNetworkBlockActuallyBlocks(_ChildCase):
    """Anti-vacuity for every "no attempts were recorded" assertion."""

    def test_a_child_cannot_resolve_a_hostname(self):
        rc, log = self._child(
            "import socket; socket.getaddrinfo('api.elections.kalshi.com', 443)")
        self.assertNotEqual(rc, 0, "DNS resolution was not blocked")
        self.assertIn("NetworkBlockedInTests", log,
                      "the child failed for some reason OTHER than the block")

    def test_a_child_cannot_open_a_connection(self):
        rc, log = self._child(
            "import socket; socket.create_connection(('1.1.1.1', 443))")
        self.assertNotEqual(rc, 0, "outbound connection was not blocked")
        self.assertIn("NetworkBlockedInTests", log,
                      "the child failed for some reason OTHER than the block")

    def test_a_child_cannot_connect_a_raw_socket_to_an_ip(self):
        """A caller with a hard-coded IP bypasses DNS; the second layer holds."""
        rc, log = self._child(
            "import socket\n"
            "s = socket.socket()\n"
            "s.connect(('1.1.1.1', 443))")
        self.assertNotEqual(rc, 0, "raw socket connect was not blocked")
        self.assertIn("NetworkBlockedInTests", log,
                      "the child failed for some reason OTHER than the block")

    def test_blocked_attempts_are_recorded_with_the_hostname(self):
        self._child(
            "import socket\n"
            "try:\n"
            "    socket.getaddrinfo('api.elections.kalshi.com', 443)\n"
            "except Exception:\n"
            "    pass\n")
        attempts = _netblock.attempts(self.network_log)
        self.assertTrue(attempts, "a blocked attempt was not recorded")
        self.assertTrue(
            _netblock.broker_attempts(self.network_log),
            "a Kalshi hostname was not recognised as a broker attempt")

    def test_a_child_that_touches_nothing_records_nothing(self):
        """The log is not a constant non-empty file."""
        rc, _ = self._child("print('quiet')")
        self.assertEqual(rc, 0)
        self.assertEqual(_netblock.attempts(self.network_log), [])
        self.assertEqual(_netblock.broker_attempts(self.network_log), [])

    def test_requests_cannot_reach_out_either(self):
        """The library the client actually uses, not just raw sockets.

        Note what is asserted, and what is not. Behind an HTTP proxy the host
        `requests` resolves is the PROXY, not the broker — so the recorded
        attempt names `127.0.0.1`, and a broker-hostname check would find
        nothing while the request was on its way to Kalshi all the same.
        That is precisely why the guard in `StartupMatrix._start` asserts
        that NO attempt of any kind was made, rather than that no Kalshi
        hostname appeared.
        """
        rc, log = self._child(
            "import requests\n"
            "requests.get('https://api.elections.kalshi.com/trade-api/v2/"
            "exchange/status', timeout=5)\n")
        self.assertNotEqual(rc, 0, "requests reached the network")
        self.assertIn("NetworkBlockedInTests", log,
                      "requests failed for some reason OTHER than the block")
        self.assertTrue(
            _netblock.attempts(self.network_log),
            "the attempt was blocked but not recorded")


class CredentialsNeverReachTheChild(_ChildCase):
    """The other half: even unblocked, the child would have no credentials."""

    def test_production_credentials_are_stripped_from_the_child_env(self):
        env = _netblock.install(
            self.tmpdir,
            {"KALSHI_KEY_ID": "REAL-KEY-ID",
             "KALSHI_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----",
             "PROD_ACCESS_MODE": "CAPITAL",
             "LIVE_TRADING": "1",
             "PATH": os.environ.get("PATH", "")},
            self.network_log)
        for key in ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY", "PROD_ACCESS_MODE",
                    "LIVE_TRADING"):
            self.assertNotIn(key, env, f"{key} was passed to the child")
        self.assertIn("PATH", env, "unrelated variables must survive")

    def test_the_child_sees_no_production_key_even_when_the_parent_has_one(self):
        rc, log = self._child(
            "import os; print('KEY=' + repr(os.environ.get('KALSHI_KEY_ID')))",
            env_extra=None)
        self.assertIn("KEY=None", log,
                      "a production key id reached the test child")

    def test_CONTROL_the_stripping_is_observable(self):
        """The check above must be able to see a key when one IS present."""
        env = _netblock.install(self.tmpdir, {"KALSHI_KEY_ID": "X"},
                                self.network_log)
        self.assertNotIn("KALSHI_KEY_ID", env)
        env2 = _netblock.install(self.tmpdir, {"UNRELATED": "X"},
                                 self.network_log)
        self.assertEqual(env2.get("UNRELATED"), "X",
                         "install() strips indiscriminately")


class TheEntrypointStopsBeforeContactingTheBroker(_ChildCase):
    """The reach that MED-3 describes, observed while safely blocked."""

    def test_read_only_startup_makes_no_broker_attempt(self):
        """The three tests the reviewer named take this exact path."""
        rc, log = self._child(
            None, argv=["kalshi_alpha_bot.py", "--loop"],
            env_extra={"KALSHI_ENV_CONFIRM": "LIVE",
                       "PROD_ACCESS_MODE": "READ_ONLY"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(
            _netblock.broker_attempts(self.network_log), [],
            "read-only startup reached for the broker")
        self.assertEqual(_netblock.attempts(self.network_log), [],
                         "read-only startup attempted an outbound connection")

    def test_it_is_the_credential_gate_that_stops_it(self):
        """Not the network block. The boot must not DEPEND on the block."""
        rc, log = self._child(
            None, argv=["kalshi_alpha_bot.py", "--loop"],
            env_extra={"KALSHI_ENV_CONFIRM": "LIVE",
                       "PROD_ACCESS_MODE": "READ_ONLY"})
        self.assertIn(
            "identifiants invalides", log,
            "startup was stopped by something other than the credential "
            "gate; the deterministic stop is the credential gate, and the "
            "network block is only the backstop behind it")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
