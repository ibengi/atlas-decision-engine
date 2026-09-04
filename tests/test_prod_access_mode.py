# -*- coding: utf-8 -*-
"""PROD_ACCESS_MODE: READ_ONLY observation vs CAPITAL trading (RC-3).

The defect this closes is architectural, not a bug: `check_live_allowed()`
treated ACCESS to production data as equivalent to AUTHORIZATION to commit
production capital. Observing a market requires no proof of edge; committing
money does. Conflating them meant the only way to *look* at LIVE was to approve
the model — the worst possible reason to approve a model.

Two structurally distinct modes now exist. READ_ONLY is the highest-priority
broker-write prohibition in the system: no combination of trading flags can
lift it. CAPITAL authorizes nothing by itself; every pre-existing gate remains.
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
from kalshi_client import BrokerWriteForbidden, KalshiClient, MUTATING_HTTP_METHODS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every trading flag, as permissive as it goes. READ_ONLY must beat all of it.
_ALL_TRADING_FLAGS_ON = {
    "ALLOW_ORDER_SUBMISSION": True,
    "KILL_SWITCH": False,
    "DAILY_RESEARCH_ORACLE_APPROVED": True,
    "LIVE_BROKER_WRITES_AUTHORIZED": True,
}
_LIVE_ENV_FLAGS = {
    "LIVE_TRADING": "1", "LIVE_TRADING_CONFIRMED": "YES",
    "KALSHI_ENV_CONFIRM": "LIVE", "MODEL_APPROVED_FOR_LIVE": "YES",
    "NO_LIVE_PROMOTION": "0",
}



def _executable_source(obj):
    """Source with comments and the docstring removed.

    Assertions about ORDER must look at code, not prose: the read-only branch
    deliberately explains itself by NAMING place_and_track, and a raw
    str.index() finds that comment before the real call — the exact class of
    false failure this file must not produce.
    """
    import ast, inspect, textwrap
    src = textwrap.dedent(inspect.getsource(obj))
    tree = ast.parse(src)
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    return ast.unparse(fn)

def _prod_client():
    c = KalshiClient.__new__(KalshiClient)
    c.env = "prod"
    c.base_url = CFG.PROD_URL
    c.key_id = "unused"
    c._pk = object()
    c._sign_headers = lambda method, url: {"Content-Type": "application/json"}
    c._raw_logged = set()
    c.cache_enabled = False
    c._balance_cache = c._markets_cache = None
    c.session = MagicMock()
    c.session.request.return_value = MagicMock(
        status_code=200, text="{}", json=lambda: {})
    return c


def _mutating(client):
    return [c for c in client.session.request.call_args_list
            if str(c.args[0]).upper() in MUTATING_HTTP_METHODS]


class ModeParsingIsStrict(unittest.TestCase):
    """Nothing unrecognised may become CAPITAL."""

    def _mode(self, raw):
        saved = os.environ.get("PROD_ACCESS_MODE")
        try:
            os.environ.pop("PROD_ACCESS_MODE", None)
            if raw is not None:
                os.environ["PROD_ACCESS_MODE"] = raw
            return config.prod_access_mode(), config.prod_is_read_only()
        finally:
            os.environ.pop("PROD_ACCESS_MODE", None)
            if saved is not None:
                os.environ["PROD_ACCESS_MODE"] = saved

    def test_absent_blank_and_malformed_are_never_capital(self):
        for raw in (None, "", " ", "\t", "READONLY", "read only", "CAPITOL",
                    "CAPITAL;x", "capital ital", "1", "true", "yes", "None"):
            mode, ro = self._mode(raw)
            with self.subTest(raw=raw):
                self.assertNotEqual(mode, config.PROD_CAPITAL)
                self.assertTrue(ro, f"{raw!r} did not resolve to read-only")

    def test_only_the_two_named_modes_are_recognised(self):
        self.assertEqual(self._mode("READ_ONLY")[0], config.PROD_READ_ONLY)
        self.assertEqual(self._mode("CAPITAL")[0], config.PROD_CAPITAL)

    def test_recognised_values_tolerate_case_and_padding(self):
        for raw in ("capital", " Capital ", "CAPITAL\n"):
            self.assertEqual(self._mode(raw)[0], config.PROD_CAPITAL, raw)
        for raw in ("read_only", " Read_Only "):
            self.assertEqual(self._mode(raw)[0], config.PROD_READ_ONLY, raw)

    def test_read_only_is_expressed_as_NOT_capital(self):
        """The helper must ask 'was CAPITAL requested?', never 'is it READ_ONLY?'.

        Phrased the other way, an unrecognised value would fail to match
        READ_ONLY and fall through to writable.
        """
        import inspect
        src = inspect.getsource(config.prod_is_read_only)
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#") and '"""' not in ln)
        self.assertIn("!=", code)
        self.assertIn("PROD_CAPITAL", code)


class ReadOnlyDominatesEveryOtherFlag(unittest.TestCase):
    """READ_ONLY is the highest-priority write prohibition."""

    def setUp(self):
        self._cfg = {k: getattr(CFG, k) for k in _ALL_TRADING_FLAGS_ON}
        for k, v in _ALL_TRADING_FLAGS_ON.items():
            setattr(CFG, k, v)
        self._env = {k: os.environ.get(k) for k in
                     list(_LIVE_ENV_FLAGS) + ["PROD_ACCESS_MODE"]}
        os.environ.update(_LIVE_ENV_FLAGS)
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
        self.client = _prod_client()

    def tearDown(self):
        for k, v in self._cfg.items():
            setattr(CFG, k, v)
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_create_order_blocked_with_every_flag_permissive(self):
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("LECTURE SEULE", str(cm.exception))
        self.assertEqual(_mutating(self.client), [])

    def test_cancel_order_blocked_with_every_flag_permissive(self):
        with self.assertRaises(BrokerWriteForbidden):
            self.client.cancel_order("ord_1")
        self.assertEqual(_mutating(self.client), [])

    def test_every_mutating_verb_blocked_including_future_ones(self):
        for verb in sorted(MUTATING_HTTP_METHODS):
            with self.subTest(verb=verb):
                with self.assertRaises(BrokerWriteForbidden):
                    self.client._req(verb, "/portfolio/future/endpoint")
        self.assertEqual(_mutating(self.client), [])

    def test_read_only_beats_live_broker_writes_authorized(self):
        """Priority order: READ_ONLY is checked BEFORE the write authorization."""
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = True
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("LECTURE SEULE", str(cm.exception))
        self.assertNotIn("LIVE_BROKER_WRITES_AUTHORIZED n'est pas",
                         str(cm.exception))

    def test_malformed_mode_also_blocks(self):
        for bad in ("", "  ", "CAPITOL", "READONLY", "capital;--"):
            os.environ["PROD_ACCESS_MODE"] = bad
            with self.subTest(mode=bad):
                with self.assertRaises(BrokerWriteForbidden):
                    self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertEqual(_mutating(self.client), [])

    def test_control_capital_mode_passes_this_guard(self):
        """Anti-vacuity: in CAPITAL the read-only guard must NOT be what blocks.

        Without this, every assertion above could pass on a client that
        refuses writes unconditionally.
        """
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        self.client.session.request.side_effect = RuntimeError("REACHED")
        with self.assertRaises(Exception) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertNotIn("LECTURE SEULE", str(cm.exception))


class ShadowDoesNotInvokeTheWriteLayer(unittest.TestCase):
    """§7: WOULD_SUBMIT is computed, not attempted-then-refused.

    A shadow defined as "call create_order and let the guard refuse" leaves a
    real attempt one bug away from the network, and pollutes rejection counters
    with refusals that are not decisions.
    """

    def test_engine_returns_before_place_and_track_in_read_only(self):
        import execution_engine
        src = _executable_source(execution_engine.ExecutionEngine._execute_decision)
        ro = src.index("prod_is_read_only()")
        pt = src.index("place_and_track")
        self.assertLess(ro, pt,
                        "the read-only branch must come BEFORE the call into "
                        "the order manager")

    def test_would_submit_branch_returns_zero_and_records_a_reason(self):
        import execution_engine
        src = _executable_source(execution_engine.ExecutionEngine._execute_decision)
        branch = src[src.index("prod_is_read_only()"):]
        head = branch[:branch.index("return 0") + len("return 0")]
        self.assertIn("would_submit", head)
        self.assertIn("prod_read_only", head)
        self.assertNotIn("place_and_track", head,
                         "the read-only branch reaches the write layer")


class ReconciliationIsReadOnly(unittest.TestCase):
    """§8: reconciliation observes and compares; it does not repair."""

    def test_recovery_does_not_cancel_under_read_only(self):
        import order_manager
        src = _executable_source(order_manager.OrderManager.reconcile_startup)
        guard = src.index("prod_is_read_only()")
        cancel = src.index("cancel_order")
        self.assertLess(guard, cancel,
                        "recovery reaches cancel_order before consulting the "
                        "read-only mode")
        seg = src[guard:cancel]
        self.assertIn("continue", seg,
                      "the read-only branch must skip the repair, not log and "
                      "fall through into it")

    def test_ttl_cancel_is_also_guarded(self):
        """The TTL cancel lives in place_and_track, and is guarded too."""
        import order_manager
        src = _executable_source(order_manager.OrderManager.place_and_track)
        self.assertIn("prod_is_read_only()", src)
        self.assertIn("ORDER_CANCEL_SKIPPED", src)


class StartupMatrix(unittest.TestCase):
    """§10: the mode matrix, exercised through the real entrypoint."""

    def _start(self, argv, env_extra):
        """Run the entrypoint far enough to observe the startup decision.

        --scan-only would skip the very checks under test, so the process is
        driven with a real (mocked-network) start and killed by the missing
        production credentials AFTER the mode logic has run. The startup log
        is what is asserted on.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("PROD_ACCESS_MODE", "LIVE_TRADING",
                            "LIVE_TRADING_CONFIRMED", "KALSHI_ENV_CONFIRM",
                            "MODEL_APPROVED_FOR_LIVE", "NO_LIVE_PROMOTION",
                            "DEMO_TRADING")}
        env.update({"PROBE_PROVIDERS_ON_START": "0",
                    "KALSHI_DEMO_KEY_ID": "test",
                    "KALSHI_DEMO_PRIVATE_KEY": "test",
                    "DATA_DIR": os.environ.get("DATA_DIR", ".")})
        env.update(env_extra)
        out = subprocess.run(
            [sys.executable, "kalshi_alpha_bot.py"] + argv,
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=180)
        return out.returncode, (out.stdout + out.stderr)

    def test_prod_without_a_mode_refuses_startup(self):
        rc, log = self._start(["--loop"], {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertNotEqual(rc, 0)
        self.assertIn("PROD_ACCESS_MODE", log)

    def test_prod_with_a_malformed_mode_refuses_startup(self):
        for bad in ("CAPITOL", "READONLY", "", "yes"):
            rc, log = self._start(["--loop"],
                                  {"KALSHI_ENV_CONFIRM": "LIVE",
                                   "PROD_ACCESS_MODE": bad})
            with self.subTest(mode=bad):
                self.assertNotEqual(rc, 0)
                self.assertIn("non reconnue", log)

    def test_read_only_starts_with_model_approved_false(self):
        """THE point of the split: observation must not require approval."""
        rc, log = self._start(["--loop"],
                              {"KALSHI_ENV_CONFIRM": "LIVE",
                               "PROD_ACCESS_MODE": "READ_ONLY"})
        self.assertIn("PRODUCTION EN LECTURE SEULE", log)
        self.assertNotIn("GATEKEEPER: live REFUSE", log)

    def test_capital_is_refused_by_the_gatekeeper_with_model_approved_false(self):
        rc, log = self._start(["--loop"],
                              {"KALSHI_ENV_CONFIRM": "LIVE",
                               "PROD_ACCESS_MODE": "CAPITAL",
                               "LIVE_TRADING": "1",
                               "LIVE_TRADING_CONFIRMED": "YES"})
        self.assertNotEqual(rc, 0)
        self.assertIn("GATEKEEPER: live REFUSE", log)

    def test_capital_still_requires_the_live_trading_confirmations(self):
        rc, log = self._start(["--loop"],
                              {"KALSHI_ENV_CONFIRM": "LIVE",
                               "PROD_ACCESS_MODE": "CAPITAL"})
        self.assertNotEqual(rc, 0)
        self.assertIn("LIVE_TRADING_CONFIRMED", log)

    def test_read_only_still_requires_explicit_production_intent(self):
        rc, log = self._start(["--loop"], {"PROD_ACCESS_MODE": "READ_ONLY"})
        self.assertNotEqual(rc, 0)
        self.assertIn("KALSHI_ENV_CONFIRM", log)

    def test_cli_flag_implies_read_only_without_any_trading_flag(self):
        rc, log = self._start(["--loop", "--live-read-only"],
                              {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertIn("PRODUCTION EN LECTURE SEULE", log)
        self.assertNotIn("REAL MONEY ENABLED", log)

    def test_shadow_does_NOT_select_a_production_mode(self):
        """M5: --shadow must never imply CAPITAL.

        --shadow means "decide but do not send". Letting it choose an access
        mode would make a safety flag silently grant production authority.
        """
        rc, log = self._start(["--loop", "--shadow"],
                              {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertNotEqual(rc, 0, "--shadow supplied a PROD_ACCESS_MODE")
        self.assertIn("PROD_ACCESS_MODE", log)
        self.assertNotIn("REAL MONEY ENABLED", log)

    def test_shadow_does_not_upgrade_read_only_to_capital(self):
        rc, log = self._start(["--loop", "--shadow"],
                              {"KALSHI_ENV_CONFIRM": "LIVE",
                               "PROD_ACCESS_MODE": "READ_ONLY"})
        self.assertIn("PRODUCTION EN LECTURE SEULE", log)
        self.assertNotIn("REAL MONEY ENABLED", log)
        self.assertNotIn("GATEKEEPER: live REFUSE", log)

    def test_conflicting_cli_modes_refuse(self):
        rc, log = self._start(["--live-read-only", "--live-capital"], {})
        self.assertNotEqual(rc, 0)
        self.assertIn("exclusifs", log)

    def test_read_only_with_demo_refuses(self):
        rc, log = self._start(["--live-read-only", "--demo"], {})
        self.assertNotEqual(rc, 0)
        self.assertIn("incompatible", log)

    def test_demo_startup_never_reaches_the_mode_logic(self):
        """DEMO is untouched — asserted structurally, not by running a scan.

        Driving a full DEMO start here would block on network the sandbox
        forbids and prove nothing about the mode logic. What matters is that
        every mode check sits inside the `env == "prod"` branch.
        """
        import ast
        import textwrap
        src = open(os.path.join(_ROOT, "kalshi_alpha_bot.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        prod_guarded, total = 0, 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            body = ast.unparse(node)
            if "prod_access_mode()" not in body:
                continue
            total += 1
            test = ast.unparse(node.test)
            if "env == 'prod'" in test or 'env == "prod"' in test:
                prod_guarded += 1
        self.assertEqual(total, 1,
                         "the startup mode logic is no longer a single block")
        self.assertEqual(prod_guarded, 1,
                         "the startup mode logic is not guarded by env == "
                         "'prod'; DEMO would be affected")


class CapitalModeAuthorizesNothingByItself(unittest.TestCase):
    """§3: mode=CAPITAL is a permission to be asked, not a permission granted."""

    def setUp(self):
        self._env = os.environ.get("PROD_ACCESS_MODE")
        os.environ["PROD_ACCESS_MODE"] = "CAPITAL"
        self._cfg = {k: getattr(CFG, k) for k in
                     ("ALLOW_ORDER_SUBMISSION", "KILL_SWITCH",
                      "LIVE_BROKER_WRITES_AUTHORIZED",
                      "DAILY_RESEARCH_ORACLE_APPROVED")}
        self.client = _prod_client()

    def tearDown(self):
        os.environ.pop("PROD_ACCESS_MODE", None)
        if self._env is not None:
            os.environ["PROD_ACCESS_MODE"] = self._env
        for k, v in self._cfg.items():
            setattr(CFG, k, v)

    def test_capital_still_needs_the_write_authorization(self):
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = False
        CFG.ALLOW_ORDER_SUBMISSION = True
        with self.assertRaises(BrokerWriteForbidden) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("LIVE_BROKER_WRITES_AUTHORIZED", str(cm.exception))
        self.assertEqual(_mutating(self.client), [])

    def test_capital_still_needs_allow_order_submission(self):
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = True
        CFG.ALLOW_ORDER_SUBMISSION = False
        with self.assertRaises(Exception) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("ALLOW_ORDER_SUBMISSION", str(cm.exception))
        self.assertEqual(_mutating(self.client), [])

    def test_capital_still_honours_the_daily_quarantine(self):
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.KILL_SWITCH = False
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = False
        with self.assertRaises(Exception) as cm:
            self.client.create_order("KXBTCD-26SEP0517-T90749.99", "yes", 1, 40)
        self.assertIn("quotidien", str(cm.exception))
        self.assertEqual(_mutating(self.client), [])

    def test_capital_still_honours_the_kill_switch(self):
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.KILL_SWITCH = True
        with self.assertRaises(Exception) as cm:
            self.client.create_order("KXBTC15M-X", "yes", 1, 40)
        self.assertIn("KILL_SWITCH", str(cm.exception))
        self.assertEqual(_mutating(self.client), [])


class ReadsRemainAvailableInReadOnly(unittest.TestCase):
    """§6: full observation must still work."""

    def setUp(self):
        self._env = os.environ.get("PROD_ACCESS_MODE")
        os.environ["PROD_ACCESS_MODE"] = "READ_ONLY"
        self.client = _prod_client()

    def tearDown(self):
        os.environ.pop("PROD_ACCESS_MODE", None)
        if self._env is not None:
            os.environ["PROD_ACCESS_MODE"] = self._env

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
                    self.fail(f"{name} was blocked by the read-only guard")
                except Exception:
                    pass
                verbs = [str(c.args[0]).upper()
                         for c in self.client.session.request.call_args_list]
                self.assertTrue(verbs, f"{name} issued no request")
                self.assertTrue(all(v == "GET" for v in verbs), verbs)


class NoEntrypointCanEscapeTheGuard(unittest.TestCase):
    """§12 — adversarial: try to prove READ_ONLY *can* mutate.

    The startup mode validation in kalshi_alpha_bot is NOT the thing that
    makes the guarantee hold, because any script may construct a production
    client directly and never pass through it. `kalshi_edge_measure.py` does
    exactly that. The guarantee holds only because the prohibition lives at
    the CLIENT boundary. These tests pin both halves of that reasoning: the
    set of modules building a prod client stays a deliberate, known list, and
    none of them reaches a write method.
    """

    #: Modules that construct a PRODUCTION KalshiClient, with why it is safe.
    PROD_CLIENT_BUILDERS = {
        "kalshi_alpha_bot.py":
            "the main entrypoint; validates PROD_ACCESS_MODE before building",
        "kalshi_edge_measure.py":
            "research CLI; passes only get_market (a GET) into resolve_pending",
    }

    def _modules_building_prod_clients(self):
        import pathlib
        import re
        found = {}
        for path in sorted(pathlib.Path(_ROOT).glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if re.search(r"KalshiClient\(\s*(?!['\"]demo)", line):
                    found.setdefault(path.name, []).append(i)
        return found

    def test_the_set_of_prod_client_builders_is_known(self):
        found = self._modules_building_prod_clients()
        unexpected = set(found) - set(self.PROD_CLIENT_BUILDERS)
        self.assertEqual(
            unexpected, set(),
            f"new module(s) construct a production Kalshi client without "
            f"review: {sorted(unexpected)}. Each is outside the entrypoint's "
            f"mode validation and relies entirely on the client-boundary "
            f"guard.")

    def test_no_secondary_entrypoint_calls_a_write_method(self):
        import pathlib
        for name in self.PROD_CLIENT_BUILDERS:
            if name == "kalshi_alpha_bot.py":
                continue        # the engine legitimately owns the write path
            text = pathlib.Path(_ROOT, name).read_text(encoding="utf-8")
            code = "\n".join(ln for ln in text.splitlines()
                              if not ln.strip().startswith("#"))
            for writer in (".create_order(", ".cancel_order(",
                           "place_and_track", "._req("):
                self.assertNotIn(
                    writer, code,
                    f"{name} reaches a broker write path; it builds a "
                    f"production client outside the startup mode check")

    def test_the_prohibition_lives_at_the_client_not_the_entrypoint(self):
        """If this ever moves up to the entrypoint, the guarantee breaks."""
        import inspect
        from kalshi_client import KalshiClient as KC
        self.assertIn("prod_is_read_only",
                      _executable_source(KC._assert_broker_write_allowed),
                      "the read-only prohibition is no longer enforced at the "
                      "client boundary; a script constructing its own prod "
                      "client would bypass it entirely")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
