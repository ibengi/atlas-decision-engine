"""pytest entry point for the suite's environment defaults.

Everything lives in `tests/_gates.py` so that `run_tests.py` — which uses
unittest discovery and never loads a conftest — can import exactly the same
setup. Keeping it in one module is the point: a default that only one of the
two runners sees is how the build gate broke in the first place.

The two import spellings cover both ways a runner can place this directory on
`sys.path`: as the `tests` package (rootdir on the path) or as the top level
(`tests/` itself on the path, which is what `discover("tests")` does).
"""

try:
    from tests import _gates  # noqa: F401
except ImportError:            # pragma: no cover - depends on the runner
    import _gates              # noqa: F401
