import os
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src", "utils"))
from paths import add_src_to_path  # noqa: E402
add_src_to_path()
os.environ.setdefault("PROBE_PROVIDERS_ON_START", "0")   # pas de reseau en tests
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
