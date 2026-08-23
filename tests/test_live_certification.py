# -*- coding: utf-8 -*-
"""AIR-001 Wave 8 (DE-P0-002) — content-bound live certification.

Reproduced defect: live execution was armed by configuration flags
alone; nothing bound "live allowed" to WHAT was audited. Every refusal
path is pinned here; the only passing path is a manifest with
atr_verdict=GO whose recomputed bindings match exactly.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from live_certification import (ABSENT,  # noqa: E402
                                LIVE_DISABLED_ATR_NO_GO,
                                LIVE_DISABLED_CERTIFICATION_DRIFT,
                                LIVE_DISABLED_NO_CERTIFICATION,
                                compute_current_bindings,
                                live_certification_check, write_manifest)


def tmp_manifest_path():
    return os.path.join(tempfile.mkdtemp(prefix="cert_"),
                        "live_certification.json")


class TestCertificationCheck(unittest.TestCase):
    def test_no_manifest_disables_live(self):
        ok, reason = live_certification_check(path=tmp_manifest_path())
        self.assertFalse(ok)
        self.assertIn(LIVE_DISABLED_NO_CERTIFICATION, reason)

    def test_no_go_verdict_never_authorizes(self):
        """Even with PERFECT bindings, a NO_GO manifest refuses live —
        the standing ATR-001 verdict cannot be bypassed by drift-free
        state."""
        path = tmp_manifest_path()
        write_manifest(atr_verdict="NO_GO", operator="op", path=path)
        ok, reason = live_certification_check(path=path)
        self.assertFalse(ok)
        self.assertIn(LIVE_DISABLED_ATR_NO_GO, reason)

    def test_matching_bindings_with_go_pass(self):
        path = tmp_manifest_path()
        write_manifest(atr_verdict="GO", operator="mechanism-test",
                       path=path)
        ok, reason = live_certification_check(path=path)
        self.assertTrue(ok, reason)

    def test_config_drift_disables_live(self):
        """A risk-knob change after certification must kill live: the
        risk_config_hash binding drifts."""
        path = tmp_manifest_path()
        write_manifest(atr_verdict="GO", operator="mechanism-test",
                       path=path)
        import config
        with patch.object(config.CFG, "MAX_POS_PCT", 7.77):
            ok, reason = live_certification_check(path=path)
        self.assertFalse(ok)
        self.assertIn(LIVE_DISABLED_CERTIFICATION_DRIFT, reason)
        self.assertIn("risk_config_hash", reason)

    def test_engine_commit_drift_disables_live(self):
        path = tmp_manifest_path()
        manifest_bindings = compute_current_bindings()
        manifest_bindings["engine_commit"] = "certified-elsewhere"
        write_manifest(atr_verdict="GO", operator="op",
                       bindings=manifest_bindings, path=path)
        ok, reason = live_certification_check(path=path)
        self.assertFalse(ok)
        self.assertIn("engine_commit", reason)

    def test_absent_is_bound_not_ignored(self):
        """An ABSENT binding (e.g. no test_report.json) must equal
        ABSENT on both sides — a certification issued without the test
        report cannot cover a process that now has one, and vice
        versa."""
        path = tmp_manifest_path()
        bindings = compute_current_bindings()
        bindings["test_report_sha256"] = ABSENT
        write_manifest(atr_verdict="GO", operator="op",
                       bindings=bindings, path=path)
        current = compute_current_bindings()
        current["test_report_sha256"] = "d" * 64
        ok, reason = live_certification_check(path=path,
                                              bindings=current)
        self.assertFalse(ok)
        self.assertIn("test_report_sha256", reason)

    def test_unreadable_manifest_fails_closed(self):
        path = tmp_manifest_path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        ok, reason = live_certification_check(path=path)
        self.assertFalse(ok)
        self.assertIn("LIVE_DISABLED_MANIFEST_UNREADABLE", reason)


class TestLivePathEnforcement(unittest.TestCase):
    def test_order_refused_without_certification(self):
        """Through the REAL engine cycle: SHADOW off, healthy signal,
        no manifest -> zero orders, rejection live_certification."""
        from test_pipeline_integration import FakeClient, make_engine
        cli = FakeClient(order_scenario="fill")
        eng, tmp = make_engine(cli)
        os.remove(os.path.join(tmp, "live_certification.json"))
        placed = eng.cycle(1)
        self.assertEqual(placed, 0)
        import json as _json
        rep = _json.load(open(os.path.join(tmp, "cycle_report.json")))
        self.assertEqual(
            rep["rejections_by_reason"].get("live_certification"), 1)
        self.assertEqual(len(cli.created_orders), 0)


if __name__ == "__main__":
    unittest.main()
