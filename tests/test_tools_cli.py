"""Each measurement tool must run the way its documentation says to run it.

These tools are invoked from a shell in the production container, as
`python tools/<name>.py <store>`. That entry point puts tools/ on sys.path
and NOT the repository root, so an import of the model or of a sibling
module fails there while passing under pytest, which adds the root itself.
A measurement tool that only runs inside its own test suite measures
nothing, so the CLI is exercised here as a subprocess.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = ("brier_oos", "shadow_census", "shadow_pnl", "model_ablation")


def store(n=40):
    """Rows the model can actually score, so a tool that runs is a tool
    that ran on data rather than one that exited on an empty file."""
    rows = []
    for i in range(n):
        rows.append({
            "ts": f"2026-09-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
            "ticker": "KXBTC15M-X", "spot": 100.0 + (i % 3), "strike": 100.0,
            "sigma_1m": 0.001, "minutes_remaining": 15.0, "ret_5m": 0.0,
            "probability_yes": 0.5, "yes_ask": 50, "no_ask": 50,
            "shadow_decision": "yes" if i % 2 else "none",
            "result": "yes" if i % 2 else "no",
            "settled_at": "2026-09-01T23:00:00+00:00",
            "features": {"model_version": "btc15m-v1.0-ref",
                         "data_quality": 99.0}})
    return rows


class CliTest(unittest.TestCase):
    def _run(self, tool, *args):
        # cwd is the repo root, as an operator's shell would be, and the
        # env carries no PYTHONPATH: the tool must bootstrap its own.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run(
            [sys.executable, os.path.join("tools", f"{tool}.py"), *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    def test_every_tool_starts_from_a_shell(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                r = self._run(tool, "--help")
                self.assertEqual(r.returncode, 0,
                                 f"{tool} --help failed: {r.stderr[-400:]}")

    def test_every_tool_reads_a_store_and_prints_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(store(), f)
            for tool in TOOLS:
                with self.subTest(tool=tool):
                    r = self._run(tool, p)
                    self.assertNotIn("ModuleNotFoundError", r.stderr)
                    self.assertEqual(r.stderr.strip(), "",
                                     f"{tool} wrote to stderr: {r.stderr[-400:]}")
                    rep = json.loads(r.stdout)   # raises if not JSON
                    self.assertEqual(len(rep["dataset_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
