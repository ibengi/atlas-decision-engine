# -*- coding: utf-8 -*-
"""Phase 2B — env-driven zero-loss state restore, FAIL-CLOSED.

The restore may only ever run against a virgin state directory, must
verify every byte against the operator hash manifest before writing
anything, and must leave the volume untouched (sentinel tripped) on any
anomaly. state_epoch.json is destination metadata created after a
successful restore so verify_state_root() accepts the volume.
"""
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from persistence import PersistenceSentinel, verify_state_root  # noqa: E402
from state_restore import (RESTORE_BASENAMES, maybe_restore_state,  # noqa: E402
                           restore_or_die)

FILES = {
    "submission_guard.json": b'{\n "KX-T1": 1787915905.081206\n}',
    "orders_state.json": b'{\n "oid-1": {\n  "ticker": "KX-T1"\n }\n}',
    "kalshi_trades.json": b'[\n {\n  "trade_id": "t1",\n  "net_pnl": -1.5\n }\n]',
    "risk_state.json": b'{\n "date": "2026-08-16"\n}',
    "positions_state.json": b'{\n "t1": {\n  "ticker": "KX-T1",\n  "count": 3\n }\n}',
}


def _tgz_b64(files=FILES):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return base64.b64encode(buf.getvalue()).decode()


def _manifest(files=FILES):
    return json.dumps({n: hashlib.sha256(p).hexdigest()
                       for n, p in files.items()})


class _RestoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_restore_")
        self._saved_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self._env = {}
        for k in ("RESTORE_STATE_TGZ_B64", "RESTORE_STATE_SHA256"):
            self._env[k] = os.environ.pop(k, None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        bot.CFG.DATA_DIR = self._saved_dir
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arm(self, b64=None, manifest=None):
        os.environ["RESTORE_STATE_TGZ_B64"] = _tgz_b64() if b64 is None else b64
        os.environ["RESTORE_STATE_SHA256"] = (_manifest() if manifest is None
                                              else manifest)


class RestoreTest(_RestoreBase):

    def test_no_env_is_noop(self):
        self.assertTrue(maybe_restore_state())
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertTrue(PersistenceSentinel.healthy())

    def test_full_restore_byte_exact_with_sidecars_and_epoch(self):
        self._arm()
        self.assertTrue(maybe_restore_state())
        self.assertTrue(PersistenceSentinel.healthy())
        for name, payload in FILES.items():
            raw = open(os.path.join(self.tmp, name), "rb").read()
            self.assertEqual(raw, payload, f"{name} must be byte-exact")
            side = open(os.path.join(self.tmp, name + ".sha256")).read()
            self.assertEqual(side, hashlib.sha256(payload).hexdigest())
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "state_epoch.json")))

    def test_restored_volume_passes_continuity_check(self):
        self._arm()
        self.assertTrue(maybe_restore_state())
        saved = bot.CFG.REQUIRE_PERSISTENT_STATE
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        try:
            self.assertTrue(verify_state_root())
            self.assertTrue(PersistenceSentinel.healthy())
        finally:
            bot.CFG.REQUIRE_PERSISTENT_STATE = saved

    def test_hash_mismatch_writes_nothing_and_trips_sentinel(self):
        bad = dict(FILES)
        bad["risk_state.json"] = b'{\n "date": "TAMPERED"\n}'
        self._arm(b64=_tgz_b64(bad))  # manifest still hashes the true bytes
        self.assertFalse(maybe_restore_state())
        self.assertFalse(PersistenceSentinel.healthy())
        self.assertEqual(
            [f for f in os.listdir(self.tmp) if not f.endswith(".tmp")], [],
            "a failed restore must not write any state file")

    def test_complete_existing_state_is_never_overwritten(self):
        for name in RESTORE_BASENAMES:
            with open(os.path.join(self.tmp, name), "wb") as f:
                f.write(b"EXISTING")
        self._arm()
        self.assertTrue(maybe_restore_state())
        self.assertTrue(PersistenceSentinel.healthy())
        for name in RESTORE_BASENAMES:
            self.assertEqual(
                open(os.path.join(self.tmp, name), "rb").read(), b"EXISTING")

    def test_partial_existing_state_fails_closed_without_writes(self):
        keep = os.path.join(self.tmp, "positions_state.json")
        with open(keep, "wb") as f:
            f.write(b"EXISTING")
        self._arm()
        self.assertFalse(maybe_restore_state())
        self.assertFalse(PersistenceSentinel.healthy())
        self.assertEqual(open(keep, "rb").read(), b"EXISTING")
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "orders_state.json")))

    def test_missing_file_in_archive_fails_closed(self):
        partial = {k: v for k, v in FILES.items()
                   if k != "kalshi_trades.json"}
        self._arm(b64=_tgz_b64(partial))
        self.assertFalse(maybe_restore_state())
        self.assertFalse(PersistenceSentinel.healthy())

    def test_missing_manifest_fails_closed(self):
        os.environ["RESTORE_STATE_TGZ_B64"] = _tgz_b64()
        self.assertFalse(maybe_restore_state())
        self.assertFalse(PersistenceSentinel.healthy())

    def test_corrupt_archive_fails_closed(self):
        self._arm(b64=base64.b64encode(b"not a tarball").decode())
        self.assertFalse(maybe_restore_state())
        self.assertFalse(PersistenceSentinel.healthy())

    def test_chunked_b64_with_whitespace_and_lost_padding_restores(self):
        b64 = _tgz_b64()
        third = len(b64) // 3
        os.environ["RESTORE_STATE_TGZ_B64"] = b64[:third] + "\n"
        os.environ["RESTORE_STATE_TGZ_B64_2"] = " " + b64[third:2 * third]
        os.environ["RESTORE_STATE_TGZ_B64_3"] = \
            b64[2 * third:].rstrip("=") + "\n"
        os.environ["RESTORE_STATE_SHA256"] = _manifest()
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("RESTORE_STATE_TGZ_B64_2",
                                  "RESTORE_STATE_TGZ_B64_3")])
        self.assertTrue(maybe_restore_state())
        self.assertTrue(PersistenceSentinel.healthy())
        for name, payload in FILES.items():
            self.assertEqual(
                open(os.path.join(self.tmp, name), "rb").read(), payload)

    def test_truncated_chunk_fails_closed(self):
        b64 = _tgz_b64()
        os.environ["RESTORE_STATE_TGZ_B64"] = b64[:len(b64) - 200]
        os.environ["RESTORE_STATE_SHA256"] = _manifest()
        self.assertFalse(maybe_restore_state())
        self.assertFalse(PersistenceSentinel.healthy())


class RestoreOrDieTest(_RestoreBase):
    """Boot-hook semantics: a REQUESTED restore that cannot complete must
    stop the process with the destination untouched (incident 2026-08-31:
    falling through to startup let RiskManager contaminate the virgin
    destination and permanently block the never-overwrite restore)."""

    def _die(self):
        with self.assertRaises(SystemExit) as cm:
            restore_or_die()
        self.assertEqual(cm.exception.code, 78)
        self.assertFalse(PersistenceSentinel.healthy())
        self.assertEqual(
            [f for f in os.listdir(self.tmp) if not f.endswith(".tmp")], [],
            "a dead restore must leave the destination with ZERO writes")

    def test_no_restore_vars_boots_normally(self):
        restore_or_die()          # must not raise
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertTrue(PersistenceSentinel.healthy())

    def test_manifest_without_payload_exits_without_writes(self):
        # The 2026-08-31 production incident: payload variable deleted,
        # manifest left behind -> restore silently no-opped and startup
        # contaminated the destination. Now: process exit, zero writes.
        os.environ["RESTORE_STATE_SHA256"] = _manifest()
        self._die()

    def test_corrupt_payload_exits_without_writes(self):
        self._arm(b64=base64.b64encode(b"garbage").decode())
        self._die()

    def test_wrong_hash_exits_without_writes(self):
        bad = dict(FILES)
        bad["kalshi_trades.json"] = b"[]"
        self._arm(b64=_tgz_b64(bad))
        self._die()

    def test_successful_restore_boots_with_require_persistent_state(self):
        # Full virgin-destination boot: REQUIRE_PERSISTENT_STATE=true and
        # ALLOW_FRESH_STATE unset from the start, valid payload.
        saved = (bot.CFG.REQUIRE_PERSISTENT_STATE, bot.CFG.ALLOW_FRESH_STATE)
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        bot.CFG.ALLOW_FRESH_STATE = False
        self.addCleanup(lambda: (setattr(bot.CFG, "REQUIRE_PERSISTENT_STATE",
                                         saved[0]),
                                 setattr(bot.CFG, "ALLOW_FRESH_STATE",
                                         saved[1])))
        self._arm()
        restore_or_die()          # must not raise
        self.assertTrue(verify_state_root())
        self.assertTrue(PersistenceSentinel.healthy())
        for name, payload in FILES.items():
            raw = open(os.path.join(self.tmp, name), "rb").read()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),
                             hashlib.sha256(payload).hexdigest())
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "state_epoch.json")))

    def test_partial_destination_exits_and_never_overwrites(self):
        keep = os.path.join(self.tmp, "risk_state.json")
        with open(keep, "wb") as f:
            f.write(b"EXISTING")
        self._arm()
        with self.assertRaises(SystemExit):
            restore_or_die()
        self.assertEqual(open(keep, "rb").read(), b"EXISTING")
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "orders_state.json")))


if __name__ == "__main__":
    unittest.main()
