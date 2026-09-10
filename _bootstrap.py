import os
import tempfile

# Test tools may be invoked directly, without run_tests.py's isolated launcher.
# Give their default economic root a lifetime-scoped temporary directory BEFORE
# config is imported. An explicit non-repository DATA_DIR remains supported.
_repo_root = os.path.realpath(os.path.dirname(__file__))
if (not os.environ.get("DATA_DIR") or
        os.path.realpath(os.environ["DATA_DIR"]) == _repo_root):
    _test_state = tempfile.TemporaryDirectory(prefix="atlas-tests-state-")
    os.environ["DATA_DIR"] = _test_state.name

os.environ.setdefault("PROBE_PROVIDERS_ON_START", "0")
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
