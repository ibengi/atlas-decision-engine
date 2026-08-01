"""
fetch_btc_candles.py — historical BTC 1-minute OHLCV fetcher (QA3 step 1).

Fetches months of 1-minute BTC candles into a resumable, gzipped-CSV cache in
research/data/, with a metadata sidecar recording exactly which venue and
source produced the data, and writes research/data/CANDLE-QUALITY-<venue>.md.

THIS IS A DATA-ACQUISITION TOOL ONLY. It never touches the Kalshi book, never
computes probabilities, and never repairs gaps: the audit measures what the
data actually contains (see btc_candle_quality.py).

Sources (reusing btc_context.py's conventions where applicable)
===============================================================
Order tried (CLI --providers or --venue):
  1. binance_rest  — https://api.binance.com/api/v3/klines, BTCUSDT 1m,
                     1000 candles/request via startTime paging. This is the
                     engine's primary kline source. Geo-blocked (HTTP 451)
                     from some locations — verified blocked from this host.
  2. binance_vision — official public archive https://data.binance.vision
                     (same venue: Binance BTCUSDT spot klines). Monthly zips
                     for full months + daily zips for partial months.
                     No rate limits, no geo-block. Used as the practical
                     primary on hosts where the REST API is blocked.
  3. bitstamp      — https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=60,
                     start/limit paging (1000/request). Verified reachable
                     from this host and honours historical `start`.
Kraken OHLC and Coinbase Exchange candles are NOT implemented as bulk
providers: Kraken serves at most the last 720 1m candles and Coinbase at most
the last 300 (per their public API), so neither can cover months of history.
Both were probed from this host and confirmed reachable but depth-limited;
this is recorded in the quality report so the audit knows why they were not
used.

Cache layout (research/data/, gitignored)
=========================================
  btc_1m_<venue>.csv.gz       gzipped CSV, columns ts,open,high,low,close,volume
                              (ts = integer unix seconds, UTC minute grid).
                              Append-only: every chunk is written as one
                              complete, validated gzip member (temp file +
                              binary append), so a crash can never leave a
                              half-written member. Duplicate minutes can occur
                              across resumes; the loader collapses them
                              keep-last and the quality report counts them.
  btc_1m_<venue>.meta.json    sidecar: venue, symbol, requested range, per-
                              chunk provenance (source, ts span, row count),
                              covered spans, and any failed ranges.
  CANDLE-QUALITY-<venue>.md   data quality report (written at the end of a run
                              and by --report-only).

Resumability
============
At startup the cache is scanned and the actually-covered minute spans are
recomputed from the file content (not trusted from the sidecar), so a crash
between a member append and the metadata update cannot cause data loss or
silent duplication beyond the dedup the loader already handles. Chunks fully
inside an existing covered span are skipped; the rest are fetched.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from research.btc_candle_quality import (
    STEP_S,
    find_bad_quotes,
    find_duplicates,
    find_nonmonotonic,
    render_markdown,
    summarize,
)
from research.btc_candle_loader import CSV_COLUMNS, cache_columns

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

SYMBOL = "BTCUSDT"
INTERVAL_S = 60

# ---------------------------------------------------------------------------
# HTTP helpers (mirror btc_context.py's conventions: telemetry, bounded retry)
# ---------------------------------------------------------------------------

HTTP_TIMEOUT_S = 30.0
MAX_RETRIES = 4
RETRY_BASE_S = 2.0


class ProviderError(RuntimeError):
    """Permanent provider failure (geo-block, auth, bad payload)."""


class ProviderUnavailable(ProviderError):
    """The provider cannot serve this range at all (e.g. geo-blocked)."""


def http_get_bytes(url: str, params: Optional[dict] = None,
                   retries: int = MAX_RETRIES) -> Tuple[bytes, dict]:
    """GET with bounded exponential backoff + jitter.

    Retries on 429/5xx and network errors; honors Retry-After when present.
    Returns (content_bytes, meta) or raises ProviderError.
    meta = {status, elapsed_ms, retries, error}
    """
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S)
            if r.status_code == 429 or r.status_code >= 500:
                raise ProviderError(f"HTTP {r.status_code}")
            if r.status_code in (403, 451):
                raise ProviderUnavailable(
                    f"HTTP {r.status_code} geo/unavailable: {url}")
            if r.status_code == 404:
                raise ProviderError(f"HTTP 404: {url}")
            r.raise_for_status()
            return r.content, {"status": r.status_code,
                               "elapsed_ms": round((time.time() - t0) * 1e3, 1),
                               "retries": attempt}
        except ProviderUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — retry logic below
            last = e
            delay = RETRY_BASE_S * (2 ** attempt) + (time.time() % 0.5)
            if attempt < retries:
                time.sleep(delay)
    raise ProviderError(f"giving up after {retries} retries: {last}")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class BaseProvider:
    name: str = ""
    venue: str = ""
    symbol: str = SYMBOL
    #: human-readable provenance, recorded per chunk and in the meta sidecar
    source_desc: str = ""

    def iter_chunks(self, start_ts: int, end_ts: int,
                    covered_spans: List[Tuple[int, int]]) -> Iterator[dict]:
        """Yield chunk dicts: {start_ts, end_ts, rows, source, n_expected}.

        ``rows`` is a list of {ts, open, high, low, close, volume} dicts,
        strictly ascending and non-empty. Chunks fully inside a covered span
        must NOT be yielded (skipped by the caller's resumability wrapper
        before this is invoked, so providers can ignore it).
        Raises ProviderUnavailable when it cannot serve the range at all.
        """
        raise NotImplementedError


def _parse_row(ts, open_, high, low, close, volume) -> dict:
    return {"ts": int(ts), "open": float(open_), "high": float(high),
            "low": float(low), "close": float(close), "volume": float(volume)}


class BinanceRestProvider(BaseProvider):
    name = "binance_rest"
    venue = "binance"
    source_desc = ("Binance REST API /api/v3/klines (BTCUSDT 1m, "
                   "1000 candles/request) — the engine's primary kline source")

    URL = "https://api.binance.com/api/v3/klines"
    PAGE = 1000

    def iter_chunks(self, start_ts: int, end_ts: int,
                    covered_spans: List[Tuple[int, int]]) -> Iterator[dict]:
        cursor = start_ts
        while cursor <= end_ts:
            content, meta = http_get_bytes(
                self.URL,
                {"symbol": self.symbol, "interval": "1m",
                 "startTime": cursor * 1000, "limit": self.PAGE})
            data = json.loads(content)
            if not isinstance(data, list) or not data:
                raise ProviderError(f"binance_rest: unexpected payload "
                                    f"{str(data)[:200]}")
            rows = []
            for k in data:
                try:
                    rows.append(_parse_row(k[0] / 1000, k[1], k[2], k[3],
                                           k[4], k[5]))
                except (IndexError, TypeError, ValueError) as e:
                    raise ProviderError(f"binance_rest: bad kline {k!r}: {e}")
            last_ts = rows[-1]["ts"]
            chunk = {"start_ts": rows[0]["ts"], "end_ts": last_ts,
                     "rows": rows, "source": self.source_desc,
                     "n_expected": len(rows)}
            yield chunk
            if last_ts >= end_ts or len(rows) < self.PAGE:
                break
            cursor = last_ts + STEP_S


class BinanceVisionProvider(BaseProvider):
    name = "binance_vision"
    venue = "binance"
    source_desc = ("Binance public archive data.binance.vision "
                   "(spot monthly/daily klines BTCUSDT 1m) — official "
                   "Binance data, same venue as the engine's kline source")

    BASE = "https://data.binance.vision/data/spot"
    _COLS = 12  # open_time(us),o,h,l,c,v,close_time(us),qv,count,tbv,tbqv,ignore

    @staticmethod
    def _month_bounds(ts: int) -> Tuple[int, int]:
        d = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
        start = _dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc)
        end = (_dt.datetime(d.year + 1, 1, 1, tzinfo=_dt.timezone.utc)
               if d.month == 12 else
               _dt.datetime(d.year, d.month + 1, 1, tzinfo=_dt.timezone.utc))
        return int(start.timestamp()), int(end.timestamp()) - 1

    @staticmethod
    def _days_in_month(year: int, month: int) -> int:
        if month == 12:
            nxt = _dt.datetime(year + 1, 1, 1)
        else:
            nxt = _dt.datetime(year, month + 1, 1)
        return (nxt - _dt.datetime(year, month, 1)).days

    def _fetch_zip(self, kind: str, date_str: str) -> List[str]:
        """kind in {monthly, daily}; date_str like '2025-08' or '2025-08-01'."""
        url = f"{self.BASE}/{kind}/klines/{self.symbol}/1m/" \
              f"{self.symbol}-1m-{date_str}.zip"
        content, meta = http_get_bytes(url)
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            names = zf.namelist()
            if len(names) != 1:
                raise ProviderError(f"binance_vision: {date_str}: "
                                    f"unexpected zip members {names}")
            text = zf.read(names[0]).decode("utf-8")
        except (zipfile.BadZipFile, KeyError) as e:
            raise ProviderError(f"binance_vision: {date_str}: bad zip: {e}")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines or lines[0].startswith("open_time"):
            lines = lines[1:] if lines else []  # tolerate a header if present
        return lines

    def _parse_lines(self, lines: List[str], expected_start: int,
                     expected_end: int) -> Tuple[List[dict], int]:
        rows = []
        for ln in lines:
            p = ln.split(",")
            if len(p) < 6:
                raise ProviderError(f"binance_vision: bad row {ln[:80]}")
            try:
                rows.append(_parse_row(int(p[0]) // 1_000_000,
                                       p[1], p[2], p[3], p[4], p[5]))
            except (TypeError, ValueError) as e:
                raise ProviderError(f"binance_vision: bad row {ln[:80]}: {e}")
        rows = [r for r in rows if expected_start <= r["ts"] <= expected_end]
        return rows, len(lines)

    def iter_chunks(self, start_ts: int, end_ts: int,
                    covered_spans: List[Tuple[int, int]]) -> Iterator[dict]:
        # Walk month by month; use the monthly zip for months fully inside the
        # range, daily zips for the partial boundary months (a monthly zip for
        # the current month is usually not published yet).
        cursor_d = _dt.datetime.fromtimestamp(start_ts, _dt.timezone.utc)
        end_d = _dt.datetime.fromtimestamp(end_ts, _dt.timezone.utc)
        cursor_d = _dt.datetime(cursor_d.year, cursor_d.month, 1,
                                tzinfo=_dt.timezone.utc)
        while cursor_d <= end_d:
            m_start = int(cursor_d.timestamp())
            m_end = int((_dt.datetime(cursor_d.year + 1, 1, 1,
                                      tzinfo=_dt.timezone.utc)
                         if cursor_d.month == 12 else
                         _dt.datetime(cursor_d.year, cursor_d.month + 1, 1,
                                      tzinfo=_dt.timezone.utc)).timestamp()) - 1
            month_str = cursor_d.strftime("%Y-%m")
            # Binance publishes the current month's archive as daily files;
            # the monthly ZIP can lag by days (and returns 404 meanwhile).
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            month_is_current = (cursor_d.year == now_utc.year and
                                cursor_d.month == now_utc.month)
            # Also use daily archives for the requested end month: the
            # monthly archive is commonly published asynchronously.
            month_archive_lagging = (cursor_d.year == end_d.year and
                                     cursor_d.month == end_d.month)
            if (m_start >= start_ts and m_end <= end_ts and
                    not month_is_current and not month_archive_lagging):
                lines = self._fetch_zip("monthly", month_str)
                rows, n_raw = self._parse_lines(lines, m_start, m_end)
                yield {"start_ts": m_start, "end_ts": m_end, "rows": rows,
                       "source": self.source_desc + f" (monthly {month_str})",
                       "n_expected": self._days_in_month(
                           cursor_d.year, cursor_d.month) * 1440,
                       "n_raw": n_raw}
            else:
                day = cursor_d
                while day <= end_d and day.month == cursor_d.month:
                    d_start = int(day.timestamp())
                    if d_start < start_ts:
                        day += _dt.timedelta(days=1)
                        continue
                    d_end = d_start + 86400 - 1
                    if d_end > end_ts:
                        d_end = end_ts
                    day_str = day.strftime("%Y-%m-%d")
                    lines = self._fetch_zip("daily", day_str)
                    rows, n_raw = self._parse_lines(lines, d_start, d_end)
                    yield {"start_ts": d_start, "end_ts": d_end, "rows": rows,
                           "source": self.source_desc + f" (daily {day_str})",
                           "n_expected": 1440, "n_raw": n_raw}
                    day += _dt.timedelta(days=1)
            cursor_d = (_dt.datetime(cursor_d.year + 1, 1, 1,
                                     tzinfo=_dt.timezone.utc)
                        if cursor_d.month == 12 else
                        _dt.datetime(cursor_d.year, cursor_d.month + 1, 1,
                                     tzinfo=_dt.timezone.utc))


class BitstampProvider(BaseProvider):
    name = "bitstamp"
    venue = "bitstamp"
    source_desc = ("Bitstamp v2 OHLC API /api/v2/ohlc/btcusd/?step=60 "
                   "(1000 candles/request, start-paged) — spot BTC/USD")

    URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
    PAGE = 1000

    def iter_chunks(self, start_ts: int, end_ts: int,
                    covered_spans: List[Tuple[int, int]]) -> Iterator[dict]:
        cursor = start_ts
        while cursor <= end_ts:
            content, meta = http_get_bytes(
                self.URL, {"step": 60, "limit": self.PAGE, "start": cursor})
            try:
                data = json.loads(content)["data"]["ohlc"]
            except (KeyError, TypeError, ValueError) as e:
                raise ProviderError(f"bitstamp: bad payload: {e}")
            rows = []
            for o in data:
                try:
                    rows.append(_parse_row(o["timestamp"], o["open"],
                                           o["high"], o["low"], o["close"],
                                           o["volume"]))
                except (KeyError, TypeError, ValueError) as e:
                    raise ProviderError(f"bitstamp: bad row {o!r}: {e}")
            rows = [r for r in rows if r["ts"] <= end_ts]
            if not rows:
                raise ProviderError("bitstamp: empty page at cursor "
                                    f"{cursor} (end of history?)")
            last_ts = rows[-1]["ts"]
            yield {"start_ts": rows[0]["ts"], "end_ts": last_ts,
                   "rows": rows, "source": self.source_desc,
                   "n_expected": len(rows)}
            if last_ts >= end_ts or len(rows) < self.PAGE:
                break
            cursor = last_ts + STEP_S
            time.sleep(0.4)  # be polite to a public endpoint


PROVIDERS = {
    p.name: p
    for p in (BinanceRestProvider(), BinanceVisionProvider(),
              BitstampProvider())
}
DEFAULT_PROVIDER_ORDER = ["binance_rest", "binance_vision", "bitstamp"]


# ---------------------------------------------------------------------------
# Cache manager (resumable, crash-safe, single copy on disk)
# ---------------------------------------------------------------------------

def cache_path_for(data_dir: str, venue: str) -> str:
    return os.path.join(data_dir, f"btc_1m_{venue}.csv.gz")


def meta_path_for_cache(data_dir: str, venue: str) -> str:
    return os.path.join(data_dir, f"btc_1m_{venue}.meta.json")


def _scan_cache(path: str) -> dict:
    """Stream the cache once and return coverage facts:
    {n_rows, first_ts, last_ts, covered_spans, duplicates, nonmonotonic} —
    computed from the FILE content, never trusted from the sidecar.
    """
    ts_list: List[int] = []
    n_rows = 0
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        import csv as _csv
        reader = _csv.DictReader(f)
        for row in reader:
            n_rows += 1
            try:
                ts_list.append(int(row["ts"]))
            except (KeyError, ValueError):
                raise ProviderError(f"{path}: bad row {row!r}")
    dups = find_duplicates(ts_list)
    nonmono = find_nonmonotonic(ts_list)
    unique = sorted(set(ts_list))
    # contiguous covered spans from the unique present minutes
    spans = []
    if unique:
        run_start = unique[0]
        prev = unique[0]
        for t in unique[1:]:
            if t - prev > STEP_S:
                spans.append((run_start, prev))
                run_start = t
            prev = t
        spans.append((run_start, prev))
    return {"n_rows": n_rows, "first_ts": unique[0] if unique else None,
            "last_ts": unique[-1] if unique else None,
            "covered_spans": spans,
            "n_duplicate_rows": sum(c - 1 for _, c in dups),
            "n_nonmonotonic": len(nonmono)}


def _span_covered(start_ts: int, end_ts: int,
                  spans: List[Tuple[int, int]]) -> bool:
    for s, e in spans:
        if s <= start_ts and e >= end_ts:
            return True
    return False


class CacheManager:
    def __init__(self, data_dir: str, venue: str, symbol: str = SYMBOL):
        self.data_dir = data_dir
        self.venue = venue
        self.symbol = symbol
        os.makedirs(data_dir, exist_ok=True)
        self.path = cache_path_for(data_dir, venue)
        self.meta_path = meta_path_for_cache(data_dir, venue)
        self.meta: dict = self._load_meta()
        if os.path.exists(self.path):
            self.scan = _scan_cache(self.path)
        else:
            self.scan = {"n_rows": 0, "first_ts": None, "last_ts": None,
                         "covered_spans": [], "n_duplicate_rows": 0,
                         "n_nonmonotonic": 0}

    def _load_meta(self) -> dict:
        if os.path.exists(self.meta_path):
            with open(self.meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"venue": self.venue, "symbol": self.symbol,
                "interval_s": INTERVAL_S, "schema": list(CSV_COLUMNS),
                "chunks": [], "failed_ranges": []}

    def save_meta(self) -> None:
        """Atomic sidecar write (tmp + rename) so a crash cannot corrupt it."""
        self.meta["venue"] = self.venue
        self.meta["symbol"] = self.symbol
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.meta, f, indent=2, sort_keys=True)
            os.replace(tmp, self.meta_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def append_chunk(self, chunk: dict) -> None:
        """Append one validated chunk as a complete gzip member.

        Crash-safe: the member is written to a temp file first, and only a
        fully-written member is appended. Metadata is saved after.
        """
        rows = chunk["rows"]
        if not rows:
            return
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".gz.tmp")
        try:
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(
                    fileobj=raw, mode="wb", compresslevel=6) as gz:
                if not os.path.exists(self.path):
                    gz.write((cache_columns() + "\n").encode("utf-8"))
                buf = io.StringIO()
                for r in rows:
                    buf.write(f"{r['ts']},{r['open']},{r['high']},"
                              f"{r['low']},{r['close']},{r['volume']}\n")
                gz.write(buf.getvalue().encode("utf-8"))
            with open(self.path, "ab") as out:
                with open(tmp, "rb") as t:
                    shutil.copyfileobj(t, out)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self.meta.setdefault("chunks", []).append({
            "start_ts": chunk["start_ts"], "end_ts": chunk["end_ts"],
            "n_rows": len(rows), "source": chunk.get("source", ""),
            "n_expected": chunk.get("n_expected"),
            "fetched_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
        self.save_meta()
        # refresh in-memory scan cheaply (extend covered spans)
        self.scan["covered_spans"] = _merge_spans(
            self.scan["covered_spans"],
            [(chunk["start_ts"], chunk["end_ts"])])
        self.scan["n_rows"] += len(rows)
        if self.scan["first_ts"] is None or chunk["start_ts"] < self.scan["first_ts"]:
            self.scan["first_ts"] = chunk["start_ts"]
        if self.scan["last_ts"] is None or chunk["end_ts"] > self.scan["last_ts"]:
            self.scan["last_ts"] = chunk["end_ts"]


def _merge_spans(a: List[Tuple[int, int]],
                 b: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    pts = sorted(a + b)
    out: List[Tuple[int, int]] = []
    for s, e in pts:
        if out and s <= out[-1][1] + STEP_S:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# ---------------------------------------------------------------------------
# Chunk validation
# ---------------------------------------------------------------------------

def validate_chunk(chunk: dict, start_ts: int, end_ts: int) -> List[str]:
    """Sanity checks on a fetched chunk. Returns a list of violation strings
    (empty = chunk is acceptable to persist). Raising-level failures (empty,
    unordered) raise ProviderError so the caller can skip the chunk.
    """
    rows = chunk["rows"]
    problems: List[str] = []
    if not rows:
        raise ProviderError(f"{chunk.get('source')}: empty chunk")
    ts = [r["ts"] for r in rows]
    if any(ts[i] >= ts[i + 1] for i in range(len(ts) - 1)):
        raise ProviderError(f"{chunk.get('source')}: unordered timestamps")
    if ts[0] < start_ts or ts[-1] > end_ts:
        problems.append(f"{chunk.get('source')}: chunk [{ts[0]},{ts[-1]}] "
                        f"outside requested [{start_ts},{end_ts}]")
    if any(r["ts"] % STEP_S != 0 for r in rows):
        problems.append(f"{chunk.get('source')}: timestamps off the 60s grid")
    bq = find_bad_quotes(rows)
    n_bad = sum(len(v) for v in bq.values())
    if n_bad:
        problems.append(f"{chunk.get('source')}: {n_bad} bad quote(s): "
                        f"{ {k: len(v) for k, v in bq.items()} }")
    expected = chunk.get("n_expected")
    if expected and len(rows) != expected:
        problems.append(f"{chunk.get('source')}: row count {len(rows)} "
                        f"!= expected {expected}")
    return problems


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def parse_date(s: str) -> _dt.datetime:
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%d").replace(
            tzinfo=_dt.timezone.utc)
    except ValueError:
        raise SystemExit(f"bad date {s!r}: use YYYY-MM-DD")


def run_fetch(start_ts: int, end_ts: int,
              provider_order: Optional[List[str]] = None,
              data_dir: str = DEFAULT_DATA_DIR,
              report_only: bool = False) -> dict:
    """Fetch [start_ts, end_ts] into the cache of the first provider that can
    serve it. Returns a summary dict."""
    order = provider_order or DEFAULT_PROVIDER_ORDER
    if report_only:
        # just regenerate the quality report for an existing cache
        venue = _find_existing_cache_venue(data_dir)
        if venue is None:
            raise SystemExit("no cache found in "
                             f"{data_dir}; cannot --report-only")
        return _write_report(data_dir, venue, start_ts, end_ts)

    failures = {}
    for pname in order:
        prov = PROVIDERS[pname]
        cm = CacheManager(data_dir, prov.venue, prov.symbol)
        if cm.scan["first_ts"] is not None:
            if (cm.scan["first_ts"] <= start_ts and
                    cm.scan["last_ts"] >= end_ts):
                print(f"[{prov.name}] cache already covers "
                      f"[{start_ts}, {end_ts}] — nothing to fetch.")
                return _write_report(data_dir, prov.venue, start_ts, end_ts)
        # does this venue already hold data that would conflict (mixed venue)?
        # a run targets ONE venue; if another venue's cache exists it is left
        # untouched (the report names the venue actually used).
        try:
            summary = _fetch_with(cm, prov, start_ts, end_ts)
            print(f"\nDONE: venue={prov.venue} source={prov.source_desc}")
            return summary
        except ProviderUnavailable as e:
            failures[prov.name] = str(e)
            print(f"[{prov.name}] UNAVAILABLE: {e}")
        except ProviderError as e:
            failures[prov.name] = str(e)
            print(f"[{prov.name}] FAILED: {e}")
    raise SystemExit("ALL PROVIDERS FAILED — no real data fetched. "
                     f"Failures: {failures}. Refusing to write anything.")


def _find_existing_cache_venue(data_dir: str) -> Optional[str]:
    if not os.path.isdir(data_dir):
        return None
    for fn in sorted(os.listdir(data_dir)):
        if fn.startswith("btc_1m_") and fn.endswith(".csv.gz"):
            return fn[len("btc_1m_"):-len(".csv.gz")]
    return None


def _fetch_with(cm: CacheManager, prov: BaseProvider,
                start_ts: int, end_ts: int) -> dict:
    fetched_chunks = 0
    total_rows = 0
    failed: List[dict] = []
    for chunk in prov.iter_chunks(start_ts, end_ts, cm.scan["covered_spans"]):
        c_start, c_end = chunk["start_ts"], chunk["end_ts"]
        if _span_covered(c_start, c_end, cm.scan["covered_spans"]):
            print(f"  skip [{c_start},{c_end}] (already cached)")
            continue
        try:
            problems = validate_chunk(chunk, start_ts, end_ts)
            if problems:
                print(f"  WARN {chunk['source']}: {problems}")
                cm.meta.setdefault("warnings", []).append({
                    "start_ts": c_start, "end_ts": c_end,
                    "problems": problems})
                if any("unordered" in p or "empty" in p for p in problems):
                    raise ProviderError(f"{chunk['source']}: {problems}")
            n = len(chunk["rows"])
            cm.append_chunk(chunk)
            fetched_chunks += 1
            total_rows += n
            print(f"  + [{c_start},{c_end}] {n} rows "
                  f"({chunk['source'].split(' — ')[-1][:60]}) "
                  f"cache={cm.scan['n_rows']}")
        except ProviderError as e:
            print(f"  ! failed {chunk.get('source')}: {e}")
            failed.append({"start_ts": c_start, "end_ts": c_end,
                           "error": str(e),
                           "source": chunk.get("source", "")})
    cm.meta["failed_ranges"] = cm.meta.get("failed_ranges", []) + failed
    cm.meta["requested"] = {"start_ts": start_ts, "end_ts": end_ts}
    cm.save_meta()
    print(f"\n{prov.name}: fetched {fetched_chunks} chunk(s), "
          f"{total_rows} rows; cache now {cm.scan['n_rows']} rows; "
          f"{len(failed)} failed range(s)")
    return _write_report(cm.data_dir, prov.venue, start_ts, end_ts)


def _write_report(data_dir: str, venue: str,
                  start_ts: int, end_ts: int) -> dict:
    path = cache_path_for(data_dir, venue)
    if not os.path.exists(path):
        raise SystemExit(f"no cache at {path}")
    scan = _scan_cache(path)
    rows = list(iter_candles_for_report(path))
    summary = summarize(rows, requested_start_ts=start_ts,
                        requested_end_ts=end_ts)
    summary["venue"] = venue
    summary["cache_path"] = path
    summary["on_disk_bytes"] = os.path.getsize(path)
    summary["sources"] = _load_meta_summary(data_dir, venue)
    md = render_markdown(summary)
    # provenance header block
    header = [
        f"- Venue: **{venue}** (Kalshi settles on its own BRTI index — this "
        f"venue is a PROXY, see metadata sidecar)",
        f"- Cache file: `{os.path.basename(path)}` "
        f"({summary['on_disk_bytes']} bytes on disk)",
        f"- Report generated: {_dt.datetime.now(_dt.timezone.utc).isoformat()}",
    ]
    md = md.replace("# Candle data quality report",
                    "# Candle data quality report\n\n"
                    + "\n".join(header) + "\n", 1)
    qpath = os.path.join(data_dir, f"CANDLE-QUALITY-{venue}.md")
    with open(qpath, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\nQuality report written to {qpath}")
    return {"venue": venue, "path": path, "quality": summary}


def _load_meta_summary(data_dir: str, venue: str) -> list:
    mp = meta_path_for_cache(data_dir, venue)
    if os.path.exists(mp):
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return meta.get("chunks", [])
    return []


def iter_candles_for_report(path: str) -> Iterator[dict]:
    """Small local re-implementation to avoid importing the loader's strict
    monotonicity raise (we WANT to count violations, not crash on them)."""
    import csv as _csv
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        for row in _csv.DictReader(f):
            try:
                yield {"ts": int(row["ts"]),
                       "open": float(row["open"]),
                       "high": float(row["high"]),
                       "low": float(row["low"]),
                       "close": float(row["close"]),
                       "volume": float(row["volume"])}
            except (KeyError, ValueError):
                continue


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fetch historical BTC 1-minute OHLCV candles into a "
                    "resumable gzipped cache (QA3 step 1).")
    ap.add_argument("--start", default="", help="start date YYYY-MM-DD (UTC); "
                    "default: 12 months before today")
    ap.add_argument("--end", default="", help="end date YYYY-MM-DD (UTC), "
                    "inclusive; default: yesterday")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDER_ORDER),
                    help="comma-separated provider order")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--report-only", action="store_true",
                    help="regenerate the quality report from the existing "
                         "cache without fetching")
    args = ap.parse_args(argv)

    now = _dt.datetime.now(_dt.timezone.utc)
    if args.end:
        end_d = parse_date(args.end)
    else:
        end_d = (now - _dt.timedelta(days=1)).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
    if args.start:
        start_d = parse_date(args.start)
    else:
        start_d = (end_d - _dt.timedelta(days=364))
    end_ts = int(end_d.timestamp()) + 86400 - 1  # last second of end day
    start_ts = int(start_d.timestamp())
    print(f"Range: {start_d.date()} 00:00 UTC -> {end_d.date()} 23:59 UTC "
          f"({(end_ts - start_ts + 1) // 60:,} expected minutes)")
    run_fetch(start_ts, end_ts,
              provider_order=[p.strip() for p in args.providers.split(",")
                              if p.strip()],
              data_dir=args.data_dir,
              report_only=args.report_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
