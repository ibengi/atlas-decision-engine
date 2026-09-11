"""Same synthetic reliability cases for the rejected and remediated trees."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from config import CFG, _p
from persistence import JsonStore, PersistenceSentinel
from position_manager import PositionManager
from risk_manager import RiskManager
from trade_logger import TradeLogger


def incomplete_settlement():
    journal = TradeLogger()
    broker = SimpleNamespace(get_market=lambda _: {"status": "settled", "result": ""})
    positions = PositionManager(broker, journal)
    positions.positions["p"] = {"trade_id": "p", "ticker": "SYNTHETIC", "side": "yes",
        "state": "open", "count": 6, "count_initial": 6, "avg_price": 50,
        "fees": 0., "opened_at": "2020-01-01T00:00:00+00:00", "fill_ids": [], "order_ids": []}
    positions.flush()
    result = positions.check_settlements()
    return positions.open_risk() == 3. and not result, {"retained_exposure": positions.open_risk()}


def unknown_journal():
    JsonStore.save(_p(CFG.TRADES_FILE), [{"schema": "future", "state": "open"}])
    before = Path(_p(CFG.TRADES_FILE)).read_bytes()
    TradeLogger()
    unchanged = Path(_p(CFG.TRADES_FILE)).read_bytes() == before
    return unchanged, {"unknown_history_preserved": unchanged}


def subcent_settlement():
    journal = TradeLogger()
    t = journal.open_trade(ticker="SYNTHETIC", market_title="synthetic", side="yes",
        req_price=50, avg_price=50, req_count=1, filled_count=1, spread=1, fees=0.,
        edge=.1, ev=.1, confidence=8, grade="A", reason="test", analysis={},
        order_id="synthetic-order", order_status="executed")
    journal.settle_trade(t["trade_id"], "no", False, -.0001, -.0001)
    value = TradeLogger().settled_trades()[0]["net_pnl"]
    return value == -.0001, {"persisted_loss": value}


def concurrent_risk_claims():
    rows = [{"net_pnl": -.1, "settled_at": "2020-01-01T00:00:00+00:00"}
            for _ in range(CFG.MAX_CONSECUTIVE_LOSSES)]
    journal = SimpleNamespace(settled_trades=lambda: rows)
    one = RiskManager(journal, None, 10.)
    two = RiskManager(journal, None, 10.)
    first = one.claim_half_open_attempt("first")[0]
    second = two.claim_half_open_attempt("second")[0]
    return first and not second, {"first_claim": first, "second_claim": second}


results = []
for label, fn in (("incomplete_settlement", incomplete_settlement),
                  ("unknown_journal", unknown_journal), ("subcent_settlement", subcent_settlement),
                  ("concurrent_risk_claims", concurrent_risk_claims)):
    previous = CFG.DATA_DIR
    with tempfile.TemporaryDirectory(prefix="atlas-before-after-") as root:
        CFG.DATA_DIR = root
        PersistenceSentinel.reset()
        for name, value in ((CFG.TRADES_FILE, []), (CFG.POSITIONS_FILE, {}),
                            (CFG.ORDERS_FILE, {}), ("pending_intents.json", {}),
                            ("submission_guard.json", {}), ("seen_fill_ids.json", [])):
            JsonStore.save(_p(name), value)
        try:
            passed, detail = fn()
            results.append({"id": label, "status": "PASS" if passed else "FAIL", "detail": detail})
        except Exception as exc:
            results.append({"id": label, "status": "NEEDS_REVIEW", "reason": repr(exc)})
        finally:
            CFG.DATA_DIR = previous
            PersistenceSentinel.reset()
report = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
          "results": results, "real_broker_writes": 0, "capital_enabled": False}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2))
print(json.dumps(report))
sys.exit(0 if all(r["status"] == "PASS" for r in results) else 1)
