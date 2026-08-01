"""Offline probability calibration analysis.

The module deliberately does not alter trading decisions.  It consumes resolved
prediction records (for example ``ShadowPredictionStore.settled()`` records or
``TradeLogger.settled_trades()`` records), and reports reliability bins, Brier
score, and expected calibration error (ECE).

Accepted records use ``probability_yes`` (also ``predicted_prob`` or ``p``)
and ``result``/``outcome`` (``yes``/``no`` or 1/0).  Invalid or unresolved
records are skipped, which makes it safe to point at a mixed journal.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Any, Iterable, Optional


def _probability(record: Any) -> Optional[float]:
    if isinstance(record, (tuple, list)) and record:
        value = record[0]
    elif isinstance(record, dict):
        value = next((record[k] for k in ("probability_yes", "predicted_prob", "p")
                      if record.get(k) is not None), None)
    else:
        value = None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 1 else None


def _outcome(record: Any) -> Optional[int]:
    if isinstance(record, (tuple, list)) and len(record) > 1:
        value = record[1]
    elif isinstance(record, dict):
        value = next((record[k] for k in ("result", "outcome", "actual_outcome")
                      if record.get(k) is not None), None)
    else:
        value = None
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"yes", "y", "true", "1"}:
            return 1
        if value in {"no", "n", "false", "0"}:
            return 0
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number in (0, 1) else None


def collect_pairs(records: Iterable[Any]) -> list[tuple[float, int]]:
    """Extract valid ``(predicted_yes_probability, actual_yes)`` pairs."""
    pairs = []
    for record in records:
        p, y = _probability(record), _outcome(record)
        if p is not None and y is not None:
            pairs.append((p, y))
    return pairs


def calibration_curve(records: Iterable[Any], bins: int = 10) -> list[dict]:
    """Return equal-width reliability bins, including empty bins.

    Each item contains ``lower``, ``upper``, ``count``, ``mean_prediction``,
    and ``actual_rate``.  The final bin includes probability exactly 1.0.
    """
    if isinstance(bins, bool) or not isinstance(bins, int) or bins <= 0:
        raise ValueError("bins must be a positive integer")
    pairs = collect_pairs(records)
    grouped = [[] for _ in range(bins)]
    for p, y in pairs:
        grouped[min(bins - 1, int(p * bins))].append((p, y))
    curve = []
    for i, values in enumerate(grouped):
        lower, upper = i / bins, (i + 1) / bins
        curve.append({
            "lower": lower, "upper": upper, "count": len(values),
            "mean_prediction": (sum(p for p, _ in values) / len(values)
                                 if values else None),
            "actual_rate": (sum(y for _, y in values) / len(values)
                            if values else None),
        })
    return curve


def brier_score(records: Iterable[Any]) -> Optional[float]:
    pairs = collect_pairs(records)
    return (sum((p - y) ** 2 for p, y in pairs) / len(pairs)
            if pairs else None)


def expected_calibration_error(records: Iterable[Any], bins: int = 10) -> Optional[float]:
    """Compute count-weighted ECE: sum(n_bin/N * |mean_p - outcome_rate|)."""
    pairs = collect_pairs(records)
    if not pairs:
        return None
    total = len(pairs)
    return sum((item["count"] / total) *
               abs(item["mean_prediction"] - item["actual_rate"])
               for item in calibration_curve(pairs, bins) if item["count"])


def analyze(records: Iterable[Any], bins: int = 10, label: str = "") -> dict:
    """Produce a JSON-serializable calibration report."""
    pairs = collect_pairs(records)
    return {"label": label, "count": len(pairs),
            "brier_score": brier_score(pairs),
            "expected_calibration_error": expected_calibration_error(pairs, bins),
            "calibration_curve": calibration_curve(pairs, bins)}

# Friendly aliases for callers that use metric terminology.
measure = analyze
reliability_curve = calibration_curve

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analyze resolved probability predictions")
    parser.add_argument("path", help="JSON file containing a list of prediction records")
    parser.add_argument("--bins", type=int, default=10)
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as stream:
        records = json.load(stream)
    print(json.dumps(analyze(records, args.bins, args.path), indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
