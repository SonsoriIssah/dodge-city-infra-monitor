"""From flagged hours to anomaly events (build contract section 8, steps 5-6, and amendment A1). No database.

Covers merging, trimming, the persistence rule, the score and its components, severity bands, descriptive
signatures, the order of the detection methods, Isolation Forest as corroboration only, and the stored
status. The last part runs the whole in-memory detection on a small synthetic sensor network.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from pipeline.detection import events, runner
from pipeline.detection.events import DetectionParams
from pipeline.models import Reading
from pipeline.sensors.thresholds import Threshold
from tests.support import ANOMALY_LABELS, DETECTOR_ORDER, SEVERITIES, THRESHOLDS

PARAMS = DetectionParams(z_strong=6.0, z_min=3.0, rolling_steps=6, iforest_threshold=0.62, merge_gap_steps=2)
N = 48


def limits_of(sensor_type: str, placement: str) -> Threshold:
    warn_low, warn_high, crit_low, crit_high = THRESHOLDS[(sensor_type, placement)]
    unit = {"temperature": "°C", "vibration": "mm/s", "moisture": "%", "pressure": "psi"}[sensor_type]
    return Threshold(sensor_type, placement, unit, warn_low, warn_high, crit_low, crit_high, "")


PRESSURE = limits_of("pressure", "water_main")  # warn 40 / 90, critical 20 / 110
NO_FOREST = np.full(N, 0.45)


def series(z_by_hour: dict[int, float], values_by_hour: dict[int, float] | None = None, n: int = N):
    """A z series (zero elsewhere) and a pressure series (60 psi elsewhere)."""
    z = np.zeros(n)
    values = np.full(n, 60.0)
    for hour, value in z_by_hour.items():
        z[hour] = value
    for hour, value in (values_by_hour or {}).items():
        values[hour] = value
    return values, z


def find(z_by_hour, values_by_hour=None, forest=None, sensor_type="pressure", limits=PRESSURE, params=PARAMS, n=N):
    values, z = series(z_by_hour, values_by_hour, n)
    forest_scores = np.full(n, 0.45) if forest is None else forest
    flags = events.hour_flags(values, z, forest_scores, limits, params)
    return events.find_events(sensor_type, values, z, flags, limits, params), flags


def expected_score(z_peak: float, duration_hours: int, threshold: float) -> tuple[float, float, float, float]:
    """M, D, T and the anomaly score, straight from the formulas of the contract."""
    magnitude = min(max(math.log2(abs(z_peak) / 3.0) / 4.0, 0.0), 1.0)
    duration = min(max(math.log(1 + duration_hours) / math.log(97), 0.0), 1.0)
    return magnitude, duration, threshold, round(0.50 * magnitude + 0.25 * duration + 0.25 * threshold, 3)


# --- settings -> parameters ---------------------------------------------------------------------------------------
def test_detection_parameters_come_from_the_settings(default_settings, settings_factory):
    params = DetectionParams.from_settings(default_settings)
    assert (params.z_strong, params.z_min, params.rolling_steps, params.iforest_threshold, params.merge_gap_steps) == (
        6.0, 3.0, 6, 0.62, 2,
    )  # fmt: skip
    assert params.min_flagged_hours == 3
    half_hourly = DetectionParams.from_settings(settings_factory(DETECT_ROLLING_HOURS=6, DETECT_MERGE_GAP_HOURS=2), 0.5)
    assert (half_hourly.rolling_steps, half_hourly.merge_gap_steps) == (12, 4)


# --- merging and trimming -----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("flagged_hours", "gap", "runs"),
    [
        ([10, 11, 12], 2, [(10, 12)]),
        ([10, 11, 14], 2, [(10, 14)]),  # two unflagged hours between: merged
        ([10, 11, 15], 2, [(10, 11), (15, 15)]),  # three: split
        ([10, 13, 16, 19], 2, [(10, 19)]),
        ([10, 12], 0, [(10, 10), (12, 12)]),
        ([10, 11], 0, [(10, 11)]),
        ([0, 47], 2, [(0, 0), (47, 47)]),
        ([], 2, []),
    ],
)
def test_flagged_hours_are_merged_across_gaps_of_at_most_the_merge_gap(flagged_hours, gap, runs):
    flagged = np.zeros(N, dtype=bool)
    flagged[flagged_hours] = True
    assert events.merge_runs(flagged, gap) == runs


def test_hours_two_apart_form_one_event_and_three_apart_form_two():
    merged, _ = find({10: 4.0, 11: 4.0, 14: 4.0})
    assert [(e.start, e.end, e.flagged_hours, e.duration_hours) for e in merged] == [(10, 14, 3, 5)]
    split, _ = find({10: 7.0, 11: 4.0, 15: 7.0})
    assert [(e.start, e.end) for e in split] == [(10, 11), (15, 15)]


def test_an_event_is_trimmed_to_its_first_and_last_hour_at_or_above_z_min():
    found, flags = find(dict.fromkeys(range(20, 30), 5.0))
    assert flags.rolling[30] and flags.rolling[31]  # the rolling median is still high two hours after the shift
    assert flags.flagged[30] and not flags.moderate[30]
    assert [(e.start, e.end, e.duration_hours) for e in found] == [(20, 29, 10)]


def test_duration_is_the_span_from_first_to_last_flagged_reading_plus_one():
    found, _ = find({10: 4.0, 12: 4.0, 13: 4.0})
    assert [(e.start, e.end, e.duration_hours, e.flagged_hours) for e in found] == [(10, 13, 4, 3)]


def test_the_peak_is_the_hour_of_the_largest_absolute_z():
    found, _ = find({10: 4.0, 11: -9.5, 12: 7.0})
    assert [(e.peak, e.robust_z) for e in found] == [(11, -9.5)]


# --- persistence rule ---------------------------------------------------------------------------------------------
def test_a_single_strong_hour_is_an_anomaly():
    found, _ = find({10: 6.0})
    assert [(e.start, e.end, e.methods) for e in found] == [(10, 10, ("robust_zscore",))]


def test_one_or_two_moderate_hours_stay_flagged_readings_only():
    for hours in ({10: 5.9}, {10: 4.0, 11: 4.0}, {10: 3.0, 13: -5.5}):
        found, flags = find(hours)
        assert found == [], hours
        assert sorted(np.flatnonzero(flags.flagged)) == sorted(hours)  # the hours are still flagged


def test_three_flagged_hours_are_an_anomaly_even_without_a_strong_peak():
    found, _ = find({10: 3.2, 11: -3.1, 12: 3.4})
    assert len(found) == 1
    assert found[0].flagged_hours == 3 and abs(found[0].robust_z) < 6.0
    assert found[0].methods == ("robust_zscore",)  # persistent moderate deviations of the z-score


def test_a_critical_threshold_breach_is_an_anomaly_even_with_an_ordinary_z():
    found, _ = find({10: 1.0}, {10: 115.0})  # above crit_high = 110 psi
    assert len(found) == 1
    event = found[0]
    assert (event.start, event.end, event.methods) == (10, 10, ("threshold",))
    assert event.components["threshold"] == 1.0
    assert event.breach.level == "critical" and event.breach.side == "above" and event.breach.limit == 110.0


def test_a_warning_breach_alone_is_not_an_anomaly():
    found, flags = find({10: 1.0}, {10: 95.0})  # above warn_high = 90, below crit_high
    assert found == [] and not flags.flagged.any()


def test_hours_without_a_reading_are_never_flagged():
    values, z = series({10: 9.0, 11: 9.0})
    z[11] = values[11] = np.nan
    flags = events.hour_flags(values, z, NO_FOREST, PRESSURE, PARAMS)
    assert flags.flagged[10] and not flags.flagged[11]
    found = events.find_events("pressure", values, z, flags, PRESSURE, PARAMS)
    assert [(e.start, e.end) for e in found] == [(10, 10)]


# --- score, severity ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("z_peak", "duration_hours", "breach", "magnitude", "duration", "threshold"),
    [
        (3.0, 1, None, 0.0, math.log(2) / math.log(97), 0.0),
        (6.0, 1, None, 0.25, math.log(2) / math.log(97), 0.0),
        (-12.0, 8, None, 0.5, math.log(9) / math.log(97), 0.0),
        (24.0, 24, "warning", 0.75, math.log(25) / math.log(97), 0.5),
        (48.0, 96, "critical", 1.0, 1.0, 1.0),
        (500.0, 720, "critical", 1.0, 1.0, 1.0),  # clipped at 1
        (2.0, 3, None, 0.0, math.log(4) / math.log(97), 0.0),  # clipped at 0
        (7.4, 19, None, math.log2(7.4 / 3) / 4, math.log(20) / math.log(97), 0.0),  # the example of the contract
    ],
)
def test_score_components_follow_the_formulas(z_peak, duration_hours, breach, magnitude, duration, threshold):
    components = events.score_components(z_peak, duration_hours, breach)
    assert components == pytest.approx({"magnitude": magnitude, "duration": duration, "threshold": threshold})
    assert events.anomaly_score(components) == round(0.50 * magnitude + 0.25 * duration + 0.25 * threshold, 3)


def test_the_contract_example_scores_062_with_components_033_and_066():
    components = events.score_components(7.4, 19, None)
    assert round(components["magnitude"], 2) == 0.33 and round(components["duration"], 2) == 0.65
    assert events.anomaly_score(components) == pytest.approx(0.327, abs=0.001)
    # the example text of the contract (score 0.62) includes a breach; the formula is what is binding
    assert events.anomaly_score(events.score_components(48, 96, "critical")) == 1.0
    assert events.anomaly_score(events.score_components(3, 0, None)) == 0.0


@pytest.mark.parametrize(
    ("score", "severity"),
    [(0.0, "low"), (0.299, "low"), (0.30, "medium"), (0.499, "medium"), (0.50, "high"), (0.699, "high"),
     (0.70, "critical"), (1.0, "critical")],
)  # fmt: skip
def test_severity_bands(score, severity):
    assert events.severity_for(score) == severity
    assert events.SEVERITIES == SEVERITIES


def test_event_score_is_recomputed_from_peak_duration_and_breach():
    found, _ = find(dict.fromkeys(range(10, 22), 8.0) | {15: -13.0}, dict.fromkeys(range(10, 22), 35.0))
    event = found[0]
    magnitude, duration, threshold, score = expected_score(-13.0, 12, 0.5)  # 35 psi is below warn_low = 40
    assert event.components == {"magnitude": round(magnitude, 3), "duration": round(duration, 3), "threshold": 0.5}
    assert event.score == score
    assert score == pytest.approx(0.5 * 0.529 + 0.25 * 0.561 + 0.25 * 0.5, abs=0.001)  # 0.53
    assert event.severity == "high"
    assert event.breach.level == "warning" and event.breach.side == "below" and event.breach.extreme == 35.0


def test_the_threshold_component_takes_the_most_serious_limit_of_the_event():
    limits = limits_of("vibration", "bridge_deck")  # warn 5, critical 10 mm/s
    values = np.array([1.0, 6.0, 12.0, 7.0, np.nan])
    assert events.find_breach(values, limits) == events.Breach("critical", "above", 10.0, 12.0)
    assert events.find_breach(values[:2], limits) == events.Breach("warning", "above", 5.0, 6.0)
    assert events.find_breach(values[:1], limits) is None
    assert events.find_breach(np.array([np.nan]), limits) is None
    assert events.find_breach(np.array([15.0, 50.0]), PRESSURE) == events.Breach("critical", "below", 20.0, 15.0)


# --- descriptive signature ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sensor_type", "z_peak", "duration_hours", "anomaly_type"),
    [
        ("vibration", 9.0, 1, "vibration_spike"),
        ("vibration", 9.0, 3, "vibration_spike"),
        ("vibration", 9.0, 4, "sustained_high_vibration"),
        ("vibration", 4.0, 40, "sustained_high_vibration"),
        ("moisture", 5.0, 30, "moisture_increase"),
        ("moisture", -5.0, 30, "moisture_decrease"),
        ("pressure", 9.0, 2, "pressure_spike"),
        ("pressure", 9.0, 60, "pressure_spike"),
        ("pressure", -9.0, 35, "pressure_drop"),
        ("pressure", -9.0, 36, "pressure_decline"),
        ("pressure", -9.0, 1, "pressure_drop"),
        ("temperature", 9.0, 23, "temperature_spike"),
        ("temperature", 9.0, 24, "temperature_drift"),
        ("temperature", -9.0, 5, "temperature_drop"),
        ("temperature", -9.0, 30, "temperature_drift"),
    ],
)
def test_anomaly_type_is_a_signature_of_type_sign_and_duration(sensor_type, z_peak, duration_hours, anomaly_type):
    assert events.classify(sensor_type, z_peak, duration_hours) == anomaly_type


@pytest.mark.parametrize(("anomaly_type", "label"), sorted(ANOMALY_LABELS.items()))
def test_human_labels(anomaly_type, label):
    assert events.anomaly_label(anomaly_type) == label


def test_no_label_names_a_cause():
    for label in events.ANOMALY_LABELS.values():
        assert not any(word in label.lower() for word in ("leak", "break", "fail", "damage", "burst", "fault"))


# --- detection method ---------------------------------------------------------------------------------------------
def test_detection_methods_are_listed_in_the_order_of_the_contract():
    forest = np.full(N, 0.45)
    forest[12] = 0.80
    z = dict.fromkeys(range(10, 20), 7.0)
    found, _ = find(z, {12: 15.0}, forest)  # 15 psi is below crit_low
    assert found[0].methods == DETECTOR_ORDER == ("threshold", "robust_zscore", "rolling_median", "isolation_forest")
    assert "+".join(found[0].methods) == "threshold+robust_zscore+rolling_median+isolation_forest"


@pytest.mark.parametrize(
    ("z_by_hour", "methods"),
    [
        ({10: 7.0}, ("robust_zscore",)),
        (dict.fromkeys(range(10, 20), 4.0), ("rolling_median",)),
        (dict.fromkeys(range(10, 20), 7.0), ("robust_zscore", "rolling_median")),
        ({10: 3.5, 11: 3.5, 12: 3.5}, ("robust_zscore",)),
    ],
)
def test_only_the_detectors_that_fired_are_listed(z_by_hour, methods):
    found, _ = find(z_by_hour)
    assert found[0].methods == methods


# --- Isolation Forest: corroboration only -------------------------------------------------------------------------
def test_isolation_forest_alone_never_creates_an_event():
    forest = np.full(N, 0.95)  # the forest calls every hour abnormal
    found, flags = find({10: 3.5, 20: 2.9}, forest=forest)
    assert found == []
    assert flags.forest[10] and not flags.forest[20]  # positive only where |z| >= z_min as well
    assert np.flatnonzero(flags.flagged).tolist() == [10]  # ... and it adds no flagged hour of its own


def test_isolation_forest_never_extends_an_event():
    forest = np.full(N, 0.45)
    forest[5:25] = 0.95
    with_forest, _ = find({10: 7.0, 11: 7.0}, forest=forest)
    without, _ = find({10: 7.0, 11: 7.0})
    assert [(e.start, e.end, e.score, e.severity) for e in with_forest] == [(e.start, e.end, e.score, e.severity) for e in without]
    assert with_forest[0].methods == ("robust_zscore", "isolation_forest")
    assert without[0].methods == ("robust_zscore",)


def test_isolation_forest_does_not_save_an_event_the_persistence_rule_drops():
    forest = np.full(N, 0.45)
    forest[10:12] = 0.99
    found, _ = find({10: 5.0, 11: 5.0}, forest=forest)
    assert found == []


# --- stored status (amendment A1) ---------------------------------------------------------------------------------
@pytest.mark.parametrize(("last_flagged_hour", "status"), [(N - 1, "active"), (N - 2, "resolved"), (N - 3, "resolved"), (20, "resolved")])
def test_an_event_is_active_only_when_it_runs_to_the_last_time_step(last_flagged_hour, status):
    found, _ = find(dict.fromkeys(range(last_flagged_hour - 3, last_flagged_hour + 1), 7.0))
    assert len(found) == 1 and found[0].end == last_flagged_hour
    assert found[0].status == status


# --- the whole detection in memory --------------------------------------------------------------------------------
START = datetime(2026, 9, 1, 5, tzinfo=UTC)
HOURS = 15 * 24


def network():
    """Five pressure sensors with white noise and four planted behaviours."""
    rng = np.random.default_rng(21)
    sensors = [runner.SensorRow(f"PRS-{n:03d}", f"WM-{n:03d}", "pressure", "water_main", "psi", -100.01 - n * 1e-3, 37.75)
               for n in range(1, 6)]  # fmt: skip
    values = {s.sensor_id: 62.0 + rng.normal(0.0, 0.5, HOURS) for s in sensors}
    values["PRS-001"][100:105] -= 30.0  # a five-hour drop to about 32 psi (below the warning limit of 40)
    values["PRS-002"][HOURS - 3 :] += 20.0  # a rise that is still there at the last hour
    values["PRS-003"][200] = 62.0 + 2.2  # one unusual reading (about 4 robust sigma): flagged, not an anomaly
    values["PRS-004"][300:302] = np.nan  # a gap
    values["PRS-005"][150] += 20.0  # starts at the same hour as ...
    values["PRS-004"][150] += 20.0  # ... this one: numbering falls back on the sensor id
    values["PRS-005"][HOURS - 2] += 20.0  # ended one hour before the end of the window
    readings = [
        Reading(sensor_id, START + timedelta(hours=i), round(float(v), 4), "psi")
        for sensor_id, row in values.items()
        for i, v in enumerate(row)
        if not np.isnan(v)
    ]
    return sensors, readings, values


@pytest.fixture(scope="module")
def detection(settings_factory):
    sensors, readings, values = network()
    settings = settings_factory()
    grid = runner.grid_from_readings(sensors, readings, timedelta(hours=1))
    thresholds = {("pressure", "water_main"): PRESSURE}
    return grid, runner.detect(grid, thresholds, settings), values


def test_grid_spans_first_to_last_reading_with_gaps_as_nan(detection):
    grid, _, _ = detection
    assert (grid.axis.start, grid.axis.end, grid.axis.count) == (START, START + timedelta(hours=HOURS - 1), HOURS)
    assert grid.n_readings == 5 * HOURS - 2
    row = [s.sensor_id for s in grid.sensors].index("PRS-004")
    assert np.isnan(grid.values[row, 300:302]).all() and grid.times[row, 300] is None
    assert grid.times[row, 299] == START + timedelta(hours=299)


def test_anomalies_are_numbered_by_start_time_then_sensor_id(detection):
    _, result, _ = detection
    anomalies = result.anomalies
    assert [a.anomaly_id for a in anomalies] == [f"ANM-{n:04d}" for n in range(1, len(anomalies) + 1)]
    assert [(a.started_at, a.sensor_id) for a in anomalies] == sorted((a.started_at, a.sensor_id) for a in anomalies)
    assert [(a.sensor_id, (a.started_at - START) // timedelta(hours=1)) for a in anomalies] == [
        ("PRS-001", 100), ("PRS-004", 150), ("PRS-005", 150), ("PRS-002", HOURS - 3), ("PRS-005", HOURS - 2),
    ]  # fmt: skip


def test_event_fields_follow_the_contract(detection):
    grid, result, values = detection
    drop = result.anomalies[0]
    assert (drop.sensor_id, drop.asset_id, drop.sensor_type, drop.unit) == ("PRS-001", "WM-001", "pressure", "psi")
    assert drop.started_at == START + timedelta(hours=100) and drop.ended_at == START + timedelta(hours=104)
    assert drop.duration_hours == (drop.ended_at - drop.started_at) // timedelta(hours=1) + 1 == 5
    assert drop.started_at <= drop.peak_at <= drop.ended_at
    peak_index = (drop.peak_at - START) // timedelta(hours=1)
    assert drop.observed_value == round(float(values["PRS-001"][peak_index]), 4)  # an existing reading
    assert drop.expected_value == pytest.approx(62.0, abs=0.6)
    assert drop.robust_z == pytest.approx((drop.observed_value - drop.expected_value) / 0.5, rel=0.25)
    assert drop.robust_z < -40 and round(drop.robust_z, 3) == drop.robust_z
    assert drop.anomaly_type == "pressure_drop"
    assert drop.detection_method == "robust_zscore+rolling_median+isolation_forest"
    assert (drop.lon, drop.lat) == pytest.approx((-100.011, 37.75))  # the position of its sensor
    assert set(drop.score_components) == {"magnitude", "duration", "threshold"}


def test_scores_and_severities_are_reproducible_from_the_stored_fields(detection):
    _, result, _ = detection
    for anomaly in result.anomalies:
        magnitude, duration, threshold, score = expected_score(
            anomaly.robust_z, anomaly.duration_hours, anomaly.score_components["threshold"]
        )
        assert anomaly.score_components["magnitude"] == pytest.approx(magnitude, abs=1e-3)
        assert anomaly.score_components["duration"] == pytest.approx(duration, abs=1e-3)
        assert anomaly.anomaly_score == pytest.approx(score, abs=1e-3)
        assert 0.0 <= anomaly.anomaly_score <= 1.0
        assert anomaly.severity == events.severity_for(anomaly.anomaly_score)
        methods = anomaly.detection_method.split("+")
        assert methods == [name for name in DETECTOR_ORDER if name in methods] and methods
    drop = result.anomalies[0]
    assert drop.score_components["threshold"] == 0.5  # about 32 psi: beyond the warning limit, not the critical one
    assert drop.score_components["magnitude"] == pytest.approx(1.0, abs=0.05)
    assert drop.severity == "critical"  # 0.5 * 1.0 + 0.25 * 0.39 + 0.25 * 0.5 = 0.72


def test_stored_status_is_the_time_rule_at_the_end_of_the_window(detection):
    grid, result, _ = detection
    t_end = grid.axis.end
    for anomaly in result.anomalies:
        assert anomaly.status == ("active" if anomaly.ended_at >= t_end else "resolved"), anomaly.anomaly_id
    by_sensor = {(a.sensor_id, a.started_at): a for a in result.anomalies}
    still_there = by_sensor[("PRS-002", START + timedelta(hours=HOURS - 3))]
    assert still_there.status == "active" and still_there.ended_at == t_end
    assert "still present at the end of the analysed window" in still_there.explanation
    ended_an_hour_before = by_sensor[("PRS-005", START + timedelta(hours=HOURS - 2))]
    assert ended_an_hour_before.ended_at == t_end - timedelta(hours=1)
    assert ended_an_hour_before.status == "resolved"  # one definition of active: no merge-gap tolerance
    assert "still present" not in ended_an_hour_before.explanation
    assert sum(a.status == "active" for a in result.anomalies) == 1


def test_not_every_unusual_reading_is_an_anomaly(detection):
    grid, result, _ = detection
    row = [s.sensor_id for s in grid.sensors].index("PRS-003")
    assert result.flagged[row, 200]  # the unusual reading is flagged ...
    assert 3.0 <= abs(result.z[row, 200]) < 6.0
    assert not [a for a in result.anomalies if a.sensor_id == "PRS-003"]  # ... but no anomaly is raised for it
    in_anomalies = sum(a.extra["flagged_hours"] for a in result.anomalies)
    assert int(result.flagged.sum()) > in_anomalies


def test_scores_exist_for_every_reading_and_only_for_readings(detection):
    grid, result, _ = detection
    has_reading = np.isfinite(grid.values)
    assert np.isfinite(result.z[has_reading]).all() and np.isnan(result.z[~has_reading]).all()
    assert np.isfinite(result.forest[has_reading]).all()
    assert not result.flagged[~has_reading].any()
    assert np.isfinite(result.expected).all()
    assert (result.expected_low < result.expected).all() and (result.expected < result.expected_high).all()
    np.testing.assert_allclose(result.expected_high - result.expected, 3.0 * 0.5, atol=0.2)  # 3 robust sigma
    assert set(result.sensor_baselines) == {s.sensor_id for s in grid.sensors}
    assert result.sensor_baselines["PRS-001"] == {"scale": pytest.approx(0.5, abs=0.08), "floor": 0.5, "domain": "native"}


def test_run_parameters_record_the_method_and_every_tunable(detection, default_settings):
    grid, result, _ = detection
    params = runner.run_parameters(default_settings, grid.axis, result)
    assert params["retrospective"] is True
    assert (params["z_strong"], params["z_min"], params["rolling_hours"], params["merge_gap_hours"]) == (6.0, 3.0, 6, 2)
    assert params["iforest"]["threshold"] == 0.62 and params["iforest"]["n_estimators"] == 100
    assert params["iforest"]["max_samples"] == 256 and params["iforest"]["random_state"] == 42
    assert params["iforest"]["role"] == "corroborating evidence only"
    assert params["baseline"]["scale_floors"] == {"temperature": 0.5, "vibration": 0.10, "moisture": 0.5, "pressure": 0.5}
    assert params["baseline"]["small_class_floors"] == {"temperature": 1.5}
    assert params["step_seconds"] == 3600 and params["timezone"] == "America/Chicago"
    assert params["score"]["weights"] == {"magnitude": 0.50, "duration": 0.25, "threshold": 0.25}
    assert params["score"]["severity_min_score"] == {"critical": 0.70, "high": 0.50, "medium": 0.30}


def test_readings_off_the_grid_fall_into_the_cell_that_ends_after_them():
    sensors = [runner.SensorRow("PRS-001", "WM-001", "pressure", "water_main", "psi", -100.0, 37.0)]
    stamps = [START, START + timedelta(minutes=50), START + timedelta(hours=1), START + timedelta(hours=3)]
    readings = [Reading("PRS-001", ts, 60.0 + i, "psi") for i, ts in enumerate(stamps)]
    readings.append(Reading("PRS-999", START, 1.0, "psi"))  # unknown sensor: ignored
    grid = runner.grid_from_readings(sensors, readings, timedelta(hours=1))
    assert grid.axis.count == 4
    np.testing.assert_array_equal(grid.values[0], [60.0, 62.0, np.nan, 63.0])  # 00:50 is replaced by 01:00
    assert (grid.n_readings, grid.n_superseded) == (3, 1)


def test_detection_without_readings_is_an_input_error():
    sensors = [runner.SensorRow("PRS-001", "WM-001", "pressure", "water_main", "psi", -100.0, 37.0)]
    with pytest.raises(runner.DetectionInputError):
        runner.grid_from_readings(sensors, [], timedelta(hours=1))


def test_the_final_hour_check_requires_a_critical_and_a_high_anomaly_when_events_are_ongoing(detection):
    from pipeline.models import InjectedEvent

    grid, result, _ = detection
    ongoing = [InjectedEvent("pressure_spike", True, grid.axis.end - timedelta(hours=3), grid.axis.end, "PRS-002", "WM-002")]
    with pytest.raises(runner.DetectionTargetError, match="critical"):
        runner.assert_final_hour(result.anomalies, ongoing, grid.axis)
    ended = [InjectedEvent("pressure_spike", True, grid.axis.start, grid.axis.start + timedelta(hours=3), "PRS-002", "WM-002")]
    runner.assert_final_hour(result.anomalies, ended, grid.axis)  # nothing ongoing: nothing to assert
    runner.assert_final_hour(result.anomalies, [], grid.axis)
