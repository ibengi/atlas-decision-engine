# -*- coding: utf-8 -*-
"""The neutral producer's import list, pinned.

`research_feed` is the only thing the trading engine knows about the research
subsystem, and the whole isolation argument rests on it knowing nothing back.
Its docstring has always claimed a test pins that; until now no such test
existed, so the claim was documentation rather than a guarantee.

WHY AN ALLOW-LIST RATHER THAN A DENY-LIST HERE
    The Alpha modules are checked against a deny-list (see
    `test_alpha_safety_boundary`), which is right for a large subsystem whose
    legitimate imports cannot be enumerated. This module is different: it is
    deliberately tiny, and the interesting failure is not "it imported the
    broker" but "it imported ANYTHING new". A neutral boundary that grows an
    import is no longer neutral, whatever the import happens to be, so the
    allowed set is spelled out and any addition fails here and gets read by a
    human.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

import unittest                                               # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCER = "research_feed.py"

#: Everything the producer is allowed to import. `config` is the only
#: repository module: it carries the spool bounds and the enable gate.
ALLOWED_IMPORTS = frozenset({"hashlib", "json", "logging", "os", "time",
                             "config"})

#: Importing any of these would put the research path inside the money path
#: (or the money path inside the research path, which is worse).
FORBIDDEN_ROOTS = frozenset({
    "order_manager", "execution_engine", "kalshi_client", "position_manager",
    "position_sizer", "risk_manager", "equity_ledger", "trade_logger",
    "kalshi_alpha_bot", "state_authority", "continuity", "persistence",
    "transport_intent", "recovery",
})

FORBIDDEN_NAMES = frozenset({
    "create_order", "cancel_order", "place_and_track", "submit_order",
    "amend_order", "batch_create_orders", "_record_intent", "resolve_intent",
    "_assert_broker_write_allowed", "record_failure", "capital_eligible",
    "set_capital", "enable_capital",
})


def _tree(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=name)


def _imported_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # a relative import
                roots.add(".")
            else:
                roots.add((node.module or "").split(".")[0])
    return roots


class TheProducerKnowsNothingAboutResearch(unittest.TestCase):

    def test_it_imports_only_the_allowed_set(self):
        extra = sorted(_imported_roots(_tree(PRODUCER)) - ALLOWED_IMPORTS)
        self.assertEqual(extra, [], f"{PRODUCER} grew an import: {extra}. The "
                                    f"neutral boundary is only neutral while "
                                    f"this list stays small; widen it "
                                    f"deliberately or not at all.")

    def test_it_imports_no_alpha_module(self):
        alpha = sorted(r for r in _imported_roots(_tree(PRODUCER))
                       if r.startswith("alpha"))
        self.assertEqual(alpha, [], "the producer learned about Alpha")

    def test_it_imports_no_execution_or_broker_module(self):
        bad = sorted(_imported_roots(_tree(PRODUCER)) & FORBIDDEN_ROOTS)
        self.assertEqual(bad, [], f"the producer reached execution: {bad}")

    def test_it_names_no_broker_or_sentinel_primitive(self):
        offences = []
        for node in ast.walk(_tree(PRODUCER)):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                offences.append(f"{node.lineno}: .{node.attr}")
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                offences.append(f"{node.lineno}: {node.id}")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_allow_list_can_actually_fail(self):
        """An allow-list nobody has seen fail is an allow-list nobody has
        tested."""
        tree = ast.parse("import alpha_gateway\nimport order_manager\n")
        self.assertTrue(_imported_roots(tree) - ALLOWED_IMPORTS)
        self.assertTrue(_imported_roots(tree) & FORBIDDEN_ROOTS)

    def test_the_consumer_side_imports_no_execution_module(self):
        """The other half of the boundary: the Alpha consumer reads the
        spool and must not be able to reach the engine's authority."""
        bad = sorted(_imported_roots(_tree("alpha_consumer.py")) & FORBIDDEN_ROOTS)
        self.assertEqual(bad, [], f"the consumer reached execution: {bad}")

    def test_the_engine_imports_the_producer_and_never_alpha(self):
        """Dependency direction, asserted rather than described: the engine
        may know the neutral producer; it may not know Alpha."""
        roots = _imported_roots(_tree("execution_engine.py"))
        self.assertIn("research_feed", roots)
        self.assertEqual(sorted(r for r in roots if r.startswith("alpha")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
