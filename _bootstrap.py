import os
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src", "utils"))
from paths import add_src_to_path  # noqa: E402
add_src_to_path()
