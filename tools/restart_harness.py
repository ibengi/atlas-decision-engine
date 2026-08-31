# -*- coding: utf-8 -*-
"""Controlled restart-safety harness on ISOLATED state.

Builds a realistic engine state (submitted order + open position + settled
trade history), tears the objects down (process exit), then rebuilds them
from the same DATA_DIR (restart) and checks every invariant the LIVE canary
depends on.

Run A: DATA_DIR survives  -> models a volume-backed deployment.
Run B: DATA_DIR is wiped  -> models the CURRENT production (no volume).

No network. No production paths. Nothing here touches DEMO or LIVE.
"""
import os
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from persistence import JsonStore, PersistenceSentinel  # noqa: E402

import tempfile
DATA = tempfile.mkdtemp(prefix="atlas_restart_harness_")
TICKER = "KXTEST-CANARY-T1"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def fresh_client(order_id="ord-1"):
    """Mock broker: accepts the order, reports it filled, holds the position."""
    c = MagicMock()
    c.env = "demo"
    c.last_http_status = 201
    c.create_order.return_value = {
        "order_id": order_id, "status": "executed",
        "fill_count": 1, "remaining_count": 0, "ts_ms": 1,
    }
    c.get_order.return_value = {
        "order_id": order_id, "status": "executed",
        "fill_count": 1, "remaining_count": 0,
    }
    c.get_fills.return_value = [{"fill_id": "f1", "count": 1, "price": 40,
                                 "yes_price": 40, "is_taker": True}]
    c.get_positions.return_value = [{"ticker": TICKER, "position": 1,
                                     "avg_price": 40}]
    c.get_market.return_value = {"ticker": TICKER, "status": "active",
                                 "result": ""}
    return c


def build_managers():
    PersistenceSentinel.reset()          # nouveau processus
    bot.CFG.DATA_DIR = DATA
    os.makedirs(DATA, exist_ok=True)
    cli = fresh_client()
    tlog = bot.TradeLogger()
    pm = bot.PositionManager(cli, tlog)
    om = bot.OrderManager(cli)
    rm = bot.RiskManager(tlog, pm, capital=100.0)
    return cli, tlog, pm, om, rm


def seed_history(tlog):
    """Three settled losing trades today -> real risk history to preserve."""
    now = datetime.now(timezone.utc)
    for i in range(3):
        ts = (now - timedelta(minutes=30 - i)).isoformat()
        tlog.trades.append({
            "schema": tlog.SCHEMA, "trade_id": f"hist-{i}",
            "decision_id": None, "timestamp": ts,
            "ticker": f"KXHIST-{i}", "market": "hist", "side": "yes",
            "requested_price": 50, "avg_fill_price": 50,
            "requested_count": 1, "filled_count": 1,
            "spread": 1, "fees": 0.05, "edge": None, "ev": None,
            "confidence": None, "grade": None, "reason": "test",
            "analysis": None, "order_id": None, "order_status": None,
            "state": "settled", "result": "no", "won": False,
            "gross_pnl": -1.0, "net_pnl": -1.05, "roi": None,
            "holding_seconds": None, "settled_at": ts,
        })
    tlog.flush()


print("=" * 72)
print("PRE-STATE: build engine state, then simulate process exit")
print("LIVE-capable configuration: REQUIRE_PERSISTENT_STATE=true;")
print("first boot on a new volume acknowledged with ALLOW_FRESH_STATE=true")
print("=" * 72)
shutil.rmtree(DATA, ignore_errors=True)
bot.CFG.REQUIRE_PERSISTENT_STATE = True
bot.CFG.ALLOW_FRESH_STATE = True         # one-time operator ack
# Une configuration LIVE-capable EXIGE un plafond de contrats explicite
# (contract_cap_config: valeur absente => soumissions desactivees).
bot.CFG.MAX_CONTRACTS_PER_ORDER = "1"
cli, tlog, pm, om, rm = build_managers()
bot.CFG.ALLOW_FRESH_STATE = False        # never implied afterwards
seed_history(tlog)

res = om.place_and_track(TICKER, "yes", 1, 40)
print(f"  order submitted: state={res.state} status={res.status} "
      f"filled={res.filled} order_id={res.order_id}")
pm.positions["t-canary"] = {
    "trade_id": "t-canary", "ticker": TICKER, "side": "yes",
    "count": 1, "count_initial": 1, "avg_price": 40, "fees": 0.05,
    "opened_at": datetime.now(timezone.utc).isoformat(), "state": "open",
    "order_ids": [res.order_id], "fill_ids": ["f1"], "strategy": "test",
}
pm.flush()

pre = {
    "guard_tickers": sorted(om.session_submitted),
    "open_orders": sorted(om.open_orders),
    "journal_len": len(tlog.trades),
    "open_count": pm.open_count(),
    "daily_pnl": round(rm.daily_realized_pnl(), 4),
    "drawdown": round(rm.rolling_drawdown(), 4),
    "consec_losses": rm.consecutive_losses(),
    "trades_today": rm.trades_today(),
    "secs_since_settle": rm.seconds_since_last_settlement() is not None,
}
print(f"  PRE  {pre}")

del cli, tlog, pm, om, rm   # process exit


def restart(label, wipe):
    print()
    print("=" * 72)
    print(f"{label}")
    print("=" * 72)
    if wipe:
        shutil.rmtree(DATA, ignore_errors=True)   # ephemeral disk dies
    cli2, tlog2, pm2, om2, rm2 = build_managers()
    post = {
        "guard_tickers": sorted(om2.session_submitted),
        "open_orders": sorted(om2.open_orders),
        "journal_len": len(tlog2.trades),
        "open_count": pm2.open_count(),
        "daily_pnl": round(rm2.daily_realized_pnl(), 4),
        "drawdown": round(rm2.rolling_drawdown(), 4),
        "consec_losses": rm2.consecutive_losses(),
        "trades_today": rm2.trades_today(),
        "secs_since_settle": rm2.seconds_since_last_settlement() is not None,
    }
    print(f"  POST {post}")
    print()
    tag = "[VOLUME]" if not wipe else "[EPHEMERAL]"
    check(f"{tag} submission_guard survived",
          post["guard_tickers"] == pre["guard_tickers"],
          f"{pre['guard_tickers']} -> {post['guard_tickers']}")
    check(f"{tag} orders_state survived",
          post["open_orders"] == pre["open_orders"],
          f"{pre['open_orders']} -> {post['open_orders']}")
    check(f"{tag} trade journal survived",
          post["journal_len"] == pre["journal_len"],
          f"{pre['journal_len']} -> {post['journal_len']} rows")
    check(f"{tag} positions_state survived",
          post["open_count"] == pre["open_count"],
          f"open_count {pre['open_count']} -> {post['open_count']}")
    check(f"{tag} daily_realized_pnl survived",
          post["daily_pnl"] == pre["daily_pnl"],
          f"{pre['daily_pnl']} -> {post['daily_pnl']}")
    check(f"{tag} rolling_drawdown survived",
          post["drawdown"] == pre["drawdown"],
          f"{pre['drawdown']} -> {post['drawdown']}")
    check(f"{tag} consecutive_losses survived",
          post["consec_losses"] == pre["consec_losses"],
          f"{pre['consec_losses']} -> {post['consec_losses']}")
    check(f"{tag} trades_today survived",
          post["trades_today"] == pre["trades_today"],
          f"{pre['trades_today']} -> {post['trades_today']}")
    check(f"{tag} settlement recency survived",
          post["secs_since_settle"] == pre["secs_since_settle"],
          f"known={post['secs_since_settle']}")

    # IDEMPOTENCY: can the same signal be re-submitted after restart?
    res2 = om2.place_and_track(TICKER, "yes", 1, 40)
    blocked = res2.status == "blocked:duplicate_submission_guard"
    check(f"{tag} restart cannot duplicate a submitted order",
          blocked,
          f"resubmit -> status={res2.status} "
          f"create_order_calls={cli2.create_order.call_count}")

    # RECONCILIATION: broker still holds the position -> must be counted
    rep = pm2.reconcile_with_broker()
    check(f"{tag} unresolved broker position stays counted",
          pm2.open_count() >= 1,
          f"open_count={pm2.open_count()} rebuilt={rep.get('rebuilt')} "
          f"ghost={rep.get('ghost')}")

    # No resurrection: broker flat -> local must go flat
    cli2.get_positions.return_value = []
    pm2.reconcile_with_broker()
    check(f"{tag} closed broker position is not resurrected",
          pm2.open_count() == 0,
          f"open_count={pm2.open_count()}")
    return post


restart("RUN A -- RESTART WITH PERSISTENT DISK (volume mounted)", wipe=False)

print()
print("=" * 72)
print("RUN B -- RESTART AFTER STATE WIPE (LIVE-capable config, fail-closed)")
print("=" * 72)
shutil.rmtree(DATA, ignore_errors=True)   # the disk dies
cliB, tlogB, pmB, omB, rmB = build_managers()
check("[WIPED] sentinel tripped at boot (state marker gone)",
      not PersistenceSentinel.healthy(),
      f"failure={PersistenceSentinel.failure()}")
cliB.create_order.reset_mock()
resB = omB.place_and_track(TICKER, "yes", 1, 40)
check("[WIPED] engine does NOT silently resume trading",
      resB.status == "blocked:persistence_failure",
      f"status={resB.status}")
check("[WIPED] create_order_calls=0 with persistence unavailable",
      cliB.create_order.call_count == 0,
      f"create_order_calls={cliB.create_order.call_count}")
engB = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
engB.posmgr, engB.risk = pmB, rmB
okB, guardB = engB._post_balance_gates()
check("[WIPED] cycle gate blocks with guard=persistence_failure",
      (not okB) and guardB == "persistence_failure",
      f"guard={guardB}")

shutil.rmtree(DATA, ignore_errors=True)

print()
print("=" * 72)
n_pass = sum(1 for _, ok, _ in results if ok)
print(f"TOTAL {n_pass}/{len(results)} checks passed")
print("=" * 72)
for name, ok, detail in results:
    if not ok:
        print(f"  FAILED: {name}  ({detail})")
sys.exit(0 if n_pass == len(results) else 1)
