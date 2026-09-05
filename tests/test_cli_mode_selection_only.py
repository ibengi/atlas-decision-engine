# -*- coding: utf-8 -*-
"""RC-3 HIGH-2: a mode flag SELECTS a mode. It authorizes nothing.

WHY THIS FILE EXISTS
    `--live-capital` names the mode in which real money is possible, so the
    tempting shortcut is to have it "just set the rest too". Nothing in the
    suite forbade that. The independent reviewer showed the gap by mutation:
    a `--live-capital` that also exported `LIVE_TRADING=1` passed every test,
    which means the separation between SELECTING capital mode and BEING
    AUTHORIZED to trade rested on nobody having written that line yet.

    The distinction is the whole point of RC-3. Choosing which account to
    look at is not the same act as being allowed to spend from it, and the
    four variables below are the ones that say "allowed":

        LIVE_TRADING                  the trading kill flag
        LIVE_TRADING_CONFIRMED        the operator's second confirmation
        LIVE_BROKER_WRITES_AUTHORIZED the client-boundary write authorization
        MODEL_APPROVED                the scientific gate

    None of them may ever be set as a SIDE EFFECT of choosing a mode.

HOW IT IS PROVED
    The real `main()` is executed with real argv, in a scrubbed environment
    and a temporary DATA_DIR. It runs far enough to apply the mode flags and
    then dies on a confirmation it was never given — which is itself the
    point. The environment is then inspected directly. No mocking of the
    thing under test: the assertions read what the entrypoint actually did.

NO NETWORK, NO CREDENTIALS
    Every run stops at a missing confirmation, before any client is built.
    Production credential variables are scrubbed on the way in and every
    variable this module touches is restored on the way out.
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

#: The variables that GRANT something. A mode flag may never set one.
AUTHORIZATION_VARS = (
    "LIVE_TRADING",
    "LIVE_TRADING_CONFIRMED",
    "LIVE_BROKER_WRITES_AUTHORIZED",
    "MODEL_APPROVED",
    "MODEL_APPROVED_FOR_LIVE",
    "ALLOW_ORDER_SUBMISSION",
    "DAILY_RESEARCH_ORACLE_APPROVED",
)

#: Everything the entrypoint reads that could change the outcome.
_SCRUBBED = AUTHORIZATION_VARS + (
    "PROD_ACCESS_MODE", "KALSHI_ENV_CONFIRM", "DEMO_TRADING",
    "NO_LIVE_PROMOTION", "RESTORE_STATE_TGZ_B64", "RESTORE_STATE_SHA256",
    "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY",
)


class ModeFlagsSelectAModeAndNothingElse(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _SCRUBBED}
        for key in _SCRUBBED:
            os.environ.pop(key, None)
        self._shadow = CFG.SHADOW_MODE
        self._tmp = tempfile.mkdtemp(prefix="atlas-cli-")
        self._data_dir = patch.object(CFG, "DATA_DIR", self._tmp)
        self._data_dir.start()

    def tearDown(self):
        self._data_dir.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        CFG.SHADOW_MODE = self._shadow
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run_entrypoint(self, argv, env_extra=None):
        """Run the REAL `main()` far enough to apply the mode flags.

        Returns the exit code. `main()` is expected to exit non-zero: every
        case here deliberately withholds a confirmation, which is what makes
        the environment inspection meaningful — the flags were applied and
        the run was still refused.
        """
        os.environ.update(env_extra or {})
        import kalshi_alpha_bot
        with patch.object(sys, "argv", ["kalshi_alpha_bot.py"] + argv):
            try:
                kalshi_alpha_bot.main()
            except SystemExit as exc:
                return exc.code
        return 0                                    # pragma: no cover

    def _assert_nothing_armed(self, flag):
        for var in AUTHORIZATION_VARS:
            self.assertIsNone(
                os.environ.get(var),
                f"{flag} set {var}={os.environ.get(var)!r}. Selecting an "
                f"access mode must never grant an authorization: that is the "
                f"separation RC-3 exists to create.")

    # -- the two flags ---------------------------------------------------

    def test_live_read_only_selects_read_only_and_arms_nothing(self):
        """CLI_MODE_SELECTION_ONLY, read-only side."""
        code = self._run_entrypoint(["--live-read-only"])
        self.assertEqual(os.environ.get("PROD_ACCESS_MODE"),
                         config.PROD_READ_ONLY)
        self._assert_nothing_armed("--live-read-only")
        self.assertNotEqual(code, 0,
                            "the run must still be refused without "
                            "KALSHI_ENV_CONFIRM=LIVE")

    def test_live_capital_selects_capital_and_arms_nothing(self):
        """CLI_AUTO_AUTHORIZATION=NO. The mutation the reviewer found."""
        code = self._run_entrypoint(["--live-capital"])
        self.assertEqual(os.environ.get("PROD_ACCESS_MODE"),
                         config.PROD_CAPITAL)
        self._assert_nothing_armed("--live-capital")
        self.assertNotEqual(code, 0)

    def test_live_capital_with_env_confirm_still_needs_the_trading_flags(self):
        """CAPITAL mode does not confirm itself.

        With the production INTENTION confirmed, the run must still be
        refused for want of `LIVE_TRADING_CONFIRMED` / `LIVE_TRADING`. If
        `--live-capital` supplied either, this would start.
        """
        code = self._run_entrypoint(["--live-capital"],
                                    {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertEqual(os.environ.get("PROD_ACCESS_MODE"),
                         config.PROD_CAPITAL)
        self._assert_nothing_armed("--live-capital")
        self.assertNotEqual(
            code, 0,
            "capital mode started without the trading confirmations")

    def test_neither_flag_leaves_the_mode_unset(self):
        """A production run with no mode flag must not default to anything."""
        code = self._run_entrypoint(["--loop"],
                                    {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertIsNone(os.environ.get("PROD_ACCESS_MODE"),
                          "an unset mode must stay unset, not be filled in")
        self._assert_nothing_armed("no flag")
        self.assertNotEqual(code, 0)

    def test_the_two_flags_are_mutually_exclusive(self):
        """And the refusal happens BEFORE either mode is applied.

        Asserting only a non-zero exit would pass for the wrong reason: any
        production run without `KALSHI_ENV_CONFIRM` exits non-zero anyway.
        The load-bearing assertion is that no mode was selected at all — the
        conflict is resolved by refusing, never by letting one flag win.
        """
        code = self._run_entrypoint(["--live-read-only", "--live-capital"],
                                    {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertNotEqual(code, 0)
        self.assertIsNone(
            os.environ.get("PROD_ACCESS_MODE"),
            "conflicting flags silently selected a mode instead of refusing")
        self._assert_nothing_armed("both flags")

    def test_a_mode_flag_is_refused_alongside_demo(self):
        """Same shape: refused before the mode is applied."""
        code = self._run_entrypoint(["--live-capital", "--demo"],
                                    {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertNotEqual(code, 0)
        self.assertIsNone(os.environ.get("PROD_ACCESS_MODE"),
                          "--demo with a production mode flag selected a mode")
        self._assert_nothing_armed("--live-capital --demo")

    def test_shadow_does_not_select_an_access_mode(self):
        """`--shadow` is a behaviour, not an account choice."""
        code = self._run_entrypoint(["--shadow"],
                                    {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertIsNone(os.environ.get("PROD_ACCESS_MODE"))
        self._assert_nothing_armed("--shadow")
        self.assertNotEqual(code, 0)

    # -- RC-3 MED-2: contradictory operator state -----------------------

    def _refusal_log(self, argv, env_extra):
        """Run the entrypoint and return (exit code, joined ERROR+ log).

        Asserting only a non-zero exit is not enough here: a production start
        without credentials also exits non-zero, so a removed check would be
        indistinguishable from an enforced one. The log says WHICH refusal
        happened, and that is the thing under test.
        """
        with self.assertLogs("BOT", level="ERROR") as logs:
            code = self._run_entrypoint(argv, env_extra)
        return code, " ".join(logs.output)

    def test_read_only_refuses_to_start_with_an_armed_write_authorization(self):
        """READ_ONLY_WITH_ARMED_WRITE_AUTH: refuse, do not reconcile silently.

        `prod_is_read_only()` already dominates every write today, so this is
        not about a mutation escaping now. It is about the state a LATER
        start inherits: a `CAPITAL` run begun from this same environment
        would find write authorization already granted, by nobody's decision.
        """
        code, log = self._refusal_log(
            ["--live-read-only"],
            {"KALSHI_ENV_CONFIRM": "LIVE",
             "LIVE_BROKER_WRITES_AUTHORIZED": "true"})
        self.assertNotEqual(
            code, 0,
            "read-only started while write authorization was armed")
        self.assertIn(
            "LIVE_BROKER_WRITES_AUTHORIZED", log,
            "the run was refused, but not for the armed authorization — a "
            "later gate refused it and this test would pass with the check "
            "deleted")
        self.assertNotIn(
            "identifiants invalides", log,
            "startup ran PAST the mode check and was stopped by the "
            "credential gate instead")

    def test_the_refusal_does_not_rewrite_the_operator_s_variable(self):
        """Silently clearing it would hide the misconfiguration."""
        self._run_entrypoint(
            ["--live-read-only"],
            {"KALSHI_ENV_CONFIRM": "LIVE",
             "LIVE_BROKER_WRITES_AUTHORIZED": "true"})
        self.assertEqual(
            os.environ.get("LIVE_BROKER_WRITES_AUTHORIZED"), "true",
            "the entrypoint overwrote an operator-set variable instead of "
            "refusing and reporting it")

    def test_every_truthy_spelling_of_the_authorization_is_caught(self):
        for value in ("true", "TRUE", "1", "yes", "on", "  true  "):
            with self.subTest(value=value):
                for key in _SCRUBBED:
                    os.environ.pop(key, None)
                code, log = self._refusal_log(
                    ["--live-read-only"],
                    {"KALSHI_ENV_CONFIRM": "LIVE",
                     "LIVE_BROKER_WRITES_AUTHORIZED": value})
                self.assertNotEqual(code, 0, f"{value!r} was not treated as "
                                             f"an armed authorization")
                self.assertIn("LIVE_BROKER_WRITES_AUTHORIZED", log,
                              f"{value!r} did not trigger the armed-flag "
                              f"refusal specifically")
                self.assertNotIn("identifiants invalides", log)

    def test_CONTROL_read_only_starts_past_this_check_when_nothing_is_armed(self):
        """Anti-vacuity: the refusal must be about the armed flag.

        With the same argv and no authorization present, startup must get
        FURTHER — here, all the way to the production credential check, whose
        distinct message proves the mode block was passed rather than that
        everything is refused alike.
        """
        with self.assertLogs("BOT", level="CRITICAL") as logs:
            code = self._run_entrypoint(["--live-read-only"],
                                        {"KALSHI_ENV_CONFIRM": "LIVE"})
        self.assertNotEqual(code, 0)
        joined = " ".join(logs.output)
        self.assertIn("identifiants invalides", joined,
                      "read-only startup did not reach the credential check, "
                      "so the armed-flag refusal above is not distinguishable "
                      "from a blanket refusal")

    def test_CONTROL_a_false_authorization_is_not_treated_as_armed(self):
        for value in ("false", "0", "no", "off", ""):
            with self.subTest(value=value):
                for key in _SCRUBBED:
                    os.environ.pop(key, None)
                with self.assertLogs("BOT", level="CRITICAL") as logs:
                    self._run_entrypoint(
                        ["--live-read-only"],
                        {"KALSHI_ENV_CONFIRM": "LIVE",
                         "LIVE_BROKER_WRITES_AUTHORIZED": value})
                self.assertIn("identifiants invalides", " ".join(logs.output),
                              f"{value!r} was wrongly treated as armed")

    # -- anti-vacuity ----------------------------------------------------

    def test_CONTROL_the_inspection_really_observes_variables_being_set(self):
        """Without this, `_assert_nothing_armed` could pass by blindness.

        The same read that reports "LIVE_TRADING is unset" must be able to
        report a variable the entrypoint DID set. `PROD_ACCESS_MODE` is that
        variable, and it is set by exactly the code under test.
        """
        self.assertIsNone(os.environ.get("PROD_ACCESS_MODE"))
        self._run_entrypoint(["--live-capital"])
        self.assertEqual(os.environ.get("PROD_ACCESS_MODE"), "CAPITAL",
                         "the inspection cannot see an environment change, "
                         "so 'nothing was armed' is not evidence")

    def test_CONTROL_the_guard_fails_when_an_authorization_is_present(self):
        """And the assertion helper is not a no-op over its variable list."""
        os.environ["LIVE_TRADING"] = "1"
        try:
            with self.assertRaises(AssertionError):
                self._assert_nothing_armed("synthetic")
        finally:
            os.environ.pop("LIVE_TRADING", None)

    def test_CONTROL_every_authorization_var_is_actually_checked(self):
        """Each name in the list must be able to trip the helper on its own."""
        for var in AUTHORIZATION_VARS:
            os.environ[var] = "1"
            try:
                with self.assertRaises(AssertionError, msg=f"{var} unchecked"):
                    self._assert_nothing_armed("synthetic")
            finally:
                os.environ.pop(var, None)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
