# -*- coding: utf-8 -*-
"""The production build must be pinned to the reviewed Dockerfile (Workstream D).

The service was building with RAILPACK, which happened to DETECT the Dockerfile
and use it — the build logs prove the test stage ran. But detection is not a
guarantee. Nothing pinned it, so a future repository change that made another
builder look applicable could have silently produced an image without the test
stage: no `run_tests.py`, no `test_report.json` generated from the tests that
actually ran, and therefore a LIVE gatekeeper reading an artifact from some
earlier build — or refusing for the wrong reason.

`railway.json` removes the ambiguity. These tests keep it removed.
"""

import json
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _railway_config():
    with open(os.path.join(_ROOT, "railway.json"), encoding="utf-8") as f:
        return json.load(f)


class ProductionBuildIsPinned(unittest.TestCase):

    def test_builder_is_explicitly_the_dockerfile(self):
        cfg = _railway_config()
        self.assertEqual(cfg.get("build", {}).get("builder"), "DOCKERFILE",
                         "the production builder must be pinned, not detected")

    def test_the_pinned_dockerfile_path_exists(self):
        path = _railway_config()["build"].get("dockerfilePath", "Dockerfile")
        self.assertTrue(os.path.isfile(os.path.join(_ROOT, path)),
                        f"railway.json points at {path!r}, which does not exist")

    def test_that_dockerfile_still_runs_the_test_stage(self):
        """Pinning the builder is worthless if the Dockerfile stops testing."""
        path = _railway_config()["build"].get("dockerfilePath", "Dockerfile")
        src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertRegex(code, r"FROM\s+\S+\s+AS\s+tests",
                         "the Dockerfile no longer declares a `tests` stage")
        self.assertRegex(code, r"RUN\s+python\s+run_tests\.py",
                         "the Dockerfile no longer RUNs the suite, so a red "
                         "suite would not fail the build")
        self.assertRegex(
            code, r"COPY\s+--from=tests\s+\S*test_report\.json",
            "the runtime image no longer copies the report produced by the "
            "tests that actually ran; the LIVE gate would read a stale or "
            "absent artifact")

    def test_the_runtime_stage_still_verifies_both_gate_artifacts(self):
        path = _railway_config()["build"].get("dockerfilePath", "Dockerfile")
        code = open(os.path.join(_ROOT, path), encoding="utf-8").read()
        for artifact in ("test_report.json", "model_validation.json"):
            self.assertTrue(
                re.search(rf"test -f /app/{re.escape(artifact)}", code),
                f"the image no longer verifies {artifact} is present")


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
