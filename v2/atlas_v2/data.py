"""Bounded public GET-only raw collection; no credentials or broker write API.

Reference: https://docs.kalshi.com/getting_started/quick_start_market_data
Schema: https://docs.kalshi.com/api-reference/market/get-markets
Collection does not imply execution freshness or qualified settlement authority.
"""
import base64
import hashlib
import re
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .domain import Refused, canonical, decimal, digest, now, strict_json, utc

ORIGIN = "https://external-api.kalshi.com"
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 20


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("redirect refused")


class PublicReader:
    """Only the fixed public markets endpoint is callable."""
    def get_markets(self, series, cursor=""):
        if not re.fullmatch(r"[A-Z0-9]+", series) or not isinstance(cursor, str) or len(cursor) > 2048:
            raise Refused("invalid public query")
        query = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            query["cursor"] = cursor
        url = ORIGIN + "/trade-api/v2/markets?" + urlencode(query)
        request = Request(url, method="GET", headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        start = now()
        try:
            response = build_opener(NoRedirect).open(request, timeout=15)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_BYTES + 1)
            length = response.headers.get("Content-Length")
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            complete = (len(raw) <= MAX_BYTES and response.headers.get("Content-Range") is None
                        and (length is None or length.isdigit() and int(length) == len(raw)))
            return {"url": url, "method": "GET", "status": response.status,
                    "started_at": start, "received_at": now(), "raw": raw[:MAX_BYTES],
                    "transport_complete": complete, "content_type": content_type,
                    "content_range": response.headers.get("Content-Range"),
                    "response_url": response.geturl(), "request_cursor": cursor}


def capture_scan(store, reader, series="KXBTC15M"):
    """Raw pages survive a late failure; no partial scan becomes complete."""
    scan_id = str(uuid.uuid4())
    cursor, seen, pages, markets = "", set(), [], []
    try:
        for _ in range(MAX_PAGES):
            response = reader.get_markets(series, cursor)
            raw = response.pop("raw")
            receipt = store.append("raw:" + scan_id + ":" + str(len(pages)), "RAW_HTTP", {
                **response, "body_base64": base64.b64encode(raw).decode(),
                "body_sha256": hashlib.sha256(raw).hexdigest(), "series": series})
            pages.append(receipt["hash"])
            if (response["transport_complete"] is not True or response["status"] != 200
                    or response["content_type"] != "application/json" or response.get("content_range") is not None
                    or response["method"] != "GET" or response["response_url"] != response["url"]
                    or not response["url"].startswith(ORIGIN + "/trade-api/v2/markets?")
                    or response["request_cursor"] != cursor
                    or utc(response["received_at"]) < utc(response["started_at"])):
                raise Refused("transport/provenance incomplete")
            data = strict_json(raw)
            if (not isinstance(data, dict) or set(data) != {"markets", "cursor"}
                    or not isinstance(data["markets"], list) or not isinstance(data["cursor"], str)):
                raise Refused("unknown/incomplete markets envelope")
            for row in data["markets"]:
                if not isinstance(row, dict) or not isinstance(row.get("ticker"), str) or row["ticker"] in seen:
                    raise Refused("invalid/duplicate market across pages")
                if not row["ticker"].startswith(series + "-"):
                    raise Refused("market outside declared universe")
                seen.add(row["ticker"])
                markets.append((row, receipt))
            cursor = data["cursor"]
            if not cursor:
                break
            # Cursor identities live separately from ticker identities.
            marker = "cursor:" + cursor
            if marker in seen:
                raise Refused("repeated cursor")
            seen.add(marker)
        else:
            raise Refused("page bound reached without terminal cursor")
        with store.transaction():
            scan = store.append("scan:" + scan_id, "SCAN", {"series": series, "pages": pages,
                "complete": True, "terminal_cursor": "", "market_count": len(markets),
                "atomic_exchange_snapshot": False})
            for row, receipt in markets:
                # Invalid quotes are retained raw and counted, never silently fixed.
                try:
                    payload = observation(row, receipt, scan)
                except (Refused, KeyError, TypeError) as exc:
                    store.append("rejected:" + scan_id + ":" + row["ticker"], "REJECTED_OBSERVATION",
                                 {"scan": scan["hash"], "receipt": receipt["hash"], "ticker": row["ticker"], "reason": str(exc)})
                else:
                    store.append("observation:" + scan_id + ":" + row["ticker"], "OBSERVATION", payload)
        return scan
    except Exception as exc:
        store.append("failed-scan:" + scan_id, "SCAN_FAILED", {"series": series, "pages": pages,
                     "complete": False, "cursor": cursor, "reason": type(exc).__name__ + ":" + str(exc)[:300]})
        raise


def observation(row, receipt, scan):
    bid, ask = decimal(row["yes_bid_dollars"]), decimal(row["yes_ask_dollars"])
    if not 0 <= bid <= ask <= 1:
        raise Refused("invalid bid/ask")
    at, close = receipt["payload"]["received_at"], row["close_time"]
    if utc(close) <= utc(at) or row["status"] not in {"active", "open"}:
        raise Refused("closed market")
    if not isinstance(row["event_ticker"], str) or not row["event_ticker"]:
        raise Refused("event identity absent")
    depth = row.get("yes_ask_size_fp")
    if depth is not None and decimal(depth) < 0:
        raise Refused("negative depth")
    return {"schema": "atlas-v2-observation/1", "observed_at": at,
            "source_quote_time": row.get("updated_time"), "ticker": row["ticker"],
            "event_id": row["event_ticker"], "bid": str(bid), "ask": str(ask),
            "depth": depth, "spread": str(ask - bid), "close_at": close,
            "seconds_to_close": (utc(close) - utc(at)).total_seconds(),
            "baseline_probability": str(ask), "underlying": None, "features": None,
            "model_id": None, "model_hash": None, "decision_probability": None,
            "execution_eligible": False, "eligibility_reason": "NO_LOCKED_APPROVED_CANDIDATE",
            "settlement": None, "settlement_authority": None,
            "receipt_hash": receipt["hash"], "scan_hash": scan["hash"],
            "raw_market_hash": digest(row), "source": "Kalshi public markets GET"}
