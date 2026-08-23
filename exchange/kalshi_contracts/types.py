# -*- coding: utf-8 -*-
"""Canonical typed Kalshi objects. Decimal-exact, no-guess parsing.

Field-name → unit mapping is EXPLICIT per field. `*_fp` and `*_dollars`
fields are decimal strings per the current Kalshi API; legacy integer
fields are cents or contract counts. A value present under an unmapped
name is NOT interpreted; a mandatory value absent under every mapped
name raises SchemaIncompatible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

EXCHANGE_SCHEMA_INCOMPATIBLE = "EXCHANGE_SCHEMA_INCOMPATIBLE"


class SchemaIncompatible(Exception):
    """Mandatory fields of a capital-critical object cannot be
    interpreted. Callers must treat trading as BLOCKED — never guess."""

    def __init__(self, kind: str, missing: list[str], raw_keys: list[str]):
        self.kind = kind
        self.missing = missing
        self.raw_keys = raw_keys
        super().__init__(
            f"{EXCHANGE_SCHEMA_INCOMPATIBLE}: {kind} — cannot interpret "
            f"{missing} from fields {sorted(raw_keys)}")


def _decimal(value: Any, *, kind: str, name: str) -> Decimal:
    """Exact Decimal from str/int/Decimal. float is refused: binary
    floats cannot represent money exactly and their acceptance would
    hide upstream precision loss."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise SchemaIncompatible(kind, [name], [name])
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise SchemaIncompatible(kind, [name], [name]) from exc
    if isinstance(value, float):
        # Explicit policy: a float in a money field is a schema breach,
        # not something to paper over.
        raise SchemaIncompatible(kind, [f"{name} (float refused)"], [name])
    raise SchemaIncompatible(kind, [name], [name])


@dataclass(frozen=True)
class KalshiMoney:
    """Exact dollars. `source_field` records which schema variant fed it."""
    dollars: Decimal
    source_field: str

    @staticmethod
    def from_raw(raw: dict, *, kind: str,
                 dollar_fields: tuple = (),
                 cent_fields: tuple = ()) -> Optional["KalshiMoney"]:
        for name in dollar_fields:
            if name in raw and raw[name] is not None:
                return KalshiMoney(_decimal(raw[name], kind=kind,
                                            name=name), name)
        for name in cent_fields:
            if name in raw and raw[name] is not None:
                cents = _decimal(raw[name], kind=kind, name=name)
                return KalshiMoney(cents / Decimal(100), name)
        return None                      # absent is ABSENT, never zero


@dataclass(frozen=True)
class KalshiPrice:
    """A contract price. Kalshi prices are cents in [1, 99]."""
    cents: Decimal
    source_field: str

    @property
    def dollars(self) -> Decimal:
        return self.cents / Decimal(100)


@dataclass(frozen=True)
class KalshiQuantity:
    """Contract count. `*_fp` decimal-string variants preserved exactly."""
    count: Decimal
    source_field: str

    @staticmethod
    def from_raw(raw: dict, *, kind: str, fields: tuple,
                 required: bool) -> Optional["KalshiQuantity"]:
        for name in fields:
            if name in raw and raw[name] is not None:
                return KalshiQuantity(_decimal(raw[name], kind=kind,
                                               name=name), name)
        if required:
            raise SchemaIncompatible(kind, list(fields),
                                     list(raw.keys()))
        return None


@dataclass(frozen=True)
class KalshiPosition:
    ticker: str
    quantity: KalshiQuantity             # signed: + yes / - no
    market_exposure: Optional[KalshiMoney]
    total_traded: Optional[KalshiMoney]
    fees_paid: Optional[KalshiMoney]
    realized_pnl: Optional[KalshiMoney]
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def side(self) -> str:
        return "yes" if self.quantity.count >= 0 else "no"

    @property
    def abs_count(self) -> Decimal:
        return abs(self.quantity.count)


@dataclass(frozen=True)
class KalshiOrder:
    order_id: str
    ticker: Optional[str]
    status: Optional[str]
    requested_price: Optional[KalshiPrice]
    avg_fill_price: Optional[KalshiPrice]
    count: Optional[KalshiQuantity]
    remaining_count: Optional[KalshiQuantity]
    taker_fill_count: Optional[KalshiQuantity]
    client_order_id: Optional[str]
    raw: dict = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class KalshiFill:
    fill_id: str
    order_id: Optional[str]
    ticker: Optional[str]
    count: KalshiQuantity
    price: Optional[KalshiPrice]
    fee: Optional[KalshiMoney]
    raw: dict = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class KalshiSettlement:
    ticker: str
    result: Optional[str]                # yes / no / None (=UNKNOWN)
    revenue: Optional[KalshiMoney]
    fees: Optional[KalshiMoney]
    raw: dict = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class KalshiBalance:
    balance: KalshiMoney
    raw: dict = field(repr=False, default_factory=dict)


# ─── parsers ───────────────────────────────────────────────────────────────

def parse_position(raw: dict) -> KalshiPosition:
    if not isinstance(raw, dict) or not raw.get("ticker"):
        raise SchemaIncompatible("position", ["ticker"],
                                 list(raw.keys())
                                 if isinstance(raw, dict) else [])
    quantity = KalshiQuantity.from_raw(
        raw, kind="position",
        fields=("position", "position_fp", "quantity", "count",
                "count_fp"),
        required=True)
    return KalshiPosition(
        ticker=str(raw["ticker"]),
        quantity=quantity,
        market_exposure=KalshiMoney.from_raw(
            raw, kind="position",
            dollar_fields=("market_exposure_dollars",),
            cent_fields=("market_exposure",)),
        total_traded=KalshiMoney.from_raw(
            raw, kind="position",
            dollar_fields=("total_traded_dollars",),
            cent_fields=("total_traded",)),
        fees_paid=KalshiMoney.from_raw(
            raw, kind="position",
            dollar_fields=("fees_paid_dollars",),
            cent_fields=("fees_paid",)),
        realized_pnl=KalshiMoney.from_raw(
            raw, kind="position",
            dollar_fields=("realized_pnl_dollars",),
            cent_fields=("realized_pnl",)),
        raw=raw)


def _price(raw: dict, *, kind: str, fields: tuple) -> Optional[KalshiPrice]:
    for name in fields:
        if name in raw and raw[name] is not None:
            return KalshiPrice(_decimal(raw[name], kind=kind, name=name),
                               name)
    return None


def parse_order(raw: dict) -> KalshiOrder:
    if not isinstance(raw, dict) or not raw.get("order_id"):
        raise SchemaIncompatible("order", ["order_id"],
                                 list(raw.keys())
                                 if isinstance(raw, dict) else [])
    return KalshiOrder(
        order_id=str(raw["order_id"]),
        ticker=raw.get("ticker"),
        status=raw.get("status"),
        requested_price=_price(raw, kind="order",
                               fields=("requested_price", "yes_price",
                                       "no_price", "price")),
        avg_fill_price=_price(raw, kind="order",
                              fields=("avg_fill_price",)),
        count=KalshiQuantity.from_raw(
            raw, kind="order", fields=("count", "count_fp"),
            required=False),
        remaining_count=KalshiQuantity.from_raw(
            raw, kind="order",
            fields=("remaining_count", "remaining_count_fp"),
            required=False),
        taker_fill_count=KalshiQuantity.from_raw(
            raw, kind="order",
            fields=("taker_fill_count", "taker_fill_count_fp"),
            required=False),
        client_order_id=raw.get("client_order_id"),
        raw=raw)


def parse_fill(raw: dict) -> KalshiFill:
    if not isinstance(raw, dict):
        raise SchemaIncompatible("fill", ["fill"], [])
    fill_id = raw.get("fill_id") or raw.get("id")
    if not fill_id:
        raise SchemaIncompatible("fill", ["fill_id"], list(raw.keys()))
    return KalshiFill(
        fill_id=str(fill_id),
        order_id=raw.get("order_id"),
        ticker=raw.get("ticker"),
        count=KalshiQuantity.from_raw(
            raw, kind="fill", fields=("count", "count_fp"),
            required=True),
        price=_price(raw, kind="fill",
                     fields=("yes_price", "no_price", "price")),
        fee=KalshiMoney.from_raw(
            raw, kind="fill",
            dollar_fields=("fee_cost", "fees", "fee"),
            cent_fields=()),
        raw=raw)


def parse_settlement(raw: dict) -> KalshiSettlement:
    if not isinstance(raw, dict) or not raw.get("ticker"):
        raise SchemaIncompatible("settlement", ["ticker"],
                                 list(raw.keys())
                                 if isinstance(raw, dict) else [])
    result = raw.get("result") or raw.get("market_result") or None
    return KalshiSettlement(
        ticker=str(raw["ticker"]),
        result=str(result) if result else None,   # UNKNOWN stays None
        revenue=KalshiMoney.from_raw(
            raw, kind="settlement",
            dollar_fields=("revenue_dollars",),
            cent_fields=("revenue",)),
        fees=KalshiMoney.from_raw(
            raw, kind="settlement",
            dollar_fields=("fees_paid_dollars", "fee_cost"),
            cent_fields=("fees_paid",)),
        raw=raw)


def parse_balance(raw: dict) -> KalshiBalance:
    if not isinstance(raw, dict):
        raise SchemaIncompatible("balance", ["balance"], [])
    money = KalshiMoney.from_raw(
        raw, kind="balance",
        dollar_fields=("balance_dollars",),
        cent_fields=("balance",))
    if money is None:
        raise SchemaIncompatible("balance", ["balance"],
                                 list(raw.keys()))
    return KalshiBalance(balance=money, raw=raw)
