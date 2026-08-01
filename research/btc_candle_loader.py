"""
btc_candle_loader.py — read the verified 1-minute BTC candle cache.

Part of the QA3 audit workstream (research/). This is the interface the replay
harness (next session) should use to read the cache produced by
fetch_btc_candles.py.

Guarantees
==========
* ``iter_candles`` / ``load_candles`` return candles SORTED ASCENDING by
  timestamp (verified while reading; a non-monotonic cache raises).
* Timestamps are integer unix seconds on the 1-minute UTC grid.
* Gaps are never repaired: the loader reports them (gap flags on the series,
  ``missing_ranges()``) and the caller decides what to do. There is no
  forward-fill anywhere in this module.
* Duplicate timestamps in the raw cache are collapsed keep-last (the most
  recently appended page wins — this is the natural semantics of the
  append-only, resumable cache), and the number of collapsed rows is exposed
  in the returned series so the caller can audit it.

No-lookahead replay pattern (recommended for the harness)
=========================================================
The replay must enforce a hard boundary at minute t: model inputs may only use
candles with ts <= t. The cheap way to do that over ~500k minutes is a single
forward pass with ``CandleCursor``, NOT repeated ``load_candles()`` slices
(each slice scans the file; doing that per replay minute is O(n^2))::

    from btc_candle_loader import CandleCursor, missing_ranges

    cursor = CandleCursor(CACHE_PATH)                 # opens once
    while (t := cursor.next_minute()) is not None:    # steps the 1m grid
        past = cursor.past(30)                        # candles with ts <= t
        # sigma_1m = pstdev of log returns of past[-30:]
        # ret_5m   = ln(past[-1].close / past[-6].close)
        # ... call btc_probability_model.probability_yes(...) ...
        # outcome minute = t + horizon: read it with cursor2 or a second
        # forward iterator, never from the past buffer.

    # Mark samples affected by missing data:
    for gap in missing_ranges(CACHE_PATH, start_ts, end_ts):
        ...   # flag every sample whose input window overlaps the gap

``CandleCursor.next_minute()`` advances by exactly STEP_S even across gaps
(the buffer simply has no candle for a missing minute), so the replay grid
stays aligned to real time.
"""

from __future__ import annotations

import gzip
import json
import os
import csv
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from research.btc_candle_quality import STEP_S, Gap, find_gaps

CSV_COLUMNS = ("ts", "open", "high", "low", "close", "volume")

# A single gzip member of the cache (one per appended chunk) is usually much
# larger than the read buffer; buffering keeps gzip.open fast.
_READ_BUFFER = 1 << 20


class CacheError(RuntimeError):
    """Raised when the cache file is corrupt or violates ordering guarantees."""


def cache_columns() -> str:
    return ",".join(CSV_COLUMNS)


def venue_from_path(path: str) -> Optional[str]:
    """Infer the venue from a filename like btc_1m_<venue>.csv.gz.

    Returns None when the filename does not match the pattern. The venue is
    also recorded in the sidecar metadata (btc_1m_<venue>.meta.json), which is
    authoritative; this is a convenience for callers that only have the path.
    """
    base = os.path.basename(path)
    if base.startswith("btc_1m_") and base.endswith(".csv.gz"):
        return base[len("btc_1m_"):-len(".csv.gz")]
    return None


def read_meta(path: str) -> Optional[dict]:
    """Read the sidecar metadata for a cache path, if present."""
    meta_path = meta_path_for(path)
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def meta_path_for(cache_path: str) -> str:
    base = os.path.basename(cache_path)
    assert base.endswith(".csv.gz"), cache_path
    return os.path.join(os.path.dirname(cache_path),
                        base[:-len(".csv.gz")] + ".meta.json")


def iter_candles(path: str) -> Iterator[dict]:
    """Stream all candles from the cache, ascending by timestamp.

    Yields dicts with keys ts (int), open/high/low/close/volume (float).
    The raw file may contain duplicate minutes (from resumable appends);
    duplicates are passed through here and collapsed keep-last by
    ``load_candles``/``CandleCursor``. Raises CacheError if timestamps ever
    DECREASE — that is true order corruption and breaks the ascending
    guarantee that higher-level readers rely on.
    """
    prev_ts: Optional[int] = None
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != list(CSV_COLUMNS):
            raise CacheError(
                f"{path}: unexpected columns {reader.fieldnames} "
                f"(expected {list(CSV_COLUMNS)})")
        for row in reader:
            try:
                ts = int(row["ts"])
                rec = {
                    "ts": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            except (KeyError, ValueError) as e:
                raise CacheError(f"{path}: bad row {row!r}: {e}") from e
            if prev_ts is not None and ts < prev_ts:
                raise CacheError(
                    f"{path}: timestamps strictly decreasing "
                    f"({prev_ts} -> {ts}); cache is corrupt or was written "
                    f"out of order")
            prev_ts = ts
            yield rec


@dataclass
class CandleSeries:
    """An in-memory window of candles, sorted ascending, gap-flagged.

    gap_flag[i] is True when candle i is the last present minute before a gap
    or the first present minute after a gap, considering gaps strictly inside
    the loaded window's own span (see also missing_ranges() for gap positions
    against the FULL cache).
    """

    ts: List[int]
    open: List[float]
    high: List[float]
    low: List[float]
    close: List[float]
    volume: List[float]
    gap_flag: List[bool]
    #: duplicate rows collapsed (keep-last) while reading the raw cache
    n_duplicates_collapsed: int = 0
    #: requested window (None if the whole file was loaded)
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    #: True when the cache has a candle immediately before / after the window,
    #: i.e. the window is not truncated by missing data at that edge
    continues_before: bool = False
    continues_after: bool = False
    venue: Optional[str] = None

    def __len__(self) -> int:
        return len(self.ts)

    def to_rows(self) -> List[dict]:
        return [
            {"ts": self.ts[i], "open": self.open[i], "high": self.high[i],
             "low": self.low[i], "close": self.close[i],
             "volume": self.volume[i]}
            for i in range(len(self.ts))
        ]


def _dedupe_keep_last(recs: List[dict]) -> Tuple[List[dict], int]:
    """Collapse duplicate timestamps keep-last (later record wins).

    Input must already be ascending by ts (iter_candles enforces this).
    Returns (records, n_collapsed).
    """
    if not recs:
        return [], 0
    out: List[dict] = []
    collapsed = 0
    for r in recs:
        if out and out[-1]["ts"] == r["ts"]:
            out[-1] = r
            collapsed += 1
        else:
            out.append(r)
    return out, collapsed


def load_candles(path: str,
                 start_ts: Optional[int] = None,
                 end_ts: Optional[int] = None) -> CandleSeries:
    """Load candles with start_ts <= ts <= end_ts (inclusive bounds), sorted
    ascending. With both bounds None, loads the whole file.

    The ascending guarantee holds even for a slice: the file is scanned in
    ascending order and only matching rows are kept.
    """
    recs: List[dict] = []
    for rec in iter_candles(path):
        if start_ts is not None and rec["ts"] < start_ts:
            continue
        if end_ts is not None and rec["ts"] > end_ts:
            break
        recs.append(rec)
    recs, n_dups = _dedupe_keep_last(recs)

    # continues_*: does the cache have data immediately around the window?
    continues_before = continues_after = False
    if start_ts is not None:
        try:
            for rec in iter_candles(path):
                if rec["ts"] >= start_ts:
                    continues_before = (rec["ts"] == start_ts - STEP_S
                                        or rec["ts"] == start_ts)
                    break
        except StopIteration:
            pass
    if end_ts is not None:
        last_ts = None
        for rec in iter_candles(path):
            last_ts = rec["ts"]
        if last_ts is not None:
            continues_after = last_ts > end_ts
    # For the no-window (full file) case there is nothing beyond the edges.

    ts = [r["ts"] for r in recs]
    # gap flags: strictly inside the window's own span
    flags = [False] * len(recs)
    if len(ts) >= 2:
        for i in range(len(ts) - 1):
            if ts[i + 1] - ts[i] > STEP_S:
                flags[i] = True
                flags[i + 1] = True

    return CandleSeries(
        ts=ts,
        open=[r["open"] for r in recs],
        high=[r["high"] for r in recs],
        low=[r["low"] for r in recs],
        close=[r["close"] for r in recs],
        volume=[r["volume"] for r in recs],
        gap_flag=flags,
        n_duplicates_collapsed=n_dups,
        window_start=start_ts,
        window_end=end_ts,
        continues_before=continues_before,
        continues_after=continues_after,
        venue=venue_from_path(path),
    )


def missing_ranges(path: str,
                   start_ts: Optional[int] = None,
                   end_ts: Optional[int] = None,
                   step: int = STEP_S) -> List[Gap]:
    """Gaps inside [start_ts, end_ts] (default: whole file) as Gap objects.

    Unlike the series-level gap_flag, this inspects the FULL cache, so a
    window that starts/ends inside a gap is reported correctly (the caller can
    flag every sample whose input window overlaps a returned Gap).
    """
    ts: List[int] = []
    for rec in iter_candles(path):
        if start_ts is not None and rec["ts"] < start_ts:
            continue
        if end_ts is not None and rec["ts"] > end_ts:
            break
        ts.append(rec["ts"])
    ts = sorted(set(ts))
    return find_gaps(ts, step=step)


class CandleCursor:
    """Forward-only, minute-by-minute cursor over the cache.

    Designed for the no-lookahead replay: ``next_minute()`` advances a
    real-time grid by exactly STEP_S seconds and consumes into the buffer
    every candle with ts <= current time; ``past(k)`` returns the last k
    buffered candles (ascending). A candle with timestamp equal to the
    current minute IS included (a 1m candle closing at ts is knowable at ts).

    Usage::

        c = CandleCursor(path)
        while (t := c.next_minute()) is not None:
            past = c.past(30)     # model inputs — strictly <= t, no future
            ...                   # compute p, then later read outcome at t+h

    After the first candle, the grid continues across gaps (next_minute()
    always steps by STEP_S), so replay minutes stay aligned to UTC even where
    data is missing — the buffer just has fewer candles.
    """

    def __init__(self, path: str, step: int = STEP_S,
                 buffer_minutes: int = 1000):
        self._it = iter_candles(path)
        #: bounded lookback: the replay only ever needs the last few dozen
        #: candles for sigma/momentum inputs; a bounded deque keeps a 500k+
        #: minute replay at constant memory. past(k) for k > buffer_minutes
        #: is truncated (documented; raise the cap if a longer lookback is
        #: ever needed).
        self._buf: deque = deque(maxlen=buffer_minutes)
        self._step = step
        self._t: Optional[int] = None
        self._started = False
        #: a candle read from the file but not yet admitted to the buffer
        #: because its timestamp is in the future of the current minute
        self._peeked: Optional[dict] = None
        #: Once the underlying stream reaches EOF, never advance the grid
        #: again.  Without this latch next_minute() returned timestamps
        #: forever after the final candle and an unbounded replay could OOM.
        self._exhausted = False

    def _read(self) -> Optional[dict]:
        if self._peeked is not None:
            rec, self._peeked = self._peeked, None
            return rec
        return next(self._it, None)

    def next_minute(self) -> Optional[int]:
        """Advance to the next minute boundary; returns its unix ts, or None
        when the cache is exhausted."""
        if self._exhausted:
            return None
        if not self._started:
            self._started = True
            first = self._read()
            if first is None:
                self._exhausted = True
                return None
            self._buf.append(first)
            self._t = first["ts"]
            return self._t
        self._t += self._step
        while True:
            nxt = self._read()
            if nxt is None:
                self._exhausted = True
                break
            if nxt["ts"] > self._t:
                self._peeked = nxt
                break
            # collapse duplicates keep-last (resumable appends may repeat a
            # minute); the buffer then holds one candle per minute
            if self._buf and self._buf[-1]["ts"] == nxt["ts"]:
                self._buf[-1] = nxt
            else:
                self._buf.append(nxt)
        return self._t

    def past(self, k: int) -> List[dict]:
        """The last k buffered candles (ascending), i.e. candles with
        ts <= current minute. Shorter than k only before k minutes elapsed."""
        return list(self._buf)[-k:]

    @property
    def current_ts(self) -> Optional[int]:
        return self._t
