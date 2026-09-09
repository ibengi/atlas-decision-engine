"""Immutable market snapshot — the one thing every AI model must agree it saw.

Alpha Gateway v1, section 2. SHADOW ONLY: nothing in this module or anywhere
downstream of it can submit, cancel or size an order.

WHY IMMUTABILITY IS ENFORCED RATHER THAN REQUESTED
    Four independent models are asked the same question about the same
    market. If any of them can change the question -- reinterpret the
    resolution rules, quote a different price, extend the deadline -- then
    the four answers are no longer comparable, the ensemble is meaningless,
    and the calibration ledger records a prediction about a market that
    never existed. "Models must not modify the snapshot" is therefore not a
    convention: the object refuses mutation, and its identity is DERIVED
    from its content, so a tampered copy cannot even pretend to be the
    original.

    `market_snapshot_id = "snap-" + sha256(canonical content)[:24]`

    A model that alters one field and echoes the old id fails validation on
    the id it quotes; a model that alters a field and recomputes the id
    fails on the id the gateway is expecting. Both are INVALID SIGNAL, which
    is not the same as -- and never becomes -- a probability.

MARKET CLASS AND DEADLINES
    `FAST | MEDIUM | DEEP` is derived from how long the market has left, and
    it decides how long models are allowed to think. The thresholds and the
    deadlines are configuration (`CFG.ALPHA_*`), not constants in this file:
    a latency policy that can only be changed by a deploy is a latency
    policy nobody tunes.

CATALYST EXPIRATION (section 6)
    A signal must never stay valid across a known information event. The
    effective validity of everything derived from this snapshot is bounded
    by `next_known_catalyst.time_utc - safety_buffer`, and that bound is
    computed here, once, so no downstream component has to remember it.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from config import CFG

SCHEMA_VERSION = "atlas-alpha-v2"

MARKET_CLASS_FAST = "FAST"
MARKET_CLASS_MEDIUM = "MEDIUM"
MARKET_CLASS_DEEP = "DEEP"
MARKET_CLASSES = (MARKET_CLASS_FAST, MARKET_CLASS_MEDIUM, MARKET_CLASS_DEEP)


class SnapshotError(ValueError):
    """The snapshot cannot be built or cannot be trusted. Never swallowed."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_utc(value, *, field_name: str = "timestamp"):
    """A timezone-aware UTC datetime, or raise.

    A naive timestamp is refused rather than assumed to be UTC: every
    deadline in this subsystem is a comparison between two instants, and a
    silent timezone assumption turns a stale signal into a fresh one.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise SnapshotError(f"{field_name}: {value!r} is not ISO 8601")
    else:
        raise SnapshotError(f"{field_name}: {value!r} is not a timestamp")
    if dt.tzinfo is None:
        raise SnapshotError(f"{field_name}: {value!r} has no timezone; "
                            f"UTC is never assumed")
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _floor_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def _ceil_second(dt: datetime) -> datetime:
    return dt if dt.microsecond == 0 else \
        dt.replace(microsecond=0) + timedelta(seconds=1)


def classify_market(seconds_to_resolution: float) -> str:
    """FAST / MEDIUM / DEEP from the time the market has left.

    The boundaries are configuration. A market whose resolution time is
    unknown is DEEP by default only when the caller says so explicitly --
    see `build_snapshot`, which refuses an unknown horizon outright, because
    "I do not know when this resolves" is not a reasoning budget.
    """
    if seconds_to_resolution <= float(CFG.ALPHA_FAST_HORIZON_S):
        return MARKET_CLASS_FAST
    if seconds_to_resolution <= float(CFG.ALPHA_MEDIUM_HORIZON_S):
        return MARKET_CLASS_MEDIUM
    return MARKET_CLASS_DEEP


def analysis_budget_seconds(market_class: str) -> float:
    """How long models may think, for this class. Configuration, not a
    constant: section 5 is explicit that production deadlines are tunable."""
    return {
        MARKET_CLASS_FAST: float(CFG.ALPHA_DEADLINE_FAST_S),
        MARKET_CLASS_MEDIUM: float(CFG.ALPHA_DEADLINE_MEDIUM_S),
        MARKET_CLASS_DEEP: float(CFG.ALPHA_DEADLINE_DEEP_S),
    }[market_class]


@dataclass(frozen=True)
class Catalyst:
    """A known, scheduled information event. `time_utc` is None when no
    catalyst is known -- which is NOT the same as "no catalyst exists", and
    is recorded as such rather than as a far-future date."""
    name: str = ""
    time_utc: str = None

    def as_dict(self) -> dict:
        return {"name": self.name, "time_utc": self.time_utc}


@dataclass(frozen=True)
class MarketSnapshot:
    """Frozen. Built once by Atlas, before any model is dispatched.

    `frozen=True` blocks attribute assignment; `as_dict()` returns a fresh
    copy every time, so a provider that mutates what it is handed mutates
    only its own copy. `verify()` re-derives the identity from the content,
    so tampering is detectable even across a serialization boundary.
    """
    schema_version: str
    market_snapshot_id: str
    contract_id: str
    event_id: str
    question: str
    resolution_rules: str
    resolution_source: str
    snapshot_time_utc: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    spread: float
    volume: float
    open_interest: float
    market_close_time_utc: str
    expected_resolution_time_utc: str
    market_class: str
    analysis_deadline_utc: str
    next_known_catalyst: Catalyst = field(default_factory=Catalyst)

    # ── identity ────────────────────────────────────────────────────────
    def content(self) -> dict:
        """Everything the id is derived from: the whole snapshot minus the
        id itself."""
        payload = self.as_dict()
        payload.pop("market_snapshot_id", None)
        return payload

    @staticmethod
    def derive_id(content: dict) -> str:
        return "snap-" + _sha(_canonical(content))[:24]

    def verify(self) -> None:
        """Raise unless the id still matches the content it names."""
        expected = self.derive_id(self.content())
        if expected != self.market_snapshot_id:
            raise SnapshotError(
                f"snapshot identity does not match its content "
                f"({self.market_snapshot_id} != {expected}): the snapshot "
                f"was altered after it was built")

    # ── views ───────────────────────────────────────────────────────────
    def as_dict(self) -> dict:
        """A fresh, plain-dict copy. Handed to providers so that whatever
        they do to it cannot reach this object."""
        out = asdict(self)
        out["next_known_catalyst"] = dict(out["next_known_catalyst"])
        return out

    def for_provider(self) -> dict:
        """The provider-facing view. Identical to `as_dict()` today; kept
        as its own method so that narrowing what providers see later is a
        one-line change rather than an audit of every adapter."""
        return self.as_dict()

    # ── deadlines (section 6) ───────────────────────────────────────────
    @property
    def snapshot_time(self) -> datetime:
        return parse_utc(self.snapshot_time_utc, field_name="snapshot_time_utc")

    @property
    def analysis_deadline(self) -> datetime:
        return parse_utc(self.analysis_deadline_utc,
                         field_name="analysis_deadline_utc")

    @property
    def catalyst_time(self):
        raw = self.next_known_catalyst.time_utc
        return None if not raw else parse_utc(raw, field_name="catalyst time")

    def catalyst_bound(self):
        """The instant after which no signal derived from this snapshot may
        be considered valid, because a known event will have moved the
        information. None when no catalyst is known."""
        ct = self.catalyst_time
        if ct is None:
            return None
        return ct - timedelta(seconds=float(CFG.ALPHA_CATALYST_BUFFER_S))

    def effective_valid_until(self, model_valid_until=None) -> datetime:
        """MIN(model validity, analysis deadline, catalyst - buffer).

        Section 6, computed in exactly one place. A caller that forgets to
        apply the catalyst bound cannot exist, because there is nowhere else
        to ask this question.
        """
        bounds = [self.analysis_deadline]
        if model_valid_until is not None:
            bounds.append(parse_utc(model_valid_until,
                                    field_name="valid_until_utc"))
        cb = self.catalyst_bound()
        if cb is not None:
            bounds.append(cb)
        return min(bounds)

    def is_expired(self, at=None, model_valid_until=None) -> bool:
        at = parse_utc(at, field_name="at") if at is not None \
            else datetime.now(timezone.utc)
        return at > self.effective_valid_until(model_valid_until)


def build_snapshot(*, contract_id, event_id, question, resolution_rules,
                   resolution_source, yes_bid, yes_ask, no_bid, no_ask,
                   volume, open_interest, market_close_time_utc,
                   expected_resolution_time_utc, snapshot_time_utc=None,
                   catalyst_name="", catalyst_time_utc=None,
                   market_class=None) -> MarketSnapshot:
    """Build the snapshot Atlas will dispatch. Pure: no I/O, no writes.

    Every numeric field is validated here rather than downstream, because a
    snapshot is the shared premise of four independent analyses: a bad
    number caught at dispatch costs one refusal, the same number caught in
    the ensemble costs four inference calls and a corrupt ledger row.
    """
    if not str(contract_id or "").strip():
        raise SnapshotError("contract_id is required")
    prices = {"yes_bid": yes_bid, "yes_ask": yes_ask,
              "no_bid": no_bid, "no_ask": no_ask}
    for name, value in prices.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SnapshotError(f"{name}: {value!r} is not a price")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise SnapshotError(f"{name}: {value!r} is not finite")
        if not 0.0 <= value <= 1.0:
            raise SnapshotError(f"{name}: {value} is outside [0, 1] "
                                f"(prices are probabilities, not cents)")
    for name, value in (("volume", volume), ("open_interest", open_interest)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or float(value) < 0 or float(value) != float(value):
            raise SnapshotError(f"{name}: {value!r} is not a non-negative size")

    # Timestamps in this repository are second-resolution ISO 8601, so the
    # snapshot time is FLOORED and the deadline CEILED before either is
    # serialized. Without that the two can truncate into the same second and
    # the analysis budget silently becomes zero -- a whole cycle reporting
    # ANALYSIS_TIMEOUT with no provider ever called. The cost of the rounding
    # is that a budget under one second is rounded up to one; no production
    # latency class is anywhere near that (FAST is 8 s), and a sub-second
    # deadline is not expressible in this timestamp format anyway.
    snap_time = _floor_second(
        parse_utc(snapshot_time_utc, field_name="snapshot_time_utc")
        if snapshot_time_utc else datetime.now(timezone.utc))
    close_time = parse_utc(market_close_time_utc,
                           field_name="market_close_time_utc")
    resolution_time = parse_utc(expected_resolution_time_utc,
                                field_name="expected_resolution_time_utc")
    horizon = (resolution_time - snap_time).total_seconds()
    if market_class is None:
        market_class = classify_market(horizon)
    if market_class not in MARKET_CLASSES:
        raise SnapshotError(f"market_class {market_class!r} is not one of "
                            f"{list(MARKET_CLASSES)}")

    deadline = _ceil_second(snap_time + timedelta(
        seconds=analysis_budget_seconds(market_class)))
    catalyst = Catalyst(name=str(catalyst_name or ""),
                        time_utc=iso(parse_utc(catalyst_time_utc,
                                               field_name="catalyst_time_utc"))
                        if catalyst_time_utc else None)
    # A catalyst inside the analysis window shortens it: there is no point
    # asking for an answer that is guaranteed to be stale on arrival.
    if catalyst.time_utc:
        bound = parse_utc(catalyst.time_utc, field_name="catalyst_time_utc") \
            - timedelta(seconds=float(CFG.ALPHA_CATALYST_BUFFER_S))
        deadline = min(deadline, _floor_second(bound))
    if deadline <= snap_time:
        # The catalyst is already inside the safety buffer. There is no
        # analysis window at all, and saying so here is better than
        # dispatching four providers against a deadline that has passed.
        raise SnapshotError(
            f"no analysis window: the deadline ({iso(deadline)}) is not after "
            f"the snapshot time ({iso(snap_time)}); the next catalyst is "
            f"within the {CFG.ALPHA_CATALYST_BUFFER_S:g}s safety buffer")

    content = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": str(contract_id).strip(),
        "event_id": str(event_id or "").strip(),
        "question": str(question or ""),
        "resolution_rules": str(resolution_rules or ""),
        "resolution_source": str(resolution_source or ""),
        "snapshot_time_utc": iso(snap_time),
        "yes_bid": float(yes_bid), "yes_ask": float(yes_ask),
        "no_bid": float(no_bid), "no_ask": float(no_ask),
        "spread": round(float(yes_ask) - float(yes_bid), 6),
        "volume": float(volume), "open_interest": float(open_interest),
        "market_close_time_utc": iso(close_time),
        "expected_resolution_time_utc": iso(resolution_time),
        "market_class": market_class,
        "analysis_deadline_utc": iso(deadline),
        "next_known_catalyst": catalyst.as_dict(),
    }
    snapshot = MarketSnapshot(
        market_snapshot_id=MarketSnapshot.derive_id(content),
        next_known_catalyst=catalyst,
        **{k: v for k, v in content.items() if k != "next_known_catalyst"})
    snapshot.verify()
    return snapshot


def snapshot_from_dict(payload: dict) -> MarketSnapshot:
    """Rebuild a snapshot from a persisted row and CHECK its identity.

    Used by the calibration ledger and by tests. It re-derives the id, so a
    row edited on disk is refused rather than replayed.
    """
    if not isinstance(payload, dict):
        raise SnapshotError(f"snapshot payload is {type(payload).__name__}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"unknown snapshot schema "
                            f"{payload.get('schema_version')!r}")
    data = dict(payload)
    cat = data.pop("next_known_catalyst", {}) or {}
    try:
        snapshot = MarketSnapshot(
            next_known_catalyst=Catalyst(name=cat.get("name", ""),
                                         time_utc=cat.get("time_utc")),
            **data)
    except TypeError as e:
        raise SnapshotError(f"snapshot payload does not match the schema: {e}")
    snapshot.verify()
    return snapshot


def redacted(snapshot: MarketSnapshot) -> dict:
    """A short form for logs. Never carries the full resolution rules, which
    can be long, and never anything secret (a snapshot holds none)."""
    return {"market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id,
            "market_class": snapshot.market_class,
            "yes_ask": snapshot.yes_ask,
            "analysis_deadline_utc": snapshot.analysis_deadline_utc}
