# -*- coding: utf-8 -*-
"""AIR-001 Wave 6 (DE-P0-008) — unified projected RiskProof validator.

Reproduced defect: the aggregate risk controls (portfolio total,
correlation group, drawdown throttle) default to 0 = DISABLED, the
single-market and category checks compare only CURRENT exposure (the
proposed order is not projected), and no single artifact proves which
risk checks an order actually passed.

Policy:
- ONE validator evaluates EVERY control against the PROJECTED state
  (current + proposed) and returns a RiskProof listing each check with
  its observed value, limit and status. Nothing is approved by
  omission.
- An aggregate control left at its disabled default is
  DISABLED_BY_DEFAULT and the proof is NOT approved: enabling live
  aggregate limits is an explicit operator decision (governed risk
  policy), never an implicit one. SHADOW observation is unaffected —
  this proof gates the LIVE order path only.
- A stale or unknown input (balance, broker schema, unresolved order
  intents) is UNKNOWN_FAIL_CLOSED — unknown financial state never
  approves an order.
- The proof binds risk_config_hash; its content_hash feeds the order
  write-ahead intent (Wave 4 risk_proof_hash).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Optional

from config import CFG

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN_FAIL_CLOSED = "UNKNOWN_FAIL_CLOSED"
DISABLED_BY_DEFAULT = "DISABLED_BY_DEFAULT"


@dataclass(frozen=True)
class RiskCheck:
    name: str
    status: str
    observed: Optional[str]
    limit: Optional[str]
    detail: str = ""


@dataclass(frozen=True)
class RiskProof:
    approved: bool
    ticker: str
    side: str
    count: int
    entry_cents: int
    proposed_risk_dollars: float
    capital: float
    checks: tuple
    failing: tuple
    risk_config_hash: str
    created_at: float

    def content_hash(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True,
                         default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def _fmt(x) -> str:
    return f"{float(x):.4f}"


def persist_proof(proof: RiskProof, path: Optional[str] = None) -> None:
    """Append-only fsynced evidence record. The proof's content_hash is
    bound into the order write-ahead intent, so the preimage must be
    durable BEFORE any submission."""
    import os
    from config import _p
    target = path or _p("risk_proofs.jsonl")
    line = json.dumps({"content_hash": proof.content_hash(),
                       **asdict(proof)}, sort_keys=True, default=str)
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def build_risk_proof(*, ticker: str, category: str, side: str,
                     count: int, entry_cents: int, risk, posmgr,
                     capital: float, balance_known: bool,
                     orders_blocked_reconciling: bool,
                     now: Optional[float] = None) -> RiskProof:
    """Evaluate every risk control for a proposed order. `risk` is the
    RiskManager, `posmgr` the PositionManager. Pure evaluation — no
    state is mutated, nothing is claimed or reserved here."""
    checks: list[RiskCheck] = []
    proposed = max(0.0, float(count) * float(entry_cents) / 100.0)

    def add(name, status, observed=None, limit=None, detail=""):
        checks.append(RiskCheck(name, status,
                                None if observed is None else str(observed),
                                None if limit is None else str(limit),
                                detail))

    # 1. kill switch
    add("kill_switch", FAIL if CFG.KILL_SWITCH else PASS,
        observed=bool(CFG.KILL_SWITCH), limit=False)

    # 2. broker schema interpretability (Wave 3 flag)
    schema_bad = bool(getattr(posmgr, "exchange_schema_incompatible",
                              False))
    add("exchange_schema_compatible",
        UNKNOWN_FAIL_CLOSED if schema_bad else PASS,
        observed=schema_bad, limit=False,
        detail="broker rows uninterpretable — exposure unknown"
        if schema_bad else "")

    # 3. unresolved order intents (Wave 4 flag)
    add("order_intents_resolved",
        UNKNOWN_FAIL_CLOSED if orders_blocked_reconciling else PASS,
        observed=orders_blocked_reconciling, limit=False,
        detail="in-flight intents unresolved against broker"
        if orders_blocked_reconciling else "")

    # 4. balance freshness — unknown balance never sizes an order
    add("balance_known",
        PASS if (balance_known and capital > 0) else UNKNOWN_FAIL_CLOSED,
        observed=f"balance_known={balance_known} capital={_fmt(capital)}",
        limit="broker balance fetched this cycle")

    # 5. max loss per order (binary contract: max loss = full cost)
    per_order_cap = capital * float(CFG.MAX_POS_PCT) / 100.0
    add("max_loss_per_order",
        PASS if proposed <= per_order_cap else FAIL,
        observed=_fmt(proposed), limit=_fmt(per_order_cap),
        detail=f"MAX_POS_PCT={CFG.MAX_POS_PCT:g}%")

    # 6. projected single-market exposure
    single_cap = capital * float(CFG.MAX_SINGLE_MARKET_RISK_PCT) / 100.0
    single_proj = float(posmgr.open_risk_on(ticker)) + proposed
    add("projected_single_market",
        PASS if single_proj <= single_cap else FAIL,
        observed=_fmt(single_proj), limit=_fmt(single_cap),
        detail="projected = current + proposed (the pre-existing check "
               "compared only current exposure)")

    # 7. projected category exposure
    cat_cap = capital * float(CFG.MAX_CATEGORY_RISK_PCT) / 100.0
    cat_proj = float(posmgr.open_risk_by_category().get(category, 0.0)) \
        + proposed
    add("projected_category",
        PASS if cat_proj <= cat_cap else FAIL,
        observed=_fmt(cat_proj), limit=_fmt(cat_cap))

    # 8. projected correlation group (aggregate — disabled default 0)
    group_pct = float(getattr(CFG, "MAX_CORRELATION_GROUP_PCT", 0.0) or 0.0)
    group = risk._group_for(ticker, category)
    group_proj = float(posmgr.open_risk_by_group().get(group, 0.0)) \
        + proposed
    if group_pct <= 0:
        add("projected_correlation_group", DISABLED_BY_DEFAULT,
            observed=_fmt(group_proj), limit="0 (disabled)",
            detail="MAX_CORRELATION_GROUP_PCT unset — operator must "
                   "enable via governed risk policy for LIVE")
    else:
        cap = capital * group_pct / 100.0
        add("projected_correlation_group",
            PASS if group_proj <= cap else FAIL,
            observed=_fmt(group_proj), limit=_fmt(cap),
            detail=f"group={group}")

    # 9. projected total portfolio risk (aggregate — disabled default 0)
    total_pct = float(getattr(CFG, "MAX_PORTFOLIO_RISK_PCT", 0.0) or 0.0)
    total_proj = float(posmgr.open_risk()) + proposed
    if total_pct <= 0:
        add("projected_portfolio_total", DISABLED_BY_DEFAULT,
            observed=_fmt(total_proj), limit="0 (disabled)",
            detail="MAX_PORTFOLIO_RISK_PCT unset — operator must "
                   "enable via governed risk policy for LIVE")
    else:
        cap = capital * total_pct / 100.0
        add("projected_portfolio_total",
            PASS if total_proj <= cap else FAIL,
            observed=_fmt(total_proj), limit=_fmt(cap))

    # 10. projected open-risk budget
    budget = capital * float(CFG.RISK_BUDGET_PCT) / 100.0
    add("projected_open_risk_budget",
        PASS if total_proj <= budget else FAIL,
        observed=_fmt(total_proj), limit=_fmt(budget),
        detail=f"RISK_BUDGET_PCT={CFG.RISK_BUDGET_PCT:g}%")

    # 11. max open positions (projected)
    pos_cap = int(getattr(CFG, "MAX_OPEN_POSITIONS", 0) or 0)
    open_count = int(posmgr.open_count())
    if pos_cap <= 0:
        add("projected_open_positions", DISABLED_BY_DEFAULT,
            observed=open_count + 1, limit="0 (disabled)")
    else:
        add("projected_open_positions",
            PASS if open_count + 1 <= pos_cap else FAIL,
            observed=open_count + 1, limit=pos_cap)

    # 12. daily loss stop (realized)
    pnl = float(risk.daily_realized_pnl())
    stop = float(risk.effective_daily_stop())
    add("daily_loss_stop",
        PASS if not (stop > 0 and pnl <= -stop) else FAIL,
        observed=_fmt(pnl), limit=_fmt(-stop))

    # 13. drawdown state (aggregate throttle — disabled default 0)
    dd_pct = float(risk.rolling_drawdown_pct())
    throttle = float(getattr(CFG, "PORTFOLIO_DRAWDOWN_THROTTLE_PCT",
                             0.0) or 0.0)
    if throttle <= 0:
        add("drawdown_throttle", DISABLED_BY_DEFAULT,
            observed=_fmt(dd_pct), limit="0 (disabled)",
            detail="PORTFOLIO_DRAWDOWN_THROTTLE_PCT unset — operator "
                   "must enable via governed risk policy for LIVE")
    else:
        add("drawdown_throttle", PASS,
            observed=_fmt(dd_pct), limit=_fmt(throttle),
            detail="throttle applied at sizing; recorded here")

    # 14. consecutive-loss circuit breaker
    losses = int(risk.consecutive_losses())
    if losses >= int(CFG.MAX_CONSECUTIVE_LOSSES):
        elapsed = risk.seconds_since_last_settlement()
        cooling = (elapsed is None
                   or elapsed < float(CFG.CONSECUTIVE_LOSS_COOLDOWN_S))
        add("consecutive_loss_circuit", FAIL if cooling else PASS,
            observed=losses, limit=int(CFG.MAX_CONSECUTIVE_LOSSES),
            detail="cooldown active" if cooling
            else "cooldown elapsed — half-open claim still required "
                 "downstream")
    else:
        add("consecutive_loss_circuit", PASS,
            observed=losses, limit=int(CFG.MAX_CONSECUTIVE_LOSSES))

    from config_identity import risk_config_hash as _rch
    failing = tuple(c.name for c in checks if c.status != PASS)
    return RiskProof(
        approved=not failing,
        ticker=ticker, side=side, count=int(count),
        entry_cents=int(entry_cents),
        proposed_risk_dollars=round(proposed, 6),
        capital=round(float(capital), 6),
        checks=tuple(checks), failing=failing,
        risk_config_hash=_rch(),
        created_at=float(now if now is not None else time.time()))
