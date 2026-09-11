"""Synthetic interaction tests for the A01–A20 transaction redesign.

No sockets or broker mutation methods are used. Independent checkpoints live
in the test process outside the restored directory, never in engine code.
"""
import copy
import errno
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from config import CFG, _p
from continuity import ContinuityChain, ChainError, KIND_EVIDENCE
from continuity_authority import account_identity
from equity_ledger import EquityLedger
from execution_engine import equity_rebase_context, _broker_open_order_ids
from order_manager import OrderManager
from persistence import JsonStore, PersistenceSentinel
from position_manager import PositionManager
from risk_manager import RiskManager
from state_authority import (Transaction, WriterLease, AuthorityError, manifest,
                             checkpoint, recovery_problem, write_all)
from strict_data import loads, dumps, finite_number
from trade_logger import TradeLogger


from authority_fixtures import CheckpointStore, FrozenSyntheticBroker


class IndependentCheckpoint(CheckpointStore):
    """Independent CAS double using a separately pinned signing authority."""


class ReadBroker:
    env = "prod"
    def __init__(self):
        self.positions, self.orders = [], []
        self.epoch = 0
        self.execution_freeze = None
    def get_positions_proof(self):
        return {"complete": True, "rows": copy.deepcopy(self.positions), "pages": 1}
    def get_positions(self):
        return copy.deepcopy(self.positions)
    def list_orders(self):
        return copy.deepcopy(self.orders)


class SyntheticFreeze(FrozenSyntheticBroker):
    """The synthetic broker exposes a signed epoch; real adapters expose none."""
    def __init__(self, broker, identity):
        super().__init__(broker, identity=identity)


def writer_race(path, barrier, queue):
    from persistence import JsonStore, PersistenceSentinel
    PersistenceSentinel.reset()
    barrier.wait(timeout=10)
    queue.put(JsonStore.save(path, {"value": os.getpid()}, expect_generation=1))


def crash_writer(path, point):
    import state_authority as sa
    original = os.replace
    def replace(src, dst):
        original(src, dst)
        if str(dst).endswith(point):
            os._exit(73)
    os.replace = replace
    JsonStore.save(path, {"value": "after"}, expect_generation=1)
    os._exit(0)


class AuthorityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-authority-test-")
        self.addCleanup(self.tmp.cleanup)
        for key, value in (("DATA_DIR", self.tmp.name), ("BROKER_ACCOUNT_ID", "account-A"),
                           ("REQUIRE_PERSISTENT_STATE", False), ("RISK_EQUITY_MODE", "strategy")):
            p = patch.object(CFG, key, value)
            p.start()
            self.addCleanup(p.stop)
        PersistenceSentinel.reset()
        self.addCleanup(PersistenceSentinel.reset)
        for name, value in (("kalshi_trades.json", []), ("positions_state.json", {}),
                            ("orders_state.json", {}), ("pending_intents.json", {}),
                            ("submission_guard.json", {})):
            self.assertTrue(JsonStore.save(_p(name), value))
        self.broker = ReadBroker()
        self.tlog = TradeLogger()
        self.pos = PositionManager(self.broker, self.tlog)
        self.orders = OrderManager(self.broker)
        self.ledger = EquityLedger(self.tlog, self.pos, account_id="account-A")
        prop = self.ledger.propose_seed(10., "2026-09-01T00:00:00Z", "synthetic", 10.)
        self.assertTrue(self.ledger.apply_seed(prop, prop["sha256"]))
        att = self.ledger.propose_attestation("initial-attest", "a" * 64)
        self.assertTrue(self.ledger.apply_attestation("initial-attest", "a" * 64, att["token"]))
        self.risk = RiskManager(self.tlog, self.pos, 10.)
        self.risk.equity = self.ledger

    def prove(self):
        initial = checkpoint(self.ledger.path, self.ledger.identity)
        self.authority = IndependentCheckpoint(initial)
        self.ledger = EquityLedger(self.tlog, self.pos, account_id="account-A",
                                   authority=self.authority)
        self.risk.equity = self.ledger
        self.assertTrue(self.ledger.capital_eligible(), self.ledger.guards())

    def loss(self, amount=-3., oid="order-1"):
        t = self.tlog.open_trade(ticker="KXBTC15M-X", market_title="synthetic", side="yes",
            req_price=50, avg_price=50, req_count=6, filled_count=6, spread=1, fees=0.,
            edge=.1, ev=.1, confidence=8, grade="A", reason="test", analysis={},
            order_id=oid, order_status="executed")
        self.tlog.settle_trade(t["trade_id"], "no", False, amount, amount)
        self.ledger.observe(10. + amount)
        return t

    def rebase(self):
        self.broker.execution_freeze = SyntheticFreeze(self.broker, self.ledger.identity)
        ctx = equity_rebase_context(self.broker, self.orders, self.pos, self.risk, self.ledger)
        prop = self.ledger.propose_rebase("synthetic acknowledgement", "rebase-1")
        return prop, ctx

    def image(self):
        return {p.name: p.read_bytes() for p in Path(self.tmp.name).iterdir() if p.is_file()}


class TransactionInteractions(AuthorityCase):
    def test_external_authority_absent_blocks_even_attested_state(self):
        self.assertFalse(self.ledger.capital_eligible())
        self.assertIn("external_continuity_unproven", self.ledger.guards())

    def test_independent_checkpoint_accepts_monotonic_journal_and_ledger(self):
        self.prove()
        self.loss()
        self.assertAlmostEqual(self.ledger.drawdown_pct(), 30.)
        self.assertNotIn("external_continuity_unproven", self.ledger.guards())

    def test_whole_volume_restore_cannot_rewind_external_checkpoint(self):
        self.prove()
        old = self.image()
        self.loss()
        for p in Path(self.tmp.name).iterdir():
            if p.is_file() and p.name not in old:
                p.unlink()
        for name, data in old.items():
            Path(self.tmp.name, name).write_bytes(data)
        loaded = EquityLedger(TradeLogger(), self.pos, account_id="account-A", authority=self.authority)
        self.assertFalse(loaded.capital_eligible())
        self.assertIn("external_continuity_unproven", loaded.guards())

    def test_rebase_requires_broker_execution_freeze(self):
        self.loss()
        p = self.ledger.propose_rebase("losses", "r")
        ctx = equity_rebase_context(self.broker, self.orders, self.pos, self.risk, self.ledger)
        self.assertFalse(self.ledger.apply_rebase("losses", "r", p["token"], ctx))
        self.assertEqual(self.ledger.risk_equity_reference(), 10.)

    def test_valid_frozen_rebase_commits_and_restart_retains_hold(self):
        self.loss()
        p, ctx = self.rebase()
        self.assertTrue(self.ledger.apply_rebase(p["reason"], "rebase-1", p["token"], ctx))
        self.assertEqual(self.ledger.risk_equity_reference(), 7.)
        loaded = EquityLedger(TradeLogger(), self.pos)
        self.assertIsNotNone(loaded.state["capital_hold"])
        self.assertFalse(loaded.capital_eligible())

    def test_failed_hold_release_cannot_clear_published_hold(self):
        self.loss()
        p, ctx = self.rebase()
        self.assertTrue(self.ledger.apply_rebase(p["reason"], "rebase-1", p["token"], ctx))
        release = self.ledger.propose_hold_release("release-2", "synthetic validation")
        before = copy.deepcopy(self.ledger.state)
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(self.ledger.apply_hold_release("release-2", "synthetic validation", release["token"]))
        self.assertEqual(self.ledger.state, before)

    def test_observer_thread_sees_old_hwm_until_commit_finishes(self):
        self.loss()
        p, ctx = self.rebase()
        entered, release = threading.Event(), threading.Event()
        original = JsonStore.save
        def delayed(path, data, **kw):
            if path == self.ledger.path:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test handshake timed out")
            return original(path, data, **kw)
        result = []
        with patch.object(JsonStore, "save", side_effect=delayed):
            worker = threading.Thread(target=lambda: result.append(self.ledger.apply_rebase(
                p["reason"], "rebase-1", p["token"], ctx)))
            worker.start()
            self.assertTrue(entered.wait(5))
            observed = self.ledger.risk_equity_reference()
            eligible = self.ledger.capital_eligible()
            release.set()
            worker.join(5)
        self.assertEqual(observed, 10.)
        self.assertFalse(eligible)
        self.assertEqual(result, [True])

    def test_nested_journal_writer_cannot_race_migration(self):
        # Unseeded second economic root, preserving exact proposal assertions.
        root = Path(self.tmp.name, "migration")
        root.mkdir()
        with patch.object(CFG, "DATA_DIR", str(root)):
            tlog = TradeLogger()
            led = EquityLedger(tlog, self.pos)
            proposal = led.propose_seed(10., "2026-09-01", "synthetic", 10.)
            before = copy.deepcopy(led.state)
            original = led._seed_dict
            def during(*args, **kwargs):
                tlog.open_trade(ticker="X", market_title="x", side="yes", req_price=50,
                    avg_price=50, req_count=1, filled_count=1, spread=1, fees=0., edge=0., ev=0.,
                    confidence=1, grade="A", reason="x", analysis={}, order_id="late", order_status="filled")
                return original(*args, **kwargs)
            with patch.object(led, "_seed_dict", side_effect=during):
                self.assertFalse(led.apply_seed(proposal, proposal["sha256"]))
            self.assertEqual(led.state, before)

    def test_equal_distinct_withdrawals_and_repeated_observations(self):
        for cycle in range(12):
            self.ledger.observe(9., cycle_n=cycle)
        self.assertEqual(len(self.ledger.unclassified_flows()), 1)
        for cycle in range(12, 24):
            self.ledger.observe(8., cycle_n=cycle)
        self.assertEqual(len(self.ledger.unclassified_flows()), 2)
        self.assertEqual(sum(f["amount"] for f in self.ledger.unclassified_flows()), -2.)
        self.assertAlmostEqual(self.ledger.drawdown_pct(), 20.)

    def test_replayed_broker_order_does_not_publish_second_positive_event(self):
        t = self.loss(3.)
        before = copy.deepcopy(self.tlog.trades)
        with self.assertRaises(ValueError):
            self.loss(3.)
        self.assertEqual(self.tlog.trades, before)

    def test_stale_reader_blocks_before_reload(self):
        self.prove()
        stale = EquityLedger(TradeLogger(), self.pos, authority=self.authority)
        self.loss()
        self.assertFalse(stale.capital_eligible())
        self.assertTrue(any("stale" in g or "continuity" in g for g in stale.guards()))

    def test_account_rotation_keeps_nonsecret_identity(self):
        one = account_identity("kalshi", "prod", "account-A")
        with patch.object(CFG, "DEMO_KEY_ID", "rotated-key"):
            two = account_identity("kalshi", "prod", "account-A")
        self.assertEqual(one, two)


class BoundaryInteractions(AuthorityCase):
    pass


def numeric_case(value, quiet):
    def test(self):
        before = copy.deepcopy(self.ledger.state)
        self.ledger.observe(value, quiet=quiet)
        self.assertEqual(self.ledger.state, before)
        self.assertFalse(self.ledger.capital_eligible())
        self.assertTrue(PersistenceSentinel.failure())
    return test


for i, value in enumerate((float("nan"), float("inf"), -float("inf"), True, -1., "NaN")):
    for quiet in (False, True):
        setattr(BoundaryInteractions, f"test_nonfinite_cash_{i}_quiet_{quiet}", numeric_case(value, quiet))


def broker_case(rows):
    def test(self):
        self.broker.positions = rows
        result = self.pos.verify_against_broker()
        self.assertNotEqual(result["status"], "MATCH")
        self.assertIsNotNone(self.pos.reconcile_halt)
    return test


for i, rows in enumerate((
    [{"ticker": "X", "position": 1}, {"ticker": "X", "position": -1}],
    [{"ticker": "X", "position": 0}, {"ticker": "X", "position": 0}],
    [{"ticker": "X", "position": False}], [{"ticker": "X", "position": True}],
    [{"ticker": "X", "position": "NaN"}], [{"ticker": "X", "position": "Infinity"}],
    [{"ticker": "X", "position": "-Infinity"}], [{"ticker": "X", "position": .5}],
    [{"ticker": "X", "position": 1, "quantity": 0}], [{"ticker": 12, "position": 0}],
    [{"ticker": "X", "position": 0, "id": "i"}, {"ticker": "Y", "position": 0, "id": "i"}],
    [None], [{"ticker": "X"}], [{"ticker": "X", "position": []}],
)):
    setattr(BoundaryInteractions, f"test_position_conflict_{i:02d}", broker_case(rows))


def identity_case(environment, account):
    def test(self):
        loaded = EquityLedger(TradeLogger(), self.pos, env=environment, account_id=account)
        self.assertFalse(loaded.capital_eligible())
        self.assertTrue(any("identity" in g for g in loaded.guards()))
    return test


for i, (environment, account) in enumerate((("demo", "account-A"), ("prod", "account-B"),
                                           ("demo", "account-B"), ("prod", ""), ("unknown", "account-A"))):
    setattr(BoundaryInteractions, f"test_restored_identity_{i}", identity_case(environment, account))


class PersistenceInteractions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-durable-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name, "equity_ledger.json"))
        PersistenceSentinel.reset()
        self.addCleanup(PersistenceSentinel.reset)
        self.assertTrue(JsonStore.save(self.path, {"value": "before"}, expect_generation=0))

    def test_multiprocess_generation_has_exactly_one_winner(self):
        ctx = mp.get_context("spawn")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        workers = [ctx.Process(target=writer_race, args=(self.path, barrier, queue)) for _ in range(2)]
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(JsonStore.load(self.path, {})["generation"], 2)

    def test_second_engine_lease_refused(self):
        first = WriterLease(self.path)
        try:
            with self.assertRaises(AuthorityError):
                WriterLease(self.path)
        finally:
            first.close()

    def test_short_writes_and_eintr_complete_exactly(self):
        payload, output = b"financial-evidence" * 7, bytearray()
        calls = [0]
        def short(fd, data):
            calls[0] += 1
            if calls[0] % 3 == 1:
                raise InterruptedError()
            count = min(3, len(data))
            output.extend(data[:count])
            return count
        with patch("os.write", side_effect=short):
            write_all(42, payload)
        self.assertEqual(bytes(output), payload)


def crash_case(suffix):
    def test(self):
        worker = mp.get_context("spawn").Process(target=crash_writer, args=(self.path, suffix))
        worker.start()
        worker.join(15)
        self.assertEqual(worker.exitcode, 73)
        self.assertIsNotNone(recovery_problem(self.path))
        JsonStore.load(self.path, {})
        self.assertFalse(PersistenceSentinel.healthy())
        self.assertFalse(JsonStore.save(self.path, {"value": "overwrite"}, expect_generation=1))
    return test


for i, suffix in enumerate(("state_transaction.pending", "equity_ledger.json", ".sha256", "state_authority.json")):
    setattr(PersistenceInteractions, f"test_actual_process_crash_{i}", crash_case(suffix))


def fault_case(operation, err):
    def test(self):
        with patch(operation, side_effect=OSError(err, "synthetic persistence failure")):
            self.assertFalse(JsonStore.save(self.path, {"value": "after"}, expect_generation=1))
        self.assertFalse(PersistenceSentinel.healthy())
    return test


for i, (operation, err) in enumerate((
    ("os.write", errno.ENOSPC), ("os.write", errno.EACCES),
    ("os.fsync", errno.EIO), ("os.replace", errno.EACCES),
    ("state_authority.fsync_dir", errno.EIO), ("os.open", errno.EACCES),
    ("persistence._fsync_dir", errno.EIO), ("state_authority.remember_file", errno.ENOSPC),
)):
    # remember_file is imported into persistence; target the actual call site.
    if operation == "state_authority.remember_file":
        operation = "persistence.remember_file"
    setattr(PersistenceInteractions, f"test_durable_failure_{i}", fault_case(operation, err))


def strict_case(raw):
    def test(self):
        with self.assertRaises(ValueError):
            loads(raw)
    return test


class StrictInteractions(unittest.TestCase):
    pass


for i, raw in enumerate(('{"a":1,"a":2}', '{"a":{"b":0,"b":1}}', '{"n":NaN}',
                         '{"n":Infinity}', '{"n":-Infinity}', '{"n":1e999}',
                         '[{"ok":true,"ok":false}]', '{"criteria":[],"criteria":null}')):
    setattr(StrictInteractions, f"test_strict_json_{i}", strict_case(raw))
