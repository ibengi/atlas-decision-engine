"""
model_calibration.py — v1-ref (2026-07-24)
Calibration de probabilites (Platt scaling) + metriques.

NOTE d'honnetete : le fichier original n'a pas ete fourni ; cette version
de REFERENCE implemente le contrat consomme par backtest_btc15m.py :
  fit(obs) -> cal | None        obs = [{"p":float, "outcome":0|1}, ...]
  apply(cal, p) -> float
  brier(obs) -> float
  log_loss(obs) -> float
  evaluate(obs, label="") -> dict (courbe de fiabilite par deciles)
Deterministe : descente de gradient a pas et iterations FIXES, aucun
aleatoire. La calibration est ajustee sur TRAIN uniquement (respecte par
l'appelant).
"""

import math

EPS = 1e-6


def _logit(p):
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def _sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def fit(obs, iters: int = 500, lr: float = 0.05):
    """Platt : p' = sigmoid(a * logit(p) + b), (a,b) par descente de
    gradient sur la log-vraisemblance. Retourne None si < 20 points
    (calibration non fiable — on n'ajuste pas sur du bruit)."""
    pts = [( _logit(o["p"]), 1 if o["outcome"] else 0) for o in obs
           if o.get("p") is not None]
    if len(pts) < 20:
        return None
    a, b = 1.0, 0.0
    n = len(pts)
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in pts:
            e = _sigmoid(a * x + b) - y
            ga += e * x
            gb += e
        a -= lr * ga / n
        b -= lr * gb / n
    return {"a": round(a, 6), "b": round(b, 6), "n_train": n,
            "method": "platt"}


def apply(cal, p: float) -> float:
    if not cal:
        return p
    return min(1 - EPS, max(EPS, _sigmoid(cal["a"] * _logit(p) + cal["b"])))


def brier(obs) -> float:
    if not obs:
        return float("nan")
    return sum((o["p"] - o["outcome"]) ** 2 for o in obs) / len(obs)


def log_loss(obs) -> float:
    if not obs:
        return float("nan")
    s = 0.0
    for o in obs:
        p = min(1 - EPS, max(EPS, o["p"]))
        s += -(o["outcome"] * math.log(p) + (1 - o["outcome"])
               * math.log(1 - p))
    return s / len(obs)


def evaluate(obs, label: str = "") -> dict:
    """Courbe de fiabilite par deciles de probabilite predite."""
    buckets = []
    for i in range(10):
        lo, hi = i / 10.0, (i + 1) / 10.0
        sel = [o for o in obs if lo <= o["p"] < hi or (i == 9 and o["p"] == 1.0)]
        buckets.append({
            "bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
            "avg_predicted": round(sum(o["p"] for o in sel) / len(sel), 4)
            if sel else None,
            "observed_rate": round(sum(o["outcome"] for o in sel) / len(sel), 4)
            if sel else None,
        })
    return {"label": label, "n": len(obs),
            "brier": round(brier(obs), 6) if obs else None,
            "log_loss": round(log_loss(obs), 6) if obs else None,
            "reliability": buckets}
