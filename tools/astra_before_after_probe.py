#!/usr/bin/env python3
"""Before/after probe for the Astra findings A01-A12.

Runs the SAME twelve scenarios against whichever tree it is placed in, using
only APIs that exist in both the rejected candidate (508899b) and the
remediated one, and prints one machine-readable line per finding:

    A01 SAFE|UNSAFE <observable>

"SAFE" means the scenario failed closed: no evidenced loss disappeared, no
absence was fabricated, no unauthorized transition committed. Run it in a
detached checkout of the old SHA to get the BEFORE column and in the working
tree to get the AFTER column.

No network, no credentials, no broker: every scenario uses a synthetic
in-process client and a throwaway DATA_DIR.

    python tools/astra_before_after_probe.py            # human readable
    python tools/astra_before_after_probe.py --json     # one JSON object
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULTS = {}


def record(name, safe, observable):
    RESULTS[name] = {"verdict": "SAFE" if safe else "UNSAFE",
                     "observable": observable}


class Client:
    env = "prod"

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
        return {"order_id": "synthetic", "status": "resting",
                "remaining_count": 1, "fill_count": 0}

    def get_order(self, order_id, **kw):
        return {"order_id": order_id, "status": "resting",
                "remaining_count": 1, "fill_count": 0}

    def get_fills(self, order_id, **kw):
        return []

    def cancel_order(self, order_id):
        return {}


class Sandbox:
    """A throwaway DATA_DIR with the production modules freshly configured."""

    def __enter__(self):
        from config import CFG
        self.tmp = tempfile.mkdtemp(prefix="probe-")
        self.saved = CFG.DATA_DIR
        CFG.DATA_DIR = self.tmp
        from persistence import PersistenceSentinel
        PersistenceSentinel.reset()
        return self

    def __exit__(self, *exc):
        from config import CFG
        from persistence import PersistenceSentinel
        CFG.DATA_DIR = self.saved
        PersistenceSentinel.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def path(self, name):
        return os.path.join(self.tmp, name)

    def digests(self):
        out = {}
        for name in sorted(os.listdir(self.tmp)):
            p = os.path.join(self.tmp, name)
            if os.path.isfile(p):
                import hashlib
                out[name] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        return out


def stack(client=None):
    from position_manager import PositionManager
    from trade_logger import TradeLogger
    client = client or Client()
    tlog = TradeLogger()
    return client, tlog, PositionManager(client, tlog)


def trade(tlog, ticker="KX-A", count=6, price=50):
    return tlog.open_trade(ticker=ticker, market_title="m", side="yes",
                           req_price=price, avg_price=price, req_count=count,
                           filled_count=count, spread=1, fees=0.0, edge=0.1,
                           ev=0.1, confidence=8, grade="A", reason="r",
                           analysis={}, order_id="o-" + ticker,
                           order_status="executed")


def seeded_reconciled(tlog, pos, cash=10.0):
    from equity_ledger import EquityLedger
    led = EquityLedger(tlog, pos, env="prod")
    prop = led.propose_seed(cash, "2026-09-07T18:01:19Z", "ref", cash)
    led.apply_seed(prop, prop["sha256"])
    att = led.propose_attestation("OPS-A", "a" * 64)
    led.apply_attestation("OPS-A", "a" * 64, att["token"])
    return led


def ok_ctx(led, extra=None):
    ctx = {"drawdown_firing": True, "reconcile_status": "MATCH",
           "open_positions": 0, "in_flight_orders": 0, "quiescent": True,
           "evidence_unstable": None,
           "orders": {"local_open": [], "pending_intents": [],
                      "resolution_halt": False, "broker_open": 0,
                      "broker_open_ids": [], "broker_error": None,
                      "disagreement": False}}
    bound = getattr(led, "bound_state", None)
    if callable(bound):
        ctx["bound_state"] = bound()
    ctx.update(extra or {})
    return ctx


# ── A01 ────────────────────────────────────────────────────────────────
def probe_a01():
    from config import CFG, _p
    from equity_ledger import LEDGER_FILE
    from persistence import JsonStore
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=i + 1, quiet=True)
        state = JsonStore.load(_p(LEDGER_FILE), {})
        state.pop("journal_watermark", None)           # the evidence field
        JsonStore.save(_p(LEDGER_FILE), state)
        JsonStore.save(_p(CFG.TRADES_FILE), [])        # the pre-loss journal
        client2, tlog2, pos2 = stack()
        from equity_ledger import EquityLedger
        led2 = EquityLedger(tlog2, pos2, env="prod")
        for c in range(20, 26):
            led2.observe(10.0, cycle_n=c, quiet=True)  # +3 deposit
        dd = led2.drawdown_pct() or 0.0
        eligible = led2.capital_eligible()
        safe = (not eligible) and dd >= 30.0 - 1e-6
        record("A01", safe, f"capital_eligible={eligible} drawdown_pct={dd}")


# ── A02 ────────────────────────────────────────────────────────────────
def probe_a02():
    from unittest.mock import patch
    from kalshi_client import KalshiAPIError, KalshiClient
    c = KalshiClient.__new__(KalshiClient)
    c._raw_logged = set()
    outcomes = {}
    cases = {
        "empty_envelope": {},
        "null_block": {"market_positions": None},
        "renamed_envelope": {"positions": [{"ticker": "KX-A", "position": 3}]},
    }
    for label, payload in cases.items():
        with patch.object(KalshiClient, "_req", return_value=payload):
            try:
                rows = c.get_positions()
            except KalshiAPIError:
                rows = "RAISED"
        outcomes[label] = rows
    pages = [{"market_positions": [{"ticker": "KX-F%d" % i, "position": 0}
                                   for i in range(100)], "cursor": "p2"},
             {"market_positions": [{"ticker": "KX-OPEN", "position": 3}],
              "cursor": ""}]
    it = iter(pages)
    with patch.object(KalshiClient, "_req", side_effect=lambda *a, **k: next(it)):
        try:
            paged = c.get_positions()
        except KalshiAPIError:
            paged = "RAISED"
    fabricated = [k for k, v in outcomes.items() if v == []]
    hidden = paged != "RAISED" and not any(
        p.get("ticker") == "KX-OPEN" for p in (paged or []))
    safe = not fabricated and not hidden
    record("A02", safe,
           f"fabricated_absences={fabricated} page_two_position_hidden={hidden}")


# ── A03 ────────────────────────────────────────────────────────────────
def probe_a03():
    import execution_engine
    from config import CFG, _p
    from order_manager import OrderManager
    from persistence import JsonStore
    from risk_manager import RiskManager
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=i + 1, quiet=True)
        orders = OrderManager(client)
        risk = RiskManager(tlog, pos, capital=10.0)
        risk.equity = led
        try:
            ctx = execution_engine.equity_rebase_context(
                client, orders, pos, risk, equity=led, quiescent=True)
        except TypeError:                       # the old four-argument form
            ctx = execution_engine.equity_rebase_context(client, orders, pos, risk)
            ctx.setdefault("quiescent", True)
            ctx.setdefault("evidence_unstable", None)
        # a SECOND process adds a resting order after the validation
        JsonStore.save(_p(CFG.ORDERS_FILE),
                       {"brk-2": {"order_id": "brk-2", "ticker": "KX-A",
                                  "status": "resting", "remaining_count": 1}})
        hwm_before = led.risk_equity_reference()
        prop = led.propose_rebase("losses acknowledged", "OPS-40")
        applied = led.apply_rebase("losses acknowledged", "OPS-40",
                                   prop["token"], ctx)
        hwm_after = led.risk_equity_reference()
        safe = (not applied) and abs(hwm_after - hwm_before) < 1e-9
        record("A03", safe,
               f"rebase_applied={applied} hwm {hwm_before} -> {hwm_after}")


# ── A04 ────────────────────────────────────────────────────────────────
def probe_a04():
    from config import CFG, _p
    from equity_ledger import EquityLedger
    from persistence import JsonStore
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        win = trade(tlog, ticker="KX-WIN")
        tlog.settle_trade(win["trade_id"], "yes", True, 3.0, 3.0)
        loss = trade(tlog, ticker="KX-LOSS")
        tlog.settle_trade(loss["trade_id"], "no", False, -4.0, -4.0)
        for i in range(3):
            led.observe(9.0, cycle_n=i + 1, quiet=True)
        before = led.drawdown_pct()
        raw = JsonStore.load(_p(CFG.TRADES_FILE), [])
        raw.append(json.loads(json.dumps(
            [r for r in raw if r.get("trade_id") == win["trade_id"]][0])))
        JsonStore.save(_p(CFG.TRADES_FILE), raw)
        client2, tlog2, pos2 = stack()
        led2 = EquityLedger(tlog2, pos2, env="prod")
        after = led2.drawdown_pct()
        safe = after >= before - 1e-9 and not led2.capital_eligible()
        record("A04", safe, f"drawdown_pct {before} -> {after} "
                            f"capital_eligible={led2.capital_eligible()}")


# ── A05 ────────────────────────────────────────────────────────────────
def probe_a05():
    from config import CFG
    from risk_manager import RiskManager
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=i + 1, quiet=True)
        for c in range(10, 16):
            led.observe(100.0, cycle_n=c, quiet=True)      # a +93 deposit
        # `capital` is the live cash the engine passes: the deposit raises it
        risk = RiskManager(tlog, pos, capital=100.0)
        risk.equity = led
        saved = CFG.RISK_EQUITY_MODE
        try:
            CFG.RISK_EQUITY_MODE = "stratgey"              # a typo
            pct = risk.rolling_drawdown_pct()
            eligible = led.capital_eligible()
        finally:
            CFG.RISK_EQUITY_MODE = saved
        safe = pct >= 30.0 - 1e-6 and not eligible
        record("A05", safe,
               f"unknown_mode_drawdown_pct={pct} capital_eligible={eligible}")


# ── A06 ────────────────────────────────────────────────────────────────
def probe_a06():
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        led.observe(9.90, cycle_n=1, quiet=True)           # -0.10, unexplained
        eligible = led.capital_eligible()
        record("A06", not eligible,
               f"capital_eligible={eligible} guards={led.guards()}")


# ── A07 ────────────────────────────────────────────────────────────────
def probe_a07():
    """Two independent sandboxes: a proposal edited after review, and a
    proposal whose evidence moved before it was applied."""
    from equity_ledger import EquityLedger
    with Sandbox():
        client, tlog, pos = stack()
        led = EquityLedger(tlog, pos, env="prod")
        proposal = led.propose_seed(10.0, "2026-09-07T18:01:19Z", "ref", 10.0)
        accepted = proposal["sha256"]
        proposal["hwm_0"] = 5.0                            # safer-looking
        proposal["strategy_equity_0"] = 5.0
        applied_modified = led.apply_seed(proposal, accepted)
        hwm_modified = led.risk_equity_reference() if led.seeded else None
    with Sandbox():
        client, tlog, pos = stack()
        led2 = EquityLedger(tlog, pos, env="prod")
        fresh = led2.propose_seed(10.0, "2026-09-07T18:01:19Z", "ref", 10.0)
        t = trade(tlog, ticker="KX-LATE")                  # settles after
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        applied_stale = led2.apply_seed(fresh, fresh["sha256"])
        dd_stale = led2.drawdown_pct() if led2.seeded else None
    safe = not applied_modified and not applied_stale
    record("A07", safe, f"modified_proposal_applied={applied_modified} "
                        f"(hwm={hwm_modified}) stale_proposal_applied="
                        f"{applied_stale} (drawdown_pct={dd_stale})")


# ── A08 ────────────────────────────────────────────────────────────────
def probe_a08():
    import model_gatekeeper as gate
    tmp = tempfile.mkdtemp(prefix="probe-gate-")
    cwd = os.getcwd()
    saved = {k: os.environ.get(k)
             for k in ("NO_LIVE_PROMOTION", "MODEL_APPROVED_FOR_LIVE")}
    os.environ["NO_LIVE_PROMOTION"] = "0"
    os.environ["MODEL_APPROVED_FOR_LIVE"] = "YES"
    try:
        os.chdir(tmp)
        json.dump({"generated_ts": time.time(), "approved": True,
                   "model_version": "v1"},
                  open("model_validation.json", "w"))
        bad = [
            ("zero_tests", {"generated_ts": time.time(), "ran": 0,
                            "failures": 0, "errors": 0, "skipped": 0}),
            ("nan_timestamp", {"generated_ts": float("nan"), "ran": 10,
                               "failures": 0, "errors": 0, "skipped": 0}),
            ("future_timestamp", {"generated_ts": time.time() + 10 * 86400,
                                  "ran": 10, "failures": 0, "errors": 0,
                                  "skipped": 0}),
            ("missing_counters", {"generated_ts": time.time(), "ran": 10}),
        ]
        accepted = []
        for label, report in bad:
            json.dump(report, open("test_report.json", "w"))
            ok, _ = gate.check_live_allowed()
            if ok:
                accepted.append(label)
        record("A08", not accepted, f"accepted_invalid_evidence={accepted}")
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── A09 ────────────────────────────────────────────────────────────────
def probe_a09():
    """The submission path is DEMO + synthetic transport. ALLOW_ORDER_SUBMISSION
    is enabled inside this sandbox only: without it the earlier
    `submission_disabled` gate answers first and the probe would report a
    refusal it did not cause. No socket is opened either way."""
    from order_manager import OrderManager
    saved = {k: os.environ.get(k) for k in
             ("ALLOW_ORDER_SUBMISSION", "MAX_CONTRACTS_PER_ORDER",
              "DAILY_RESEARCH_ORACLE_APPROVED")}
    os.environ["ALLOW_ORDER_SUBMISSION"] = "true"
    os.environ["DAILY_RESEARCH_ORACLE_APPROVED"] = "true"
    os.environ.setdefault("MAX_CONTRACTS_PER_ORDER", "10")
    try:
        from config import CFG
        prev_allow = getattr(CFG, "ALLOW_ORDER_SUBMISSION", None)
        prev_oracle = getattr(CFG, "DAILY_RESEARCH_ORACLE_APPROVED", None)
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.DAILY_RESEARCH_ORACLE_APPROVED = True
        with Sandbox() as box:
            client = Client()
            client.env = "demo"
            _, tlog, pos = stack(client)
            om = OrderManager(client)
            os.makedirs(box.path(OrderManager.PENDING_FILE), exist_ok=True)
            try:
                # a non-daily ticker: the daily-settlement quarantine is a
                # different, legitimate gate and would answer first
                result = om.place_and_track("KXBTC15M-PROBE", "yes", 1, 40)
                detail = f"result={result.status}"
            except Exception as e:                         # noqa: BLE001
                detail = f"raised={type(e).__name__}"
            record("A09", client.create_calls == 0,
                   f"broker_calls_after_failed_intent_persistence="
                   f"{client.create_calls} {detail}")
    finally:
        if prev_allow is not None:
            CFG.ALLOW_ORDER_SUBMISSION = prev_allow
        if prev_oracle is not None:
            CFG.DAILY_RESEARCH_ORACLE_APPROVED = prev_oracle
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── A10 ────────────────────────────────────────────────────────────────
def probe_a10():
    from unittest.mock import patch
    from persistence import JsonStore
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=i + 1, quiet=True)
        hwm_before = led.risk_equity_reference()
        prop = led.propose_rebase("losses acknowledged", "OPS-50")
        with patch.object(JsonStore, "save", return_value=False):
            applied = led.apply_rebase("losses acknowledged", "OPS-50",
                                       prop["token"], ok_ctx(led))
        hwm_after = led.risk_equity_reference()
        hold = led.state.get("capital_hold")
        safe = (not applied) and abs(hwm_after - hwm_before) < 1e-9 \
            and hold is None
        record("A10", safe, f"applied={applied} hwm {hwm_before} -> "
                            f"{hwm_after} hold={bool(hold)}")


# ── A11 ────────────────────────────────────────────────────────────────
def probe_a11():
    with Sandbox():
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        for i in range(9):
            led.observe(9.0, cycle_n=i + 1, quiet=True)    # ONE -1 movement
        n = len(led.unclassified_flows())
        record("A11", n == 1, f"unclassified_flows_for_one_movement={n}")


# ── A12 ────────────────────────────────────────────────────────────────
def probe_a12():
    import subprocess
    from config import CFG, _p
    from persistence import JsonStore
    with Sandbox() as box:
        client, tlog, pos = stack()
        led = seeded_reconciled(tlog, pos)
        t = trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        for i in range(3):
            led.observe(7.0, cycle_n=i + 1, quiet=True)
        JsonStore.save(_p(CFG.TRADES_FILE), [])            # a restored journal
        before = box.digests()
        env = dict(os.environ, DATA_DIR=box.tmp, PYTHONPATH=ROOT)
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "equity_ledger_tool.py"),
             "status"], cwd=ROOT, env=env, capture_output=True, text=True)
        after = box.digests()
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        record("A12", not changed and proc.returncode == 0,
               f"files_modified_by_status={changed}")


PROBES = [probe_a01, probe_a02, probe_a03, probe_a04, probe_a05, probe_a06,
          probe_a07, probe_a08, probe_a09, probe_a10, probe_a11, probe_a12]


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
        except Exception as e:                             # noqa: BLE001
            record(name, False, f"probe raised {type(e).__name__}: {e}")
    if args.json:
        print(json.dumps(RESULTS, indent=1, sort_keys=True))
    else:
        for name in sorted(RESULTS):
            r = RESULTS[name]
            print(f"{name} {r['verdict']:6s} {r['observable']}")
    return 0 if all(r["verdict"] == "SAFE" for r in RESULTS.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
