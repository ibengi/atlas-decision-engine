"""Economic projections from explicit receipts, never cash-residual guesses."""
from dataclasses import dataclass
from decimal import Decimal
from .domain import Refused, decimal, hash_id, utc, Scope


@dataclass(frozen=True)
class CashEntry:
    event_id: str
    scope: Scope
    kind: str
    amount: str
    actor: str
    effective_at: str
    receipt_hash: str


def cash_bridge(opening, closing, entries, scope, start, end,
                opening_receipt, closing_receipt, completeness_receipt):
    for receipt in (opening_receipt, closing_receipt, completeness_receipt):
        hash_id(receipt)
    first, last = utc(start), utc(end)
    if first >= last:
        raise Refused("cash interval")
    seen, total = set(), Decimal(0)
    by_actor = {"ATLAS": Decimal(0), "MANUAL": Decimal(0), "EXTERNAL": Decimal(0)}
    for entry in entries:
        hash_id(entry.receipt_hash)
        if entry.scope != scope or entry.event_id in seen or not entry.event_id:
            raise Refused("cash identity/scope")
        if not first < utc(entry.effective_at) <= last:
            raise Refused("cash entry outside explicit (start,end] interval")
        if entry.kind not in {"BUY", "SELL", "SETTLEMENT", "FEE", "DEPOSIT", "WITHDRAWAL", "TRANSFER", "ADJUSTMENT"} or entry.actor not in by_actor:
            raise Refused("unattributed cashflow")
        amount = decimal(entry.amount)
        if (entry.kind in {"BUY", "FEE", "WITHDRAWAL"} and amount > 0
                or entry.kind in {"SELL", "SETTLEMENT", "DEPOSIT"} and amount < 0):
            raise Refused("cashflow sign mismatch")
        seen.add(entry.event_id)
        total += amount
        by_actor[entry.actor] += amount
    residual = decimal(closing) - decimal(opening) - total
    return {"status": "RECONCILED" if residual == 0 else "UNEXPLAINED_RESIDUAL",
            "residual": str(residual), "cash_change": str(total),
            "by_actor_cash": {k: str(v) for k, v in by_actor.items()},
            "economic_authority": "receipt hashes require independent authentication"}


def atlas_equity(opening_allocation, allocation_receipt, events):
    """Unit NAV adjusts explicit external flows without rebasing trading loss.

    Input PNL is attributed Atlas economic PnL, not broker cash movement. MARK
    replaces the previous unrealized mark; corrections are signed delta events.
    All figures are unqualified until their external receipts are authenticated.
    """
    hash_id(allocation_receipt)
    equity = decimal(opening_allocation)
    if equity <= 0:
        raise Refused("positive evidenced opening allocation required")
    units, nav, high = equity, Decimal(1), Decimal(1)
    realized, unrealized, external = Decimal(0), Decimal(0), Decimal(0)
    seen, previous_at = {}, None
    drawdown, max_drawdown = Decimal(0), Decimal(0)
    for event in events:
        if set(event) != {"id", "kind", "amount", "receipt", "at", "corrects"}:
            raise Refused("economic event schema")
        hash_id(event["receipt"])
        at = utc(event["at"])
        if previous_at and at < previous_at or event["id"] in seen:
            raise Refused("economic ordering/identity")
        amount = decimal(event["amount"])
        if event["kind"] == "FLOW":
            if nav <= 0 or equity + amount <= 0:
                raise Refused("cannot rebase exhausted/fully withdrawn equity")
            units += amount / nav
            equity += amount
            external += amount
        elif event["kind"] in {"PNL", "CORRECTION"}:
            if event["kind"] == "CORRECTION" and seen.get(event["corrects"]) != "PNL":
                raise Refused("correction must reference retained original")
            realized += amount
            equity += amount
        elif event["kind"] == "MARK":
            equity += amount - unrealized
            unrealized = amount
        else:
            raise Refused("manual/broker cash is not Atlas PnL")
        if equity <= 0 or units <= 0:
            raise Refused("nonpositive equity; risk blocked")
        nav = equity / units
        high = max(high, nav)
        drawdown = (high - nav) / high
        max_drawdown = max(max_drawdown, drawdown)
        seen[event["id"]] = event["kind"]
        previous_at = at
    return {"allocated_opening": str(decimal(opening_allocation)), "equity": str(equity),
            "realized_pnl": str(realized), "unrealized_pnl": str(unrealized),
            "external_flow": str(external), "unit_nav": str(nav),
            "drawdown": str(drawdown), "max_drawdown": str(max_drawdown),
            "live_limit": "0.20", "risk_blocked": drawdown >= Decimal("0.20")}


def lifecycle(store, intent_id, receipt_id, state, cumulative_filled, total_quantity,
              authority_receipt, settlement=None):
    """Append-only offline lifecycle projection. Never sends an order/cancel.

    Ambiguous/partial states reserve inventory. Only explicit terminal authority
    can resolve it; no time-to-live or polling count changes economic disposition.
    """
    hash_id(authority_receipt)
    total, filled = decimal(total_quantity), decimal(cumulative_filled)
    if total <= 0 or not 0 <= filled <= total:
        raise Refused("fill quantity")
    if state not in {"AMBIGUOUS", "OPEN", "PARTIAL", "FILLED", "CANCELLED", "SETTLED"}:
        raise Refused("unknown lifecycle state")
    if state == "FILLED" and filled != total or state == "PARTIAL" and not 0 < filled < total:
        raise Refused("inconsistent fill state")
    with store.transaction():
        intent = store.get(intent_id)
        if not intent or intent["kind"] != "SHADOW_INTENT":
            raise Refused("unknown Atlas intent; never adopt manual position")
        if total != decimal(intent["payload"]["economics"]["count"]):
            raise Refused("lifecycle quantity exceeds original intent")
        history = [e for e in store.events("LIFECYCLE") if e["payload"]["intent"] == intent_id]
        if history:
            previous = history[-1]["payload"]
            if previous["state"] == "SETTLED" or filled < decimal(previous["filled"]) or total != decimal(previous["total"]):
                raise Refused("terminal/quantity regression; append explicit correction instead")
        if state == "SETTLED":
            if (not isinstance(settlement, dict) or set(settlement) != {"status", "outcome", "payout", "settled_at", "rules_hash"}
                    or settlement["status"] != "finalized" or settlement["outcome"] not in {"yes", "no"}):
                raise Refused("no authoritative settlement; inventory remains unresolved")
            hash_id(settlement["rules_hash"])
            settled_at = utc(settlement["settled_at"])
            if settled_at < max(utc(intent["recorded_at"]), utc(intent["payload"]["economics"]["quote"]["closes_at"])):
                raise Refused("settlement predates intent/market close")
            expected = filled if settlement["outcome"] == intent["payload"]["economics"]["side"] else Decimal(0)
            if decimal(settlement["payout"]) != expected:
                raise Refused("payout violates binary contract quantity")
        return store.append("lifecycle:" + receipt_id, "LIFECYCLE", {
            "intent": intent_id, "state": state, "filled": str(filled), "total": str(total),
            "authority": authority_receipt, "settlement": settlement,
            "qualification": "SIMULATION_ONLY_RECEIPT_NOT_AUTHENTICATED",
            "inventory_unresolved": state != "SETTLED" and (filled > 0 or state in {"AMBIGUOUS", "OPEN", "PARTIAL"})})
