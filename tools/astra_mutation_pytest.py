"""Structured, local-only evidence for the Astra mutation classifier.

This plugin does not decide whether a failure is a safety failure.  It records
the actual phase and traceback; the runner requires a separately reviewed
invariant witness.  In particular unittest executes setUp/tearDown inside
pytest's ``call`` phase, so the phase label alone is insufficient.
"""
import ast
from functools import lru_cache
import json
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--astra-phase-file", default=None)


def pytest_configure(config):
    config._astra_evidence = {"schema": "astra-mutation-phases-v1",
                              "reports": [], "collection_errors": [],
                              "collected": 0}


@lru_cache(maxsize=128)
def _source_tree(path):
    source = Path(path).read_text(encoding="utf-8")
    return source, ast.parse(source)


def _statement(path, line):
    try:
        source, tree = _source_tree(str(path))
        nodes = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.Assert, ast.Expr, ast.With))
                 and n.lineno <= line <= getattr(n, "end_lineno", n.lineno)]
        if nodes:
            node = min(nodes, key=lambda n: n.end_lineno - n.lineno)
            return ast.unparse(node)
        return source.splitlines()[line - 1].strip()
    except (OSError, UnicodeError, SyntaxError, IndexError):
        return ""


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    entry = {"nodeid": report.nodeid, "when": report.when,
             "outcome": report.outcome, "exception": "", "frames": [],
             "fixture_failure": report.when in ("setup", "teardown")
                                and report.failed}
    if call.excinfo is not None:
        entry["exception"] = call.excinfo.type.__name__
        for frame in call.excinfo.traceback:
            function = frame.name
            entry["frames"].append({"path": str(frame.path),
                                    "line": frame.lineno + 1,
                                    "function": function,
                                    "statement": _statement(frame.path,
                                                            frame.lineno + 1)})
            if function in {"setUp", "tearDown", "setUpClass", "tearDownClass",
                            "_callSetUp", "_callTearDown", "doCleanups",
                            "doClassCleanups"}:
                entry["fixture_failure"] = True
    item.config._astra_evidence["reports"].append(entry)


def pytest_collectreport(report):
    # pytest does not attach config to collection reports.  Session's finish
    # hook records collection failures via this small plugin-local list.
    if report.failed:
        _collection_errors.append(str(report.longrepr))


_collection_errors = []


def pytest_sessionfinish(session, exitstatus):
    evidence = session.config._astra_evidence
    evidence.update(collected=session.testscollected,
                    collection_errors=list(_collection_errors),
                    session_exit=int(exitstatus))
    destination = session.config.getoption("--astra-phase-file")
    if destination:
        Path(destination).write_text(json.dumps(evidence, indent=2),
                                     encoding="utf-8")
