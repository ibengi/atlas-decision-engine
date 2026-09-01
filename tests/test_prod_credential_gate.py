# -*- coding: utf-8 -*-
"""PRODUCTION credential gate: a prod boot without usable keys STOPS.

Before this gate, `KalshiClient._load_key` only WARNED on an absent or
malformed private key and returned None, and `_sign_headers` then omitted
the KALSHI-ACCESS-SIGNATURE header entirely. A production deployment with
broken credentials therefore came up, built its managers, reconciled,
scanned and called the broker UNAUTHENTICATED -- collecting silent 401s
instead of stopping. DEMO already refused to start without its dedicated
keys; production, where the money is real, must not be more permissive.

Every failure path below asserts the same two things: the process exits,
and the broker client is NEVER EVEN CONSTRUCTED -- so there is no object
on which a request could have been made. Nothing here touches a network,
DEMO or LIVE, and no test enables order submission.
"""
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from config import CFG, prod_credentials_config  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402


def _pem(key) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()


#: Generated once: key generation is the slow part, not the assertions.
VALID_RSA_PEM = _pem(rsa.generate_private_key(public_exponent=65537,
                                              key_size=2048))
EC_PEM = _pem(ec.generate_private_key(ec.SECP256R1()))
PUBLIC_PEM = rsa.generate_private_key(
    public_exponent=65537, key_size=2048).public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()

TRUNCATED_PEM = "\n".join(VALID_RSA_PEM.splitlines()[:4]) + "\n"

#: Every way the credentials can be unusable. (label, key_id, key_pem)
BAD_CREDENTIALS = [
    ("key_id absent", None, VALID_RSA_PEM),
    ("key_id vide", "", VALID_RSA_PEM),
    ("key_id blanc", "   \t\n ", VALID_RSA_PEM),
    ("pem absent", "kid-1", None),
    ("pem vide", "kid-1", ""),
    ("pem blanc", "kid-1", "   \n\t "),
    ("pem non-PEM", "kid-1", "definitely-not-a-key"),
    ("pem tronque", "kid-1", TRUNCATED_PEM),
    ("pem corps invalide", "kid-1",
     "-----BEGIN PRIVATE KEY-----\nnot-base64!!\n-----END PRIVATE KEY-----"),
    ("cle publique", "kid-1", PUBLIC_PEM),
    ("cle EC (non RSA)", "kid-1", EC_PEM),
    ("les deux absents", None, None),
]


class _CredBase(unittest.TestCase):
    ENV_KEYS = ("DEMO_TRADING", "KALSHI_ENV_CONFIRM", "LIVE_TRADING",
                "LIVE_TRADING_CONFIRMED", "RESTORE_STATE_SHA256",
                "RESTORE_STATE_TGZ_B64", "RESTORE_STATE_SRC_DIR")

    def setUp(self):
        self._cfg = (CFG.KEY_ID, CFG.PRIV_KEY, CFG.SHADOW_MODE,
                     CFG.ALLOW_ORDER_SUBMISSION)
        self._env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)      # no DEMO_TRADING => env is "prod"
        CFG.SHADOW_MODE = False
        CFG.ALLOW_ORDER_SUBMISSION = False        # stays false throughout
        self.addCleanup(self._restore)

    def _restore(self):
        (CFG.KEY_ID, CFG.PRIV_KEY, CFG.SHADOW_MODE,
         CFG.ALLOW_ORDER_SUBMISSION) = self._cfg
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _set(key_id, key_pem):
        CFG.KEY_ID = key_id
        CFG.PRIV_KEY = key_pem

    def _run_main(self, argv):
        """Runs main() with the broker client and the engine replaced by
        mocks, so reaching either is observable and harmless. Returns the
        SystemExit (if any), both mocks, and the CRITICAL log output."""
        client_cls = MagicMock(name="KalshiClient")
        engine_cls = MagicMock(name="ExecutionEngine")
        records = []
        handler = logging.Handler()
        handler.setLevel(logging.CRITICAL)
        handler.emit = records.append
        logger = logging.getLogger("BOT")
        logger.addHandler(handler)
        try:
            with patch.object(bot, "KalshiClient", client_cls), \
                 patch.object(bot, "ExecutionEngine", engine_cls), \
                 patch.object(sys, "argv", argv):
                try:
                    bot.main()
                    raised = None
                except SystemExit as e:
                    raised = e
        finally:
            logger.removeHandler(handler)
        return (raised, client_cls, engine_cls,
                "\n".join(r.getMessage() for r in records))


class ProdCredentialValidatorTest(_CredBase):
    """The validator itself, one case per failure mode."""

    def test_every_unusable_credential_is_refused(self):
        for label, key_id, key_pem in BAD_CREDENTIALS:
            with self.subTest(case=label):
                self._set(key_id, key_pem)
                ok, err = prod_credentials_config()
                self.assertFalse(ok, f"{label} was accepted")
                self.assertTrue(err)

    def test_a_valid_rsa_pair_is_accepted(self):
        self._set("kid-1", VALID_RSA_PEM)
        self.assertEqual(prod_credentials_config(), (True, None))

    def test_surrounding_whitespace_is_tolerated(self):
        """A PEM pasted with stray newlines is valid, not a refusal."""
        self._set("  kid-1  ", "\n  " + VALID_RSA_PEM + "  \n")
        self.assertEqual(prod_credentials_config(), (True, None))

    def test_error_never_leaks_key_material(self):
        secret_lines = [ln for ln in VALID_RSA_PEM.splitlines()
                        if ln and not ln.startswith("-----")]
        for label, key_id, key_pem in BAD_CREDENTIALS:
            with self.subTest(case=label):
                self._set(key_id, key_pem)
                _ok, err = prod_credentials_config()
                for chunk in secret_lines:
                    self.assertNotIn(chunk, err)


class ProdBootGateTest(_CredBase):
    """The gate on the real startup path: exit, zero broker construction."""

    def _assert_refused(self, raised, client_cls, engine_cls, logged):
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 1)
        # It must be THIS gate that stopped the boot, not an earlier exit
        # that would make the assertions below vacuously true.
        self.assertIn("PRODUCTION: identifiants invalides", logged)
        self.assertEqual(client_cls.call_count, 0,
                         "a broker client was constructed")
        self.assertEqual(engine_cls.call_count, 0,
                         "the engine (managers, reconciliation) was built")
        self.assertEqual(client_cls.return_value.method_calls, [],
                         "a broker method was called")

    def test_scan_only_prod_boot_refuses_on_every_bad_credential(self):
        """--scan-only skips the LIVE-confirmation block, so the credential
        gate is the ONLY thing standing between a prod boot and the
        broker. Every failure mode must stop there."""
        for label, key_id, key_pem in BAD_CREDENTIALS:
            with self.subTest(case=label):
                self._set(key_id, key_pem)
                out = self._run_main(
                    ["kalshi_alpha_bot.py", "--scan-only"])
                self._assert_refused(*out)

    def test_rank_only_prod_boot_refuses(self):
        self._set("kid-1", "not-a-key")
        self._assert_refused(*self._run_main(
            ["kalshi_alpha_bot.py", "--rank-only"]))

    def test_full_engine_prod_boot_refuses_after_live_confirmations(self):
        """With every LIVE confirmation and the model gatekeeper satisfied,
        the credential gate is what stops the boot."""
        os.environ.update({"KALSHI_ENV_CONFIRM": "LIVE",
                           "LIVE_TRADING_CONFIRMED": "YES",
                           "LIVE_TRADING": "1"})
        for label, key_id, key_pem in BAD_CREDENTIALS:
            with self.subTest(case=label):
                self._set(key_id, key_pem)
                with patch("model_gatekeeper.check_live_allowed",
                           return_value=(True, [])):
                    out = self._run_main(
                        ["kalshi_alpha_bot.py", "--loop"])
                self._assert_refused(*out)

    def test_the_gate_logs_the_reason_without_key_material(self):
        self._set("kid-1", TRUNCATED_PEM)
        raised, _client, _engine, blob = self._run_main(
            ["kalshi_alpha_bot.py", "--scan-only"])
        self.assertIsInstance(raised, SystemExit)
        self.assertIn("PRODUCTION", blob)
        self.assertIn("AUCUN appel", blob)
        for line in TRUNCATED_PEM.splitlines():
            if line and not line.startswith("-----"):
                self.assertNotIn(line, blob)

    def test_valid_credentials_pass_the_gate(self):
        """Proof the gate is not simply always-refusing: with a usable RSA
        pair the boot proceeds to construct the client."""
        self._set("kid-1", VALID_RSA_PEM)

        class _Reached(Exception):
            pass

        client_cls = MagicMock(side_effect=_Reached("client constructed"))
        with patch.object(bot, "KalshiClient", client_cls), \
             patch.object(sys, "argv", ["kalshi_alpha_bot.py", "--scan-only"]):
            with self.assertRaises(_Reached):
                bot.main()
        self.assertEqual(client_cls.call_count, 1)

    def test_demo_boot_is_unaffected_by_prod_credentials(self):
        """The gate is prod-only: a DEMO boot with empty PROD keys still
        proceeds (DEMO has its own, separate refusal)."""
        self._set("", "")
        os.environ["DEMO_TRADING"] = "1"

        class _Reached(Exception):
            pass

        client_cls = MagicMock(side_effect=_Reached("client constructed"))
        with patch.object(bot, "KalshiClient", client_cls), \
             patch.object(sys, "argv", ["kalshi_alpha_bot.py", "--scan-only"]):
            with self.assertRaises(_Reached):
                bot.main()
        self.assertEqual(client_cls.call_count, 1)
        self.assertEqual(client_cls.call_args[0][0], "demo")


if __name__ == "__main__":
    unittest.main()
