import csv
import gzip
from pathlib import Path

from research.btc_candle_loader import CandleCursor
from research.btc_candle_quality import find_duplicates, find_gaps, summarize


def write_cache(path: Path, rows):
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=("ts", "open", "high", "low", "close", "volume"))
        w.writeheader()
        w.writerows(rows)


def row(ts, close=50000):
    return {"ts": ts, "open": close - 1, "high": close + 2,
            "low": close - 2, "close": close, "volume": 1}


def test_quality_detects_gap_and_duplicate():
    rows = [row(0), row(60), row(180), row(180)]
    assert find_gaps([r["ts"] for r in rows])[0].n_missing == 1
    assert find_duplicates([r["ts"] for r in rows]) == [(180, 2)]
    s = summarize(rows, requested_start_ts=0, requested_end_ts=180)
    assert s["total_missing_minutes"] == 1
    assert s["n_duplicate_ts"] == 1


def test_cursor_terminates_at_eof(tmp_path):
    path = tmp_path / "short.csv.gz"
    write_cache(path, [row(0), row(60)])
    c = CandleCursor(str(path))
    assert c.next_minute() == 0
    assert c.next_minute() == 60
    assert c.next_minute() is None
    assert c.next_minute() is None
    assert c.past(10)[-1]["ts"] == 60
