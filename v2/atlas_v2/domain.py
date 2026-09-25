"""Strict, explicit units and provenance. Missing evidence is never zero."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


class Refused(ValueError):
    pass


def decimal(value):
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise Refused("decimal requires exact string/integer")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise Refused("invalid decimal") from exc
    if not result.is_finite():
        raise Refused("nonfinite decimal")
    return result


def utc(value):
    if not isinstance(value, str):
        raise Refused("UTC timestamp required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refused("invalid timestamp") from exc
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise Refused("explicit UTC required")
    return result


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        raise Refused("noncanonical payload") from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def hash_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Refused("SHA256 required")
    return value


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused("duplicate JSON key")
            result[key] = value
        return result
    def reject(_):
        raise Refused("nonfinite JSON")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused("invalid JSON") from exc


@dataclass(frozen=True)
class Scope:
    account: str
    subaccount: int

    def __post_init__(self):
        if (not isinstance(self.account, str) or not self.account.strip()
                or type(self.subaccount) is not int or self.subaccount < 0):
            raise Refused("explicit account/subaccount required")


@dataclass(frozen=True)
class Page:
    request_cursor: str
    response_cursor: str
    rows: tuple
    scope: Scope
    receipt_hash: str
    transport_complete: bool
    envelope_complete: bool
    error: str = ""


def complete_rows(pages, scope):
    """Adapter must attest scope and envelope, not merely return a row list."""
    if not pages:
        raise Refused("no completeness proof")
    cursor, seen, rows = "", set(), []
    for index, page in enumerate(pages):
        hash_id(page.receipt_hash)
        if (page.scope != scope or page.transport_complete is not True
                or page.envelope_complete is not True or page.error
                or not isinstance(page.rows, tuple)
                or not isinstance(page.request_cursor, str)
                or not isinstance(page.response_cursor, str)
                or page.request_cursor != cursor):
            raise Refused("incomplete or wrong-scope page")
        cursor = page.response_cursor
        if cursor and cursor in seen:
            raise Refused("repeated cursor")
        seen.add(cursor)
        if cursor == "" and index != len(pages) - 1:
            raise Refused("data/error after terminal page")
        rows.extend(page.rows)
    if cursor != "":
        raise Refused("unterminated pagination")
    return tuple(rows)


def position_quantity(value):
    quantity = decimal(value)
    if quantity != quantity.quantize(Decimal("0.01")):
        raise Refused("unsupported fractional precision")
    return quantity


def reconcile(pages, scope, local):
    broker = {}
    for row in complete_rows(pages, scope):
        if not isinstance(row, dict) or set(row) != {"ticker", "position_fp"}:
            raise Refused("malformed position")
        ticker = row["ticker"]
        if not isinstance(ticker, str) or not ticker or ticker in broker:
            raise Refused("ambiguous ticker")
        broker[ticker] = position_quantity(row["position_fp"])
    normalized = {k: position_quantity(v) for k, v in local.items()}
    broker = {k: v for k, v in broker.items() if v}
    normalized = {k: v for k, v in normalized.items() if v}
    return "MATCH" if broker == normalized else "MISMATCH"

