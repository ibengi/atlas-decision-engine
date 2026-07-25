#!/usr/bin/env python3
"""Execute toute la suite et ecrit test_report.json (resultats REELS,
consomme par model_gatekeeper). Code retour != 0 si un test echoue."""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src", "utils"))
from paths import add_src_to_path  # noqa: E402
add_src_to_path()


def main():
    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    report = {
        "generated_ts": time.time(),
        "ran": res.testsRun,
        "failures": len(res.failures),
        "errors": len(res.errors),
        "skipped": len(res.skipped),
        "failed_tests": [str(t) for t, _ in res.failures + res.errors],
    }
    with open("test_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print(json.dumps(report, indent=1))
    sys.exit(0 if (res.wasSuccessful()) else 1)


if __name__ == "__main__":
    main()
