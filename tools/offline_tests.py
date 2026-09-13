#!/usr/bin/env python3
"""Run canonical tests (or explicit Python test arguments) without external I/O.

Uses a credential/proxy-free environment and an inherited startup audit guard.
Any unexpected denied network attempt invalidates the run, even if application
code catches it. Existing tests of their own stronger network block remain valid.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="atlas-offline-tests-") as directory:
        log = Path(directory) / "denials.log"
        env = {key: os.environ[key] for key in
               ("PATH", "PYTHONPATH", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
        env["PYTHONPATH"] = str(root / "tests/_offline") + os.pathsep + env.get("PYTHONPATH", "")
        env["ATLAS_OFFLINE_DENIAL_LOG"] = str(log)
        arguments = sys.argv[1:] or ["run_tests.py"]
        result = subprocess.run([sys.executable] + arguments, cwd=root, env=env)
        denials = log.read_text().splitlines() if log.exists() else []
        print(json.dumps({"offline_runner": "atlas-python-network-deny-v1",
                          "test_exit": result.returncode,
                          "unexpected_external_transport_denials": len(denials)}), flush=True)
        return result.returncode or (1 if denials else 0)


if __name__ == "__main__":
    raise SystemExit(main())
