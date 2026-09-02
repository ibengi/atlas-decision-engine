"""
model_research.py — walk-forward research harness for the BTC 15m model.

WHY THIS EXISTS
    `tools/brier_oos.py` answers one question: does the deployed model beat
    the market baseline out of sample? This module answers the next one:
    is there ANY model, fit only on the past, that beats the market by more
    than the noise in the sample?

    It is a research instrument, not a trading path. It imports no engine
    module, writes no state, and cannot enable anything.

THE THREE WAYS THIS KIND OF STUDY LIES, AND WHAT IS DONE ABOUT EACH

    1. Fitting on data the model would not have had. Every transform,
       every fitted parameter, every standardisation constant and every
       threshold comes from the TRAIN slice of the fold it is used in.
       A fold additionally PURGES any training row whose label
       (`settled_at`) was still unknown at the moment the test fold's
       first decision was taken: a row you could not have scored yet is
       not a row you could have trained on.

    2. Counting correlated rows as independent evidence. Several
       observations of the same 15-minute window resolve from one BTC
       price path. They are one event, not many. Confidence intervals use
       a BLOCK bootstrap resampling whole settlement windows, and every
       report states the number of windows alongside the number of rows.

    3. Reporting the winner of a search as if it had been the only
       candidate. Every candidate is scored on the same folds and all of
       them are reported, ranked, with intervals. A candidate whose
       interval covers zero has not beaten the market, whatever its point
       estimate says.

NO CANDIDATE IS PROMOTED BY THIS MODULE. It reports; the operator decides.
"""

import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timezone

EPS = 1e-6
FEE_RATE = 0.07                      # config.CFG.FEE_RATE, inlined: no engine import


# ── helpers ──────────────────────────────────────────────────────────────

def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def logit(p): 
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def log_loss(pairs):
    if not pairs:
        return None
    s = 0.0
    for p, y in pairs:
        p = min(1 - EPS, max(EPS, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return s / len(pairs)


def sharpness(pairs):
    """Mean |p - 0.5|. A forecaster that always says 0.5 is perfectly
    calibrated and perfectly useless; sharpness is what separates them."""
    return sum(abs(p - 0.5) for p, _ in pairs) / len(pairs) if pairs else None


# ── data ─────────────────────────────────────────────────────────────────

def usable_rows(records):
    """Settled rows carrying every field each candidate needs, so all
    candidates are scored on exactly the same events."""
    out = []
    for r in records or []:
        if not isinstance(r, dict) or r.get("result") not in ("yes", "no"):
            continue
        need = ("probability_yes", "yes_ask", "yes_bid", "spot", "strike",
                "sigma_1m", "minutes_remaining", "ts", "settled_at", "ticker")
        if any(r.get(k) is None for k in need):
            continue
        try:
            if not (0 < float(r["spot"]) and 0 < float(r["strike"])
                    and 0 < float(r["sigma_1m"])
                    and 0 < float(r["minutes_remaining"])
                    and 0 <= float(r["yes_ask"]) <= 100
                    and 0 <= float(r["yes_bid"]) <= 100
                    and 0 <= float(r["probability_yes"]) <= 1):
                continue
        except (TypeError, ValueError):
            continue
        out.append(r)
    out.sort(key=lambda r: _ts(r["ts"]))
    return out


def features(r):
    """CAUSALLY VALID inputs only — every one is observable at decision
    time. `result` and `settled_at` are never read here."""
    spot, strike = float(r["spot"]), float(r["strike"])
    sig, t = float(r["sigma_1m"]), float(r["minutes_remaining"])
    denom = sig * math.sqrt(t)
    ret5 = r.get("ret_5m")
    ret5 = float(ret5) if isinstance(ret5, (int, float)) else 0.0
    ask, bid = float(r["yes_ask"]) / 100.0, float(r["yes_bid"]) / 100.0
    return {
        "d_norm": math.log(spot / strike) / denom if denom > 0 else 0.0,
        "log_moneyness": math.log(spot / strike),
        "sigma_1m": sig,
        "sqrt_t": math.sqrt(t),
        "ret_5m": ret5,
        "spread": max(0.0, ask - bid),
        "market_ask": ask,
        "market_mid": (ask + bid) / 2.0,
        "logit_mid": logit((ask + bid) / 2.0),
    }


def label(r):
    return 1 if r["result"] == "yes" else 0


# ── pure-python learners (deterministic: fixed lr and iterations) ────────

def fit_logistic(X, y, iters=800, lr=0.15, l2=1e-3):
    """Standardises on TRAIN statistics only, then gradient descent. The
    standardisation constants travel with the model so test rows are
    transformed by numbers the training data alone produced."""
    if not X or len(X) < 8:
        return None
    k = len(X[0])
    mu = [sum(row[j] for row in X) / len(X) for j in range(k)]
    sd = []
    for j in range(k):
        v = sum((row[j] - mu[j]) ** 2 for row in X) / len(X)
        sd.append(math.sqrt(v) if v > 1e-12 else 1.0)
    Z = [[(row[j] - mu[j]) / sd[j] for j in range(k)] for row in X]
    w = [0.0] * k
    b = math.log((sum(y) + 0.5) / (len(y) - sum(y) + 0.5))
    n = len(Z)
    for _ in range(iters):
        gw, gb = [0.0] * k, 0.0
        for zi, yi in zip(Z, y):
            e = sigmoid(sum(w[j] * zi[j] for j in range(k)) + b) - yi
            for j in range(k):
                gw[j] += e * zi[j]
            gb += e
        for j in range(k):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * gb / n
    return {"w": w, "b": b, "mu": mu, "sd": sd}


def apply_logistic(m, x):
    if not m:
        return None
    z = [(x[j] - m["mu"][j]) / m["sd"][j] for j in range(len(x))]
    return sigmoid(sum(m["w"][j] * z[j] for j in range(len(z))) + m["b"])


def fit_platt(pairs):
    """Platt scaling of an existing probability, fit on train only."""
    if len(pairs) < 20:
        return None
    X = [[logit(p)] for p, _ in pairs]
    return fit_logistic(X, [y for _, y in pairs])


def fit_isotonic(pairs):
    """Pool-adjacent-violators. Returns breakpoints for a step function."""
    if len(pairs) < 20:
        return None
    pts = sorted(pairs)
    blocks = [[p, float(y), 1.0] for p, y in pts]       # [x, mean, weight]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1] + 1e-12:
            a, b = blocks[i], blocks[i + 1]
            tot = a[2] + b[2]
            blocks[i] = [a[0], (a[1] * a[2] + b[1] * b[2]) / tot, tot]
            del blocks[i + 1]
            if i:
                i -= 1
        else:
            i += 1
    return [(b[0], b[1]) for b in blocks]


def apply_isotonic(m, p):
    if not m:
        return None
    lo = None
    for x, v in m:
        if x <= p:
            lo = v
        else:
            break
    return min(1 - EPS, max(EPS, lo if lo is not None else m[0][1]))


# ── candidates ───────────────────────────────────────────────────────────
#: Each entry fits on train rows and predicts test rows. A candidate that
#: cannot be fitted on a fold returns None for that fold and is scored on
#: the folds where it could — its fold count is reported alongside.

LOGIT_SETS = {
    "logit_d_only":       ["d_norm"],
    "logit_simple":       ["log_moneyness", "sigma_1m", "sqrt_t", "ret_5m"],
    "logit_market_only":  ["logit_mid"],
    "logit_market_plus_d": ["logit_mid", "d_norm"],
    "logit_market_full":  ["logit_mid", "d_norm", "sigma_1m", "sqrt_t",
                           "ret_5m", "spread"],
}


def build_candidates():
    cands = {}

    cands["market_ask"] = lambda tr: (lambda r: features(r)["market_ask"])
    cands["market_mid"] = lambda tr: (lambda r: features(r)["market_mid"])
    cands["existing_model"] = lambda tr: (lambda r: float(r["probability_yes"]))

    def base_rate(tr):
        rate = sum(label(r) for r in tr) / len(tr) if tr else 0.5
        return lambda r: rate
    cands["base_rate_train"] = base_rate

    for name, keys in LOGIT_SETS.items():
        def make(tr, keys=keys):
            X = [[features(r)[k] for k in keys] for r in tr]
            m = fit_logistic(X, [label(r) for r in tr])
            if not m:
                return None
            return lambda r: apply_logistic(m, [features(r)[k] for k in keys])
        cands[name] = make

    def platt(tr):
        m = fit_platt([(float(r["probability_yes"]), label(r)) for r in tr])
        if not m:
            return None
        return lambda r: apply_logistic(m, [logit(float(r["probability_yes"]))])
    cands["platt_existing"] = platt

    def iso(tr):
        m = fit_isotonic([(float(r["probability_yes"]), label(r)) for r in tr])
        if not m:
            return None
        return lambda r: apply_isotonic(m, float(r["probability_yes"]))
    cands["isotonic_existing"] = iso

    def resid(tr):
        """Predict the CORRECTION to the market, not the outcome: fit the
        model's disagreement with the market and add it back in logit
        space. If the market is already right, this collapses to it."""
        X, y = [], []
        for r in tr:
            f = features(r)
            X.append([f["logit_mid"], logit(float(r["probability_yes"]))
                      - f["logit_mid"]])
            y.append(label(r))
        m = fit_logistic(X, y)
        if not m:
            return None
        def f_(r):
            f = features(r)
            return apply_logistic(m, [f["logit_mid"],
                                      logit(float(r["probability_yes"]))
                                      - f["logit_mid"]])
        return f_
    cands["residual_on_market"] = resid
    return cands


# ── walk-forward ─────────────────────────────────────────────────────────

def event_of(row):
    """The settlement event a row belongs to. Several observations of one
    market resolve from a single price path and share one outcome."""
    return row.get("ticker")


def walk_forward(rows, n_folds=4, min_train=40, event_key=event_of):
    """Expanding-window folds over decision time. Yields (train, test).

    Two independent guarantees, both enforced here rather than relied upon:

      LABEL AVAILABILITY - a training row whose `settled_at` was still in
      the future when the test fold's first decision was taken is purged.
      You cannot train on a label you could not yet have scored.

      EVENT ISOLATION - no settlement event may appear on both sides. One
      market observed at several time-to-expiry checkpoints yields several
      rows carrying ONE outcome; splitting them across train and test would
      let the model see the answer it is being asked for. Overlapping
      events are purged from TRAIN, never from test, so the evaluation set
      is never quietly shrunk.

    Event isolation was previously an emergent consequence of the label
    purge under the invariant settled_at >= observed_at. It held, but was
    undefended: a row violating that invariant reintroduced leakage
    silently. It is now checked directly, and a fold whose training set no
    longer meets `min_train` is dropped rather than run undersized.
    """
    n = len(rows)
    if n < min_train + n_folds:
        return []
    start = max(min_train, int(n * 0.5))
    edges = [start + round(i * (n - start) / n_folds) for i in range(n_folds + 1)]
    folds = []
    for i in range(n_folds):
        a, b = edges[i], edges[i + 1]
        if b - a < 1:
            continue
        test = rows[a:b]
        cutoff = _ts(test[0]["ts"])
        test_events = {event_key(r) for r in test}
        train = [r for r in rows[:a]
                 if _ts(r["settled_at"]) < cutoff
                 and event_key(r) not in test_events]
        if len(train) >= min_train:
            assert not ({event_key(r) for r in train} & test_events), \
                "event isolation violated"
            folds.append((train, test))
    return folds


def block_bootstrap_ci(rows, deltas_by_row, n=4000, seed=20260902):
    """Resamples whole settlement windows, not rows: observations of one
    window share a price path and are one piece of evidence."""
    by = defaultdict(list)
    for r, d in zip(rows, deltas_by_row):
        by[r["ticker"]].append(d)
    keys = list(by)
    if len(keys) < 2:
        return None, None
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        s = []
        for _ in range(len(keys)):
            s.extend(by[rng.choice(keys)])
        if s:
            out.append(sum(s) / len(s))
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


# ── trading simulation ───────────────────────────────────────────────────

def trading_fee(count, price_cents):
    p = max(1, min(99, price_cents)) / 100.0
    return math.ceil(FEE_RATE * count * p * (1 - p) * 100) / 100.0


def simulate(preds, min_edge, max_spread_c, slippage_c=1):
    """One contract per opportunity, taking the ask on the side the model
    prefers. A trade is only possible where the book actually quotes it,
    so an absent or crossed price is skipped rather than assumed."""
    trades, pnl_list = [], []
    for r, p in preds:
        f = features(r)
        ask_c, bid_c = float(r["yes_ask"]), float(r["yes_bid"])
        if (ask_c - bid_c) > max_spread_c:
            continue
        y = label(r)
        if p - f["market_ask"] > min_edge and 0 < ask_c < 100:
            cost_c = ask_c + slippage_c
            gross = (100.0 - cost_c) / 100.0 if y == 1 else -cost_c / 100.0
            side, price_c = "yes", cost_c
        elif (1 - p) - (1 - f["market_mid"] + (ask_c - bid_c) / 200.0) > min_edge \
                and 0 < (100 - bid_c) < 100:
            no_ask_c = 100.0 - bid_c
            cost_c = no_ask_c + slippage_c
            gross = (100.0 - cost_c) / 100.0 if y == 0 else -cost_c / 100.0
            side, price_c = "no", cost_c
        else:
            continue
        fee = trading_fee(1, int(round(price_c)))
        net = gross - fee
        trades.append({"ticker": r["ticker"], "side": side,
                       "price_c": price_c, "gross": gross, "fee": fee,
                       "net": net, "won": net > 0})
        pnl_list.append(net)
    if not trades:
        return {"trades": 0}
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [t["net"] for t in trades if t["net"] <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pnl_list:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(trades),
        "gross_pnl": round(sum(t["gross"] for t in trades), 4),
        "fees": round(sum(t["fee"] for t in trades), 4),
        "slippage_cents_per_trade": slippage_c,
        "net_pnl": round(sum(pnl_list), 4),
        "win_rate": round(len(wins) / len(trades), 4),
        "profit_factor": (round(gross_win / gross_loss, 4)
                          if gross_loss > 0 else None),
        "expectancy_per_trade": round(sum(pnl_list) / len(trades), 4),
        "max_drawdown": round(dd, 4),
        "capital_at_risk": round(sum(t["price_c"] for t in trades) / 100.0, 4),
        "return_on_capital": (round(sum(pnl_list)
                                    / (sum(t["price_c"] for t in trades) / 100.0), 4)
                              if trades else None),
    }


# ── evaluation ───────────────────────────────────────────────────────────

def evaluate(rows, n_folds=4, min_edge=0.05, max_spread_c=4):
    folds = walk_forward(rows, n_folds=n_folds)
    cands = build_candidates()
    pooled = {k: [] for k in cands}          # (row, p) over all test folds
    fold_info, used_folds = [], {k: 0 for k in cands}

    for i, (train, test) in enumerate(folds):
        fold_info.append({
            "fold": i, "train_n": len(train), "test_n": len(test),
            "train_windows": len({r["ticker"] for r in train}),
            "test_windows": len({r["ticker"] for r in test}),
            "test_from": test[0]["ts"], "test_to": test[-1]["ts"],
            "test_yes_rate": round(sum(label(r) for r in test) / len(test), 4),
        })
        for name, make in cands.items():
            fn = make(train)
            if fn is None:
                continue
            used_folds[name] += 1
            for r in test:
                pooled[name].append((r, min(1 - EPS, max(EPS, fn(r)))))

    market = {id(r): features(r)["market_ask"] for r in rows}
    results = []
    for name, preds in pooled.items():
        if not preds:
            continue
        rs = [r for r, _ in preds]
        pairs = [(p, label(r)) for r, p in preds]
        mpairs = [(market[id(r)], label(r)) for r, _ in preds]
        bm, bb = brier(pairs), brier(mpairs)
        per_row = [(p - y) ** 2 - (market[id(r)] - y) ** 2
                   for (r, p), (_, y) in zip(preds, pairs)]
        lo, hi = block_bootstrap_ci(rs, per_row)
        sim = simulate(preds, min_edge, max_spread_c)
        results.append({
            "model": name,
            "oos_n": len(preds),
            "oos_windows": len({r["ticker"] for r in rs}),
            "folds_fitted": used_folds[name],
            "brier": round(bm, 6),
            "market_brier": round(bb, 6),
            "delta_brier": round(bm - bb, 6),
            "delta_ci95": [round(lo, 6), round(hi, 6)] if lo is not None else None,
            "beats_market_significantly": bool(hi is not None and hi < 0),
            "log_loss": round(log_loss(pairs), 6),
            "sharpness": round(sharpness(pairs), 6),
            "trading": sim,
        })
    results.sort(key=lambda d: d["delta_brier"])
    return {"folds": fold_info, "results": results}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward model research")
    ap.add_argument("path")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--max-spread-cents", type=float, default=4)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    raw = open(a.path, "rb").read()
    rows = usable_rows(json.loads(raw.decode("utf-8")))
    rep = evaluate(rows, a.folds, a.min_edge, a.max_spread_cents)
    rep = {"generated_at": datetime.now(timezone.utc)
                                   .isoformat(timespec="seconds")
                                   .replace("+00:00", "Z"),
           "dataset_sha256": hashlib.sha256(raw).hexdigest(),
           "n_usable": len(rows),
           "n_settlement_windows": len({r["ticker"] for r in rows}),
           "n_settlement_dates": len({r["settled_at"][:10] for r in rows}),
           **rep}
    text = json.dumps(rep, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
