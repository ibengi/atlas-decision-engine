# -*- coding: utf-8 -*-
"""Broker/local position reconciliation with honest classification.

The broker is authoritative — but authority means the broker's PARSED
truth, never a parser's silence. Every comparison is classified; nothing
is deleted or invented; unknown schema blocks trading instead of zeroing
exposure.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from exchange.kalshi_contracts.types import (SchemaIncompatible,
                                             parse_position)

MATCHED = "MATCHED"
BROKER_ONLY = "BROKER_ONLY"
LOCAL_ONLY = "LOCAL_ONLY"
CONTRADICTED = "CONTRADICTED"
UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"

#: Conservative worst-case cost basis (cents) for BROKER_ONLY exposure
#: whose cost basis is not reported: a Kalshi contract can cost at most
#: 99c, so risk is never understated while unresolved. This is a
#: labeled conservative BOUND, not an invented price.
CONSERVATIVE_COST_CENTS = Decimal(99)


def classify_positions(broker_rows: list, local_positions: list[dict]
                       ) -> dict[str, Any]:
    """Classify every broker row and every local open position.

    Returns {entries, counts, unknown_schema, trading_blocked,
    conservative_broker_only_exposure_dollars}. A single UNKNOWN_SCHEMA
    row sets trading_blocked (EXCHANGE_SCHEMA_INCOMPATIBLE) and disables
    any ghost/delete authority for the whole pass — a parser failure
    must never erase local knowledge of exposure.
    """
    entries: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    parsed = {}
    for raw in broker_rows or []:
        try:
            position = parse_position(raw)
        except SchemaIncompatible as exc:
            unknown.append({"classification": UNKNOWN_SCHEMA,
                            "error": str(exc),
                            "raw_keys": sorted(raw.keys())
                            if isinstance(raw, dict) else [],
                            "ticker": (raw or {}).get("ticker")
                            if isinstance(raw, dict) else None})
            continue
        if position.abs_count == 0:
            continue                     # a genuinely flat broker line
        parsed[(position.ticker, position.side)] = position

    local_by_key = {}
    for p in local_positions or []:
        if p.get("state") == "open":
            local_by_key[(p.get("ticker"), p.get("side"))] = p

    conservative_exposure = Decimal(0)
    for key, position in parsed.items():
        local = local_by_key.pop(key, None)
        if local is None:
            if position.market_exposure is not None:
                exposure = position.market_exposure.dollars
                basis = position.market_exposure.source_field
            else:
                exposure = (position.abs_count
                            * CONSERVATIVE_COST_CENTS / Decimal(100))
                basis = "ESTIMATE_CONSERVATIVE(count*99c)"
            conservative_exposure += exposure
            entries.append({"classification": BROKER_ONLY,
                            "ticker": position.ticker,
                            "side": position.side,
                            "count": str(position.abs_count),
                            "exposure_dollars": str(exposure),
                            "exposure_basis": basis})
        elif Decimal(str(local.get("count", 0))) == position.abs_count:
            entries.append({"classification": MATCHED,
                            "ticker": position.ticker,
                            "side": position.side,
                            "count": str(position.abs_count)})
        else:
            entries.append({"classification": CONTRADICTED,
                            "ticker": position.ticker,
                            "side": position.side,
                            "broker_count": str(position.abs_count),
                            "local_count": str(local.get("count"))})
    for key, local in local_by_key.items():
        entries.append({"classification": LOCAL_ONLY,
                        "ticker": key[0], "side": key[1],
                        "local_count": str(local.get("count"))})

    entries.extend(unknown)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["classification"]] = \
            counts.get(entry["classification"], 0) + 1
    return {
        "entries": entries,
        "counts": counts,
        "unknown_schema": len(unknown),
        "trading_blocked": bool(unknown),
        "blocked_reason": ("EXCHANGE_SCHEMA_INCOMPATIBLE: "
                           f"{len(unknown)} broker row(s) not "
                           "interpretable — trading must stay blocked "
                           "and no local position may be ghosted"
                           if unknown else None),
        "conservative_broker_only_exposure_dollars":
            str(conservative_exposure),
    }
