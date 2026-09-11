# -*- coding: utf-8 -*-
"""Every entrypoint runs the same READ_ONLY code with the same arguments.

The repository's READ_ONLY semantics (observation through `equity_drawdown`,
the `would_block_capital` evidence, the startup dashboard snapshot) live in
the application and are selected by `--live-read-only` alone. This file pins
that no entrypoint -- Dockerfile CMD, Procfile, the alias launcher a platform
start command may point at, or a launch by hand -- carries semantics the
others lack, and that no runtime monkey-patching exists anywhere.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot                                    # noqa: E402
import read_only_dashboard_bootstrap as launcher                  # noqa: E402
import test_shadow_write_layer_isolation as shadow_iso            # noqa: E402
from config import _p                                             # noqa: E402
from execution_engine import ExecutionEngine                      # noqa: E402
from persistence import JsonStore                                 # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANONICAL = ["python", "kalshi_alpha_bot.py", "--loop", "--live-read-only"]


def _dockerfile_cmd():
    text = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    cmds = re.findall(r"^CMD\s+(\[.*\])\s*$", text, flags=re.M)
    return [json.loads(c) for c in cmds]


def _procfile_worker():
    text = open(os.path.join(ROOT, "Procfile"), encoding="utf-8").read()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    workers = [l.split(":", 1)[1].split() for l in lines
               if l.startswith("worker:")]
    return workers


def _launcher_argv():
    return ["python", "kalshi_alpha_bot.py", *launcher.CANONICAL_ARGV]


class TheThreeEntrypointsAreOneCommand(unittest.TestCase):

    def test_dockerfile_has_exactly_one_cmd_and_it_is_canonical(self):
        self.assertEqual(_dockerfile_cmd(), [CANONICAL])

    def test_procfile_worker_is_canonical(self):
        self.assertEqual(_procfile_worker(), [CANONICAL])

    def test_the_alias_launcher_is_canonical(self):
        self.assertEqual(_launcher_argv(), CANONICAL)

    def test_the_alias_launcher_only_sets_argv_and_calls_main(self):
        seen = {}
        saved = list(sys.argv)
        try:
            with patch.object(bot, "main",
                              lambda: seen.setdefault("argv", list(sys.argv))):
                launcher.main()
        finally:
            sys.argv = saved
        self.assertEqual(seen["argv"][1:], CANONICAL[2:])

    def test_a_platform_start_command_needs_no_semantics_absent_from_the_repo(self):
        """Whatever a platform runs -- the Dockerfile CMD, the Procfile
        worker, or the alias file -- resolves to the same argv, so a start
        command override can neither add nor remove behaviour."""
        variants = {"dockerfile": _dockerfile_cmd()[0],
                    "procfile": _procfile_worker()[0],
                    "launcher": _launcher_argv()}
        self.assertEqual(len({tuple(v) for v in variants.values()}), 1,
                         variants)


class NothingIsMonkeyPatched(unittest.TestCase):

    def test_the_engine_owns_its_gate_finalization_and_evidence(self):
        for name in ("_post_balance_gates", "_evaluate_global_guards",
                     "_finish_cycle", "_finalize_cycle",
                     "_record_cycle_evidence", "_is_prod_read_only"):
            with self.subTest(method=name):
                fn = ExecutionEngine.__dict__[name]
                self.assertEqual(fn.__module__, "execution_engine")

    def test_the_bot_owns_its_banner_and_startup_snapshot(self):
        self.assertEqual(bot.banner.__module__, "kalshi_alpha_bot")
        self.assertEqual(bot.publish_read_only_startup_snapshot.__module__,
                         "kalshi_alpha_bot")

    def test_the_launcher_module_defines_no_patching(self):
        self.assertFalse(hasattr(launcher, "install"))
        self.assertFalse(hasattr(launcher, "uninstall"))
        src = inspect.getsource(launcher)
        self.assertNotRegex(src, r"ExecutionEngine\.\w+\s*=")
        self.assertNotRegex(src, r"bot\.\w+\s*=")
        self.assertNotIn("import execution_engine", src)
        self.assertNotIn("from execution_engine", src)

    def test_main_publishes_the_startup_snapshot_in_read_only_only(self):
        src = inspect.getsource(bot.main)
        self.assertIn('if env == "prod" and config.prod_is_read_only() \\\n'
                      '            and not (args.scan_only or args.rank_only):\n'
                      '        publish_read_only_startup_snapshot(client, '
                      'args.capital)', src)


class TheStartupSnapshotIsRepoOwned(shadow_iso._IsolatedState,
                                    unittest.TestCase):

    def test_snapshot_is_written_from_the_application(self):
        client = type("C", (), {"env": "prod",
                                "get_balance": lambda self: 9.84})()
        bot.publish_read_only_startup_snapshot(client, 500.0)
        state = JsonStore.load(_p("dashboard_state.json"), {})
        self.assertIs(state.get("read_only"), True)
        self.assertIs(state.get("startup_snapshot"), True)
        self.assertEqual(state.get("balance"), 9.84)
        self.assertEqual(state.get("capital"), 9.84)
        self.assertEqual(state.get("configured_capital"), 500.0)
        self.assertIsNone(state.get("capital_blocking_guard"))
        self.assertEqual(state.get("cycle"), 0)

    def test_a_failing_snapshot_never_raises(self):
        client = type("C", (), {"env": "prod", "get_balance":
                                lambda self: (_ for _ in ()).throw(
                                    RuntimeError("no broker"))})()
        bot.publish_read_only_startup_snapshot(client, 500.0)   # no raise
        self.assertFalse(os.path.exists(_p("dashboard_state.json")))


class BothLaunchesReachTheSameFailClosedCheck(unittest.TestCase):
    """Process-level: the Dockerfile CMD, the Procfile worker and the alias
    launcher are each executed for real, with production intent NOT
    confirmed. All three must stop at the same repository check with the
    same exit status, before any client exists and with zero writes to
    DATA_DIR. This is the same `main` being reached by every entrypoint."""

    STRIP = ("KALSHI_ENV_CONFIRM", "PROD_ACCESS_MODE", "DEMO_TRADING",
             "LIVE_TRADING", "LIVE_TRADING_CONFIRMED",
             "LIVE_BROKER_WRITES_AUTHORIZED", "ALLOW_ORDER_SUBMISSION")

    def _launch(self, argv):
        tmp = tempfile.mkdtemp(prefix="atlas-entry-")
        env = {k: v for k, v in os.environ.items() if k not in self.STRIP}
        env["DATA_DIR"] = tmp
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cmd = [sys.executable, *argv[1:]] if argv[0] == "python" else argv
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr, os.listdir(tmp)

    def test_all_entrypoints_stop_at_the_same_repository_check(self):
        variants = {"dockerfile": _dockerfile_cmd()[0],
                    "procfile": _procfile_worker()[0],
                    "launcher": ["python", "read_only_dashboard_bootstrap.py"]}
        results = {}
        for name, argv in variants.items():
            with self.subTest(entrypoint=name):
                code, out, written = self._launch(argv)
                self.assertEqual(code, 1, out[-2000:])
                self.assertIn("KALSHI_ENV_CONFIRM=LIVE", out)
                self.assertNotIn("CAPITAL ENABLED", out)
                self.assertEqual(written, [], "wrote before the check")
                results[name] = (code, "KALSHI_ENV_CONFIRM=LIVE" in out)
        self.assertEqual(len(set(results.values())), 1, results)


if __name__ == "__main__":
    unittest.main()
