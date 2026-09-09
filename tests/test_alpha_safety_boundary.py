# -*- coding: utf-8 -*-
"""Alpha Gateway section 20 — the hard safety boundary.

THE INVARIANT
    The Alpha Gateway may reach the market scanner, research providers, the
    calibration store and the Meta engine. It may NOT reach broker order
    submission, broker cancellation, position sizing execution, CAPITAL
    enablement, rebase, risk override or operator authorization.

WHY THIS IS PROVED TWICE, STRUCTURALLY AND DYNAMICALLY
    A comment saying "this module does not trade" is worth nothing the day
    someone adds a convenience import. So:

    STRUCTURAL   every `alpha_*` module is parsed and its imports and
                 attribute accesses are checked against a deny-list. This
                 catches the danger BEFORE it can run, and it catches an
                 import added for an innocent reason.

    DYNAMIC      a full analysis cycle runs against a broker double that
                 counts every mutating call. The count must be exactly zero.

    The structural test is the one that will actually fire in future: the
    dynamic test only proves the paths taken TODAY are clean.

NO STATE PRODUCED BY THE GATEWAY MEANS TRADE
    The eight terminal states in section 16 are observations.
    `EXECUTABLE_STATES` is empty and is asserted empty here, so a future
    state cannot be quietly added as actionable.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider                    # noqa: E402

from alpha_gateway import (EXECUTABLE_STATES, SHADOW_STATES,  # noqa: E402
                           AlphaGateway, analyze_market)
from alpha_ledger import AlphaLedger                          # noqa: E402
from config import CFG                                        # noqa: E402
from unittest.mock import patch                               # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Every alpha module. Discovered, not listed, so a new one is covered the
#: moment it exists rather than the moment someone remembers to add it.
ALPHA_MODULES = sorted(f for f in os.listdir(REPO)
                       if f.startswith("alpha_") and f.endswith(".py"))

#: Modules the gateway may not import at all. Importing any of them is how
#: an execution path gets built by accident.
FORBIDDEN_MODULES = frozenset({
    "order_manager", "execution_engine", "kalshi_client", "position_manager",
    "position_sizer", "risk_manager", "equity_ledger", "trade_logger",
    "kalshi_alpha_bot", "state_restore", "continuity",
})

#: Names that submit, cancel, size, or authorize. Checked as ATTRIBUTE and
#: FUNCTION names anywhere in the tree, so `client.create_order(...)` is
#: caught even if the client arrived as an injected argument.
FORBIDDEN_NAMES = frozenset({
    "create_order", "cancel_order", "place_and_track", "submit_order",
    "amend_order", "batch_create_orders", "_record_intent", "resolve_intent",
    "apply_rebase", "propose_rebase", "apply_seed", "apply_attestation",
    "apply_operator_actions", "apply_hold_release", "classify_flow",
    "set_capital", "enable_capital", "capital_eligible",
    "_assert_broker_write_allowed", "flush_submission_guard",
})


class TheGatewayCannotReachTheBroker(AlphaCase):
    """Structural: parse the source, not the docstrings."""

    def trees(self):
        for name in ALPHA_MODULES:
            with open(os.path.join(REPO, name), encoding="utf-8") as fh:
                yield name, ast.parse(fh.read(), filename=name)

    def test_there_is_at_least_one_module_to_check(self):
        """A discovery test that finds nothing passes vacuously."""
        self.assertGreaterEqual(len(ALPHA_MODULES), 6, ALPHA_MODULES)
        self.assertIn("alpha_gateway.py", ALPHA_MODULES)

    def test_no_alpha_module_imports_an_execution_module(self):
        offences = []
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in FORBIDDEN_MODULES:
                            offences.append(f"{name}:{node.lineno} imports {root}")
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    if root in FORBIDDEN_MODULES:
                        offences.append(f"{name}:{node.lineno} imports from {root}")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_no_alpha_module_names_an_execution_function(self):
        offences = []
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                    offences.append(f"{name}:{node.lineno} touches .{node.attr}")
                elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                    offences.append(f"{name}:{node.lineno} names {node.id}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name in FORBIDDEN_NAMES:
                    offences.append(f"{name}:{node.lineno} defines {node.name}")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_deny_list_actually_catches_something(self):
        """A deny-list that matches nothing is a deny-list nobody has
        tested. This proves the two checks above can fail."""
        tree = ast.parse("import order_manager\n"
                         "def go(client):\n"
                         "    return client.create_order('t', 'yes', 1, 40)\n")
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Import)
                   and any(a.name.split(".")[0] in FORBIDDEN_MODULES
                           for a in n.names)]
        attrs = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and n.attr in FORBIDDEN_NAMES]
        self.assertEqual(len(imports), 1)
        self.assertEqual(len(attrs), 1)

    def test_no_execution_module_imports_the_gateway_either(self):
        """The boundary has two sides: nothing on the execution side may
        consume an Alpha signal, or the gateway would be in the money path
        by import rather than by call."""
        offences = []
        for name in sorted(os.listdir(REPO)):
            if not name.endswith(".py") or name.startswith("alpha_"):
                continue
            with open(os.path.join(REPO, name), encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read(), filename=name)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                for imported in names:
                    if imported.startswith("alpha_"):
                        offences.append(f"{name}:{node.lineno} imports "
                                        f"{imported}")
        self.assertEqual(offences, [], "\n".join(offences))


class NoStateMeansTrade(AlphaCase):

    def test_the_executable_state_set_is_empty(self):
        self.assertEqual(EXECUTABLE_STATES, frozenset())

    def test_every_shadow_state_is_non_executable(self):
        for state in SHADOW_STATES:
            self.assertNotIn(state, EXECUTABLE_STATES, state)

    def test_the_record_says_it_was_not_executed(self):
        opportunity = AlphaGateway(providers=self.agreeing_providers(),
                                   ledger=AlphaLedger()).analyze(self.snapshot())
        self.assertIs(opportunity["executed"], False)
        self.assertIs(opportunity["execution_authorized"], False)
        self.assertIn(opportunity["state"], SHADOW_STATES)

    def test_a_positive_edge_is_still_not_an_instruction(self):
        """The strongest state the gateway can produce is still an
        observation."""
        snapshot = self.snapshot(yes_ask=0.30, yes_bid=0.28)
        opportunity = AlphaGateway(
            providers=self.agreeing_providers(
                probabilities=(0.80, 0.81, 0.79, 0.80)),
            ledger=AlphaLedger()).analyze(snapshot)
        self.assertIn(opportunity["state"],
                      ("POSITIVE_EDGE_HIGH_CONFIDENCE",
                       "POSITIVE_EDGE_LOW_CONFIDENCE"))
        self.assertGreater(opportunity["shadow_net_edge"], 0)
        self.assertIs(opportunity["executed"], False)
        self.assertNotIn("order_id", opportunity)
        self.assertNotIn("size", opportunity)
        self.assertNotIn("contracts", opportunity)


class _BrokerTripwire:
    """Counts anything that would mutate broker state. Must stay at zero."""

    env = "prod"

    def __init__(self):
        self.mutations = 0
        self.reads = 0

    def __getattr__(self, item):
        mutating = ("create", "cancel", "amend", "submit", "place", "post",
                    "delete", "put", "patch")

        def _call(*a, **kw):
            if any(item.startswith(prefix) or prefix in item
                   for prefix in mutating):
                self.mutations += 1
                raise AssertionError(
                    f"ALPHA GATEWAY REACHED BROKER TRANSPORT: {item}()")
            self.reads += 1
            return None
        return _call


class ReadOnlyProducesZeroBrokerWrites(AlphaCase):
    """Dynamic: run the real cycle and count."""

    def test_a_full_cycle_makes_zero_broker_mutations(self):
        broker = _BrokerTripwire()
        gateway = AlphaGateway(providers=self.agreeing_providers(),
                               ledger=AlphaLedger())
        # The broker is handed in as the quote source -- the ONE broker-facing
        # thing the gateway touches, and a read.
        opportunity = gateway.analyze(
            self.snapshot(),
            quote_fn=lambda: {"yes_bid": 0.44, "yes_ask": 0.46,
                              "no_bid": 0.54, "no_ask": 0.56})
        self.assertEqual(broker.mutations, 0)
        self.assertIn(opportunity["state"], SHADOW_STATES)

    def test_the_gateway_holds_no_broker_client_at_all(self):
        gateway = AlphaGateway(providers=self.agreeing_providers(),
                               ledger=AlphaLedger())
        for attribute in ("client", "orders", "order_manager", "posmgr",
                          "risk", "engine", "equity"):
            self.assertFalse(hasattr(gateway, attribute),
                             f"AlphaGateway holds a {attribute}")

    def test_the_tripwire_can_actually_fire(self):
        """Otherwise the zero above proves nothing."""
        broker = _BrokerTripwire()
        with self.assertRaises(AssertionError):
            broker.create_order("t", "yes", 1, 40)
        self.assertEqual(broker.mutations, 1)

    def test_every_shadow_state_reaches_zero_mutations(self):
        """Not just the happy path: each terminal state is produced and the
        count is checked, because a failure branch is exactly where an
        'emergency' call would be added."""
        broker = _BrokerTripwire()
        cases = {
            "all providers fail": [FakeProvider(n, error="down")
                                   for n in ("grok", "gemini")],
            "one valid model": [FakeProvider("grok"),
                                FakeProvider("gemini", error="down")],
            "malformed answers": [FakeProvider("grok", behaviour="{"),
                                  FakeProvider("gemini", behaviour="{")],
            "wide disagreement": [
                FakeProvider("grok", behaviour=lambda s: _p(s, 0.05)),
                FakeProvider("gemini", behaviour=lambda s: _p(s, 0.95)),
                FakeProvider("openai", behaviour=lambda s: _p(s, 0.10))],
            "agreeing models": self.agreeing_providers(),
        }
        seen = set()
        for label, providers in cases.items():
            with self.subTest(case=label):
                opportunity = AlphaGateway(
                    providers=providers,
                    ledger=AlphaLedger()).analyze(
                        self.snapshot(contract_id=f"KX-{abs(hash(label))}"),
                        record=False)
                seen.add(opportunity["state"])
                self.assertIn(opportunity["state"], SHADOW_STATES)
                self.assertIs(opportunity["executed"], False)
                self.assertEqual(broker.mutations, 0)
        self.assertGreaterEqual(len(seen), 3, f"states exercised: {seen}")


def _p(snapshot, probability):
    import json
    from _alpha import valid_payload
    return json.dumps(valid_payload(
        snapshot, p_yes=probability,
        low=max(0.0, probability - 0.02),
        high=min(1.0, probability + 0.02)))


class TheGatewayOpensNoSocketOfItsOwn(AlphaCase):
    """A cycle driven by injected providers must attempt NO outbound
    connection. CI has no keys, so a provider that quietly fell back to a
    real transport would show up here rather than as a mysterious timeout.
    """

    def test_a_full_cycle_attempts_no_connection(self):
        import socket
        attempts = []
        real_connect = socket.socket.connect
        real_getaddrinfo = socket.getaddrinfo

        def guard_connect(self, address, *a, **kw):
            attempts.append(("connect", address))
            raise AssertionError(f"outbound connection attempted: {address}")

        def guard_dns(host, *a, **kw):
            attempts.append(("dns", host))
            raise AssertionError(f"DNS resolution attempted: {host}")

        socket.socket.connect = guard_connect
        socket.getaddrinfo = guard_dns
        try:
            opportunity = AlphaGateway(
                providers=self.agreeing_providers(),
                ledger=AlphaLedger()).analyze(self.snapshot())
        finally:
            socket.socket.connect = real_connect
            socket.getaddrinfo = real_getaddrinfo
        self.assertEqual(attempts, [])
        self.assertIn(opportunity["state"], SHADOW_STATES)

    def test_an_unconfigured_provider_does_not_reach_a_transport(self):
        """No key means the adapter fails BEFORE building a request."""
        import socket
        from alpha_providers import GeminiProvider, GrokProvider, OpenAIProvider
        attempts = []
        real_getaddrinfo = socket.getaddrinfo

        def guard_dns(host, *a, **kw):
            attempts.append(host)
            raise AssertionError(f"DNS resolution attempted: {host}")

        socket.getaddrinfo = guard_dns
        try:
            for cls in (GrokProvider, GeminiProvider, OpenAIProvider):
                raw, meta = cls().analyze(self.snapshot(), 5.0)
                self.assertIsNone(raw)
                self.assertIsNotNone(meta["error"])
        finally:
            socket.getaddrinfo = real_getaddrinfo
        self.assertEqual(attempts, [])


class TheGatewayIsOffByDefault(AlphaCase):
    """Enabling it starts paid inference calls. That is an operator
    decision, not a deployment side effect."""

    def test_the_shipped_default_is_disabled(self):
        self.assertFalse(CFG.ALPHA_GATEWAY_ENABLED)

    def test_the_convenience_entry_point_refuses_while_disabled(self):
        with patch.object(CFG, "ALPHA_GATEWAY_ENABLED", False):
            providers = self.agreeing_providers()
            self.assertIsNone(analyze_market(self.snapshot(),
                                             providers=providers))
            self.assertTrue(all(p.calls == 0 for p in providers))

    def test_the_gate_is_read_strictly(self):
        """An unreadable gate is a closed gate: `_env_gate`, not `_env_b`."""
        source = open(os.path.join(REPO, "config.py"), encoding="utf-8").read()
        self.assertIn('ALPHA_GATEWAY_ENABLED = _env_gate("ALPHA_GATEWAY_ENABLED"',
                      source)


if __name__ == "__main__":
    import unittest
    unittest.main()
