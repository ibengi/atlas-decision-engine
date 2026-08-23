# -*- coding: utf-8 -*-
"""AIR-001 Wave 3 (DE-P0-004/DE-P1-001/DE-P1-005): the ONE typed Kalshi
API boundary.

Reproduced defect (permanent regression in
tests/test_kalshi_contracts.py): a broker position in the current
fixed-point schema ({"position_fp": "3", "market_exposure_dollars":
"1.44"}) was parsed to quantity 0 by the tolerant pick_int fallbacks,
silently skipped, and the matching LOCAL position was ghosted and
DELETED — real broker exposure became invisible (open_risk 0).

Policy implemented here:
- Decimal / fixed-point-safe parsing. A money value is NEVER coerced
  through float, NEVER rounded on ingest, and NEVER defaulted: absence
  is absence.
- No financial value silently becomes 0 (or 50) because a field name
  changed: if the mandatory fields of a capital-critical object cannot
  be interpreted, parsing raises SchemaIncompatible and the caller must
  treat the exchange as EXCHANGE_SCHEMA_INCOMPATIBLE / trading BLOCKED.
  No guessing.
- Reconciliation classifies every broker/local comparison as MATCHED /
  BROKER_ONLY / LOCAL_ONLY / CONTRADICTED / UNKNOWN_SCHEMA. The broker
  is authoritative, but a local position is never deleted because a
  parser failed, and BROKER_ONLY exposure is counted CONSERVATIVELY
  (worst-case cost basis, labeled as such) until resolved.
"""

from exchange.kalshi_contracts.types import (  # noqa: F401
    EXCHANGE_SCHEMA_INCOMPATIBLE, KalshiBalance, KalshiFill, KalshiMoney,
    KalshiOrder, KalshiPosition, KalshiPrice, KalshiQuantity,
    KalshiSettlement, SchemaIncompatible, parse_balance, parse_fill,
    parse_order, parse_position, parse_settlement)
from exchange.kalshi_contracts.reconciliation import (  # noqa: F401
    BROKER_ONLY, CONTRADICTED, LOCAL_ONLY, MATCHED, UNKNOWN_SCHEMA,
    classify_positions)
