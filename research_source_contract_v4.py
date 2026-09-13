"""Pure, versioned replay of public market/event/series source evidence.

Hashes establish byte integrity and this verifier establishes semantic joins.
Neither a TLS label nor a source name qualifies a settlement authority. Actual
capture authentication and outcome-authority qualification remain external
evidence requirements. No network, configuration, execution or storage imports.
"""
import base64
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from candidate_contract import (canonical_json, canonical_content,
                                compute_checksum, strict_text, strict_timestamp)
from source_identity import settlement_source_identity, render_settlement_source

SCHEMA = "atlas-research-candidate-v4"
BUNDLE_SCHEMA = "atlas-market-event-series-bundle-v1"
SOURCE_SCHEMA = "atlas-joined-settlement-source-v2"
CAPTURE_SCHEMA = "atlas-public-source-capture-v1"
MARKET_CAPTURE_SCHEMA = "atlas-public-market-capture-v1"
NORMALIZATION = "kalshi-binary-usd-4dp-count-2dp-v1"
ORIGIN = "https://external-api.kalshi.com/trade-api/v2"
MARKET_URL = ORIGIN + "/markets?status=open&series_ticker=KXBTC15M&limit=10"
MAX_CAPTURE_BYTES = 262144
MAX_BUNDLE_BYTES = 393216
MAX_RECORD_BYTES = 524288
MAX_CAPTURE_SKEW_SECONDS = 120


class SourceContractError(ValueError):
    pass


def _refuse(message):
    raise SourceContractError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse("duplicate source JSON member")
        result[key] = value
    return result


def _constant(_value):
    _refuse("nonfinite source JSON constant")


def _identity(value, field):
    canonical = strict_text(value, field=field, max_length=200)
    if canonical != value or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,199}", value):
        _refuse(field + " is not a supported exact source identifier")
    return value


def _timestamp(value, field):
    parsed = strict_timestamp(value, field=field)
    if type(value) is not str or value != value.strip():
        _refuse(field + " is not a canonical timestamp string")
    return parsed


def _matching_aliases(obj, canonical, aliases):
    for alias in aliases:
        if alias in obj and (type(obj[alias]) is not str or obj[alias] != canonical):
            _refuse(alias + " contradicts the exact canonical source identity")


def _market_identity(market, expected_event=None):
    if type(market) is not dict:
        _refuse("event markets member is not an object")
    ticker = _identity(market.get("ticker"), "market ticker")
    event_id = _identity(market.get("event_ticker"), "market event ticker")
    _matching_aliases(market, ticker, ("contract_id", "market_ticker"))
    _matching_aliases(market, event_id, ("event_id",))
    if expected_event is not None and event_id != expected_event:
        _refuse("event market collection contains a different event")
    # This profile supports documented modern units only. Presence of an older
    # cents/integer field is not silently ignored or guessed to be an alias.
    unsupported = {"yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "open_interest"}
    if unsupported.intersection(market):
        _refuse("mixed legacy and modern numeric source profile is unsupported")
    return ticker, event_id


def _capture(capture, *, role, expected_url):
    if type(capture) is not dict:
        _refuse(role + " capture missing")
    schemas = ((MARKET_CAPTURE_SCHEMA,) if role == "market" else (CAPTURE_SCHEMA,))
    if capture.get("schema") not in schemas:
        _refuse(role + " capture schema")
    expected = {
        "source_authority": "kalshi-public-market-data", "environment": "prod",
        "source_scope": "public-market-data",
        "http_method": "GET", "http_status": 200,
        "source_url": expected_url,
        "transport_authentication": "TLS-server-certificate-verification",
    }
    for key, value in expected.items():
        if type(capture.get(key)) is not type(value) or capture[key] != value:
            _refuse(role + " capture " + key)
    allowed_capture = set(expected) | {"schema", "emitted_at_utc", "record_sha256",
        "response_bytes_base64", "response_byte_count", "response_sha256",
        "sample_has_more", "complete_account_snapshot", "test_fixture"}
    if set(capture) - allowed_capture:
        _refuse(role + " capture has unsupported metadata")
    if "complete_account_snapshot" in capture and capture["complete_account_snapshot"] is not False:
        _refuse("public market evidence cannot prove account completeness")
    if "test_fixture" in capture and type(capture["test_fixture"]) is not str:
        _refuse("fixture annotation must be text")
    _timestamp(capture.get("emitted_at_utc"), role + " capture time")
    if capture.get("record_sha256") != compute_checksum(capture):
        _refuse(role + " capture digest")
    try:
        encoded = capture["response_bytes_base64"]
        if type(encoded) is not str or len(encoded) > 4 * MAX_CAPTURE_BYTES // 3 + 8:
            _refuse(role + " preimage byte bound")
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceContractError(role + " capture preimage") from exc
    if not 0 < len(raw) <= MAX_CAPTURE_BYTES or \
            type(capture.get("response_byte_count")) is not int or \
            capture["response_byte_count"] != len(raw) or \
            capture.get("response_sha256") != hashlib.sha256(raw).hexdigest():
        _refuse(role + " response digest/length")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SourceContractError(role + " source JSON") from exc
    if type(body) is not dict:
        _refuse(role + " source envelope")
    allowed = {"market": {"markets", "cursor"}, "event": {"event", "markets"},
               "series": {"series"}}[role]
    if set(body) - allowed:
        _refuse(role + " source envelope has unsupported fields")
    if "sample_has_more" in capture and (role != "market" or
            type(capture["sample_has_more"]) is not bool or
            capture["sample_has_more"] != bool(body.get("cursor"))):
        _refuse(role + " capture continuation metadata contradicts response")
    _bounded(body)
    return body


def capture_response(raw, observed_at, *, role, url):
    """Frame bytes obtained by the fixed TLS collector; labels are not trust.

    This pure function does not perform authentication. The caller must obtain
    the response through the independently reviewed capture transport.
    """
    if role not in ("market", "event", "series") or type(raw) is not bytes:
        _refuse("unsupported capture role/bytes")
    if role == "market":
        if url != MARKET_URL:
            _refuse("market endpoint outside fixed profile")
    elif type(url) is not str or not re.fullmatch(
            re.escape(ORIGIN + ("/events/" if role == "event" else "/series/")) +
            r"[A-Z0-9][A-Z0-9_-]{0,199}", url):
        _refuse("metadata endpoint outside fixed profile")
    if not 0 < len(raw) <= MAX_CAPTURE_BYTES:
        _refuse("capture byte bound")
    capture = {
        "schema": MARKET_CAPTURE_SCHEMA if role == "market" else CAPTURE_SCHEMA,
        "source_authority": "kalshi-public-market-data", "environment": "prod",
        "source_scope": "public-market-data", "source_url": url,
        "http_method": "GET", "http_status": 200,
        "transport_authentication": "TLS-server-certificate-verification",
        "emitted_at_utc": observed_at, "response_byte_count": len(raw),
        "response_bytes_base64": base64.b64encode(raw).decode("ascii"),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }
    capture["record_sha256"] = compute_checksum(capture)
    _capture(capture, role=role, expected_url=url)
    return capture


def _decimal(value, *, field, places, maximum):
    if type(value) is not str or not re.fullmatch(
            rf"(?:0|[1-9][0-9]{{0,12}})\.[0-9]{{{places}}}", value):
        _refuse(field + " requires exact declared fixed-point text")
    try:
        exact = Decimal(value)
        number = float(exact)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise SourceContractError(field + " decimal conversion") from exc
    if not exact.is_finite() or not Decimal(0) <= exact <= maximum or \
            Decimal(str(number)) != exact:
        _refuse(field + " is outside the lossless JSON numeric profile")
    return number


def _copy(value):
    return json.loads(canonical_json(value))


def _bounded(value):
    stack = [(value, 0)]
    nodes, characters = 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 20000 or depth > 24:
            _refuse("source structure exceeds bound")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                _refuse("nontext source member")
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            characters += len(item)
            if characters > MAX_RECORD_BYTES:
                _refuse("source text exceeds bound")
        elif type(item) is int:
            if item.bit_length() > 128:
                _refuse("oversized source integer")
        elif item is not None and type(item) not in (float, bool):
            _refuse("non-JSON source value")


def _build_record(bundle):
    """Reconstruct one observation; incomplete metadata is a strict refusal.

    This pure interface cannot supply capture authenticity or settlement trust
    by assertion. The opt-in collector frames responses; replay still verifies
    every exact source identity independently.
    """
    _bounded(bundle)
    if type(bundle) is not dict or set(bundle) != {
            "schema", "market_capture", "market_pointer", "event_capture", "series_capture"} \
            or bundle.get("schema") != BUNDLE_SCHEMA:
        _refuse("complete versioned market/event/series bundle required")
    if len(canonical_json(bundle).encode("utf-8")) > MAX_BUNDLE_BYTES:
        _refuse("capture bundle exceeds byte bound")
    market_body = _capture(bundle["market_capture"], role="market", expected_url=MARKET_URL)
    markets = market_body.get("markets")
    if type(markets) is not list or not 0 < len(markets) <= 10 or \
            any(type(row) is not dict for row in markets) or \
            type(market_body.get("cursor", "")) is not str:
        _refuse("market response collection")
    pointer = bundle["market_pointer"]
    if type(pointer) is not str or not re.fullmatch(r"/markets/[0-9]", pointer):
        _refuse("exact market pointer required")
    index = int(pointer.rsplit("/", 1)[1])
    if index >= len(markets):
        _refuse("market pointer outside response")
    market = markets[index]
    ticker, event_id = _market_identity(market)
    if market.get("market_type") != "binary" or market.get("notional_value_dollars") != "1.0000":
        _refuse("unsupported binary payout profile")
    if market.get("status") not in ("open", "active") or market.get("result", "") != "":
        _refuse("market was not captured as an unresolved open observation")
    event_body = _capture(bundle["event_capture"], role="event",
                          expected_url=ORIGIN + "/events/" + event_id)
    event = event_body.get("event")
    if type(event) is not dict or event.get("event_ticker") != event_id:
        _refuse("event response does not identify the market's exact event")
    _matching_aliases(event, event_id, ("event_id", "ticker"))
    # The optional collection is not an atomic quote snapshot or completeness
    # proof. If supplied, its structure and membership must nevertheless agree.
    if "markets" in event_body:
        members = event_body["markets"]
        if type(members) is not list or len(members) > 1000:
            _refuse("event markets collection schema")
        seen = set()
        for member in members:
            member_ticker, _ = _market_identity(member, expected_event=event_id)
            if member_ticker in seen:
                _refuse("duplicate market identity in event collection")
            seen.add(member_ticker)
    series_id = _identity(event.get("series_ticker"), "event series ticker")
    if series_id != "KXBTC15M":
        _refuse("series is outside the fixed capture profile")
    if "series_ticker" in market and market["series_ticker"] != series_id:
        _refuse("market and event series contradict")
    series_body = _capture(bundle["series_capture"], role="series",
                           expected_url=ORIGIN + "/series/" + series_id)
    series = series_body.get("series")
    if type(series) is not dict or series.get("ticker") != series_id:
        _refuse("series response does not identify the event's exact series")
    _matching_aliases(series, series_id, ("series_ticker", "series_id"))
    sources = series.get("settlement_sources")
    if type(sources) is not list or not sources or any(type(source) is not dict for source in sources):
        _refuse("series requires a complete structured settlement source list")
    identity = settlement_source_identity(sources)
    if not identity:
        _refuse("series publishes no structured settlement source")
    # Every independently supplied source alias must agree structurally. An
    # absent market source is never populated with a pretend raw market alias.
    for obj in (market, event, series):
        for alias in ("settlement_sources", "settlement_source"):
            if alias in obj and settlement_source_identity(obj[alias]) != identity:
                _refuse("captured settlement source identities contradict")
    times = {role: _timestamp(bundle[role + "_capture"]["emitted_at_utc"], role + " observed")
             for role in ("market", "event", "series")}
    if (max(times.values()) - min(times.values())).total_seconds() > MAX_CAPTURE_SKEW_SECONDS:
        _refuse("metadata capture skew exceeds versioned policy")
    for role, obj in (("market", market), ("event", event), ("series", series)):
        if "updated_time" in obj and _timestamp(obj["updated_time"], role + " updated") > times[role]:
            _refuse(role + " source update is after its capture")
    close = _timestamp(market.get("close_time"), "market close")
    expected = _timestamp(market.get("expected_expiration_time"), "expected expiration")
    expiration = _timestamp(market.get("expiration_time"), "expiration")
    latest = _timestamp(market.get("latest_expiration_time"), "latest expiration")
    if not close <= expected <= latest or not close <= expiration <= latest:
        _refuse("captured market schedule is inconsistent")
    if times["market"] >= close:
        _refuse("market was not observed before its close")
    # Occurrence remains a distinct observation, never an expected-time alias.
    if "occurrence_datetime" in market:
        _timestamp(market["occurrence_datetime"], "occurrence time")
    digest = hashlib.sha256(canonical_json(bundle).encode("utf-8")).hexdigest()
    field_paths = {}

    def path(role, json_pointer, transform="identity"):
        return "capture:" + bundle[role + "_capture"]["response_sha256"] + "#" + json_pointer + "[" + transform + "]"

    fields = {"contract_id": ticker, "event_id": event_id}
    aliases = {"contract_id": "ticker", "event_id": "event_ticker",
               "question": "title", "resolution_rules": "rules_primary",
               "market_close_time_utc": "close_time",
               "expected_resolution_time_utc": "expected_expiration_time"}
    for field, key in aliases.items():
        fields[field] = strict_text(market.get(key), field=field)
        if fields[field] != market[key]:
            _refuse(field + " is not exact canonical source text")
        field_paths[field] = path("market", pointer + "/" + key)
    for field in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        key = field + "_dollars"
        fields[field] = _decimal(market.get(key), field=key, places=4, maximum=Decimal(1))
        field_paths[field] = path("market", pointer + "/" + key, "usd-binary-unit-payout-4dp")
    if fields["yes_bid"] > fields["yes_ask"] or fields["no_bid"] > fields["no_ask"]:
        _refuse("crossed observed quote side")
    for field in ("volume", "open_interest"):
        key = field + "_fp"
        fields[field] = _decimal(market.get(key), field=key, places=2, maximum=Decimal(2 ** 53))
        field_paths[field] = path("market", pointer + "/" + key, "fractional-contract-count-2dp")
    fields["resolution_source"] = render_settlement_source(identity)
    field_paths["resolution_source"] = path("series", "/series/settlement_sources", "lossless-structured-identity")
    fields["emitted_at_utc"] = bundle["market_capture"]["emitted_at_utc"]
    field_paths["emitted_at_utc"] = "capture:" + bundle["market_capture"]["record_sha256"] + "#/emitted_at_utc[observer-time]"
    record = {
        "schema": SCHEMA, **fields, "catalyst_name": None, "catalyst_time_utc": None,
        "source": "isolated-public-market-adapter-v4", "cycle_id": digest,
        "field_provenance": field_paths,
        "unavailable_fields": ["catalyst_name", "catalyst_time_utc"],
        "quote_observation": {key: "observed" for key in ("yes_bid", "yes_ask", "no_bid", "no_ask")},
        "contradictory_fields": {},
        "settlement_source_evidence": {"schema": SOURCE_SCHEMA,
            "bundle_sha256": digest, "series_ticker": series_id,
            "source_pointer": "/series/settlement_sources", "raw_source": _copy(sources)},
        "source_binding_v4": {"schema": BUNDLE_SCHEMA, "normalization": NORMALIZATION,
            "bundle_sha256": digest, "bundle": _copy(bundle),
            "environment": "prod", "atomic_exchange_snapshot": False,
            "latest_capture_at_utc": max(times.values()).isoformat(timespec="microseconds"),
            "settlement_authority_qualification": "NOT_ESTABLISHED"},
    }
    record["record_sha256"] = compute_checksum(record)
    if len(canonical_json(record).encode("utf-8")) > MAX_RECORD_BYTES:
        _refuse("candidate exceeds bounded transport record size")
    return record


def build_record(bundle):
    """Build or raise one bounded, explicit source-contract refusal."""
    try:
        return _build_record(bundle)
    except SourceContractError:
        raise
    except Exception as exc:
        raise SourceContractError(type(exc).__name__ + ": " + str(exc)[:300]) from exc


def validate_record(record, *, require_checksum=True):
    """Independently replay exact bytes; arbitrary labels or hashes cannot win."""
    try:
        _bounded(record)
        if type(record) is not dict or record.get("schema") != SCHEMA:
            _refuse("unsupported versioned candidate")
        binding = record.get("source_binding_v4")
        if type(binding) is not dict:
            _refuse("missing independently replayable source binding")
        expected = build_record(binding.get("bundle"))
        if canonical_json(canonical_content(record)) != canonical_json(canonical_content(expected)):
            _refuse("candidate does not reconstruct from its original source bytes")
        if require_checksum and record.get("record_sha256") != expected["record_sha256"]:
            _refuse("record_sha256 mismatch")
        return []
    except Exception as exc:
        return ["v4 source contract refused: " + type(exc).__name__ + ": " + str(exc)[:400]]
