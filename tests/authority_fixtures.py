"""Explicit non-secret identities and independent synthetic authority fixtures.

Only fixture construction lives here. No engine method, guard or test assertion
is patched. Checkpoints survive a simulated disk restore within the test.
"""
import os
import threading
from config import CFG, _p
from continuity_authority import account_identity
from state_authority import checkpoint


class CheckpointStore:
    def __init__(self, request):
        self.current = self.key(request)
        self.lock = threading.Lock()
    @staticmethod
    def key(request):
        return request.account_fingerprint, request.generation, request.digest
    def verify_current(self, request):
        with self.lock:
            return request if self.key(request) == self.current else None
    def advance(self, previous, candidate):
        with self.lock:
            if self.key(previous) != self.current or candidate.generation <= previous.generation:
                return None
            if candidate.account_fingerprint != previous.account_fingerprint:
                return None
            self.current = self.key(candidate)
            return candidate


_providers = {}


def corrupt_json(path, value):
    """Inject restored/corrupt bytes outside the engine API, without advancing
    its authoritative manifest. Used only by filesystem fault scenarios."""
    from pathlib import Path
    from strict_data import dumps
    import hashlib
    raw = dumps(value, indent=1, ensure_ascii=False).encode()
    Path(path).write_bytes(raw)
    Path(path + ".sha256").write_text(hashlib.sha256(raw).hexdigest())


def provider_for(path=None, env="prod"):
    path = path or _p("equity_ledger.json")
    key = (os.getpid(), os.path.realpath(os.path.dirname(path)))
    if key not in _providers:
        identity = account_identity("kalshi", env, CFG.BROKER_ACCOUNT_ID)
        _providers[key] = CheckpointStore(checkpoint(path, identity))
    return _providers[key]


class FrozenSyntheticBroker:
    """A certificate from a broker with no uncontrolled writers in this test."""
    def __init__(self, broker, env="prod"):
        self.broker = broker
        self.identity = account_identity("kalshi", env, CFG.BROKER_ACCOUNT_ID)
    def verify(self, identity, versions):
        return (identity == self.identity and not self.broker.orders
                and not self.broker.positions)


def initialize_empty():
    from persistence import JsonStore
    for name, value in (("kalshi_trades.json", []), ("positions_state.json", {}),
                        ("orders_state.json", {}), ("pending_intents.json", {}),
                        ("submission_guard.json", {})):
        if not os.path.exists(_p(name)):
            if not JsonStore.save(_p(name), value):
                raise RuntimeError("synthetic empty state initialization failed")


def freeze_for(ledger):
    from types import SimpleNamespace
    broker = getattr(ledger.posmgr, "client", None)
    if broker is None:
        broker = SimpleNamespace(orders=[], positions=[])
    return FrozenSyntheticBroker(broker, ledger.env)
