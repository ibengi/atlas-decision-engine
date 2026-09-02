# -*- coding: utf-8 -*-
"""The live gatekeeper's two artifacts: present, fresh, green -- or no live.

`model_gatekeeper.check_live_allowed()` is the last lock before real
money. It reads `test_report.json` and `model_validation.json` by
RELATIVE path, i.e. from the process working directory (/app in the
container), and refuses live unless both are present, recent and
positive.

Generating the test report during the image build (see Dockerfile) makes
that lock EVALUABLE for the first time. This file pins the other half of
the bargain: that making it evaluable did not make it permissive.

Every case below asserts a REFUSAL. The single acceptance case exists
only to prove the refusals are caused by the criterion under test and
not by a broken fixture -- a suite where nothing can ever pass proves
nothing.

No network, no broker, no order: the gatekeeper reads two files and some
environment variables, nothing else.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import model_gatekeeper as g  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = 86400.0

#: Promotion variables that a human must set deliberately. Every test
#: below runs with them ALREADY satisfied, so the only thing that can
#: refuse live is the artifact under test.
PROMOTION_OK = {"NO_LIVE_PROMOTION": "0", "MODEL_APPROVED_FOR_LIVE": "YES"}


def green_report(age_days=0.0, ran=618):
    return {"generated_ts": time.time() - age_days * DAY, "ran": ran,
            "failures": 0, "errors": 0, "skipped": 0, "failed_tests": []}


def approved_validation(age_days=0.0, approved=True):
    return {"generated_ts": time.time() - age_days * DAY,
            "approved": approved, "model_version": "btc15m-baseline-0.1"}


class _GateBase(unittest.TestCase):
    """Each test runs in a throwaway working directory, because the code
    under test resolves both files relative to the cwd."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_gate_")
        self._cwd = os.getcwd()
        self._env = {k: os.environ.get(k) for k in
                     ("NO_LIVE_PROMOTION", "MODEL_APPROVED_FOR_LIVE")}
        os.environ.update(PROMOTION_OK)
        os.chdir(self.tmp)
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self._cwd)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, payload):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def gate(self):
        return g.check_live_allowed()

    def assertRefused(self, needle, msg=""):
        ok, failed = self.gate()
        self.assertFalse(ok, f"LIVE AUTORISE alors que: {msg or needle}")
        joined = " | ".join(failed)
        self.assertIn(needle, joined)
        return failed


class TestReportGatesLive(_GateBase):
    """The build-generated report is the only thing standing between a red
    suite and a live promotion."""

    def setUp(self):
        super().setUp()
        self.write("model_validation.json", approved_validation())

    def test_a_green_fresh_pair_is_the_control_that_allows_live(self):
        """The one case that must PASS. Without it, every refusal below
        could be an artefact of a broken fixture."""
        self.write("test_report.json", green_report())
        ok, failed = self.gate()
        self.assertTrue(ok, f"le controle echoue: {failed}")

    def test_an_absent_report_blocks(self):
        self.assertRefused("test_report.json absent")

    def test_a_stale_report_blocks(self):
        """Age is measured, not assumed: a report from a fortnight ago
        describes a tree nobody is deploying any more."""
        self.write("test_report.json", green_report(age_days=14))
        self.assertRefused("trop ancien")

    def test_a_report_just_past_the_window_blocks(self):
        """The boundary, not just the comfortable case."""
        self.write("test_report.json", green_report(age_days=7.01))
        self.assertRefused("trop ancien")

    def test_a_report_just_inside_the_window_is_accepted(self):
        self.write("test_report.json", green_report(age_days=6.9))
        ok, failed = self.gate()
        self.assertTrue(ok, f"un rapport de 6.9 jours refuse: {failed}")

    def test_a_red_report_blocks(self):
        r = green_report()
        r["failures"] = 1
        r["failed_tests"] = ["test_something (tests.test_x)"]
        self.write("test_report.json", r)
        self.assertRefused("tests non verts")

    def test_a_report_with_errors_blocks(self):
        r = green_report()
        r["errors"] = 3
        self.write("test_report.json", r)
        self.assertRefused("tests non verts")

    def test_a_report_without_a_timestamp_blocks(self):
        """No timestamp means the age cannot be checked, and unknown age is
        not young age.

        The refusal arrives as "trop ancien" rather than a missing-field
        message: `float(tr.get("generated_ts", 0))` dates the report to
        1970. That is the fail-closed direction -- an absent timestamp
        makes the report infinitely old, never fresh -- so the behaviour
        is pinned as-is rather than "corrected"."""
        r = green_report()
        del r["generated_ts"]
        self.write("test_report.json", r)
        self.assertRefused("trop ancien")

    def test_a_report_with_a_non_numeric_timestamp_blocks(self):
        r = green_report()
        r["generated_ts"] = "hier"
        self.write("test_report.json", r)
        self.assertRefused("generated_ts")

    def test_an_unreadable_report_blocks(self):
        """Truncated or corrupt JSON reads as absent, never as green."""
        with open(os.path.join(self.tmp, "test_report.json"), "w") as f:
            f.write('{"generated_ts": 1788')
        self.assertRefused("test_report.json absent")


class ModelValidationGatesLive(_GateBase):
    """approved:false must keep refusing, whatever else is in order."""

    def setUp(self):
        super().setUp()
        self.write("test_report.json", green_report())

    def test_approved_false_blocks_even_with_everything_else_green(self):
        """The artifact shipped in the image today. A fresh green test
        report and both promotion variables set must NOT be enough."""
        self.write("model_validation.json", approved_validation(approved=False))
        failed = self.assertRefused(
            "model_validation.json absent ou non approuve",
            "approved:false a laisse passer le live")
        self.assertEqual(len(failed), 1,
                         f"un autre critere masquait le test: {failed}")

    def test_a_missing_approved_field_blocks(self):
        self.write("model_validation.json", {"generated_ts": time.time()})
        self.assertRefused("model_validation.json absent ou non approuve")

    def test_a_truthy_non_boolean_approved_blocks(self):
        """`approved: "yes"` is not approval. The gate tests identity with
        True on purpose; this pins that it stays that way."""
        for value in ("YES", "true", 1, [1]):
            with self.subTest(approved=value):
                self.write("model_validation.json",
                           {"generated_ts": time.time(), "approved": value})
                self.assertRefused("non approuve", f"approved={value!r}")

    def test_a_stale_validation_blocks(self):
        self.write("model_validation.json", approved_validation(age_days=31))
        self.assertRefused("validation modele trop ancienne")

    def test_an_absent_validation_blocks(self):
        self.assertRefused("model_validation.json absent ou non approuve")


class ShippedArtifactTest(unittest.TestCase):
    """The file this repository actually ships."""

    def test_model_validation_is_committed_and_not_approved(self):
        path = os.path.join(REPO, "model_validation.json")
        self.assertTrue(os.path.exists(path),
                        "model_validation.json doit etre versionne: c'est "
                        "ainsi qu'il arrive dans l'image")
        mv = json.load(open(path, encoding="utf-8"))
        self.assertIs(mv["approved"], False,
                      "le modele n'est pas valide (voir "
                      "MODEL_VALIDATION_GUIDE.md); approved doit rester false")
        self.assertIsInstance(mv["generated_ts"], (int, float))

    def test_test_report_stays_gitignored(self):
        """It is build-generated. Versioning it would ship a report about
        somebody else's working tree."""
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
        self.assertIn("test_report.json", lines)

    def test_the_dockerfile_generates_and_copies_the_report(self):
        """A build that stopped running the suite would ship an image whose
        report describes nothing. Cheap to assert, expensive to miss."""
        with open(os.path.join(REPO, "Dockerfile"), encoding="utf-8") as f:
            dockerfile = f.read()
        self.assertIn("RUN python run_tests.py", dockerfile)
        self.assertIn("COPY --from=tests /src/test_report.json "
                      "/app/test_report.json", dockerfile)
        self.assertIn("test -f /app/model_validation.json", dockerfile)


if __name__ == "__main__":
    unittest.main()
