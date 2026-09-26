"""Bounded, data-only startup proof. No financial client or Alpha integration.

The provider key list is read once for scope, never retained verbatim. Runtime
secrets stay in memory. All failures use fixed codes; exception strings are never
logged. The probe-only server never opens BTC or learning databases.
"""
import asyncio
import base64
import ctypes
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
import uuid
import websockets


class NoRedirectConnect(websockets.connect):
    def process_redirect(self, exc):
        return exc

from .data import NoRedirect
from .domain import Refused, canonical, decimal, digest, now, strict_json, utc
from .sports_capture import identifier, levels
from .store import Store

REST = "https://external-api.kalshi.com/trade-api/v2"
WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
SYNC_MS, AGE_MS, CLOCK_MS = 250, 1000, 25
MAX_SECONDS, MAX_HTTP, MAX_PAGES, MAX_LEGS = 180, 40, 20, 32
MAX_MESSAGES, MAX_BYTES, CONNECTIONS = 100, 262144, 2
KEY_NAME, PRIVATE_NAME = "KALSHI_SPORTS_READ_KEY_ID", "KALSHI_SPORTS_READ_PRIVATE_KEY"


def require(condition, code):
    if not condition:
        raise Refused(code)


def financial_guard(env):
    require(env.get("CAPITAL", "OFF") == "OFF" and env.get("PROD_ACCESS_MODE", "READ_ONLY") == "READ_ONLY"
            and env.get("BROKER_WRITES", "0") == "0" and env.get("REAL_ORDERS_SUBMITTED", "0") == "0"
            and env.get("LIVE_TRADING", "0").lower() in {"0", "false", "off"}, "FINANCIAL_MODE_REFUSED")


class Credentials:
    def __init__(self, env):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
        key_id, pem = env.get(KEY_NAME), env.get(PRIVATE_NAME)
        require(isinstance(key_id, str) and bool(re.fullmatch(r"[A-Za-z0-9-]{1,128}", key_id)), "KEY_ID_MISSING_OR_INVALID")
        require(isinstance(pem, str) and 0 < len(pem) <= 16384, "PRIVATE_KEY_MISSING_OR_INVALID")
        self.key_id = key_id
        self.secrets = [key_id, pem] + [line for line in pem.splitlines() if len(line) > 24]
        try:
            self.key = serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception:
            raise Refused("PRIVATE_KEY_FORMAT_REFUSED") from None
        require(isinstance(self.key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)), "KEY_ALGORITHM_REFUSED")

    def headers(self, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, ed25519
        require(path in {"/trade-api/v2/api_keys", "/trade-api/ws/v2"}, "SIGNING_PATH_REFUSED")
        stamp = str(time.time_ns() // 1000000)
        message = (stamp + "GET" + path).encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            signature = self.key.sign(message)
        else:
            signature = self.key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                      salt_length=32), hashes.SHA256())
        encoded = base64.b64encode(signature).decode()
        self.secrets.append(encoded)
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": stamp,
                "KALSHI-ACCESS-SIGNATURE": encoded}


def verify_scope(body, key_id):
    require(isinstance(body, dict) and set(body) <= {"api_keys", "api_key_region_expiration_ts"}
            and isinstance(body.get("api_keys"), list), "SCOPE_ENVELOPE_REFUSED")
    matches = [r for r in body["api_keys"] if isinstance(r, dict) and r.get("api_key_id") == key_id]
    require(len(matches) == 1 and matches[0].get("scopes") == ["read"], "READ_ONLY_SCOPE_NOT_PROVEN")
    return {"matching_key_found": True, "scopes": ["read"], "key_id_redacted": True}


class Evidence:
    def __init__(self, store, secrets, run_id):
        self.store, self.secrets, self.run_id = store, secrets, run_id

    def add(self, kind, value):
        encoded = canonical(value).decode()
        require(not any(s and s in encoded for s in self.secrets), "SECRET_REDACTION_REFUSED")
        return self.store.append(str(uuid.uuid4()), "SP_" + kind, {"run_id": self.run_id, **value})

    def raw(self, raw, received, generation, public=True):
        # Unrecognized/provider-error payloads and scope responses retain only hash.
        value = {"sha256": hashlib.sha256(raw).hexdigest(), "received_at": received,
                 "generation": generation, "bytes": len(raw), "raw_retained": False}
        text = raw.decode("utf-8", errors="replace")
        try: text += canonical(strict_json(raw)).decode()
        except Exception: public = False
        if public and not any(s and s in text for s in self.secrets) and not any(
                word in text for word in ('"client_order_id"', '"subaccount"', '"private_key"', '"signature"', '"api_key_id"')):
            value.update(raw_retained=True, body_base64=base64.b64encode(raw).decode())
        return self.add("RECEIPT", value)


class Reader:
    def __init__(self, evidence, deadline):
        self.evidence, self.deadline, self.count = evidence, deadline, 0

    def get(self, path, params=None, credentials=None):
        require(path in {"/api_keys", "/milestones", "/markets", "/events"}, "REST_PATH_REFUSED")
        require(self.count < MAX_HTTP and time.monotonic() < self.deadline, "REST_BUDGET_EXHAUSTED")
        require((path == "/api_keys") == (credentials is not None), "REST_AUTH_SCOPE_REFUSED")
        allowed = {"/api_keys": set(), "/milestones": {"category", "limit", "cursor"},
                   "/markets": {"event_ticker", "limit", "cursor"}, "/events": {"tickers", "limit", "cursor"}}
        params = params or {}
        require(set(params) <= allowed[path], "REST_QUERY_REFUSED")
        self.count += 1
        url = REST + path + (("?" + urlencode(params)) if params else "")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if credentials:
            headers.update(credentials.headers("/trade-api/v2/api_keys"))
        started = now()
        try:
            response = build_opener(NoRedirect).open(Request(url, headers=headers, method="GET"),
                                                    timeout=min(5, max(.01, self.deadline-time.monotonic())))
        except HTTPError as exc:
            response = exc
        except Exception:
            raise Refused("REST_CONNECTION_FAILED") from None
        with response:
            raw = b""
            while len(raw) <= MAX_BYTES:
                require(time.monotonic() < self.deadline, "REST_DEADLINE")
                chunk = response.read1(min(8192, MAX_BYTES+1-len(raw)))
                if not chunk: break
                raw += chunk
            received = now()
            self.evidence.raw(raw[:MAX_BYTES], received, "REST", public=path != "/api_keys" and response.status == 200)
            self.evidence.add("HTTP", {"path": path, "params": params, "started_at": started,
                              "received_at": received, "status": response.status,
                              "body_sha256": hashlib.sha256(raw[:MAX_BYTES]).hexdigest()})
            require(response.status == 200, "REST_HTTP_" + str(response.status))
            require(response.geturl() == url and len(raw) <= MAX_BYTES and not response.headers.get("Content-Range")
                    and response.headers.get("Content-Type", "").split(";")[0] == "application/json", "REST_ENVELOPE_REFUSED")
            length = response.headers.get("Content-Length")
            require(length is None or (length.isdigit() and int(length) == len(raw)), "REST_INCOMPLETE")
            body = strict_json(raw)
            require(isinstance(body, dict) and "error" not in body and "errors" not in body, "REST_BODY_REFUSED")
            return body

    def pages(self, path, field, params):
        rows, cursor, seen = [], "", set()
        for _ in range(MAX_PAGES):
            body = self.get(path, dict(params, cursor=cursor))
            require(isinstance(body.get(field), list) and isinstance(body.get("cursor"), str), "PAGINATION_INCOMPLETE")
            rows.extend(body[field])
            next_cursor = body["cursor"]
            require(not next_cursor or next_cursor not in seen, "PAGINATION_CURSOR_LOOP")
            if not next_cursor:
                return rows
            seen.add(next_cursor)
            cursor = next_cursor
        raise Refused("PAGINATION_LIMIT")


def membership(reader, selected=None):
    milestones = reader.pages("/milestones", "milestones", {"category": "Sports", "limit": 100})
    require(all(isinstance(m, dict) and isinstance(m.get("id"), str) for m in milestones), "MILESTONE_SCHEMA")
    require(len({m["id"] for m in milestones}) == len(milestones), "MILESTONE_DUPLICATE")
    if selected is None:
        eligible = [m for m in milestones if m.get("category") == "Sports"
                    and isinstance(m.get("type"), str)
                    and m["type"].startswith(("soccer_", "football_", "tennis_", "basketball_"))
                    and m.get("start_date") and utc(m["start_date"]) <= utc(now())
                    and m.get("end_date") and utc(m["end_date"]) > utc(now())]
        require(bool(eligible), "NO_NATIVE_ACTIVE_SPORTS_MILESTONE")
        selected = sorted(eligible, key=lambda m: (m["start_date"], m["id"]))[0]["id"]
    matches = [m for m in milestones if m["id"] == selected]
    require(len(matches) == 1, "MILESTONE_NOT_UNIQUE")
    milestone = matches[0]
    require(milestone.get("category") == "Sports", "SPORTS_IDENTITY_REFUSED")
    primary, related = milestone.get("primary_event_tickers"), milestone.get("related_event_tickers")
    require(isinstance(primary, list) and primary and isinstance(related, list), "MEMBERSHIP_UNKNOWN")
    events = sorted(set(primary + related))
    require(0 < len(events) <= 10, "EVENT_SCOPE_BOUND")
    result = {}
    for event in events:
        identifier(event)
        event_rows = reader.pages("/events", "events", {"tickers": event, "limit": 100})
        require(len(event_rows) == 1 and event_rows[0].get("event_ticker") == event
                and event_rows[0].get("category") == "Sports", "EVENT_IDENTITY_INCOMPLETE")
        markets = reader.pages("/markets", "markets", {"event_ticker": event, "limit": 100})
        require(bool(markets), "MARKET_MEMBERSHIP_EMPTY")
        for m in markets:
            require(isinstance(m, dict) and m.get("event_ticker") == event and m.get("market_type") == "binary", "MARKET_IDENTITY_REFUSED")
            ticker = identifier(m.get("ticker"))
            require(ticker not in result and m.get("status") == "active", "MARKET_DUPLICATE_OR_NOT_ACTIVE")
            result[ticker] = {"event_ticker": event, "status": m["status"], "close_time": m.get("close_time")}
            utc(m["close_time"])
    require(0 < len(result) <= MAX_LEGS, "MARKET_SCOPE_BOUND")
    return {"milestone_id": selected, "events": events, "markets": result}


def clock_sample():
    """Read-only Linux adjtimex modes=0; no clock adjustment or manual assertion."""
    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]
    class Timex(ctypes.Structure):
        _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                    ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                    ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
                    ("time", Timeval), ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                    ("jitter", ctypes.c_long), ("shift", ctypes.c_int), ("stabil", ctypes.c_long),
                    ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                    ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int), ("padding", ctypes.c_int*11)]
    try:
        value = Timex()
        state = ctypes.CDLL(None, use_errno=True).adjtimex(ctypes.byref(value))
        # maxerror is microseconds irrespective of STA_NANO; add timestamp quantization.
        error_ms = Decimal(value.maxerror)/1000 + 1
        return {"at": now(), "synchronized": 0 <= state < 5 and not value.status & 64,
                "uncertainty_ms": str(error_ms), "source_uncertainty_ms": None,
                "method": "LINUX_ADJTIMEX_READ_ONLY", "source_bound_reason":"PROVIDER_CLOCK_BOUND_NOT_EXPOSED"}
    except Exception:
        return {"at": now(), "synchronized": False, "uncertainty_ms": None, "method": "UNAVAILABLE"}


def qualify_ticker(msg, received, clock, members):
    require(isinstance(msg, dict), "TICKER_SCHEMA")
    ticker = identifier(msg.get("market_ticker"))
    require(ticker in members and isinstance(msg.get("market_id"), str) and bool(msg["market_id"]), "TICKER_IDENTITY")
    ts = msg.get("ts_ms")
    require(type(ts) is int and ts > 0, "NATIVE_TIMESTAMP_MISSING")
    bid, ask = decimal(msg.get("yes_bid_dollars")), decimal(msg.get("yes_ask_dollars"))
    bsize, asize = decimal(msg.get("yes_bid_size_fp")), decimal(msg.get("yes_ask_size_fp"))
    require(0 <= bid < ask <= 1 and bsize > 0 and asize > 0, "EXECUTABLE_QUOTE_INCOMPLETE")
    require(clock.get("synchronized") is True and clock.get("uncertainty_ms") is not None, "CLOCK_UNQUALIFIED")
    require(clock.get("source_uncertainty_ms") is not None, "SOURCE_CLOCK_BOUND_UNPROVEN")
    source_uncertainty = decimal(clock["source_uncertainty_ms"])
    require(0 <= source_uncertainty <= CLOCK_MS, "SOURCE_CLOCK_UNCERTAINTY")
    uncertainty = decimal(clock["uncertainty_ms"]) + source_uncertainty
    require(0 <= uncertainty <= CLOCK_MS, "CLOCK_UNCERTAINTY")
    received_ms = Decimal(str(utc(received).timestamp())) * 1000
    delta = received_ms-ts
    require(uncertainty <= delta and delta+uncertainty <= AGE_MS, "QUOTE_STALE_OR_FUTURE")
    require(utc(received) < utc(members[ticker]["close_time"]), "MARKET_CLOSED")
    return {"ticker": ticker, "market_id": msg["market_id"], "native_ts_ms": ts,
            "received_at": received, "clock_delta_ms": str(delta), "clock_uncertainty_ms": str(uncertainty),
            "bid": str(bid), "ask": str(ask), "bid_size": str(bsize), "ask_size": str(asize)}


def synchronized_snapshot(quotes, members, at):
    require(set(quotes) == set(members) and bool(members), "SNAPSHOT_INCOMPLETE")
    native = [q["native_ts_ms"] for q in quotes.values()]
    uncertainty = max(decimal(q["clock_uncertainty_ms"]) for q in quotes.values())
    require(max(native)-min(native)+2*uncertainty <= SYNC_MS, "SNAPSHOT_SOURCE_SKEW")
    received = [utc(q["received_at"]) for q in quotes.values()]
    require(Decimal(str((max(received)-min(received)).total_seconds()))*1000+2*uncertainty <= SYNC_MS, "SNAPSHOT_CAPTURE_SKEW")
    at_ms = Decimal(str(utc(at).timestamp()))*1000
    require(all(uncertainty <= at_ms-q["native_ts_ms"] and at_ms-q["native_ts_ms"]+uncertainty <= AGE_MS
                and utc(at) < utc(members[t]["close_time"]) for t,q in quotes.items()), "SNAPSHOT_STALE")
    return {"at": at, "quotes": [quotes[t] for t in sorted(quotes)], "membership_hash": digest(members)}


class Session:
    def __init__(self, members, evidence, generation):
        self.members, self.evidence, self.generation = members, evidence, generation
        self.acks, self.seq, self.books, self.quotes, self.identities = {}, None, {}, {}, {}
        self.quotes_count = self.snapshots_count = self.tickers = self.deltas = 0
        self.snapshot_keys = set()
        self.rejection_reasons = set()

    def process(self, raw, received, clock):
        require(isinstance(raw, str) and len(raw.encode()) <= MAX_BYTES, "MESSAGE_SIZE_OR_TYPE")
        self.evidence.raw(raw.encode(), received, self.generation, public=False)
        body = strict_json(raw)
        require(isinstance(body, dict), "MESSAGE_SCHEMA")
        kind, msg = body.get("type"), body.get("msg")
        receipt = self.evidence.raw(raw.encode(), received, self.generation,
                                    public=kind in {"ticker", "orderbook_delta", "orderbook_snapshot"})
        if kind == "subscribed":
            require(body.get("id") in {1, 2} and isinstance(msg, dict), "ACK_UNKNOWN")
            expected = {1:"ticker", 2:"orderbook_delta"}[body["id"]]
            require(msg.get("channel") == expected and type(msg.get("sid")) is int
                    and expected not in self.acks and msg["sid"] not in self.acks.values(), "ACK_MISMATCH")
            self.acks[expected] = msg["sid"]
            self.evidence.add("SUBSCRIBED", {"generation": self.generation, "channel": expected,
                                            "sid": msg["sid"], "receipt_hash": receipt["hash"]})
            return
        require(kind in {"ticker", "orderbook_snapshot", "orderbook_delta"}, "MESSAGE_TYPE_REFUSED")
        channel = "ticker" if kind == "ticker" else "orderbook_delta"
        require(channel in self.acks and body.get("sid") == self.acks[channel], "MESSAGE_BEFORE_ACK_OR_WRONG_SID")
        require(isinstance(msg, dict) and msg.get("market_ticker") in self.members, "MESSAGE_SCOPE")
        ticker = msg["market_ticker"]
        require(isinstance(msg.get("market_id"), str) and bool(msg["market_id"]), "MESSAGE_MARKET_ID")
        require(self.identities.get(ticker, msg["market_id"]) == msg["market_id"], "MARKET_ID_CHANGED")
        self.identities[ticker] = msg["market_id"]
        if kind == "ticker":
            self.tickers += 1
            ts = msg.get("ts_ms")
            self.evidence.add("TICKER_OBSERVED", {"generation":self.generation,"ticker":ticker,
                "native_ts_ms":ts if type(ts) is int else None,"received_at":received,
                "clock_delta_ms":str(Decimal(str(utc(received).timestamp()))*1000-ts) if type(ts) is int else None,
                "receipt_hash":receipt["hash"],"qualified":False})
            try:
                q = qualify_ticker(msg, received, clock, self.members)
            except Refused as exc:
                self.quotes.pop(ticker, None)
                code = str(exc) if re.fullmatch(r"[A-Z0-9_]{1,100}",str(exc)) else "TICKER_INVALID"
                self.rejection_reasons.add(code)
                self.evidence.add("TICKER_REJECTED", {"generation":self.generation,"ticker":ticker,"reason":code})
                return
            require(ticker not in self.quotes or q["native_ts_ms"] > self.quotes[ticker]["native_ts_ms"], "TICKER_REPLAY_OR_REORDER")
            q["receipt_hash"] = receipt["hash"]
            self.quotes[ticker] = q
            self.quotes_count += 1
            self.evidence.add("QUOTE", {"generation": self.generation, **q})
            if len(self.acks) == 2:
                try:
                    snapshot = synchronized_snapshot(self.quotes, self.members, received)
                    key = digest([q["receipt_hash"] for q in snapshot["quotes"]])
                    if key not in self.snapshot_keys:
                        self.evidence.add("SNAPSHOT", {"generation": self.generation, **snapshot})
                        self.snapshot_keys.add(key)
                        self.snapshots_count += 1
                except Refused as exc:
                    self.evidence.add("SNAPSHOT_REJECTED", {"generation": self.generation, "reason": str(exc)})
            return
        sequence = body.get("seq")
        require(type(sequence) is int and (self.seq is None or sequence == self.seq+1), "BOOK_SEQUENCE_GAP")
        self.seq = sequence
        if kind == "orderbook_snapshot":
            yes, no = levels(msg.get("yes_dollars_fp")), levels(msg.get("no_dollars_fp"))
            self.books[ticker] = {"yes": dict(yes), "no": dict(no)}
            self.evidence.add("BOOK_BASELINE", {"generation": self.generation, "ticker": ticker,
                              "receipt_hash": receipt["hash"], "qualified": False, "reason": "NO_NATIVE_SNAPSHOT_TIMESTAMP"})
            return
        require(ticker in self.books and type(msg.get("ts_ms")) is int, "DELTA_WITHOUT_BASELINE_OR_TIME")
        side, price, change = msg.get("side"), decimal(msg.get("price_dollars")), decimal(msg.get("delta_fp"))
        require(side in {"yes", "no"} and 0 <= price <= 1, "DELTA_SCHEMA")
        size = self.books[ticker][side].get(price, Decimal(0)) + change
        require(size >= 0, "NEGATIVE_BOOK_SIZE")
        if size: self.books[ticker][side][price] = size
        else: self.books[ticker][side].pop(price, None)
        delta_ms = Decimal(str(utc(received).timestamp()))*1000-msg["ts_ms"]
        require(0 <= delta_ms <= AGE_MS, "DELTA_STALE_OR_FUTURE")
        self.deltas += 1
        self.evidence.add("DELTA", {"generation": self.generation, "ticker": ticker,
                          "native_ts_ms": msg["ts_ms"], "received_at": received, "clock_delta_ms": str(delta_ms),
                          "receipt_hash": receipt["hash"], "seq": sequence, "whole_book_qualified": False})


async def connection(credentials, members, evidence, generation, deadline):
    import websockets
    logger = logging.Logger("atlas.sports.probe.silent")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    session = Session(members, evidence, generation)
    async with NoRedirectConnect(WS, additional_headers=credentials.headers("/trade-api/ws/v2"),
            open_timeout=8, close_timeout=2, ping_interval=10, ping_timeout=5,
            max_size=MAX_BYTES, max_queue=16, logger=logger) as socket:
        evidence.add("HANDSHAKE", {"generation": generation, "authenticated": True,
                                  "host": "external-api-ws.kalshi.com", "at": now()})
        for number, channel in ((1,"ticker"), (2,"orderbook_delta")):
            await asyncio.wait_for(socket.send(json.dumps({"id": number, "cmd":"subscribe", "params":{
                "channels":[channel], "market_tickers": sorted(members)}})), timeout=3)
        ack_deadline = min(deadline, time.monotonic()+8)
        message_deadline = min(deadline, time.monotonic()+30)
        start_wall, start_mono = time.time(), time.monotonic()
        for _ in range(MAX_MESSAGES):
            remaining = (ack_deadline if len(session.acks)<2 else message_deadline)-time.monotonic()
            if remaining <= 0:
                require(len(session.acks)==2, "SUBSCRIPTION_TIMEOUT")
                break
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            except TimeoutError:
                require(len(session.acks)==2, "SUBSCRIPTION_TIMEOUT")
                break
            received, clock = now(), clock_sample()
            jump = abs((time.time()-start_wall)-(time.monotonic()-start_mono))*1000
            require(jump <= CLOCK_MS, "LOCAL_CLOCK_JUMP")
            evidence.add("CLOCK", dict(clock, generation=generation, wall_monotonic_drift_ms=jump))
            try:
                session.process(raw, received, clock)
            except (Refused, ValueError, TypeError, KeyError):
                # Invalid messages invalidate the entire generation; nothing carried on reconnect.
                evidence.add("MESSAGE_REJECTED", {"generation": generation, "reason":"MALFORMED_STALE_INCOMPLETE_OR_UNQUALIFIED"})
                raise Refused("MESSAGE_REJECTED") from None
            if session.quotes_count and session.snapshots_count and session.deltas and len(session.acks)==2:
                break
        evidence.add("SESSION_END", {"generation": generation, "ticker_messages": session.tickers,
                         "delta_messages": session.deltas, "qualified_quotes": session.quotes_count,
                         "qualified_snapshots": session.snapshots_count, "rejection_reasons":sorted(session.rejection_reasons)})
    return session


def guard_selfchecks():
    """Synthetic negative checks, explicitly never native quote evidence."""
    members = {"TEST": {"close_time":"2030-01-01T00:00:00Z"}}
    at = "2026-09-26T00:00:02Z"
    msg = {"market_ticker":"TEST", "market_id":"fixture", "ts_ms":1790380800000,
           "yes_bid_dollars":"0.4", "yes_ask_dollars":"0.5", "yes_bid_size_fp":"1", "yes_ask_size_fp":"1"}
    outcomes = {}
    for name, change in (("stale", {}), ("incomplete", {"yes_ask_size_fp": None})):
        try:
            qualify_ticker(dict(msg, **change), at, {"synchronized":True,"uncertainty_ms":"1","source_uncertainty_ms":"1"}, members)
        except (Refused, ValueError, TypeError): outcomes[name] = True
        else: outcomes[name] = False
    require(all(outcomes.values()), "GUARD_SELFCHECK_FAILED")
    return {"synthetic_only":True,"rejections":outcomes,"native_quote_contribution":0}


def run_probe(directory, source_sha, env=None):
    env = os.environ if env is None else env
    financial_guard(env)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    summary = {"run_id":run_id,"sha":source_sha,"state":"SPORTS_TRANSPORT_BLOCKED",
               "reason":"PROBE_NOT_COMPLETED","capital":"OFF","broker_writes":0,"real_orders_submitted":0,
               "qualified_quote_count":0,"qualified_snapshot_count":0,"handshakes":0,"reconnect":False,
               "scope_verified":False,"sync_ms":SYNC_MS,"max_quote_age_ms":AGE_MS}
    store = Store(directory / "sports-probe.sqlite")
    evidence = Evidence(store, [], run_id)
    deadline = time.monotonic()+MAX_SECONDS
    try:
        selfchecks = guard_selfchecks()
        evidence.add("GUARD_SELFCHECK", selfchecks)
        summary["guard_selfchecks"] = selfchecks
        credentials = Credentials(env)
        evidence.secrets = credentials.secrets
        evidence.add("START", {"sha":source_sha,"at":now(),"max_seconds":MAX_SECONDS,"max_messages_per_connection":MAX_MESSAGES,
            "frozen_relationship_graph_sha256":"da7cfcc393312fc31ba7f090f146550e9febfd6d3362e2aee391283798b4df83",
            "frozen_admission_policy_sha256":"c88af435f649df2966ba2ff26a2619932520dc43115bdf3c9268c4b39ded890f"})
        reader = Reader(evidence, deadline)
        scope = verify_scope(reader.get("/api_keys", credentials=credentials), credentials.key_id)
        evidence.add("SCOPE", scope)
        summary["scope_verified"] = True
        before = membership(reader)
        evidence.add("MEMBERSHIP", before)
        sessions = []
        async def capture():
            async with asyncio.timeout(max(.01, deadline-time.monotonic())):
                for generation in range(CONNECTIONS):
                    try:
                        sessions.append(await connection(credentials, before["markets"], evidence, generation, deadline))
                    except Exception as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        reason = "HTTP_"+str(status) if type(status) is int and 100 <= status <= 599 else "HANDSHAKE_SUBSCRIPTION_OR_MESSAGE_FAILED"
                        if isinstance(exc, Refused) and re.fullmatch(r"[A-Z0-9_]{1,100}", str(exc)): reason = str(exc)
                        evidence.add("CONNECTION_BLOCKED", {"generation":generation,"reason":reason,"generation_admission_revoked":True})
        asyncio.run(capture())
        after = membership(reader, before["milestone_id"])
        evidence.add("MEMBERSHIP_RECHECK", after)
        require(before == after, "MEMBERSHIP_CHANGED")
        summary["handshakes"] = len([e for e in store.events("SP_HANDSHAKE") if e["payload"]["run_id"] == run_id])
        summary["reconnect"] = len(sessions) == CONNECTIONS and all(len(s.acks)==2 and s.tickers and s.deltas for s in sessions)
        # Admit counts only after both session integrity and complete membership recheck.
        summary["qualified_quote_count"] = sum(s.quotes_count for s in sessions)
        summary["qualified_snapshot_count"] = sum(s.snapshots_count for s in sessions)
        summary["rejection_reasons"] = sorted(set().union(*(s.rejection_reasons for s in sessions)))
        if "SOURCE_CLOCK_BOUND_UNPROVEN" in summary["rejection_reasons"] and not summary["qualified_snapshot_count"]:
            raise Refused("SOURCE_CLOCK_BOUND_UNPROVEN")
        require(summary["reconnect"] and summary["qualified_quote_count"] > 0
                and summary["qualified_snapshot_count"] > 0, "NATIVE_SESSION_OR_QUALIFIED_SNAPSHOT_MISSING")
        summary.update(state="SPORTS_TRANSPORT_READY", reason=None)
    except Refused as exc:
        # Only our own fixed codes leave the process; arbitrary exception text never does.
        code = str(exc)
        summary["reason"] = code if re.fullmatch(r"[A-Z0-9_]{1,100}", code) else "VALIDATION_REFUSED"
    except Exception:
        summary["reason"] = "PROBE_RUNTIME_FAILURE"
    finally:
        summary["handshakes"] = len([e for e in store.events("SP_HANDSHAKE") if e["payload"]["run_id"] == run_id])
        evidence.add("RESULT", summary)
        summary["anchor"] = store.anchor()
        events = [e for e in store.events() if e["payload"].get("run_id") == run_id]
        raw = canonical({"schema":"sports-startup-proof/1","summary":summary,"events":events})
        path = directory / ("sports-probe-"+run_id+".json")
        with path.open("xb") as target: target.write(raw)
        summary["evidence_sha256"] = hashlib.sha256(raw).hexdigest()
        summary["evidence_path"] = str(path)
        store.close()
    return summary


def _child_probe(directory, sha, sender):
    try:
        result = run_probe(directory, sha)
    except Exception:
        result = {"state":"SPORTS_TRANSPORT_BLOCKED","reason":"PROBE_START_OR_STORAGE_FAILURE"}
    try: sender.send(result)
    finally: sender.close()


def bounded_probe(directory, sha):
    """A process deadline also bounds DNS, TLS and a slow/dripping HTTP body."""
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_probe, args=(str(directory), sha, sender), daemon=True)
    process.start()
    sender.close()
    try:
        if receiver.poll(MAX_SECONDS+10):
            try: return receiver.recv()
            except EOFError: return {"state":"SPORTS_TRANSPORT_BLOCKED","reason":"PROBE_PROCESS_ENDED_WITHOUT_RESULT"}
        return {"state":"SPORTS_TRANSPORT_BLOCKED","reason":"PROBE_HARD_DEADLINE",
                "qualified_quote_count":0,"qualified_snapshot_count":0}
    finally:
        if process.is_alive(): process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        receiver.close()


def serve_probe(identity):
    """Explicit temporary probe-only mode; original BTC stores remain untouched."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from .service import persistent_directory
    financial_guard(os.environ)
    directory = persistent_directory() / "sports-probe"
    state = {"state":"SPORTS_PROBE_STARTING","sha":identity["sha"],"capital":"OFF",
             "broker_writes":0,"real_orders_submitted":0,"qualified_quote_count":0,"qualified_snapshot_count":0}
    lock = threading.Lock()
    def worker():
        try: result = bounded_probe(directory, identity["sha"])
        except Exception: result = {"state":"SPORTS_TRANSPORT_BLOCKED","reason":"PROBE_START_OR_STORAGE_FAILURE"}
        with lock: state.update(result)
        print(json.dumps({"at":now(), **state}), flush=True)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {"/health", "/status"}:
                self.send_error(404); return
            with lock: raw = canonical(state)
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args): pass
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT","8080"))), Handler)
    threading.Thread(target=worker, daemon=True).start()
    try: server.serve_forever()
    finally: server.server_close()
