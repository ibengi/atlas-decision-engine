"""test_replay_btc.py — mandatory QA3b harness tests (4).

Run from the repo root::

    .venv/bin/python -m pytest research/test_replay_btc.py -v

Tests
=====
1. test_truncation_invariance       — replaying against a candle series
                                      truncated at a future horizon yields a
                                      bit-identical dataset (no lookahead by
                                      construction).
2. test_input_boundary_assertion    — max ts of every candle touched for a
                                      row is <= decision_ts; the runtime guard
                                      passes on cursor windows and the row's
                                      spot equals the candle close at
                                      decision_ts.
3. test_mutation_lookahead_fails    — injecting a one-candle lookahead makes
                                      the guard FAIL (a guard that cannot fail
                                      is not a guard).
4. test_determinism                 — two runs over the same window produce
                                      identically-hashed output files.

The suite skips if the verified candle cache is absent.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import os

import pytest

from research import replay_btc as rb
from research.btc_candle_loader import CandleCursor, iter_candles

CACHE = os.path.join(os.path.dirname(__file__), "data",
                     "btc_1m_binance.csv.gz")
FEB1_0000 = 1769904000                      # 2026-02-01 00:00:00 UTC

pytestmark = pytest.mark.skipif(
    not os.path.exists(CACHE),
    reason="verified candle cache research/data/btc_1m_binance.csv.gz missing")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_rows(path: str) -> list:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_truncated_cache(dst: str, trunc_ts: int) -> int:
    """Copy the cache up to and including trunc_ts into dst. Returns n rows."""
    n = 0
    with gzip.open(dst, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=("ts", "open", "high", "low",
                                          "close", "volume"))
        w.writeheader()
        for c in iter_candles(CACHE):
            if c["ts"] > trunc_ts:
                break
            w.writerow(c)
            n += 1
    return n


def _window_at(decision_ts: int, cache: str) -> list:
    """The last-30-candle point-in-time window at decision_ts (as the harness
    builds it in run_replay)."""
    cur = CandleCursor(cache)
    cur.next_minute()
    while cur.current_ts is not None and cur.current_ts < decision_ts:
        cur.next_minute()
    assert cur.current_ts == decision_ts, "cursor did not land on decision_ts"
    return list(cur.past(rb.LOOKBACK_CANDLES))


def _event_open(decision_ts: int, m: int) -> int:
    """Window open W for a decision (m, decision_ts): decision at W+(15-m)*60."""
    return decision_ts - (rb.EVENT_GRID_MIN - m) * 60


def _strike_from_window(window: list, w: int) -> float:
    for c in reversed(window):
        if c["ts"] == w:
            return c["close"]
        if c["ts"] < w:
            break
    raise AssertionError(f"window lacks the event-open candle at {w}")


# ── 1. truncation invariance ─────────────────────────────────────────────────

def test_truncation_invariance(tmp_path):
    """Full cache vs a cache truncated after the sub-period: byte-identical
    rows (end-to-end through run_replay)."""
    sub_start, sub_end = FEB1_0000, FEB1_0000 + 12 * 3600   # Feb 1, 00:00-12:00
    trunc_ts = FEB1_0000 + 30 * 3600                         # Feb 2, 06:00
    truncated = str(tmp_path / "truncated.csv.gz")
    n = _write_truncated_cache(truncated, trunc_ts)
    assert n > 1700, "truncated cache suspiciously small"

    out_full = str(tmp_path / "full.csv.gz")
    out_trunc = str(tmp_path / "trunc.csv.gz")
    s1 = rb.run_replay(CACHE, sub_start, sub_end, out_full,
                       str(tmp_path / "s1.json"), str(tmp_path / "c1.md"))
    s2 = rb.run_replay(truncated, sub_start, sub_end, out_trunc,
                       str(tmp_path / "s2.json"), str(tmp_path / "c2.md"))

    assert s1["counts"]["rows_emitted"] == s2["counts"]["rows_emitted"] > 0
    rows1, rows2 = _read_rows(out_full), _read_rows(out_trunc)
    assert rows1 == rows2, "truncation invariance violated: rows differ"
    # 49 events (00:00 through 12:00 inclusive) x 7 - 3 boundary exclusions
    # (first event t=15/10/8 have < MIN_KLINES=11 candles at cache start)
    assert len(rows1) == 49 * 7 - 3
    # identical hashes of the raw gzip files (deterministic gzip, mtime=0)
    assert _sha256(out_full) == _sha256(out_trunc)


def test_compute_row_bit_identical_across_truncated_window(tmp_path):
    """compute_row on a window from the full cache vs the same window from a
    truncated cache: bit-identical dicts."""
    trunc_ts = FEB1_0000 + 24 * 3600
    truncated = str(tmp_path / "t.csv.gz")
    _write_truncated_cache(truncated, trunc_ts)
    for m, minute_offset in ((5, 600), (1, 840), (15, 0)):
        decision_ts = FEB1_0000 + 6 * 3600 + minute_offset   # 06:00 event
        w = _event_open(decision_ts, m)
        win_full = _window_at(decision_ts, CACHE)
        win_trunc = _window_at(decision_ts, truncated)
        assert win_full == win_trunc
        strike = _strike_from_window(win_full, w)
        r1 = rb.compute_row(decision_ts, w + 900, m, strike, False,
                            win_full, False)
        r2 = rb.compute_row(decision_ts, w + 900, m, strike, False,
                            win_trunc, False)
        assert r1 == r2
        assert r1["y"] is None  # outcome filled later, never here


# ── 2. input-boundary assertion ──────────────────────────────────────────────

def test_input_boundary_assertion(tmp_path):
    """Max ts of every candle touched for a row is <= decision_ts: the runtime
    guard passes on cursor windows, and the emitted row's spot equals the
    candle close AT decision_ts (cross-checked against the cache directly)."""
    out = str(tmp_path / "b.csv.gz")
    s = rb.run_replay(CACHE, FEB1_0000, FEB1_0000 + 12 * 3600, out,
                      str(tmp_path / "b.json"), str(tmp_path / "b.md"))
    rows = _read_rows(out)
    assert len(rows) == s["counts"]["rows_emitted"] == 49 * 7 - 3

    for r in rows:
        decision_ts = int(r["decision_ts"])
        # the harness's own runtime guard must pass on the cursor window
        win = _window_at(decision_ts, CACHE)
        assert max(c["ts"] for c in win) <= decision_ts
        rb.guard_no_lookahead(win, decision_ts)   # must NOT raise
        assert win[-1]["ts"] == decision_ts
        # spot column == close of the candle at decision_ts (cache direct read)
        assert float(r["spot"]) == win[-1]["close"]
        # horizon consistency
        assert int(r["minutes_remaining"]) == \
            (int(r["expiry_ts"]) - decision_ts) // 60
        assert decision_ts <= int(r["expiry_ts"])

    # and the P-Sat-1 identity must already hold on emitted rows
    fid = rb.fidelity_over_rows([dict(row, **{k: float(row[k]) for k in (
        "d", "p_yes", "momentum_term_postclip", "sign_5m_return")})
        for row in rows])
    assert fid["max_abs_dev_capped"] <= 1e-12


# ── 3. mutation test: a guard that cannot fail is not a guard ────────────────

def test_mutation_lookahead_fails():
    """Inject a one-candle lookahead (a candle stamped decision_ts + 60) and
    assert the point-in-time guard FAILS; the control (no lookahead) passes."""
    decision_ts = FEB1_0000 + 6 * 3600 + 600          # 06:00 event, t=5
    win = _window_at(decision_ts, CACHE)

    # control: unmutated window computes fine
    strike = _strike_from_window(win, _event_open(decision_ts, 5))
    rb.compute_row(decision_ts, decision_ts + 900, 5, strike, False, win, False)

    # mutation: a candle stamped one minute in the FUTURE appended to the input
    future = dict(win[-1])
    future["ts"] = decision_ts + 60
    future["close"] = future["close"] * 1.001
    mutated = list(win) + [future]

    with pytest.raises(AssertionError, match="LOOKAHEAD"):
        rb.guard_no_lookahead(mutated, decision_ts)
    with pytest.raises(AssertionError, match="LOOKAHEAD"):
        rb.compute_row(decision_ts, decision_ts + 900, 5, strike, False,
                       mutated, False)
    # the engine-boundary adapter guard must fail too (defense in depth)
    with pytest.raises(AssertionError, match="LOOKAHEAD"):
        rb.make_klines_fn(mutated, decision_ts)()


# ── 4. determinism ───────────────────────────────────────────────────────────

def test_determinism(tmp_path):
    """Two runs over the same window produce identically-hashed output files."""
    out1 = str(tmp_path / "d1.csv.gz")
    out2 = str(tmp_path / "d2.csv.gz")
    end = FEB1_0000 + 2 * 24 * 3600 - 900          # Feb 1 + Feb 2 (all day 2)
    s1 = rb.run_replay(CACHE, FEB1_0000, end, out1,
                       str(tmp_path / "d1.json"), str(tmp_path / "d1.md"))
    s2 = rb.run_replay(CACHE, FEB1_0000, end, out2,
                       str(tmp_path / "d2.json"), str(tmp_path / "d2.md"))
    assert _sha256(out1) == _sha256(out2), "two runs differ: not deterministic"
    assert s1["counts"] == s2["counts"]
    assert s1["clip"] == s2["clip"]
    assert _read_rows(out1) == _read_rows(out2)

def test_atomic_writer_refuses_truncated_artifact(tmp_path, monkeypatch):
    """A post-publish truncation is detected and never reported as success."""
    out = str(tmp_path / "truncated.csv.gz")
    rows = [{"event_id": 1, "decision_ts": 1, "expiry_ts": 2,
             "date_utc": "2026-01-01", **{k: "" for k in rb.COLUMNS
             if k not in {"event_id", "decision_ts", "expiry_ts", "date_utc"}}}]
    real_replace = rb.os.replace
    def truncate_after_replace(src, dst):
        real_replace(src, dst)
        with open(dst, "r+b") as f:
            f.truncate(max(1, os.path.getsize(dst) // 2))
    monkeypatch.setattr(rb.os, "replace", truncate_after_replace)
    with pytest.raises((OSError, EOFError, gzip.BadGzipFile, RuntimeError)):
        rb._write_dataset_atomic(rows, out)
    assert not os.path.exists(out)
    assert not os.path.exists(out + ".tmp")
