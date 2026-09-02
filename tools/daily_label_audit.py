"""
daily_label_audit.py — can the KXBTCD settlement labels be trusted?

A settlement row says a market resolved yes or no. This tool checks each
one against something the label cannot contradict: where the underlying
actually was when the market closed. "BTC above $68,000" cannot resolve
"no" while BTC trades at $77,360. When it does, the label is wrong, and a
model scored against it is being scored against noise.

INPUTS
    settlements   btc_daily_settlements.jsonl (append-only journal)
    spot series   any JSON list of records carrying a timestamp and a spot
                  price — the 15m shadow store (`ts`, `spot`) works as is.

WHAT IT DOES NOT DO
    It never rewrites the journal and never decides a label itself. It
    reports counts and names the rows, so a person can decide what the
    journal is worth. A row whose close time falls outside the spot series
    is reported as UNVERIFIABLE, not as fine.
"""

import argparse
import bisect
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

TICKER_RE = re.compile(r"^KXBTCD-(\d{2})([A-Z]{3})(\d{2})(\d{2})-T([\d.]+)$")
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}

#: Ticker hours are US Eastern; the API's close_time is UTC. This offset is
#: EDT. A row whose journal carries `market_close_time` uses that instead
#: and never touches this constant.
DEFAULT_ET_OFFSET_H = 4

#: A label is called IMPOSSIBLE only when the underlying was on the wrong
#: side of the strike by more than this fraction at close. The margin
#: absorbs the settlement index differing from the engine's consensus and
#: the spot sample not landing exactly on the close.
MIN_MARGIN_FRAC = 0.005


def _ts(v) -> float:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def parse_ticker(ticker: str, et_offset_h=DEFAULT_ET_OFFSET_H):
    """(close_ts_utc, strike) from a KXBTCD ticker, or (None, None)."""
    m = TICKER_RE.match(ticker or "")
    if not m:
        return None, None
    yy, mon, dd, hh, strike = m.groups()
    try:
        d = datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh),
                     tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None, None
    return d.timestamp() + et_offset_h * 3600, float(strike)


class SpotSeries:
    def __init__(self, points):
        pts = sorted((float(t), float(v)) for t, v in points if v)
        self.t = [t for t, _ in pts]
        self.v = [v for _, v in pts]

    @classmethod
    def from_records(cls, records, ts_key="ts", spot_key="spot"):
        return cls((_ts(r[ts_key]), r[spot_key]) for r in records
                   if isinstance(r, dict) and r.get(ts_key) and r.get(spot_key))

    def at(self, t: float, tolerance_s: float = 900.0):
        """(spot, |dt|) of the nearest sample within tolerance, else None."""
        if not self.t:
            return None
        i = bisect.bisect_left(self.t, t)
        cands = [j for j in (i - 1, i) if 0 <= j < len(self.t)
                 and abs(self.t[j] - t) <= tolerance_s]
        if not cands:
            return None
        j = min(cands, key=lambda j: abs(self.t[j] - t))
        return self.v[j], abs(self.t[j] - t)


def classify(result, strike, spot, margin_frac=MIN_MARGIN_FRAC) -> str:
    """IMPOSSIBLE when the label contradicts the price by more than the
    margin; CONSISTENT when it agrees by more than the margin; AMBIGUOUS
    inside the margin — never rounded to either side."""
    if spot is None:
        return "UNVERIFIABLE"
    edge = (spot - strike) / strike
    if abs(edge) <= margin_frac:
        return "AMBIGUOUS"
    above = edge > 0
    if (result == "yes") == above:
        return "CONSISTENT"
    return "IMPOSSIBLE"


def audit(settlement_rows, spot: SpotSeries, tolerance_s=900.0,
          margin_frac=MIN_MARGIN_FRAC) -> dict:
    """One verdict per distinct event (ticker), not per journal row: the
    rows of one ticker share one outcome, and counting them separately
    would inflate whichever verdict they carry."""
    by_ticker = {}
    for r in settlement_rows:
        if not isinstance(r, dict) or r.get("kind") == "observation":
            continue
        tk, res = r.get("ticker"), r.get("result")
        if not tk or res not in ("yes", "no"):
            continue
        e = by_ticker.setdefault(tk, {"rows": 0, "result": res,
                                      "close_time": r.get("market_close_time"),
                                      "settled_at": r.get("settled_at"),
                                      "confirmed_by": r.get("confirmed_by"),
                                      "results": set()})
        e["rows"] += 1
        e["results"].add(res)
        if r.get("confirmed_by"):
            e["confirmed_by"] = r["confirmed_by"]

    verdicts, lags, impossible = Counter(), [], []
    per_close = defaultdict(Counter)
    for tk, e in by_ticker.items():
        close_ts, strike = parse_ticker(tk)
        if e.get("close_time"):
            try:
                close_ts = _ts(e["close_time"])
            except ValueError:
                pass
        if close_ts is None or strike is None:
            verdicts["UNPARSEABLE"] += 1
            continue
        hit = spot.at(close_ts, tolerance_s)
        v = classify(e["result"], strike, hit[0] if hit else None, margin_frac)
        verdicts[v] += 1
        per_close[datetime.fromtimestamp(close_ts, timezone.utc)
                  .isoformat(timespec="minutes")][v] += 1
        if e.get("settled_at"):
            try:
                lags.append(_ts(e["settled_at"]) - close_ts)
            except ValueError:
                pass
        if v == "IMPOSSIBLE":
            impossible.append({
                "ticker": tk, "result": e["result"], "strike": strike,
                "spot_at_close": hit[0], "spot_sample_offset_s": round(hit[1]),
                "margin_frac": round((hit[0] - strike) / strike, 4),
                "rows": e["rows"], "confirmed_by": e["confirmed_by"],
                "settled_at": e["settled_at"]})
    impossible.sort(key=lambda d: -abs(d["margin_frac"]))
    inconsistent = sum(1 for e in by_ticker.values() if len(e["results"]) > 1)

    n_verified = verdicts["CONSISTENT"] + verdicts["IMPOSSIBLE"]
    return {
        "events": len(by_ticker),
        "journal_rows": sum(e["rows"] for e in by_ticker.values()),
        "events_with_conflicting_results": inconsistent,
        "verdicts": dict(verdicts),
        "impossible_share_of_verified": (round(verdicts["IMPOSSIBLE"]
                                               / n_verified, 4)
                                         if n_verified else None),
        "result_distribution": dict(Counter(e["result"]
                                            for e in by_ticker.values())),
        "confirmed_events": sum(1 for e in by_ticker.values()
                                if e["confirmed_by"]),
        "settle_lag_s": ({"min": round(min(lags)), "median":
                          round(sorted(lags)[len(lags) // 2]),
                          "max": round(max(lags))} if lags else None),
        "per_close_time": {k: dict(v) for k, v in sorted(per_close.items())},
        "impossible": impossible,
        "label_trustworthy": bool(n_verified and verdicts["IMPOSSIBLE"] == 0),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit KXBTCD settlement labels")
    ap.add_argument("settlements")
    ap.add_argument("spot", help="JSON list of records with ts + spot")
    ap.add_argument("--tolerance-s", type=float, default=900.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    raw_s = open(a.settlements, "rb").read()
    raw_p = open(a.spot, "rb").read()
    rows = [json.loads(l) for l in raw_s.decode("utf-8").splitlines()
            if l.strip()]
    spot = SpotSeries.from_records(json.loads(raw_p.decode("utf-8")))
    rep = audit(rows, spot, a.tolerance_s)
    rep = {"generated_at": datetime.now(timezone.utc)
                                   .isoformat(timespec="seconds")
                                   .replace("+00:00", "Z"),
           "settlements_sha256": hashlib.sha256(raw_s).hexdigest(),
           "spot_sha256": hashlib.sha256(raw_p).hexdigest(),
           "spot_points": len(spot.t), **rep}
    text = json.dumps(rep, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")
    return 0 if rep["label_trustworthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
