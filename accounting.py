# -*- coding: utf-8 -*-
"""AIR-001 Wave 7 (DE-P1 accounting) — canonical Decimal settlement math.

Reproduced defects:
- void_unreadable / expired_stale DOUBLE-COUNTED fees: gross was set to
  -fees and net = gross - fees, i.e. net = -2x fees;
- the same paths UNDERSTATED the conservative loss (only fees, while a
  force-closed unresolved position can have lost its full cost);
- an unreadable settlement result was silently CONVERTED into an
  invented outcome instead of staying UNKNOWN.

This module is the ONE place where settlement PnL is computed. All
arithmetic is Decimal-exact; callers receive floats only at the edge
(the trade journal stores 2-decimal dollars). An UNKNOWN result yields
no numbers at all — refusing to fabricate is the contract.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

YES = "yes"
NO = "no"
VOID = "void"
UNKNOWN = "UNKNOWN"


def _dec(value, name: str) -> Decimal:
    """Exact Decimal via str() — never through binary-float arithmetic."""
    if value is None:
        raise ValueError(f"{name} is None — accounting refuses unknowns")
    return Decimal(str(value))


def _round2(d: Decimal) -> float:
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def classify_settlement(market: Optional[dict]) -> str:
    """YES / NO / VOID from a market row; anything else is UNKNOWN.
    UNKNOWN carries no numbers — the caller must keep the position
    visible instead of settling it."""
    result = str(((market or {}).get("result")) or "").lower()
    if result in (YES, NO, VOID):
        return result
    return UNKNOWN


def settle_yes_no(*, side: str, result: str, count, avg_price_cents,
                  fees_dollars) -> dict:
    """Canonical win/loss settlement for a binary contract.

    won:  gross = count * $1 - cost      (payout minus premium)
    lost: gross = -cost
    net = gross - fees (fees counted exactly ONCE).
    """
    if result not in (YES, NO):
        raise ValueError(f"settle_yes_no: result {result!r} is not a "
                         "yes/no outcome — UNKNOWN stays UNKNOWN")
    if side not in (YES, NO):
        raise ValueError(f"settle_yes_no: side {side!r} invalid")
    n = _dec(count, "count")
    cost = n * _dec(avg_price_cents, "avg_price_cents") / Decimal(100)
    fees = _dec(fees_dollars, "fees_dollars")
    won = (result == side)
    gross = (n * Decimal(1) - cost) if won else -cost
    net = gross - fees
    return {"won": won, "gross": _round2(gross), "net": _round2(net),
            "cost": _round2(cost)}


def settle_void(*, fees_dollars) -> dict:
    """VOID: premium returned; the only loss is the fees, once."""
    fees = _dec(fees_dollars, "fees_dollars")
    return {"won": False, "gross": 0.0, "net": _round2(-fees),
            "cost": None}


def settle_forced_conservative(*, count, avg_price_cents,
                               fees_dollars) -> dict:
    """Force-closure of a position whose true outcome is unresolvable
    (expired_stale escape hatch). Conservative = the WORST case: the
    full cost is lost plus fees, counted once. The old code recorded
    only -fees as gross and then subtracted fees again."""
    n = _dec(count, "count")
    cost = n * _dec(avg_price_cents, "avg_price_cents") / Decimal(100)
    fees = _dec(fees_dollars, "fees_dollars")
    return {"won": False, "gross": _round2(-cost),
            "net": _round2(-cost - fees), "cost": _round2(cost)}
