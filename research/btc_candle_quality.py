"""
btc_candle_quality.py — pure data-quality checks for 1-minute BTC candle series.

Part of the QA3 audit workstream (research/). This module contains ONLY pure
functions over plain lists/dicts: no network access, no filesystem access, no
dependency beyond the standard library. The unit tests in
tests/test_btc_candle_quality.py exercise every function with tiny synthetic
fixtures, and the fetcher (fetch_btc_candles.py) reuses these same functions to
produce research/data/CANDLE-QUALITY-<venue>.md.

Semantics that matter for the audit:
  * Gaps are RECORDED, never repaired. No forward-fill, no interpolation.
    The replay harness is expected to mark samples affected by gaps (via the
    loader's gap flags / missing_ranges()) and drop or flag them, never to
    pretend the data is continuous.
  * Timestamps are integer unix seconds aligned to the 1-minute UTC grid
    (ts % 60 == 0). A "minute slot" is one such timestamp.
  * expected_minutes(start, end) counts inclusive minute slots in
    [start, end]: (end - start) // 60 + 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

STEP_S = 60  # 1-minute candles

# Fields the quality checks inspect, in canonical order.
OHLCV_FIELDS = ("open", "high", "low", "close", "volume")

# Plausible absolute bound for BTC price sanity (USD). Prices outside this are
# almost certainly corrupted rows, not real regime moves.
PRICE_SANE_MIN = 1.0
PRICE_SANE_MAX = 1_000_000.0


@dataclass
class Gap:
    """A run of missing minute slots between two present candles.

    prev_ts / next_ts are the present candles bounding the gap;
    start_ts is the first missing slot (prev_ts + STEP_S);
    end_ts is the last missing slot (next_ts - STEP_S);
    n_missing = (next_ts - prev_ts) // STEP_S - 1.
    """

    start_ts: int
    end_ts: int
    n_missing: int
    prev_ts: int
    next_ts: int


def expected_minutes(start_ts: int, end_ts: int, step: int = STEP_S) -> int:
    """Number of minute slots in the inclusive range [start_ts, end_ts]."""
    if end_ts < start_ts:
        return 0
    return (end_ts - start_ts) // step + 1


def find_duplicates(ts: Iterable[int]) -> List[Tuple[int, int]]:
    """Count occurrences of each timestamp.

    ``ts`` is assumed already sorted ascending. Returns a list of
    (timestamp, count) for every timestamp that appears more than once,
    ordered by timestamp.
    """
    counts: Dict[int, int] = {}
    for t in ts:
        counts[t] = counts.get(t, 0) + 1
    return [(t, c) for t, c in sorted(counts.items()) if c > 1]


def find_nonmonotonic(ts: Iterable[int]) -> List[int]:
    """Indices i (i >= 1) where ts[i] < ts[i-1] (strictly decreasing steps).

    Duplicates (ts[i] == ts[i-1]) are NOT reported here — they belong to
    find_duplicates. A strictly-increasing check with separate duplicate
    reporting keeps the two failure modes distinct in the report.
    """
    prev: Optional[int] = None
    bad: List[int] = []
    for i, t in enumerate(ts):
        if prev is not None and t < prev:
            bad.append(i)
        prev = t
    return bad


def find_gaps(ts: Iterable[int], step: int = STEP_S) -> List[Gap]:
    """Gaps between consecutive present candles.

    ``ts`` must be sorted ascending with no duplicate timestamps (dedupe
    first — the loader does). A gap is any forward step larger than ``step``.
    """
    prev: Optional[int] = None
    gaps: List[Gap] = []
    for t in ts:
        if prev is not None and t - prev > step:
            n_missing = (t - prev) // step - 1
            gaps.append(Gap(
                start_ts=prev + step,
                end_ts=t - step,
                n_missing=n_missing,
                prev_ts=prev,
                next_ts=t,
            ))
        prev = t
    return gaps


def _num(x) -> Optional[float]:
    """float() if convertible and finite, else None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def find_bad_quotes(rows: Iterable[dict]) -> Dict[str, list]:
    """Quote-level anomalies. Each row: dict with keys ts, open, high, low,
    close, volume (values may be numbers or numeric strings).

    Returns a dict with lists of (ts, field) tuples:
      zero_or_negative : price <= 0 in open/high/low/close
      nan_or_inf       : non-finite price in open/high/low/close/volume
      insane_price     : price outside [PRICE_SANE_MIN, PRICE_SANE_MAX]
      volume_negative  : volume < 0
      high_lt_low      : high < low (strictly)
      close_outside    : close < low or close > high
      open_outside     : open < low or open > high
    Each list is sorted by ts.
    """
    out = {
        "zero_or_negative": [],
        "nan_or_inf": [],
        "insane_price": [],
        "volume_negative": [],
        "high_lt_low": [],
        "close_outside": [],
        "open_outside": [],
    }
    for r in rows:
        ts = int(r["ts"])
        prices = {}
        for f in ("open", "high", "low", "close"):
            v = _num(r.get(f))
            prices[f] = v
            if v is None:
                out["nan_or_inf"].append((ts, f))
                continue
            if v <= 0:
                out["zero_or_negative"].append((ts, f))
            if not (PRICE_SANE_MIN <= v <= PRICE_SANE_MAX):
                out["insane_price"].append((ts, f))
        vol = _num(r.get("volume"))
        if vol is None:
            out["nan_or_inf"].append((ts, "volume"))
        elif vol < 0:
            out["volume_negative"].append((ts, "volume"))

        lo, hi = prices.get("low"), prices.get("high")
        if lo is not None and hi is not None:
            if hi < lo:
                out["high_lt_low"].append((ts, "high<low"))
            else:
                # Only check containment when high/low are internally
                # consistent; a high<low candle makes the [low, high] interval
                # empty, so "outside" is meaningless there and would cascade.
                cl = prices.get("close")
                if cl is not None and (cl < lo or cl > hi):
                    out["close_outside"].append((ts, "close"))
                op = prices.get("open")
                if op is not None and (op < lo or op > hi):
                    out["open_outside"].append((ts, "open"))
    for v in out.values():
        v.sort(key=lambda item: item[0])
    return out


def summarize(rows: List[dict],
              requested_start_ts: Optional[int] = None,
              requested_end_ts: Optional[int] = None,
              step: int = STEP_S) -> dict:
    """Full quality summary of a candle series.

    ``rows`` are the RAW cached rows (may contain duplicates / disorder —
    they are detected here, not silently fixed). Timestamps must be ints.

    Returns a dict with:
      n_rows_raw, n_unique_ts, n_duplicate_ts, duplicate_ts (sample),
      nonmonotonic_indices (sample), bad_quotes (from find_bad_quotes),
      first_ts, last_ts, requested {start,end,expected_minutes},
      covered_span {start,end,expected_minutes},
      gaps (list of Gap dicts, capped at 100), n_gaps,
      interior_missing_minutes, edge_missing_minutes, total_missing_minutes,
      coverage_pct (n_unique / requested_expected).
    """
    ts_all = [int(r["ts"]) for r in rows]
    dups = find_duplicates(ts_all)
    nonmono = find_nonmonotonic(ts_all)
    unique_sorted = sorted(set(ts_all))

    gaps = find_gaps(unique_sorted, step=step)
    interior_missing = sum(g.n_missing for g in gaps)

    first_ts = unique_sorted[0] if unique_sorted else None
    last_ts = unique_sorted[-1] if unique_sorted else None

    req_expected = (expected_minutes(requested_start_ts, requested_end_ts, step)
                    if requested_start_ts is not None and requested_end_ts is not None
                    else None)
    span_expected = (expected_minutes(first_ts, last_ts, step)
                     if first_ts is not None and last_ts is not None else None)

    n_unique = len(unique_sorted)
    if req_expected is not None:
        total_missing = max(0, req_expected - n_unique)
        edge_missing = total_missing - interior_missing
        coverage_pct = round(100.0 * n_unique / req_expected, 4) if req_expected else 100.0
    else:
        total_missing = interior_missing
        edge_missing = None
        coverage_pct = None

    def _sample(items, k=20):
        return items[:k]

    return {
        "step_s": step,
        "n_rows_raw": len(rows),
        "n_unique_ts": n_unique,
        "n_duplicate_ts": sum(c - 1 for _, c in dups),
        "duplicate_ts_sample": _sample(dups),
        "n_nonmonotonic": len(nonmono),
        "nonmonotonic_indices_sample": _sample([(i, ts_all[i]) for i in nonmono]),
        "bad_quotes": find_bad_quotes(rows),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "requested": {
            "start_ts": requested_start_ts,
            "end_ts": requested_end_ts,
            "expected_minutes": req_expected,
        },
        "covered_span": {
            "start_ts": first_ts,
            "end_ts": last_ts,
            "expected_minutes": span_expected,
        },
        "gaps": [vars(g) for g in gaps[:100]],
        "n_gaps": len(gaps),
        "gaps_truncated": len(gaps) > 100,
        "interior_missing_minutes": interior_missing,
        "edge_missing_minutes": edge_missing,
        "total_missing_minutes": total_missing,
        "coverage_pct": coverage_pct,
    }


def fmt_utc(ts: Optional[int]) -> str:
    """UTC ISO-8601 for a unix-epoch second timestamp (or '—')."""
    if ts is None:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


def render_markdown(s: dict) -> str:
    """Render the summarize() dict as the CANDLE-QUALITY-<venue>.md report."""
    bq = s["bad_quotes"]
    lines = []
    a = lines.append
    a("# Candle data quality report")
    a("")
    a(f"- Step: {s['step_s']} s (1-minute)")
    a(f"- First candle: {fmt_utc(s['first_ts'])} (ts {s['first_ts']})")
    a(f"- Last candle:  {fmt_utc(s['last_ts'])} (ts {s['last_ts']})")
    a(f"- Raw rows in cache: {s['n_rows_raw']}")
    a(f"- Unique minutes: {s['n_unique_ts']}")
    req = s["requested"]
    if req["expected_minutes"] is not None:
        a(f"- Requested range: {fmt_utc(req['start_ts'])} → {fmt_utc(req['end_ts'])}"
          f" (expected {req['expected_minutes']} minute slots)")
    a("")
    a("## Coverage")
    a("")
    if s["coverage_pct"] is not None:
        a(f"- Coverage vs requested range: {s['coverage_pct']}% "
          f"({s['n_unique_ts']}/{req['expected_minutes']} minutes present)")
    a(f"- Missing minutes (total, vs requested range): {s['total_missing_minutes']}")
    a(f"  - interior (gaps strictly between present candles): {s['interior_missing_minutes']}")
    if s["edge_missing_minutes"] is not None:
        a(f"  - edges (before first / after last candle, within requested range): {s['edge_missing_minutes']}")
    a("")
    a("## Gaps (recorded, NOT repaired)")
    a("")
    a(f"- Number of distinct gaps: {s['n_gaps']}"
      + ("  (list truncated at 100)" if s.get("gaps_truncated") else ""))
    a("")
    if s["gaps"]:
        a("| gap start (UTC) | gap end (UTC) | missing minutes | prev ts | next ts |")
        a("|---|---|---|---|---|")
        for g in s["gaps"]:
            a(f"| {fmt_utc(g['start_ts'])} | {fmt_utc(g['end_ts'])} | "
              f"{g['n_missing']} | {g['prev_ts']} | {g['next_ts']} |")
        a("")
    else:
        a("_No interior gaps — every minute slot between the first and last "
          "candle is present._")
        a("")
    a("## Duplicate timestamps")
    a("")
    if s["n_duplicate_ts"]:
        a(f"- Duplicate rows (rows beyond the first per minute): {s['n_duplicate_ts']}")
        a(f"- Affected minutes (sample): {s['duplicate_ts_sample']}")
    else:
        a("- None.")
    a("")
    a("## Non-monotonic timestamps")
    a("")
    if s["n_nonmonotonic"]:
        a(f"- Strictly-decreasing steps: {s['n_nonmonotonic']} "
          f"(sample: {s['nonmonotonic_indices_sample']})")
    else:
        a("- None (timestamps are strictly increasing).")
    a("")
    a("## Bad quotes")
    a("")
    a(f"- Zero/negative prices (open/high/low/close ≤ 0): {len(bq['zero_or_negative'])} {bq['zero_or_negative'][:5]}")
    a(f"- NaN/Inf prices or volume: {len(bq['nan_or_inf'])} {bq['nan_or_inf'][:5]}")
    a(f"- Implausible prices outside [$1, $1,000,000]: {len(bq['insane_price'])} {bq['insane_price'][:5]}")
    a(f"- Negative volume: {len(bq['volume_negative'])} {bq['volume_negative'][:5]}")
    a(f"- high < low: {len(bq['high_lt_low'])} {bq['high_lt_low'][:5]}")
    a(f"- close outside [low, high]: {len(bq['close_outside'])} {bq['close_outside'][:5]}")
    a(f"- open outside [low, high]: {len(bq['open_outside'])} {bq['open_outside'][:5]}")
    a("")
    a("## Notes")
    a("")
    a("- Gaps are reported, never forward-filled or interpolated. The replay "
      "harness must mark affected samples via the loader's gap flags / "
      "missing_ranges() and treat them as suspect.")
    a("- 'Coverage vs requested range' counts only unique minute slots; "
      "duplicates do not inflate it.")
    return "\n".join(lines)
