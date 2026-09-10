#!/usr/bin/env python3
"""Before/after safety probe for the second Astra pass (A01-A20).

Runs the same scenarios against whichever tree it is placed in and prints
one machine-readable line per finding:

    A13 SAFE|UNSAFE|NOT_APPLICABLE <observable>

``SAFE`` means the scenario failed closed: nothing became visible before it
was durable, no refused action left a side effect, no non-finite value
passed a guard, no stale view stayed authoritative, no contradictory
evidence read as agreement.

``NOT_APPLICABLE`` is printed -- never faked into a pass -- when the tree
under test has no API that can express the case at all. Compatibility is
not invented: a finding that cannot be posed on the old tree says so.

Run it in a detached checkout of the rejected SHA for the BEFORE column and
in the working tree for the AFTER column:

    git worktree add /tmp/before 3af848e
    python3 /tmp/before/tools/astra_a13_a20_probe.py --json > before.json
    python3 tools/astra_a13_a20_probe.py --json > after.json

No network, no credentials, no broker writes: every scenario uses a
synthetic in-process client and a throwaway DATA_DIR.
"""
import argparse
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULTS = {}
PRE_AT = "2026-09-07T18:01:19Z"


def record(name, safe, observable):
    RESULTS[name] = {"verdict": "SAFE" if safe else "UNSAFE",
                     "observable": observable}


def skip(name, why):
    RESULTS[name] = {"verdict": "NOT_APPLICABLE", "observable": why}


class Client:
    env = "prod"
    base_url = "https://api.elections.kalshi.com/trade-api/v2"
    key_id = "probe-key"

    def __init__(self, orders=(), positions=()):
        self.orders, self.positions = list(orders), list(positions)
        self.create_calls = 0

    def list_orders(self, **kw):
        return list(self.orders)

    def get_positions(self):
        return list(self.positions)

    def get_positions_proof(self, **kw):
        return {"rows": list(self.positions), "complete": True, "pages": 1,
                "cursors": [], "reason": None}

    def create_order(self, *a, **kw):
        self.create_calls += 1
        return {"order_id": "synthetic", "status": "executed",
                "remaining_count": 0, "fill_count": 1, "taker_fill_count": 1,
                "taker_fill_cost": 40, "taker_fees": 0,
                "client_order_id": kw.get("client_order_id")}

    def get_order(self, order_id, **kw):
        return {"order_id": order_id, "status": "executed", "fill_count": 1,
                "taker_fill_count": 1, "remaining_count": 0,
                "taker_fill_cost": 40, "taker_fees": 0}

    def get_fills(self, order_id, **kw):
        return [{"fill_id": "f-1", "order_id": order_id, "count": 1,
                 "yes_price": 40, "is_taker": True}]

    def cancel_order(self, *a, **kw):
        return {}


class Box:
    """A throwaway DATA_DIR with the production classes pointed at it."""

    def __init__(self, client=None):
        from config import CFG
        self.tmp = tempfile.mkdtemp(prefix="probe-")
        self._old = CFG.DATA_DIR
        CFG.DATA_DIR = self.tmp
        self._mode = getattr(CFG, "RISK_EQUITY_MODE", None)
        try:
            CFG.RISK_EQUITY_MODE = "strategy"
        except Exception:                                   # noqa: BLE001
            pass
        from persistence import PersistenceSentinel
        PersistenceSentinel.reset()
        self.client = client or Client()

    def stack(self):
        from position_manager import PositionManager
        from trade_logger import TradeLogger
        tlog = TradeLogger()
        pos = PositionManager(self.client, tlog)
        return tlog, pos

    def ledger(self, tlog, pos, env="prod"):
        from equity_ledger import EquityLedger
        return EquityLedger(tlog, pos, env=env)

    def seeded(self, env="prod", cash=10.0):
        tlog, pos = self.stack()
        led = self.ledger(tlog, pos, env=env)
        prop = led.propose_seed(cash, PRE_AT, "evidence-ref", cash)
        led.apply_seed(prop, prop["sha256"])
        return led, tlog, pos

    def reconciled(self, cash=10.0):
        led, tlog, pos = self.seeded(cash=cash)
        att = led.propose_attestation("OPS-A", "a" * 64)
        led.apply_attestation("OPS-A", "a" * 64, att["token"])
        return led, tlog, pos

    def trade(self, tlog, ticker="KXBTC15M-X", order_id=None):
        return tlog.open_trade(
            ticker=ticker, market_title="m", side="yes", req_price=50,
            avg_price=50, req_count=6, filled_count=6, spread=1, fees=0.0,
            edge=0.1, ev=0.1, confidence=8, grade="A", reason="r",
            analysis={}, order_id=order_id or ("o-" + ticker),
            order_status="executed")

    def close(self):
        from config import CFG
        CFG.DATA_DIR = self._old
        if self._mode is not None:
            CFG.RISK_EQUITY_MODE = self._mode
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── A13 ────────────────────────────────────────────────────────────────
def _race_child(tmp, path, generation, barrier, results, index):
    """Astra's interleaving, made deterministic.

    Child 0 PAUSES after the generation is read -- the exact "writer A
    pauses after validation, writer B commits, writer A resumes" scenario.
    The pause is injected into the read, not into the decision logic, so it
    changes timing only. Under a real fence the pause happens INSIDE the
    critical section and child 1 waits; without one, child 1 slips through
    and both commit the same generation.
    """
    import time
    from config import CFG
    CFG.DATA_DIR = tmp
    import persistence
    from persistence import JsonStore
    if index == 0:
        real_read = persistence.read_generation

        def slow_read(*a, **kw):
            value = real_read(*a, **kw)
            time.sleep(0.75)
            return value
        persistence.read_generation = slow_read
    else:
        time.sleep(0.15)          # let child 0 get into its read first
    barrier.wait()
    ok = JsonStore.save(path, {"w": index}, expect_generation=generation)
    results[index] = 1 if ok else 0


def probe_a13():
    from persistence import JsonStore, read_generation
    box = Box()
    try:
        path = os.path.join(box.tmp, "raced.json")
        JsonStore.save(path, {"v": 0}, expect_generation=0)
        ctx = mp.get_context("fork")
        barrier, results = ctx.Barrier(2), ctx.Array("i", [0, 0])
        procs = [ctx.Process(target=_race_child,
                             args=(box.tmp, path, 1, barrier, results, i))
                 for i in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
        winners = sum(results)
        record("A13", winners <= 1,
               f"processes_that_advanced_generation_1={winners} "
               f"final_generation={read_generation(path)}")
    finally:
        box.close()


# ── A14 ────────────────────────────────────────────────────────────────
def probe_a14():
    from unittest.mock import patch
    from persistence import JsonStore
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        if not hasattr(led, "_commit"):
            return skip("A14", "no _commit in this tree")
        seen = {}
        real = JsonStore.save

        def slow(path, data, *a, **kw):
            seen["hwm_during_commit"] = led.risk_equity_reference()
            seen["hold_during_commit"] = led.state.get("capital_hold")
            return real(path, data, *a, **kw)

        hwm_before = led.risk_equity_reference()
        prepared = json.loads(json.dumps(led.state))
        prepared["hwm"] = {"risk_equity_reference": 7.0, "at": "x",
                           "rebased_from": None,
                           "floor_from_settled_index": 0}
        prepared["capital_hold"] = {"reason": "probe", "since": "x",
                                    "rebase_id": "r", "released_by": None}
        with patch.object(JsonStore, "save", side_effect=slow):
            led._commit(prepared)
        during = seen.get("hwm_during_commit")
        record("A14", during == hwm_before and
               seen.get("hold_during_commit") is None,
               f"hwm_before={hwm_before} hwm_visible_during_commit={during} "
               f"hold_visible_during_commit={seen.get('hold_during_commit')}")
    finally:
        box.close()


# ── A15 ────────────────────────────────────────────────────────────────
def probe_a15():
    from unittest.mock import patch
    from persistence import JsonStore
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        led.state["capital_hold"] = {"reason": "post_rebase_validation",
                                     "since": "t", "rebase_id": "rb-1",
                                     "released_by": None}
        led.state["rebases"].append({"rebase_id": "rb-1",
                                     "operator_action_id": "OPS-R",
                                     "validation": None})
        led.save()
        prop = led.propose_hold_release("OPS-REL", "vref")
        with patch.object(JsonStore, "save", return_value=False):
            applied = led.apply_hold_release("OPS-REL", "vref", prop["token"])
        hold_after = led.state.get("capital_hold")
        record("A15", (not applied) and hold_after is not None,
               f"refused={not applied} hold_still_in_memory="
               f"{hold_after is not None}")
    finally:
        box.close()


# ── A16 ────────────────────────────────────────────────────────────────
def probe_a16():
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        led.observe(float("nan"), cycle_n=1, quiet=True)
        eligible = led.capital_eligible()
        persisted_nan = False
        try:
            led.state["hwm"]["risk_equity_reference"] = float("nan")
            persisted_nan = bool(led.save())
        except Exception:                                   # noqa: BLE001
            persisted_nan = False
        record("A16", (not eligible) and (not persisted_nan),
               f"capital_eligible_after_nan_balance={eligible} "
               f"nan_hwm_persisted={persisted_nan}")
    finally:
        box.close()


# ── A17 ────────────────────────────────────────────────────────────────
def probe_a17():
    """Is an UNREADABLE intent state distinguishable from an EMPTY one?

    Deliberately NOT probed by attempting a submission: reaching the
    transport would require arming a promotion/authorization variable, and
    no probe in this repository is allowed to do that. The defect is
    observable one layer earlier and more precisely: after a corrupt
    ``pending_intents.json``, does anything in the process know that the
    state is unknown rather than empty?
    """
    box = Box()
    try:
        from config import _p
        from order_manager import OrderManager
        from persistence import PersistenceSentinel
        box.client.env = "demo"
        tlog, pos = box.stack()
        with open(_p(OrderManager.PENDING_FILE), "w", encoding="utf-8") as fh:
            fh.write('{"KXA": {"client_order_id": "alpha_x", "count": 1')
        om = OrderManager(box.client)
        looks_empty = (om.pending_intents == {})
        knows = bool(getattr(om, "intent_recovery_required", None))
        trustworthy = getattr(om, "intent_state_trustworthy", None)
        refuses = (not trustworthy()) if callable(trustworthy) else False
        sentinel_tripped = not PersistenceSentinel.healthy()
        safe = (knows or refuses or sentinel_tripped)
        record("A17", safe,
               f"loaded_as_empty_dict={looks_empty} "
               f"recovery_required_flag={knows} refuses_submission={refuses} "
               f"sentinel_tripped={sentinel_tripped}")
    finally:
        box.close()


# ── A18 ────────────────────────────────────────────────────────────────
def probe_a18():
    from unittest.mock import patch
    box = Box()
    try:
        from continuity import CONTINUITY_FILE, ChainError, ContinuityChain
        chain = ContinuityChain(os.path.join(box.tmp, CONTINUITY_FILE))
        real_write = os.write
        truncated = {"done": False}

        def short_write(fd, buf):
            if not truncated["done"] and len(buf) > 4:
                truncated["done"] = True
                return real_write(fd, bytes(buf[:2]))     # partial record
            return real_write(fd, buf)

        reported_success = False
        try:
            with patch("os.write", side_effect=short_write):
                chain.append("evidence", {"settled_count": 1,
                                          "strategy_equity": 1.0}, "t")
            reported_success = True
        except ChainError:
            reported_success = False
        except Exception:                                   # noqa: BLE001
            reported_success = False
        # The observable is not "is the file still parseable" -- a torn tail
        # is deliberately tolerated as a crash artefact. It is whether the
        # append REPORTED success for a record that is not actually there.
        recorded = 0
        try:
            recorded = len(chain.records())
        except ChainError:
            recorded = -1
        lied = bool(reported_success) and recorded <= 0
        record("A18", not lied,
               f"short_write_reported_success={reported_success} "
               f"records_actually_present={recorded}")
    finally:
        box.close()


# ── A19 ────────────────────────────────────────────────────────────────
def probe_a19():
    box = Box()
    try:
        led_a, tlog, pos = box.reconciled()
        led_a.save()
        led_b = box.ledger(*box.stack())
        led_b.save()                       # a second writer advances it
        eligible = led_a.capital_eligible()
        current = None
        if hasattr(led_a, "authority_is_current"):
            current = led_a.authority_is_current()[0]
        record("A19", not eligible,
               f"stale_view_capital_eligible={eligible} "
               f"authority_is_current={current}")
    finally:
        box.close()


# ── A20 ────────────────────────────────────────────────────────────────
def probe_a20():
    box = Box()
    try:
        led, tlog, pos = box.seeded(env="demo")
        att = led.propose_attestation("OPS-A", "a" * 64)
        led.apply_attestation("OPS-A", "a" * 64, att["token"])
        led.save()
        demo_eligible = led.capital_eligible()
        tlog2, pos2 = box.stack()
        try:
            import account_binding
            from equity_ledger import EquityLedger
            prod = account_binding.fingerprint(
                env="prod",
                base_url="https://api.elections.kalshi.com/trade-api/v2",
                key_id="other-key")
            reloaded = EquityLedger(tlog2, pos2, env="prod", binding=prod)
        except ImportError:
            reloaded = box.ledger(tlog2, pos2, env="prod")
        eligible = reloaded.capital_eligible()
        status = getattr(reloaded, "binding_status", "no-binding-concept")
        record("A20", not eligible,
               f"demo_state_capital_eligible_in_demo={demo_eligible} "
               f"same_state_under_prod_capital_eligible={eligible} "
               f"binding_status={status}")
    finally:
        box.close()


# ── A02 ────────────────────────────────────────────────────────────────
def probe_a02():
    box = Box(client=Client(positions=[{"ticker": "KXA", "position": 1},
                                       {"ticker": "KXA", "position": -1}]))
    try:
        tlog, pos = box.stack()
        report = pos.verify_against_broker()
        record("A02", report.get("status") != "MATCH",
               f"contradictory_rows_verdict={report.get('status')}")
    finally:
        box.close()


# ── A04 ────────────────────────────────────────────────────────────────
def probe_a04():
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        t = box.trade(tlog, order_id="BROKER-ORDER-1")
        tlog.settle_trade(t["trade_id"], "yes", True, 5.0, 5.0)
        replay = dict(tlog.trades[-1])
        replay["trade_id"] = "new-local-id"
        tlog.trades.append(replay)
        tlog.flush()
        dupes = led.duplicate_events()
        record("A04", bool(dupes) and not led.capital_eligible(),
               f"replay_under_new_local_id_detected={bool(dupes)}")
    finally:
        box.close()


# ── A06 ────────────────────────────────────────────────────────────────
def probe_a06():
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        for cycle in range(1, 8):
            led.observe(10.0 - 2.0, cycle_n=cycle, quiet=False)
        eligible = led.capital_eligible()
        record("A06", not eligible,
               f"adverse_residual_in_noisy_window_capital_eligible={eligible}")
    finally:
        box.close()


# ── A07 ────────────────────────────────────────────────────────────────
def probe_a07():
    box = Box()
    try:
        tlog, pos = box.stack()
        led = box.ledger(tlog, pos)
        prop = led.propose_seed(10.0, PRE_AT, "evidence-ref", 10.0)
        from unittest.mock import patch
        from persistence import JsonStore
        # Astra's remaining half: the durable write fails, and the migration
        # must not exist in memory either. A ledger that believes it is
        # seeded while the disk says otherwise is half-migrated.
        with patch.object(JsonStore, "save", return_value=False):
            applied = led.apply_seed(prop, prop["sha256"])
        seeded_in_memory = bool(led.seeded)
        record("A07", (not applied) and (not seeded_in_memory),
               f"apply_reported={applied} "
               f"seeded_in_memory_after_failed_write={seeded_in_memory}")
    finally:
        box.close()


# ── A08 ────────────────────────────────────────────────────────────────
def probe_a08():
    box = Box()
    cwd = os.getcwd()
    try:
        import model_gatekeeper as MG
        os.chdir(box.tmp)
        with open("model_validation.json", "w", encoding="utf-8") as fh:
            fh.write('{"approved": false, "approved": true, '
                     '"model_version": "v", "generated_ts": 1}')
        raised = None
        try:
            allowed, _ = MG.check_live_allowed()
        except Exception as e:                              # noqa: BLE001
            allowed, raised = False, f"{type(e).__name__}"
        with open("model_validation.json", "w", encoding="utf-8") as fh:
            json.dump({"approved": True, "model_version": "v",
                       "generated_ts": 1, "criteria": ["oops"]}, fh)
        crashed = None
        try:
            MG.check_live_allowed()
        except Exception as e:                              # noqa: BLE001
            crashed = f"{type(e).__name__}: {e}"
        record("A08", (not allowed) and crashed is None,
               f"duplicate_key_allowed_live={allowed} "
               f"malformed_criterion_raised={crashed}")
    finally:
        os.chdir(cwd)
        box.close()


# ── A09 ────────────────────────────────────────────────────────────────
def probe_a09():
    """Does the intent read-back bind the WHOLE intent, or only its id?

    Probed at the verification function itself rather than through a
    submission, for the same reason as A17: no probe arms an authorization
    variable. The scenario is Astra's -- the row on disk carries the right
    deterministic client_order_id but a DIFFERENT size, so a recovery driven
    by it would ask the broker about the wrong order.
    """
    from unittest.mock import patch
    box = Box()
    try:
        from order_manager import OrderManager
        from persistence import JsonStore
        box.client.env = "demo"
        tlog, pos = box.stack()
        om = OrderManager(box.client)
        ticker, coid = "KXA", "alpha_deterministic"
        om.pending_intents[ticker] = {"client_order_id": coid, "count": 1,
                                      "price": 40, "at": "t",
                                      "resolution": None}
        om._flush_pending_intents()
        stale_row = {ticker: {"client_order_id": coid, "count": 999,
                              "price": 999, "at": "t", "resolution": None}}

        def wrong(path, default, *a, **kw):
            if os.path.basename(path) == OrderManager.PENDING_FILE:
                return dict(stale_row)
            return JsonStore.load.__wrapped__(path, default, *a, **kw) \
                if hasattr(JsonStore.load, "__wrapped__") else default

        detected = None
        with patch.object(JsonStore, "load", side_effect=wrong):
            with patch.object(JsonStore, "load_reporting",
                              side_effect=lambda p, d: (dict(stale_row), None),
                              create=True):
                # Pass what THIS tree's read-back expects. Handing a dict
                # to the old (ticker, client_order_id) signature would make
                # the string comparison fail for the wrong reason and report
                # a detection that never happened.
                binds_whole_intent = hasattr(OrderManager,
                                             "INTENT_BOUND_FIELDS")
                argument = (om.pending_intents[ticker] if binds_whole_intent
                            else coid)
                detected = not om._verify_intent_durable(ticker, argument)
        record("A09", bool(detected),
               f"readback_with_wrong_count_detected={detected}")
    finally:
        box.close()


# ── A10 ────────────────────────────────────────────────────────────────
def probe_a10():
    from unittest.mock import patch
    from persistence import JsonStore
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        t = box.trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=1 + i, quiet=True)
        hwm_before = led.risk_equity_reference()
        hold_before = led.state.get("capital_hold")
        status_before = led.derive_status()
        if not hasattr(led, "_commit"):
            return skip("A10", "no _commit in this tree")
        prepared = json.loads(json.dumps(led.state))
        prepared["hwm"] = {"risk_equity_reference": 1.0, "at": "x",
                           "rebased_from": "rb", "floor_from_settled_index": 0}
        prepared["capital_hold"] = {"reason": "post_rebase_validation",
                                    "since": "x", "rebase_id": "rb",
                                    "released_by": None}
        with patch.object(JsonStore, "save", return_value=False):
            led._commit(prepared)
        unchanged = (led.risk_equity_reference() == hwm_before
                     and led.state.get("capital_hold") == hold_before
                     and led.derive_status() == status_before)
        record("A10", unchanged,
               f"failed_rebase_left_state_unchanged={unchanged} "
               f"hwm={led.risk_equity_reference()} (was {hwm_before})")
    finally:
        box.close()


# ── A11 ────────────────────────────────────────────────────────────────
def probe_a11():
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        for cycle in range(1, 6):
            led.observe(10.0 - 25.0, cycle_n=cycle, quiet=True)
        for cycle in range(400, 406):
            led.observe(10.0 - 50.0, cycle_n=cycle, quiet=True)
        rows = [f for f in led.state["flows"] if f.get("kind") == "unclassified"]
        record("A11", len(rows) >= 2,
               f"distinct_equal_movements_recorded={len(rows)}")
    finally:
        box.close()


# ── A01 ────────────────────────────────────────────────────────────────
def probe_a01():
    box = Box()
    try:
        from config import CFG
        import equity_ledger as EL
        led, tlog, pos = box.reconciled()
        before = {}
        for name in os.listdir(box.tmp):
            path = os.path.join(box.tmp, name)
            if os.path.isfile(path):
                before[name] = open(path, "rb").read()
        t = box.trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=1 + i, quiet=True)
        led.save()
        for name in (EL.LEDGER_FILE, CFG.TRADES_FILE):
            if name in before:
                with open(os.path.join(box.tmp, name), "wb") as fh:
                    fh.write(before[name])
        tlog2, pos2 = box.stack()
        again = box.ledger(tlog2, pos2)
        record("A01", not again.capital_eligible(),
               f"coordinated_restore_capital_eligible="
               f"{again.capital_eligible()} guards={again.guards()}")
    finally:
        box.close()


# ── A03 ────────────────────────────────────────────────────────────────
def probe_a03():
    box = Box()
    try:
        led, tlog, pos = box.reconciled()
        if not hasattr(led, "_source_versions"):
            return skip("A03", "no source-version binding in this tree")
        bind = led._source_versions()
        t = box.trade(tlog, ticker="KXDRIFT")
        tlog.settle_trade(t["trade_id"], "no", False, -1.0, -1.0)
        prepared = json.loads(json.dumps(led.state))
        committed = led._commit(prepared, bind=bind)
        record("A03", not committed,
               f"commit_against_moved_evidence={committed}")
    finally:
        box.close()


PROBES = [probe_a01, probe_a02, probe_a03, probe_a04, probe_a06, probe_a07,
          probe_a08, probe_a09, probe_a10, probe_a11, probe_a13, probe_a14,
          probe_a15, probe_a16, probe_a17, probe_a18, probe_a19, probe_a20]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    import logging
    logging.disable(logging.CRITICAL)
    for probe in PROBES:
        name = probe.__name__[-3:].upper()
        try:
            probe()
        except Exception as e:                              # noqa: BLE001
            record(name, False, f"probe raised {type(e).__name__}: {e}")
    if args.json:
        print(json.dumps(RESULTS, indent=1, sort_keys=True))
    else:
        for name in sorted(RESULTS):
            r = RESULTS[name]
            print(f"{name} {r['verdict']:15s} {r['observable']}")
    unsafe = [n for n, r in RESULTS.items() if r["verdict"] == "UNSAFE"]
    return 1 if unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
