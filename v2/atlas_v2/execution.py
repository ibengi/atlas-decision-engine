"""Deterministic simulated intents. No submit/cancel broker adapter exists."""
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

from .domain import Refused, decimal, digest, hash_id, utc, now, Scope


@dataclass(frozen=True)
class Quote:
    ticker: str
    side: str
    bid: str
    ask: str
    available: str
    observed_at: str
    closes_at: str
    receipt_hash: str


@dataclass(frozen=True)
class Limits:
    # Existing ceilings retained; these are NOT approval thresholds.
    max_price: str = "0.85"
    max_spread: str = "0.04"
    min_gross_edge: str = "0.05"
    min_net_edge: str = "0.03"
    min_ev: str = "0.02"
    uncertainty: str = "0.01"
    max_age_seconds: int = 5
    max_contracts: int = 1


def reprice(quote, probability, budget, fee_per_contract, slippage, limits, at):
    """Everything is recomputed from the fresh selected-side quote, in dollars.

    Fees/slippage are explicitly supplied conservative bounds. Their authentic
    qualification is a separate prerequisite, not implied by positive economics.
    """
    hash_id(quote.receipt_hash)
    if not quote.ticker or quote.side not in ("yes", "no"):
        raise Refused("quote identity")
    if type(limits.max_age_seconds) is not int or not 0 < limits.max_age_seconds <= 5 or type(limits.max_contracts) is not int or limits.max_contracts != 1:
        raise Refused("invalid limits")
    bid, ask, available = decimal(quote.bid), decimal(quote.ask), decimal(quote.available)
    p, funds, fee, slip = map(decimal, (probability, budget, fee_per_contract, slippage))
    if fee < 0:
        raise Refused("negative fee")
    fee = fee.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    cap, spread_limit, min_gross, min_net, min_ev, uncertainty = map(decimal, (
        limits.max_price, limits.max_spread, limits.min_gross_edge,
        limits.min_net_edge, limits.min_ev, limits.uncertainty))
    if not (0 <= bid <= ask < 1 and ask > 0 and 0 < p < 1):
        raise Refused("malformed quote/probability")
    if (not 0 < cap <= Decimal("0.85") or not 0 <= spread_limit <= Decimal("0.04")
            or min_gross < Decimal("0.05") or min_net < Decimal("0.03")
            or min_ev < Decimal("0.02") or uncertainty < Decimal("0.01") or min(fee, slip) < 0):
        raise Refused("invalid/permissive economic limits")
    age = (utc(at) - utc(quote.observed_at)).total_seconds()
    if age < 0 or age > limits.max_age_seconds or utc(at) >= utc(quote.closes_at):
        raise Refused("stale/future/closed quote")
    if ask > cap:
        raise Refused("refreshed price exceeds cap")
    if ask - bid > spread_limit:
        raise Refused("refreshed spread exceeds limit")
    if available < 1:
        raise Refused("liquidity disappeared")
    gross = p - ask
    net = gross - fee - slip - uncertainty
    ev = gross - fee - slip
    if gross < min_gross:
        raise Refused("refreshed gross edge")
    if net < min_net:
        raise Refused("refreshed net edge")
    if ev <= min_ev:
        raise Refused("refreshed expected value")
    count = min(limits.max_contracts, int(available.to_integral_value(rounding=ROUND_FLOOR)),
                int((funds / (ask + fee + slip)).to_integral_value(rounding=ROUND_FLOOR)))
    if count < 1:
        raise Refused("insufficient evidenced budget")
    # Round fee bounds upward, never make a trade affordable by rounding down.
    total_cost = count * ask + (count * fee).quantize(Decimal("0.01"), rounding=ROUND_CEILING) + count * slip
    if total_cost > funds:
        raise Refused("fee-inclusive size exceeds budget")
    return {"ticker": quote.ticker, "side": quote.side, "count": count,
            "price": str(ask), "gross_edge": str(gross), "net_edge": str(net),
            "ev": str(ev), "cost_bound": str(total_cost), "quote": asdict(quote),
            "limits_hash": digest(asdict(limits)), "probability": str(p),
            "fee_bound": str(fee), "slippage_bound": str(slip)}


def reserve_shadow(store, scope, candidate_lock, expected_control_hash, refresh,
                   probability, fee_per_contract, slippage, limits=Limits()):
    """Refresh first; fence risk/version/uniqueness and persist under one DB tx.

    A SHADOW_INTENT is never an order and never reports would_submit=true. Its
    control and cost inputs are simulation/evidence bindings, not live approval.
    """
    if not isinstance(scope, Scope):
        raise Refused("scope required")
    quote = refresh()
    with store.transaction():
        control = store.latest("CONTROL")
        lock = store.get(candidate_lock)
        if not control or control["hash"] != expected_control_hash:
            raise Refused("risk/control changed during refresh")
        if not lock or lock["kind"] != "LOCK":
            raise Refused("candidate lineage absent")
        state = control["payload"]
        if (state.get("scope") != asdict(scope) or state.get("dedicated_scope") is not True
                or state.get("reconciliation") != "MATCH" or state.get("open_orders_complete") is not True
                or state.get("manual_positions") != [] or state.get("kill_switch") is not False
                or state.get("mode") != "READ_ONLY" or state.get("capital") != "OFF"):
            raise Refused("account/safety evidence blocks")
        dd = decimal(state.get("drawdown"))
        if dd < 0 or dd >= Decimal("0.20"):
            raise Refused("unchanged 20% drawdown guard")
        hash_id(state.get("allocation_receipt"))
        identity = "shadow-intent:" + digest({"scope": asdict(scope), "ticker": quote.ticker})
        if store.get(identity):
            raise Refused("market intent already reserved; no TTL expiry")
        reserved = [e for e in store.events("SHADOW_INTENT") if e["payload"]["scope"] == asdict(scope)]
        # Initial research implementation conservatively retains every reservation.
        # No TTL or unqualified settlement receipt restores budget or a position slot.
        if len(reserved) >= 3:
            raise Refused("three-position ceiling")
        available = decimal(state["available_budget"]) - sum(
            (decimal(e["payload"]["economics"]["cost_bound"]) for e in reserved), Decimal(0))
        economics = reprice(quote, probability, available,
                            fee_per_contract, slippage, limits, now())
        result = store.append(identity, "SHADOW_INTENT", {
            "scope": asdict(scope), "lock": lock["hash"], "control": control["hash"],
            "economics": economics, "would_submit": False, "broker_writes": 0,
            "status": "SIMULATED_INTENT_ONLY"})
        reprice(quote, probability, available, fee_per_contract,
                slippage, limits, result["recorded_at"])
        return result


def deny_financial_mutation(*_args, **_kwargs):
    raise Refused("financial mutation capability absent: CAPITAL OFF / READ_ONLY")
