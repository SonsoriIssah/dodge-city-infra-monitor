"""Derived Asset Health Score (build contract section 9 and amendment A4). Pure functions, no database.

Every worked scenario of the contract is asserted, the formula is recomputed independently, and the
vectorised implementation (all assets, all hours) is compared with that independent recomputation.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from pipeline.analysis import health
from pipeline.analysis.health import AnomalyState, health_score
from pipeline.analysis.status import AnomalyIntervals, SensorMatrix, TimeAxis, status_codes
from tests.support import SEVERITY_WEIGHT

QUIET = 0.4  # an ordinary trailing median |z|
LOUD = 9.0  # a sensor whose last six hours sit far beyond 3 sigma (reading penalty at its cap)


def reference(anomalies: list[tuple[str, float | None]], sensor_levels: list[float | None],
              half_life: float = 48.0, window_hours: float = 168.0) -> tuple[int, str, float, float, float, float]:  # fmt: skip
    """The formula of the contract, written out independently of the code under test.

    ``anomalies``: (severity, hours since the anomaly ended or None while active).
    ``sensor_levels``: per sensor the median |z| of its last 6 h, None = no reading.
    """
    weight_sum = weighted_severity = 0.0
    for severity, hours_since_end in anomalies:
        if hours_since_end is not None and hours_since_end > window_hours:
            continue  # ended before t - HEALTH_WINDOW_DAYS
        weight = 1.0 if hours_since_end is None or hours_since_end <= 0 else 0.5 ** (hours_since_end / half_life)
        weight_sum += weight
        weighted_severity += weight * SEVERITY_WEIGHT[severity]
    frequency = min(20.0, 6.0 * weight_sum)
    severity_penalty = min(45.0, 6.0 * weighted_severity)
    reporting = [level for level in sensor_levels if level is not None]
    reading = min(10.0, 2.0 * statistics.fmean(min(max(level - 3.0, 0.0), 5.0) for level in reporting)) if reporting else 0.0
    sensor = 20.0 * (len(sensor_levels) - len(reporting)) / len(sensor_levels)
    score = int(min(max(math.floor(100.0 - frequency - severity_penalty - reading - sensor + 0.5), 0), 100))
    status = "normal" if score >= 90 else "watch" if score >= 70 else "at_risk" if score >= 45 else "critical"
    return score, status, frequency, severity_penalty, reading, sensor


def states(anomalies: list[tuple[str, float | None]]) -> list[AnomalyState]:
    return [AnomalyState(severity, hours) for severity, hours in anomalies]


# --- worked scenarios of the contract -----------------------------------------------------------------------------
SCENARIOS = [
    # name, anomalies, sensor levels, expected score (or range), expected status
    ("nothing", [], [QUIET], 100, "normal"),
    ("one low anomaly ended 72 h ago", [("low", 72.0)], [QUIET], 96, "normal"),
    ("one active critical", [("critical", None)], [LOUD], 42, "critical"),
    ("one active critical on a 3-sensor asset", [("critical", None)], [LOUD, QUIET, QUIET], 49, "at_risk"),
    ("two medium ended 24 h and 96 h ago", [("medium", 24.0), ("medium", 96.0)], [QUIET], 83, "watch"),
    ("sole sensor offline", [], [None], 80, "watch"),
    ("active high, median |z| = 6 (A4)", [("high", None)], [6.0], 64, "at_risk"),
    ("active high, median |z| = 8 (A4)", [("high", None)], [8.0], 60, "at_risk"),
    ("active high, median |z| = 20 (A4)", [("high", None)], [20.0], 60, "at_risk"),
    ("active high on a quiet sensor (A4: up to 70, watch)", [("high", None)], [QUIET], 70, "watch"),
    ("active medium, quiet sensor", [("medium", None)], [QUIET], 82, "watch"),
    ("active medium, median |z| = 8", [("medium", None)], [8.0], 72, "watch"),
]


@pytest.mark.parametrize(("anomalies", "levels", "score", "status"), [s[1:] for s in SCENARIOS], ids=[s[0] for s in SCENARIOS])
def test_worked_scenarios_of_the_contract(anomalies, levels, score, status):
    result = health_score(states(anomalies), levels)
    assert (result.health_score, result.status) == (score, status)
    assert reference(anomalies, levels)[:2] == (score, status)  # the contract's formula gives the same


@pytest.mark.parametrize("median_z", [6.0, 6.5, 7.0, 7.9, 8.0, 12.0, 40.0])
def test_active_high_scores_60_to_64_at_risk_when_the_trailing_median_z_is_at_least_six(median_z):
    result = health_score([AnomalyState("high")], [median_z])
    assert 60 <= result.health_score <= 64 and result.status == "at_risk"


@pytest.mark.parametrize("median_z", [0.0, 2.0, 3.0, 5.0, 8.0, 30.0])
def test_active_medium_scores_72_to_82_watch(median_z):
    result = health_score([AnomalyState("medium")], [median_z])
    assert 72 <= result.health_score <= 82 and result.status == "watch"


def test_penalties_of_the_worked_scenarios():
    critical = health_score([AnomalyState("critical")], [LOUD])
    assert (critical.frequency_penalty, critical.severity_penalty, critical.reading_penalty, critical.sensor_penalty) == (6.0, 42.0, 10.0, 0.0)
    assert (critical.anomalies_in_window, critical.active_anomalies, critical.sensors_reporting, critical.sensors_total) == (1, 1, 1, 1)
    three = health_score([AnomalyState("critical")], [LOUD, QUIET, QUIET])
    assert three.reading_penalty == pytest.approx(2 * 5 / 3, abs=1e-3)  # mean over the three reporting sensors
    old = health_score([AnomalyState("low", 72.0)], [QUIET])
    weight = 0.5 ** (72 / 48)
    assert old.frequency_penalty == pytest.approx(6 * weight, abs=1e-3) and old.severity_penalty == pytest.approx(6 * weight, abs=1e-3)
    assert (old.anomalies_in_window, old.active_anomalies) == (1, 0)
    offline = health_score([], [None])
    assert (offline.sensor_penalty, offline.reading_penalty, offline.sensors_reporting, offline.sensors_total) == (20.0, 0.0, 0, 1)


# --- weights, window, caps, clamps --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hours_since_end", "weight"),
    [(None, 1.0), (0.0, 1.0), (48.0, 0.5), (96.0, 0.25), (24.0, 0.5**0.5), (168.0, 0.5**3.5)],
)
def test_resolved_anomalies_fade_with_a_48_hour_half_life(hours_since_end, weight):
    assert health.anomaly_weight(hours_since_end, 48.0) == pytest.approx(weight)
    result = health_score([AnomalyState("low", hours_since_end)], [QUIET])
    assert result.frequency_penalty == pytest.approx(6.0 * weight, abs=1e-3)


def test_anomalies_that_ended_before_the_window_do_not_count():
    inside = health_score([AnomalyState("critical", 168.0)], [QUIET])
    outside = health_score([AnomalyState("critical", 168.01)], [QUIET])
    assert inside.anomalies_in_window == 1 and inside.health_score < 100
    assert outside.anomalies_in_window == 0 and outside.health_score == 100 and outside.status == "normal"


def test_window_and_half_life_are_parameters():
    quick = health_score([AnomalyState("high", 24.0)], [QUIET], half_life_hours=24.0)
    slow = health_score([AnomalyState("high", 24.0)], [QUIET], half_life_hours=96.0)
    assert quick.frequency_penalty == pytest.approx(3.0) and slow.frequency_penalty == pytest.approx(6 * 0.5**0.25, abs=1e-3)
    assert health_score([AnomalyState("high", 30.0)], [QUIET], window_hours=24.0).health_score == 100


def test_severity_weights_are_1_2_4_7():
    assert health.SEVERITY_WEIGHTS == SEVERITY_WEIGHT
    for severity, weight in SEVERITY_WEIGHT.items():
        assert health_score([AnomalyState(severity)], [QUIET]).severity_penalty == min(45.0, 6.0 * weight)


def test_penalties_are_capped_at_20_45_10_and_20():
    worst = health_score([AnomalyState("critical")] * 10, [50.0])
    assert (worst.frequency_penalty, worst.severity_penalty, worst.reading_penalty, worst.sensor_penalty) == (20.0, 45.0, 10.0, 0.0)
    assert (worst.health_score, worst.status) == (25, "critical")
    dark = health_score([AnomalyState("critical")] * 10, [None, None])
    assert (dark.reading_penalty, dark.sensor_penalty, dark.health_score) == (0.0, 20.0, 15)
    half_dark = health_score([AnomalyState("critical")] * 10, [None, 50.0])
    assert (half_dark.reading_penalty, half_dark.sensor_penalty, half_dark.health_score) == (10.0, 10.0, 15)


def test_the_score_is_clamped_to_0_100():
    assert int(health.score_from_penalties(20.0, 45.0, 10.0, 20.0)) == 5  # the largest total the formula can reach
    assert int(health.score_from_penalties(80.0, 45.0, 10.0, 20.0)) == 0
    assert int(health.score_from_penalties(-5.0, 0.0, 0.0, 0.0)) == 100
    assert health.score_from_penalties(np.array([0.0, 200.0]), 0.0, 0.0, 0.0).tolist() == [100, 0]


@pytest.mark.parametrize(("total_penalty", "score"), [(0.49, 100), (0.5, 100), (0.51, 99), (29.5, 71), (30.49, 70), (30.51, 69)])
def test_the_score_is_rounded_to_the_nearest_integer(total_penalty, score):
    assert int(health.score_from_penalties(total_penalty, 0.0, 0.0, 0.0)) == score


@pytest.mark.parametrize(
    ("score", "status"),
    [(100, "normal"), (90, "normal"), (89, "watch"), (70, "watch"), (69, "at_risk"), (45, "at_risk"), (44, "critical"), (0, "critical")],
)
def test_status_bands(score, status):
    assert health.status_for(score) == status


def test_assets_at_risk_means_a_score_below_70():
    from pipeline.analysis.status import AT_RISK_BELOW

    assert AT_RISK_BELOW == 70
    assert dict(health.STATUS_BANDS) == {"normal": 90, "watch": 70, "at_risk": 45, "critical": 0}
    assert health.ASSET_STATUS_CHARS == {"normal": "n", "watch": "w", "at_risk": "r", "critical": "c"}


@pytest.mark.parametrize(("median_z", "excess"), [(0.0, 0.0), (3.0, 0.0), (3.5, 0.5), (8.0, 5.0), (100.0, 5.0)])
def test_reading_excess_is_clipped_between_three_and_eight_sigma(median_z, excess):
    assert float(health.reading_excess(median_z)) == pytest.approx(excess)
    assert health_score([], [median_z]).reading_penalty == pytest.approx(min(10.0, 2.0 * excess))


# --- no division by zero ------------------------------------------------------------------------------------------
def test_all_sensors_offline_gives_a_finite_score_without_a_reading_penalty():
    result = health_score([AnomalyState("high")], [None, None, None])
    assert result.reading_penalty == 0.0 and result.sensor_penalty == 20.0
    assert result.health_score == 100 - 6 - 24 - 20 and result.sensors_reporting == 0
    assert all(math.isfinite(value) for value in (result.frequency_penalty, result.severity_penalty, result.reading_penalty))


def test_an_asset_without_sensors_has_no_score():
    with pytest.raises(ValueError, match="not_monitored"):
        health_score([], [])
    assert health.STATUS_NOT_MONITORED == "not_monitored"


def test_partial_outage_scales_the_sensor_penalty_by_the_offline_share():
    result = health_score([], [QUIET, None, QUIET, None, None])
    assert result.sensor_penalty == pytest.approx(20.0 * 3 / 5)
    assert (result.sensors_reporting, result.sensors_total) == (2, 5)
    assert result.health_score == 88 and result.status == "watch"


# --- the pure function against the independent formula ------------------------------------------------------------
def test_health_score_equals_the_contract_formula_on_random_cases():
    rng = np.random.default_rng(17)
    for _ in range(400):
        anomalies = [
            (str(rng.choice(list(SEVERITY_WEIGHT))), None if rng.random() < 0.3 else float(rng.uniform(0.0, 220.0)))
            for _ in range(int(rng.integers(0, 6)))
        ]
        levels = [None if rng.random() < 0.2 else float(rng.uniform(0.0, 12.0)) for _ in range(int(rng.integers(1, 5)))]
        result = health_score(states(anomalies), levels)
        score, status, frequency, severity, reading, sensor = reference(anomalies, levels)
        assert (result.health_score, result.status) == (score, status), (anomalies, levels)
        assert result.frequency_penalty == pytest.approx(frequency, abs=1e-3)
        assert result.severity_penalty == pytest.approx(severity, abs=1e-3)
        assert result.reading_penalty == pytest.approx(reading, abs=1e-3)
        assert result.sensor_penalty == pytest.approx(sensor, abs=1e-3)


# --- every asset, every hour (vectorised) -------------------------------------------------------------------------
START = datetime(2026, 9, 1, 5, tzinfo=UTC)
STEPS = 400


@pytest.fixture(scope="module")
def synthetic():
    """Three assets (3, 2 and 1 sensors), 400 hours, gaps, and eight anomalies of mixed severity."""
    rng = np.random.default_rng(23)
    sensor_ids = [f"S{n}" for n in range(1, 7)]
    asset_ids = ["A", "A", "A", "B", "B", "C"]
    z = rng.normal(0.0, 1.2, (6, STEPS))
    z[0, 100:140] += 9.0
    z[3, 250:256] -= 5.0
    z[5, 300:] += 4.5
    reporting = np.ones((6, STEPS), dtype=bool)
    reporting[1, 50:80] = False
    reporting[5, 380:] = False  # the only sensor of asset C goes dark at the end
    reporting[3, 10:13] = False
    reporting[4, 10:13] = False
    z[~reporting] = np.nan
    axis = TimeAxis(START, START + timedelta(hours=STEPS - 1), timedelta(hours=1), run_id=1)
    epoch = lambda hour: (START + timedelta(hours=hour)).timestamp()  # noqa: E731
    spans = [("S1", "A", "critical", 100, 139), ("S1", "A", "low", 20, 22), ("S2", "A", "medium", 200, 260),
             ("S4", "B", "high", 250, 255), ("S4", "B", "medium", 30, 31), ("S6", "C", "high", 300, STEPS - 1),
             ("S6", "C", "low", 5, 5), ("S5", "B", "critical", 390, STEPS - 1)]  # fmt: skip
    anomalies = AnomalyIntervals(
        anomaly_ids=[f"ANM-{n:04d}" for n in range(1, len(spans) + 1)],
        sensor_ids=[s[0] for s in spans],
        asset_ids=[s[1] for s in spans],
        severities=[s[2] for s in spans],
        started=np.array([epoch(s[3]) for s in spans]),
        ended=np.array([epoch(s[4]) for s in spans]),
    )
    nothing = np.zeros((6, STEPS), dtype=bool)
    matrix = SensorMatrix(
        axis=axis, sensor_ids=sensor_ids, asset_ids=asset_ids, sensor_types=["pressure"] * 6,
        values=np.where(reporting, 60.0, np.nan), robust_z=z, reporting=reporting, flagged=nothing,
        outside_warn=nothing, anomaly_active=nothing, status=status_codes(reporting, nothing, nothing, nothing),
    )  # fmt: skip
    return matrix, anomalies, spans


def independent_health(matrix, spans, asset: str, t: int):
    """Health of one asset at one hour from the raw definitions (no shared code with the implementation)."""
    anomalies = []
    for _sensor, owner, severity, started, ended in spans:
        if owner != asset or started > t:
            continue  # only anomalies that have started count
        anomalies.append((severity, None if ended >= t else float(t - ended)))
    levels: list[float | None] = []
    for row, owner in enumerate(matrix.asset_ids):
        if owner != asset:
            continue
        if not matrix.reporting[row, t]:
            levels.append(None)
            continue
        window = matrix.robust_z[row, max(t - 5, 0) : t + 1]
        levels.append(float(np.median(np.abs(window[np.isfinite(window)]))))
    in_window = sum(1 for _, hours in anomalies if hours is None or hours <= 168.0)
    active = sum(1 for _, hours in anomalies if hours is None)
    return reference(anomalies, levels), in_window, active, sum(level is not None for level in levels), len(levels)


def test_vectorised_health_equals_the_independent_formula_at_every_hour(synthetic, default_settings):
    matrix, anomalies, spans = synthetic
    series = health.compute_health(matrix, anomalies, default_settings)
    assert series.asset_ids == ["A", "B", "C"]
    assert series.sensors_total.tolist() == [3, 2, 1]
    for i, asset in enumerate(series.asset_ids):
        for t in range(STEPS):
            (score, _status, frequency, severity, reading, sensor), in_window, active, reporting, _total = independent_health(matrix, spans, asset, t)
            where = (asset, t)
            assert series.health_score[i, t] == score, where
            assert series.frequency_penalty[i, t] == pytest.approx(frequency, abs=1e-3), where
            assert series.severity_penalty[i, t] == pytest.approx(severity, abs=1e-3), where
            assert series.reading_penalty[i, t] == pytest.approx(reading, abs=1e-3), where
            assert series.sensor_penalty[i, t] == pytest.approx(sensor, abs=1e-3), where
            assert series.anomalies_in_window[i, t] == in_window, where
            assert series.active_anomalies[i, t] == active, where
            assert series.sensors_reporting[i, t] == reporting, where


def test_vectorised_health_handles_an_asset_whose_sensors_are_all_offline(synthetic, default_settings):
    matrix, anomalies, _ = synthetic
    series = health.compute_health(matrix, anomalies, default_settings)
    c = series.asset_ids.index("C")
    assert series.sensors_reporting[c, -1] == 0
    assert series.reading_penalty[c, -1] == 0.0 and series.sensor_penalty[c, -1] == 20.0
    assert np.isfinite(series.reading_penalty).all() and np.isfinite(series.sensor_penalty).all()
    assert series.health_score.min() >= 0 and series.health_score.max() <= 100
    b = series.asset_ids.index("B")
    assert series.sensors_reporting[b, 11] == 0 and series.health_score[b, 11] == 80  # both sensors dark, no anomaly yet


def test_status_strings_have_one_character_per_hour(synthetic, default_settings):
    matrix, anomalies, _ = synthetic
    series = health.compute_health(matrix, anomalies, default_settings)
    strings = series.status_strings()
    assert set(strings) == {"A", "B", "C"} and {len(text) for text in strings.values()} == {STEPS}
    for i, asset in enumerate(series.asset_ids):
        expected = "".join({"normal": "n", "watch": "w", "at_risk": "r", "critical": "c"}[reference_status(int(score))]
                           for score in series.health_score[i])  # fmt: skip
        assert strings[asset] == expected


def reference_status(score: int) -> str:
    return "normal" if score >= 90 else "watch" if score >= 70 else "at_risk" if score >= 45 else "critical"


def test_an_anomaly_does_not_lower_the_score_before_it_starts(synthetic, default_settings):
    matrix, anomalies, _ = synthetic
    series = health.compute_health(matrix, anomalies, default_settings)
    a = series.asset_ids.index("A")
    assert series.health_score[a, 0] == 100 and series.anomalies_in_window[a, 19] == 0
    assert series.anomalies_in_window[a, 20] == 1 and series.active_anomalies[a, 20] == 1


def test_formula_text_states_every_term():
    text = health.FORMULA_TEXT
    for fragment in ("min(20, 6", "min(45, 6", "min(10, 2", "20 *", "low 1, medium 2, high 4", "critical 7", "0.5 **",
                     "normal >= 90 > watch >= 70 > at_risk >= 45 > critical"):  # fmt: skip
        assert fragment in text, fragment
