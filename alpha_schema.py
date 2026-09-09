"""`atlas-alpha-v2` model output schema and its strict validator.

Alpha Gateway v1, sections 3 and 7. SHADOW ONLY.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
    A validation failure is INVALID / EXCLUDED. It is never converted into a
    probability, and above all never into 0.5.

    0.5 is not "no opinion": it is a confident claim that the market is a
    coin flip, and it drags the ensemble toward the midpoint exactly when
    one model has failed and the others may be right. A missing model is a
    missing model. `AlphaSignal.valid` is a boolean the ensemble filters on,
    and an invalid signal carries `p_yes = None` -- there is no numeric
    field for a caller to reach past the boolean and use by accident.

WHY THE VALIDATOR IS THIS PEDANTIC
    Model output is untrusted input from a remote service that is being
    asked to produce JSON. Everything here has been observed in the wild
    from one LLM API or another: a probability as a string, a percentage
    where a probability was asked for, NaN serialized bare into JSON,
    prose wrapped around a code fence, an interval that excludes its own
    point estimate, an echoed contract id from the previous request. Each
    of those has a named refusal below, because "the ensemble looked wrong
    that day" is not a diagnosis.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from alpha_snapshot import SCHEMA_VERSION, SnapshotError, parse_utc

STATUS_VALID = "VALID"
STATUS_STALE = "STALE"
STATUS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
STATUS_ERROR = "ERROR"
STATUSES = (STATUS_VALID, STATUS_STALE, STATUS_INSUFFICIENT, STATUS_ERROR)

#: Fields a model MAY NOT set. A model that returns any of these is trying
#: to place an order, and the whole signal is refused rather than stripped:
#: a model that thinks it can trade has misunderstood the task, and its
#: probability is not more trustworthy than its instructions.
FORBIDDEN_FIELDS = frozenset({
    "action", "side", "order", "orders", "trade", "execute", "execution",
    "buy", "sell", "size", "quantity", "count", "contracts",
    "limit_price", "price", "stake", "capital", "leverage", "position_size",
})


class SignalRejected(ValueError):
    """Carries the machine-readable reason, for the metrics by-reason table."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _finite(value):
    """(ok, float). Rejects bool, str, None, NaN and +/-Infinity.

    `json.loads` accepts bare `NaN`, `Infinity` and `-Infinity` by default,
    so these really do arrive as Python floats rather than as strings, and
    every comparison against them is silently False -- which is how a NaN
    probability passes a `0 <= p <= 1` check.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, None
    v = float(value)
    if not math.isfinite(v):
        return False, None
    return True, v


@dataclass(frozen=True)
class AlphaSignal:
    """One model's answer about one snapshot. Frozen: once validated, the
    record the ledger persists is the record the ensemble consumed."""

    schema_version: str
    market_snapshot_id: str
    contract_id: str
    model: str
    model_version: str
    generated_at_utc: str
    p_yes: float
    probability_low: float
    probability_high: float
    confidence: float
    evidence_quality: float
    data_completeness: float
    analysis_latency_ms: int
    valid_until_utc: str
    status: str
    key_drivers: tuple = ()
    counterarguments: tuple = ()
    invalidation_triggers: tuple = ()
    assumptions: tuple = ()
    resolution_interpretation: str = ""
    #: Set by the gateway, not by the model.
    provider: str = ""
    rejected_reason: str = None
    rejected_detail: str = ""
    cost: dict = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.status == STATUS_VALID and self.rejected_reason is None

    @property
    def interval_width(self):
        if self.probability_low is None or self.probability_high is None:
            return None
        return float(self.probability_high) - float(self.probability_low)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        out = asdict(self)
        for key in ("key_drivers", "counterarguments",
                    "invalidation_triggers", "assumptions"):
            out[key] = list(out[key])
        return out


def rejected(provider: str, model: str, reason: str, detail: str = "",
             *, snapshot_id: str = "", contract_id: str = "",
             latency_ms: int = 0, cost: dict = None) -> AlphaSignal:
    """An EXCLUDED signal.

    Every probability field is None on purpose. There is no number here for
    the ensemble to pick up, so "treat a failure as 0.5" is not a mistake a
    caller can make quietly -- it would have to invent the value itself.
    """
    return AlphaSignal(
        schema_version=SCHEMA_VERSION, market_snapshot_id=snapshot_id,
        contract_id=contract_id, model=model, model_version="",
        generated_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        p_yes=None, probability_low=None, probability_high=None,
        confidence=None, evidence_quality=None, data_completeness=None,
        analysis_latency_ms=int(latency_ms), valid_until_utc="",
        status=STATUS_ERROR, provider=provider,
        rejected_reason=reason, rejected_detail=str(detail)[:500],
        cost=dict(cost or {}))


def parse_json_payload(raw) -> dict:
    """Model output as an object, or raise.

    Models wrap JSON in prose and in ``` fences often enough that refusing
    outright would throw away usable answers; peeling exactly one fence is
    tolerated, and anything beyond that is malformed. The tolerance is
    deliberately this narrow: "find the JSON somewhere in the text" is how
    a validator starts accepting the model's commentary as data.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise SignalRejected("malformed_json",
                             f"payload is {type(raw).__name__}")
    text = raw.strip()
    if text.startswith("```"):
        body = text.split("```")
        if len(body) < 2:
            raise SignalRejected("malformed_json", "unterminated code fence")
        text = body[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise SignalRejected("malformed_json", str(e))
    if not isinstance(payload, dict):
        raise SignalRejected("malformed_json",
                             f"top level is {type(payload).__name__}, "
                             f"object expected")
    return payload


def validate_signal(payload, snapshot, *, provider: str, model: str,
                    latency_ms: int = 0, received_at=None,
                    cost: dict = None) -> AlphaSignal:
    """Validate one model's output against the snapshot it was asked about.

    Returns a VALID signal or an EXCLUDED one. Never raises to the caller
    and never returns a probability it could not verify: the dispatcher runs
    four of these concurrently and one bad provider must not end the cycle.
    """
    ctx = {"snapshot_id": snapshot.market_snapshot_id,
           "contract_id": snapshot.contract_id,
           "latency_ms": latency_ms, "cost": cost}
    try:
        return _validate(payload, snapshot, provider=provider, model=model,
                         latency_ms=latency_ms, received_at=received_at,
                         cost=cost)
    except SignalRejected as e:
        return rejected(provider, model, e.reason, e.detail, **ctx)
    except (SnapshotError, TypeError, ValueError) as e:          # noqa: BLE001
        return rejected(provider, model, "invalid_schema", str(e), **ctx)


def _validate(payload, snapshot, *, provider, model, latency_ms,
              received_at, cost) -> AlphaSignal:
    data = parse_json_payload(payload)

    # 1. Schema identity ------------------------------------------------
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SignalRejected("invalid_schema_version",
                             f"{data.get('schema_version')!r}")

    # 2. The model may not have re-decided what it was asked about ------
    if "contract_id" not in data:
        raise SignalRejected("missing_contract_id")
    if str(data.get("contract_id")) != snapshot.contract_id:
        raise SignalRejected("wrong_contract_id",
                             f"{data.get('contract_id')!r} != "
                             f"{snapshot.contract_id!r}")
    if str(data.get("market_snapshot_id")) != snapshot.market_snapshot_id:
        raise SignalRejected("wrong_snapshot_id",
                             f"{data.get('market_snapshot_id')!r} != "
                             f"{snapshot.market_snapshot_id!r}")

    # 3. The model may not issue instructions ---------------------------
    forbidden = sorted(FORBIDDEN_FIELDS.intersection(data))
    if forbidden:
        raise SignalRejected("execution_instruction",
                             f"model returned execution fields {forbidden}")

    # 4. Status ---------------------------------------------------------
    status = data.get("status")
    if status not in STATUSES:
        raise SignalRejected("unknown_status", f"{status!r}")
    if status != STATUS_VALID:
        # The model itself says it has nothing usable. Honour that; it is
        # information, not a failure, and it is recorded as its own reason.
        raise SignalRejected(f"model_status_{str(status).lower()}",
                             str(data.get("resolution_interpretation", ""))[:200])

    # 5. Probabilities --------------------------------------------------
    ok, p_yes = _finite(data.get("p_yes"))
    if not ok:
        raise SignalRejected("p_yes_not_finite", repr(data.get("p_yes")))
    if not 0.0 <= p_yes <= 1.0:
        raise SignalRejected("p_yes_out_of_range", f"{p_yes}")
    ok_lo, low = _finite(data.get("probability_low"))
    ok_hi, high = _finite(data.get("probability_high"))
    if not ok_lo or not ok_hi:
        raise SignalRejected("interval_not_finite",
                             f"low={data.get('probability_low')!r} "
                             f"high={data.get('probability_high')!r}")
    if low < 0.0:
        raise SignalRejected("interval_low_out_of_range", f"{low}")
    if high > 1.0:
        raise SignalRejected("interval_high_out_of_range", f"{high}")
    if low > p_yes:
        raise SignalRejected("interval_excludes_estimate",
                             f"low {low} > p_yes {p_yes}")
    if high < p_yes:
        raise SignalRejected("interval_excludes_estimate",
                             f"high {high} < p_yes {p_yes}")

    # 6. Quality scalars ------------------------------------------------
    scalars = {}
    for name in ("confidence", "evidence_quality", "data_completeness"):
        ok_s, value = _finite(data.get(name))
        if not ok_s:
            raise SignalRejected(f"{name}_not_finite", repr(data.get(name)))
        if not 0.0 <= value <= 1.0:
            raise SignalRejected(f"{name}_out_of_range", f"{value}")
        scalars[name] = value

    # 7. Time -----------------------------------------------------------
    generated_at = parse_utc(data.get("generated_at_utc"),
                             field_name="generated_at_utc")
    valid_until = parse_utc(data.get("valid_until_utc"),
                            field_name="valid_until_utc")
    if generated_at > valid_until:
        raise SignalRejected("generated_after_valid_until",
                             f"{generated_at.isoformat()} > "
                             f"{valid_until.isoformat()}")
    now = parse_utc(received_at, field_name="received_at") \
        if received_at is not None else datetime.now(timezone.utc)
    if now > snapshot.analysis_deadline:
        raise SignalRejected("late_response",
                             f"arrived {(now - snapshot.analysis_deadline).total_seconds():.3f}s "
                             f"after the analysis deadline")
    # Section 6: the catalyst bound is applied to the model's own validity,
    # so a signal cannot outlive an information event it never saw.
    effective = snapshot.effective_valid_until(data.get("valid_until_utc"))
    if now > effective:
        raise SignalRejected("stale",
                             f"effective validity ended at {effective.isoformat()}")

    ok_lat, latency_field = _finite(data.get("analysis_latency_ms", latency_ms))
    measured = int(latency_ms) if not ok_lat or latency_field < 0 \
        else int(round(latency_field))

    return AlphaSignal(
        schema_version=SCHEMA_VERSION,
        market_snapshot_id=snapshot.market_snapshot_id,
        contract_id=snapshot.contract_id,
        model=str(data.get("model") or model),
        model_version=str(data.get("model_version") or ""),
        generated_at_utc=generated_at.isoformat(timespec="seconds"),
        p_yes=p_yes, probability_low=low, probability_high=high,
        confidence=scalars["confidence"],
        evidence_quality=scalars["evidence_quality"],
        data_completeness=scalars["data_completeness"],
        # The gateway's measured latency wins over the model's self-report:
        # one is observed, the other is claimed.
        analysis_latency_ms=int(latency_ms) or measured,
        valid_until_utc=valid_until.isoformat(timespec="seconds"),
        status=STATUS_VALID,
        key_drivers=tuple(str(x) for x in (data.get("key_drivers") or [])[:20]),
        counterarguments=tuple(str(x) for x in (data.get("counterarguments") or [])[:20]),
        invalidation_triggers=tuple(str(x) for x in (data.get("invalidation_triggers") or [])[:20]),
        assumptions=tuple(str(x) for x in (data.get("assumptions") or [])[:20]),
        resolution_interpretation=str(data.get("resolution_interpretation") or "")[:2000],
        provider=provider, cost=dict(cost or {}))
