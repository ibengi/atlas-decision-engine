import math

import pytest

from calibration import (analyze, brier_score, calibration_curve, collect_pairs,
                         expected_calibration_error)


RECORDS = [
    {"probability_yes": 0.05, "result": "no"},
    {"probability_yes": 0.15, "result": "no"},
    {"probability_yes": 0.25, "result": "yes"},
    {"probability_yes": 0.35, "result": "yes"},
    {"probability_yes": 0.85, "result": "yes"},
    {"probability_yes": 1.0, "result": "yes"},
    {"probability_yes": None, "result": "yes"},  # unresolved prediction
    {"probability_yes": 0.5, "result": None},    # unresolved market
]


def test_collect_pairs_accepts_shadow_records_and_skips_unresolved():
    assert collect_pairs(RECORDS) == [(.05, 0), (.15, 0), (.25, 1),
                                      (.35, 1), (.85, 1), (1.0, 1)]


def test_calibration_curve_has_empty_bins_and_includes_one():
    curve = calibration_curve(RECORDS, bins=10)
    assert len(curve) == 10
    assert curve[0]["count"] == 1
    assert curve[0]["mean_prediction"] == .05
    assert curve[0]["actual_rate"] == 0
    assert curve[1]["count"] == 1
    assert curve[8]["count"] == 1
    assert curve[9]["count"] == 1
    assert curve[9]["actual_rate"] == 1
    assert curve[4]["count"] == 0
    assert curve[4]["mean_prediction"] is None


def test_brier_and_ece_known_values():
    # Squared errors: .0025, .0225, .5625, .4225, .0225, 0.
    assert brier_score(RECORDS) == pytest.approx(1.0325 / 6)
    # Every non-empty bin has absolute calibration gap .05, .15, .75, .65, .15, 0.
    assert expected_calibration_error(RECORDS) == pytest.approx(1.75 / 6)


def test_analyze_is_json_friendly_and_aliases_work():
    report = analyze([{"p": .2, "outcome": 1}, {"p": .8, "outcome": 0}], bins=2)
    assert report["count"] == 2
    assert report["brier_score"] == pytest.approx(.64)
    assert math.isclose(report["expected_calibration_error"], .6)


def test_invalid_bin_count_rejected():
    with pytest.raises(ValueError):
        calibration_curve([], bins=0)
