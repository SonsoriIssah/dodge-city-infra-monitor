"""Risk-zone formula (build contract section 9) on tiny hand cases. Pure functions, no database."""

from __future__ import annotations

import math

import numpy as np
import pytest

from pipeline.analysis import risk_zones
from tests.support import SEVERITY_WEIGHT

HOUR = 3600.0
CENTRE = np.array([[0.0, 0.0]])


def scores(default_settings, anomalies, grid_hours, cells=CENTRE):
    """Risk of the given cells; ``anomalies`` = (severity, x, y, started hour, ended hour)."""
    xy = np.array([[a[1], a[2]] for a in anomalies], dtype=float).reshape(-1, 2)
    severity = np.array([SEVERITY_WEIGHT[a[0]] for a in anomalies], dtype=float)
    started = np.array([a[3] * HOUR for a in anomalies], dtype=float)
    ended = np.array([a[4] * HOUR for a in anomalies], dtype=float)
    grid = np.array(grid_hours, dtype=float) * HOUR
    return risk_zones.risk_scores(np.asarray(cells, dtype=float), xy, severity, started, ended, grid, default_settings)


def test_one_active_critical_at_the_cell_centre_scores_50(default_settings):
    assert scores(default_settings, [("critical", 0, 0, 10, 20)], [15])[0, 0] == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("severity", "score", "rounded"),
    [("critical", 50.0, 50), ("high", 100 * 4 / 14, 29), ("medium", 100 * 2 / 14, 14), ("low", 100 * 1 / 14, 7)],
)
def test_one_active_anomaly_of_each_severity(default_settings, severity, score, rounded):
    value = scores(default_settings, [(severity, 0, 0, 10, 20)], [15])[0, 0]
    assert value == pytest.approx(score)
    assert round(value) == rounded  # 50, 29 and 14 are the figures quoted in the contract


def test_two_active_criticals_at_the_centre_reach_the_reference_and_the_score_is_capped(default_settings):
    two = [("critical", 0, 0, 10, 20)] * 2
    assert scores(default_settings, two, [15])[0, 0] == pytest.approx(100.0)
    assert scores(default_settings, two * 3, [15])[0, 0] == 100.0  # min(100, ...)


def test_scores_add_up_over_anomalies(default_settings):
    mixed = [("critical", 0, 0, 10, 20), ("high", 0, 0, 10, 20), ("low", 0, 0, 10, 20)]
    assert scores(default_settings, mixed, [15])[0, 0] == pytest.approx(100 * (7 + 4 + 1) / 14)


@pytest.mark.parametrize("distance_m", [0.0, 125.0, 250.0, 500.0, 1000.0])
def test_the_kernel_is_gaussian_in_the_distance_from_the_cell_centre(default_settings, distance_m):
    value = scores(default_settings, [("critical", distance_m, 0, 10, 20)], [15])[0, 0]
    assert value == pytest.approx(50.0 * math.exp(-(distance_m**2) / (2 * 250.0**2)))
    diagonal = scores(default_settings, [("critical", distance_m / math.sqrt(2), -distance_m / math.sqrt(2), 10, 20)], [15])[0, 0]
    assert diagonal == pytest.approx(value)  # only the distance matters


def test_an_anomaly_counts_fully_while_active_and_decays_with_a_72_hour_half_life(default_settings):
    series = scores(default_settings, [("critical", 0, 0, 10, 20)], [9, 10, 20, 21, 20 + 72, 20 + 144, 20 + 14 * 24, 20 + 14 * 24 + 1])
    assert series[0].tolist() == pytest.approx(
        [0.0,  # not started yet
         50.0, 50.0,  # active from started_at to ended_at, both inclusive
         50.0 * 0.5 ** (1 / 72),
         25.0, 12.5,  # one and two half-lives after it ended
         50.0 * 0.5 ** (14 * 24 / 72),  # 14 days after it ended: still counted
         0.0]  # later than that: ignored
    )  # fmt: skip


def test_bandwidth_half_life_and_reference_come_from_the_settings(settings_factory):
    custom = settings_factory(RISK_BANDWIDTH_M=100.0, RISK_HALF_LIFE_HOURS=24.0, RISK_REFERENCE=7.0)
    assert scores(custom, [("critical", 0, 0, 10, 20)], [15])[0, 0] == pytest.approx(100.0)
    assert scores(custom, [("high", 100.0, 0, 10, 20)], [15])[0, 0] == pytest.approx(100 * 4 / 7 * math.exp(-0.5))
    assert scores(custom, [("high", 0, 0, 10, 20)], [44])[0, 0] == pytest.approx(100 * 4 / 7 / 2)


def test_every_cell_gets_its_own_distance(default_settings):
    cells = [[0.0, 0.0], [300.0, 0.0], [0.0, 600.0]]
    values = scores(default_settings, [("critical", 0, 0, 10, 20)], [15], cells)[:, 0]
    assert values.tolist() == pytest.approx([50.0, 50.0 * math.exp(-0.72), 50.0 * math.exp(-2.88)])


def test_no_anomalies_means_no_risk(default_settings):
    assert scores(default_settings, [], [0, 100]).tolist() == [[0.0, 0.0]]


@pytest.mark.parametrize(
    ("score", "level"),
    [(0.0, "low"), (24.99, "low"), (25.0, "moderate"), (49.99, "moderate"), (50.0, "high"), (74.99, "high"),
     (75.0, "very_high"), (100.0, "very_high")],
)  # fmt: skip
def test_risk_levels(score, level):
    assert risk_zones.risk_level(score) == level


def test_anomaly_count_is_the_number_of_anomalies_active_in_the_cell(default_settings):
    from pipeline.analysis.status import active_matrix

    started = np.array([10, 12, 30, 10]) * HOUR
    ended = np.array([20, 14, 40, 20]) * HOUR
    grid = np.array([9, 10, 13, 20, 21, 35]) * HOUR
    counts = risk_zones.active_counts(["0_0", "0_1"], ["0_0", "0_0", "0_0", "0_1"], active_matrix(started, ended, grid))
    assert counts.tolist() == [[0, 1, 2, 1, 0, 1], [0, 1, 1, 1, 0, 0]]
    outside = risk_zones.active_counts(["0_0"], [None, "9_9"], active_matrix(started[:2], ended[:2], grid))
    assert outside.tolist() == [[0, 0, 0, 0, 0, 0]]  # an anomaly outside every cell is counted nowhere


def test_storage_is_sparse_from_half_a_point_and_old_anomalies_are_dropped_after_14_days():
    assert risk_zones.MIN_STORED_SCORE == 0.5
    assert risk_zones.MAX_AGE_DAYS == 14.0
