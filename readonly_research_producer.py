"""Isolated public-market capture and authenticated research export.

This process has no financial authority. Its default mode uses one fixed,
credential-free, verified-TLS market GET and the unchanged v3 contract. The
explicit v4 opt-in additionally captures fixed-host event and series GETs and
replays their identities under a separate versioned contract. The listener
cannot start captures or alter state. A bounded-rate background worker refuses
missing facts or metadata; it never fabricates a source or authority.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from candidate_contract import canonical_json, compute_checksum, validate_record
from durable_append import exclusive_lock, file_generation, sync_path
from research_feed import ResearchFeed, candidate_from_market
from research_spool import BoundedSpool, RESERVATION_LOCK

SOURCE_URL = "https://external-api.kalshi.com/trade-api/v2/markets?status=open&series_ticker=KXBTC15M&limit=10"
SOURCE_AUTHORITY = "kalshi-public-market-data"
CAPTURE_SCHEMA = "atlas-public-market-capture-v1"
BINDING_SCHEMA = "atlas-unmodified-market-binding-v1"
MAX_CAPTURE_BYTES = 262144
MAX_RECORD_BYTES = 524288
MAX_RESPONSE_BYTES = 4194304
CAPTURE_INTERVAL_SECONDS = 60
MAX_MARKETS = 10
SHADOW_ONLY = True
FORBIDDEN_MODULES = frozenset({
    "order_manager", "execution_engine", "kalshi_client", "risk_manager",
    "equity_ledger", "position_manager", "position_sizer", "trade_logger",
    "kalshi_alpha_bot", "state_restore", "transport_intent", "continuity",
    "persistence", "recovery", "state_authority",
})
FORBIDDEN_ENV = frozenset({
    "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_DEMO_KEY_ID",
    "KALSHI_DEMO_PRIVATE_KEY", "KALSHI_PROD_KEY_ID", "KALSHI_PROD_PRIVATE_KEY",
    "ALLOW_ORDER_SUBMISSION", "LIVE_TRADING", "LIVE_TRADING_CONFIRMED",
    "LIVE_BROKER_WRITES_AUTHORIZED", "KALSHI_ENV_CONFIRM", "DEMO_TRADING",
    "MODEL_APPROVED_FOR_LIVE", "ALLOW_FALLBACK_CAPITAL", "PROD_ACCESS_MODE",
    "EXECUTION_MODE", "CAPITAL", "CAPITAL_ENABLED", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "ANTHROPIC_API_KEY",
})


class CaptureRefused(ValueError):
    """Observed bytes cannot establish the documented source contract."""


def assert_isolated(env=None):
    """Inspect names only; even empty authority variables are refused."""
    names = set(os.environ if env is None else env)
    offending = sorted((names & FORBIDDEN_ENV) | {
        name for name in names if name.startswith(("KALSHI_", "BROKER_"))})
    imported = sorted(FORBIDDEN_MODULES & set(sys.modules))
    if offending or imported:
        raise RuntimeError("RESEARCH_STARTUP_REFUSED: authority variables or modules present")
    return {"shadow_only": True, "execution_imports": len(imported),
            "broker_credentials_present": False, "financial_authority_present": False}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaptureRefused("duplicate JSON member")
        result[key] = value
    return result


def _constant(_value):
    raise CaptureRefused("non-finite JSON constant")


def parse_market_capture(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_CAPTURE_BYTES:
        raise CaptureRefused("capture byte bound")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CaptureRefused("invalid source JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise CaptureRefused("expected markets envelope")
    markets = payload["markets"]
    if len(markets) > MAX_MARKETS or any(not isinstance(m, dict) for m in markets):
        raise CaptureRefused("malformed or excessive markets collection")
    cursor = payload.get("cursor", "")
    if not isinstance(cursor, str):
        raise CaptureRefused("invalid market cursor")
    # This is explicitly a bounded sample, never account-wide completeness.
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CaptureRefused("source redirects are forbidden")


def capture_public_source(url):
    """Fixed-host GET; identifiers cannot alter host, route class or query."""
    metadata = r"https://external-api\.kalshi\.com/trade-api/v2/(?:events|series)/[A-Z0-9][A-Z0-9_-]{0,199}"
    if type(url) is not str or (url != SOURCE_URL and not re.fullmatch(metadata, url)):
        raise CaptureRefused("source endpoint outside fixed allowlist")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "User-Agent": "Atlas-Shadow-ReadOnly-Research/1",
    })
    with opener.open(request, timeout=10) as response:
        if response.status != 200 or response.geturl() != url:
            raise CaptureRefused("unexpected source response")
        if response.headers.get_content_type() != "application/json":
            raise CaptureRefused("source content type")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise CaptureRefused("source content encoding")
        raw = response.read(MAX_CAPTURE_BYTES + 1)
        observed = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    if len(raw) > MAX_CAPTURE_BYTES:
        raise CaptureRefused("source response exceeds byte bound")
    return raw, observed


def capture_public_markets():
    """Legacy default: only the original fixed market GET."""
    return capture_public_source(SOURCE_URL)


class RollingResearchSpool(BoundedSpool):
    """Explicit bounded research retention, not an authoritative ledger.

    Called inside the inherited capacity lock. Eviction touches only complete
    entries of this dedicated ephemeral spool. In-progress reservations count
    fully and cannot be evicted. Each candidate embeds the full capture, so a
    separately pruned capture is never the candidate's only source preimage.
    """
    def _reserve_and_write(self, record, payload):
        self.prune()
        while True:
            records, partials = self._scan()
            occupied = len(records) + len(partials)
            size = sum(item[1] for item in records + partials)
            if occupied < self.max_records and size + len(payload) <= self.max_bytes:
                break
            if not records or not self._remove(records[0][0]):
                self.stats["dropped_full"] += 1
                return False
            self.stats["pruned"] += 1
        return super()._reserve_and_write(record, payload)


def _new_spool(directory, *, records=100, byte_cap=33554432):
    return RollingResearchSpool(directory, max_records=records, max_bytes=byte_cap,
                        max_record_bytes=MAX_RECORD_BYTES, max_age_s=21600)


class Producer:
    def __init__(self, data_dir, *, fetch=capture_public_markets,
                 source_contract="legacy-v3", metadata_fetch=capture_public_source):
        if source_contract not in ("legacy-v3", "market-event-series-v4"):
            raise CaptureRefused("unsupported explicit source contract mode")
        self.source_contract = source_contract
        self.metadata_fetch = metadata_fetch
        self.data_dir = os.path.realpath(data_dir)
        self.spool = _new_spool(os.path.join(self.data_dir, "research_spool"))
        self.captures = _new_spool(os.path.join(self.data_dir, "research_captures"),
                                   records=32, byte_cap=16777216)
        self.feed = ResearchFeed(directory=self.spool.directory, start_writer=False)
        self.fetch = fetch
        self.stop = threading.Event()
        self.worker = None
        self._status = {"capture_attempts": 0, "captures_durable": 0,
                        "candidates_durable": 0, "candidates_refused": 0,
                        "last_capture": None, "last_error": None,
                        "last_source_http_status": None,
                        "last_schema_refusal": None,
                        "last_contract_refusal_fields": [],
                        "authenticated_reads": 0, "authenticated_200_reads": 0,
                        "authentication_refusals": 0}

    def health(self):
        # No disk scan, provider request, capture or caller-controlled action.
        try:
            isolation = assert_isolated()
        except RuntimeError:
            isolation = {"shadow_only": True, "isolation_verified": False,
                         "execution_imports": len(FORBIDDEN_MODULES & set(sys.modules))}
        else:
            isolation["isolation_verified"] = True
        return {"service": "atlas-readonly-research-producer", "shadow_only": True,
                "source_contract": self.source_contract,
                "source": SOURCE_URL, "source_scope": "bounded public market sample",
                "capture_authority_qualification": "not a settlement authority attestation",
                "retention": "rolling bounded research opportunity window; not a ledger",
                **isolation,
                **self._status}

    def _record(self, capture, market, index):
        candidate = candidate_from_market(
            market, {}, raw_book=market,
            cycle_id=capture["record_sha256"],
            observed_at_utc=capture["emitted_at_utc"])
        record = self.feed._build(candidate)
        if record is None:
            return None
        record["source_capture"] = {
            "schema": BINDING_SCHEMA, "capture": capture,
            "market_pointer": f"/markets/{index}",
            "canonical_market": canonical_json(market),
            "canonical_market_sha256": hashlib.sha256(
                canonical_json(market).encode("utf-8")).hexdigest(),
            "transformation": "none; audited candidate_from_market unit conversion only",
        }
        record["record_sha256"] = compute_checksum(record)
        if validate_record(record):
            raise CaptureRefused("generated record violated audited contract")
        return record

    def ingest(self, raw, observed_at):
        """Ingest an internally captured response; never exposed by HTTP."""
        payload = parse_market_capture(raw)
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise CaptureRefused("capture timestamp") from exc
        if observed.tzinfo is None:
            raise CaptureRefused("capture requires timezone")
        capture = {
            "schema": CAPTURE_SCHEMA, "source_authority": SOURCE_AUTHORITY,
            "environment": "prod", "source_scope": "public-market-data",
            "source_url": SOURCE_URL,
            "http_method": "GET", "http_status": 200,
            "transport_authentication": "TLS-server-certificate-verification",
            "emitted_at_utc": observed_at,
            "response_bytes_base64": base64.b64encode(raw).decode("ascii"),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "response_byte_count": len(raw), "complete_account_snapshot": False,
            "sample_has_more": bool(payload.get("cursor")),
        }
        capture["record_sha256"] = compute_checksum(capture)
        if not self.captures.write(capture):
            raise CaptureRefused("capture durability not confirmed")
        self._status["captures_durable"] += 1
        written = refused = 0
        for index, market in enumerate(payload["markets"]):
            record = self._record(capture, market, index)
            if record is None:
                refused += 1
                self._status["last_contract_refusal_fields"] = sorted({
                    e.split(":", 1)[0][:80] for e in self.feed.last_errors})[:32]
                continue
            if not self.spool.write(record):
                raise CaptureRefused("candidate durability not confirmed")
            written += 1
        self._status["candidates_durable"] += written
        self._status["candidates_refused"] += refused
        self._status["last_capture"] = capture["record_sha256"]
        return {"written": written, "refused": refused,
                "capture_sha256": capture["record_sha256"]}

    def ingest_bundle(self, bundle):
        """Offline supplied complete v4 bundle; never exposed as HTTP control.

        The default collector remains legacy; a separate explicit opt-in mode
        captures parent metadata. No authority is invented by this interface. The
        candidate retains all three raw preimages in its own durable record.
        """
        from research_source_contract_v4 import build_record
        record = build_record(bundle)
        if not self.spool.write(record):
            raise CaptureRefused("versioned candidate durability not confirmed")
        self._status["candidates_durable"] += 1
        return {"written": 1, "record_sha256": record["record_sha256"],
                "authority_qualification": "NOT_ESTABLISHED"}

    def _ingest_v4(self, raw, observed_at):
        """Bounded automatic join; no alternate source or metadata fallback.

        At most ten event requests and one series request follow one market
        response. Each is a fixed-host public GET. Any unsupported envelope,
        403, missing identity or absent source refuses the affected poll.
        """
        from research_source_contract_v4 import (BUNDLE_SCHEMA, ORIGIN,
            SourceContractError, capture_response, _identity, _capture)
        try:
            payload = parse_market_capture(raw)
        except CaptureRefused as exc:
            raise SourceContractError("market schema unqualified: " + str(exc)[:200]) from exc
        market_capture = capture_response(raw, observed_at, role="market", url=SOURCE_URL)
        if not self.captures.write(market_capture):
            raise CaptureRefused("v4 market capture durability not confirmed")
        self._status["captures_durable"] += 1
        self._status["last_capture"] = market_capture["record_sha256"]
        events, series_captures = {}, {}
        bundles = []
        for index, market in enumerate(payload["markets"]):
            event_id = _identity(market.get("event_ticker"), "market event ticker")
            if event_id not in events:
                url = ORIGIN + "/events/" + event_id
                event_raw, event_time = self.metadata_fetch(url)
                evidence = capture_response(event_raw, event_time, role="event", url=url)
                body = _capture(evidence, role="event", expected_url=url)
                event = body.get("event")
                if type(event) is not dict or event.get("event_ticker") != event_id:
                    raise SourceContractError("event response identity/schema unqualified")
                series_id = _identity(event.get("series_ticker"), "event series ticker")
                if series_id != "KXBTC15M":
                    raise SourceContractError("event series outside fixed source profile")
                if not self.captures.write(evidence):
                    raise CaptureRefused("v4 event capture durability not confirmed")
                self._status["captures_durable"] += 1
                events[event_id] = (evidence, series_id)
            event_capture, series_id = events[event_id]
            if series_id not in series_captures:
                url = ORIGIN + "/series/" + series_id
                series_raw, series_time = self.metadata_fetch(url)
                evidence = capture_response(series_raw, series_time, role="series", url=url)
                if not self.captures.write(evidence):
                    raise CaptureRefused("v4 series capture durability not confirmed")
                self._status["captures_durable"] += 1
                series_captures[series_id] = evidence
            bundles.append({"schema": BUNDLE_SCHEMA, "market_capture": market_capture,
                "market_pointer": f"/markets/{index}", "event_capture": event_capture,
                "series_capture": series_captures[series_id]})
        # Validate the entire captured batch before publishing any candidate.
        from research_source_contract_v4 import build_record
        for bundle in bundles:
            build_record(bundle)
        written = 0
        for bundle in bundles:
            self.ingest_bundle(bundle)
            written += 1
        return {"written": written, "refused": 0,
                "source_contract": self.source_contract,
                "authority_qualification": "NOT_ESTABLISHED"}

    def poll(self):
        self._status["capture_attempts"] += 1
        self._status["last_source_http_status"] = None
        self._status["last_schema_refusal"] = None
        try:
            raw, observed_at = self.fetch()
            self._status["last_source_http_status"] = 200
            result = (self._ingest_v4(raw, observed_at)
                      if self.source_contract == "market-event-series-v4"
                      else self.ingest(raw, observed_at))
            self._status["last_error"] = None
            return result
        except Exception as exc:  # no request headers, tokens or body in logs
            self._status["last_error"] = type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                self._status["last_source_http_status"] = exc.code
            if type(exc).__name__ == "SourceContractError":
                self._status["last_schema_refusal"] = str(exc)[:300]
            return None

    def _run(self):
        while not self.stop.is_set():
            self.poll()
            self.stop.wait(CAPTURE_INTERVAL_SECONDS)

    def start(self):
        self.worker = threading.Thread(target=self._run, name="public-capture", daemon=True)
        self.worker.start()

    def verify_capture_binding(self, record):
        """Recompute from retained exact bytes; never trust a capture label."""
        if validate_record(record):
            raise CaptureRefused("invalid persisted candidate")
        if record.get("schema") == "atlas-research-candidate-v4":
            return True  # shared validator independently replayed all raw captures
        binding = record.get("source_capture")
        if not isinstance(binding, dict) or binding.get("schema") != BINDING_SCHEMA:
            raise CaptureRefused("missing versioned capture binding")
        capture = binding.get("capture")
        if not isinstance(capture, dict) or capture.get("schema") != CAPTURE_SCHEMA:
            raise CaptureRefused("missing capture")
        if capture.get("record_sha256") != compute_checksum(capture):
            raise CaptureRefused("capture checksum")
        fixed = {"source_authority": SOURCE_AUTHORITY, "source_url": SOURCE_URL,
                 "http_method": "GET", "http_status": 200,
                 "environment": "prod", "source_scope": "public-market-data",
                 "transport_authentication": "TLS-server-certificate-verification",
                 "complete_account_snapshot": False}
        if any(type(capture.get(k)) is not type(v) or capture.get(k) != v
               for k, v in fixed.items()):
            raise CaptureRefused("source identity mismatch")
        try:
            raw = base64.b64decode(capture["response_bytes_base64"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureRefused("capture preimage") from exc
        if hashlib.sha256(raw).hexdigest() != capture.get("response_sha256") \
                or type(capture.get("response_byte_count")) is not int \
                or len(raw) != capture["response_byte_count"]:
            raise CaptureRefused("raw capture checksum")
        payload = parse_market_capture(raw)
        if type(capture.get("sample_has_more")) is not bool \
                or capture["sample_has_more"] != bool(payload.get("cursor")):
            raise CaptureRefused("capture sample continuation mismatch")
        pointer = binding.get("market_pointer", "")
        if not isinstance(pointer, str) or not re.fullmatch(r"/markets/[0-9]", pointer):
            raise CaptureRefused("market pointer")
        index = int(pointer.rsplit("/", 1)[1])
        if index >= len(payload["markets"]):
            raise CaptureRefused("market pointer outside capture")
        expected = self._record(capture, payload["markets"][index], index)
        if expected != record:
            raise CaptureRefused("candidate differs from exact unmodified source")
        return True

    def page(self, *, cursor="", limit=100):
        rows = []
        # Serializes scan/read/sync with writers. Any metadata or durability
        # uncertainty is 503; readable bytes alone never become publishable.
        with exclusive_lock(os.path.join(self.spool.directory, RESERVATION_LOCK), timeout=1):
            records, _partials = self.spool._scan()
            for name, size, _mtime in records:
                if size > MAX_RECORD_BYTES:
                    raise CaptureRefused("persisted record exceeds bound")
                path = os.path.join(self.spool.directory, name)
                if os.path.islink(path):
                    raise CaptureRefused("candidate symlink")
                with open(path, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise CaptureRefused("non-regular candidate")
                    generation = file_generation(info)
                    data = handle.read(MAX_RECORD_BYTES + 1)
                    if len(data) != info.st_size:
                        raise CaptureRefused("candidate changed while read")
                record = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
                self.verify_capture_binding(record)
                if not sync_path(path, expected_generation=generation):
                    raise CaptureRefused("candidate disappeared before durability barrier")
                rows.append(record)
        rows.sort(key=lambda r: (r["emitted_at_utc"], r["record_sha256"]))
        if cursor:
            found = next((i for i, row in enumerate(rows)
                          if row["record_sha256"] == cursor), None)
            if found is None:
                raise CaptureRefused("cursor expired; bounded sample changed")
            rows = rows[found + 1:]
        selected, total = [], 0
        for row in rows[:limit]:
            size = len(canonical_json(row).encode("utf-8"))
            if total + size > MAX_RESPONSE_BYTES:
                break
            selected.append(row)
            total += size
        versions = sorted({row["schema"] for row in selected})
        default_version = ("atlas-research-candidate-v4" if self.source_contract ==
                           "market-event-series-v4" else "atlas-research-candidate-v3")
        version = (versions[0] if len(versions) == 1 else
                   "atlas-research-candidate-page-v1" if versions else default_version)
        return {"dataset": "candidates", "schema_version": version,
                **({"record_schema_versions": versions} if version != "atlas-research-candidate-v3" else {}),
                "source_scope": "bounded public market sample", "rows": selected,
                "has_more": len(rows) > len(selected),
                "next_cursor": selected[-1]["record_sha256"] if selected else ""}


def handler_for(producer, token):
    if not isinstance(token, str) or len(token) < 16 or not token.isascii() \
            or any(c.isspace() for c in token):
        raise RuntimeError("research bearer token is absent or malformed")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass  # neither credentials nor attacker-controlled paths are logged

        def _reply(self, status, body):
            data = canonical_json(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                health = producer.health()
                return self._reply(200 if health["isolation_verified"] else 503, health)
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/api/research/v1/candidates":
                return self._reply(404, {"error": "route not found"})
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"),
                                                        ("Bearer " + token).encode("utf-8")):
                producer._status["authentication_refusals"] += 1
                return self._reply(401, {"error": "unauthorized"})
            producer._status["authenticated_reads"] += 1
            try:
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True,
                                              max_num_fields=2)
                if set(query) - {"limit", "cursor"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("invalid parameters")
                raw_limit = query.get("limit", ["100"])[0]
                if not re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit):
                    raise ValueError("invalid limit")
                limit = int(raw_limit)
                cursor = query.get("cursor", [""])[0]
                if limit > 100 or (cursor and not re.fullmatch(r"[0-9a-f]{64}", cursor)):
                    raise ValueError("invalid page")
            except ValueError:
                return self._reply(400, {"error": "invalid page parameters"})
            try:
                page = producer.page(cursor=cursor, limit=limit)
            except Exception:
                return self._reply(503, {"error": "research evidence unavailable"})
            producer._status["authenticated_200_reads"] += 1
            return self._reply(200, page)

        def _forbidden(self):
            return self._reply(405, {"error": "GET only; no control interface"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _forbidden

    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    assert_isolated()
    token = os.environ.get("RESEARCH_API_TOKEN", "")
    data_dir = os.environ.get("DATA_DIR", "")
    if not data_dir or not os.path.isabs(data_dir):
        raise RuntimeError("absolute dedicated DATA_DIR required")
    producer = Producer(data_dir, source_contract=os.environ.get(
        "RESEARCH_SOURCE_CONTRACT", "legacy-v3"))
    handler = handler_for(producer, token)
    isolation = assert_isolated()  # include imports made during construction
    print(canonical_json({"event": "RESEARCH_PRODUCER_STARTED", **isolation,
                          "source_url": SOURCE_URL, "data_dir": data_dir}), flush=True)
    server = BoundedHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), handler)
    producer.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        producer.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
