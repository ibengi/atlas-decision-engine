import os
os.environ.setdefault("PROBE_PROVIDERS_ON_START", "0")
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
# AUD-P2-001 (live-readiness audit 2026-08-23): DATA_DIR defaults to "."
# so any test exercising JsonStore persistence without isolating it
# (test_reconciliation, test_settlements, test_accounting_golden) wrote
# positions_state.json / seen_fill_ids.json into the CHECKOUT ROOT —
# dirtying deploy/audit clones and tripping the platform's
# engine-checkout-pristine guards. Tests now get an isolated tempdir by
# default; a test that sets its own DATA_DIR (env or CFG) is unaffected.
if "DATA_DIR" not in os.environ:
    import tempfile
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="atlas_engine_tests_")
