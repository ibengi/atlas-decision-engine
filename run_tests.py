#!/usr/bin/env python3
"""Execute toute la suite et ecrit test_report.json (resultats REELS,
consomme par model_gatekeeper). Code retour != 0 si un test echoue."""
import json
import sys
import time
import unittest

# Les defauts d'ENVIRONNEMENT DE TEST (identiques a ceux que pytest recoit via
# tests/conftest.py). Ce fichier est le lanceur REEL du build Docker : sans cet
# import, la decouverte unittest voyait des portes fermees que pytest voyait
# ouvertes, et la meme suite passait sous un lanceur et echouait sous l'autre.
# Importe AVANT toute decouverte, donc avant que le moindre module de test
# n'importe `config` (dont les attributs de classe sont figes a l'import).
#
# Aucun defaut de PRODUCTION n'est touche : l'etape `runtime` du Dockerfile
# n'execute jamais run_tests.py, et aucun module de production n'importe
# `tests`.
from tests import _gates  # noqa: F401


def main():
    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    test_count = suite.countTestCases()
    if test_count == 0:
        print("CRITICAL: No tests discovered. Check that test files exist in tests/ directory.", file=sys.stderr)
        sys.exit(1)
    print(f"Discovered {test_count} tests in {len(suite._tests)} modules")
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
