"""Baseline and the four detectors on synthetic arrays (build contract section 8, steps 1-4). No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pipeline.detection import baseline, detectors

NAN = float("nan")


def trailing_median_reference(z: list[float], window: int, min_readings: int) -> list[float]:
    """Plain-Python trailing median, written independently of the code under test."""
    out = []
    for i in range(len(z)):
        values = sorted(v for v in z[max(0, i - window + 1) : i + 1] if not np.isnan(v))
        if len(values) < min_readings:
            out.append(NAN)
        elif len(values) % 2:
            out.append(values[len(values) // 2])
        else:
            out.append((values[len(values) // 2 - 1] + values[len(values) // 2]) / 2.0)
    return out


# --- threshold ----------------------------------------------------------------------------------------------------
def test_threshold_flags_values_beyond_either_limit_only():
    values = np.array([19.9, 20.0, 20.1, 60.0, 109.9, 110.0, 110.1, NAN])
    flags = detectors.beyond_limits(values, 20.0, 110.0)
    assert flags.tolist() == [True, False, False, False, False, False, True, False]  # limits themselves are allowed


def test_threshold_without_a_limit_on_one_side():
    values = np.array([-5.0, 0.5, 9.99, 10.01, NAN])
    assert detectors.beyond_limits(values, None, 10.0).tolist() == [False, False, False, True, False]
    assert detectors.beyond_limits(values, 0.0, None).tolist() == [True, False, False, False, False]
    assert not detectors.beyond_limits(values, None, None).any()


# --- robust z-score -----------------------------------------------------------------------------------------------
def test_zscore_detector_is_inclusive_at_the_level_and_two_sided():
    z = np.array([5.999, 6.0, -6.0, -5.999, 0.0, 12.5, NAN])
    assert detectors.at_or_above(z, 6.0).tolist() == [False, True, True, False, False, True, False]
    assert detectors.at_or_above(z, 3.0).tolist() == [True, True, True, True, False, True, False]


# --- rolling median -----------------------------------------------------------------------------------------------
def test_rolling_median_ignores_a_single_spike_and_catches_a_sustained_shift():
    z = np.zeros(40)
    z[5] = 50.0  # one huge reading: the median of the window does not move
    z[20:30] = 3.4  # ten hours of a moderate shift
    flags = detectors.rolling_median_flags(z, window=6)
    assert not flags[:20].any()
    # with a 6 h window the median reaches 3.4 once four of its six readings are shifted (hour 23) ...
    assert np.flatnonzero(flags).tolist() == list(range(23, 32))  # ... and stays there two hours after the shift
    reference = trailing_median_reference(z.tolist(), 6, 4)
    assert flags.tolist() == [abs(m) >= 3.0 for m in reference]


def test_rolling_median_works_for_either_sign():
    z = np.zeros(30)
    z[10:20] = -4.0
    flags = detectors.rolling_median_flags(z, window=6)
    assert flags[13:20].all() and not flags[:13].any()


def test_rolling_median_needs_four_readings_in_the_window():
    z = np.array([NAN, NAN, NAN, 5.0, 5.0, 5.0, 5.0, 5.0, NAN, 5.0])
    flags = detectors.rolling_median_flags(z, window=6)
    assert flags.tolist() == [False, False, False, False, False, False, True, True, False, True]
    assert detectors.ROLLING_MIN_READINGS == 4 and detectors.ROLLING_MEDIAN_LEVEL == 3.0


def test_rolling_median_never_flags_a_step_without_a_reading():
    z = np.full(20, 5.0)
    z[10] = NAN
    flags = detectors.rolling_median_flags(z, window=6)
    assert not flags[10] and flags[9] and flags[11]


def test_trailing_median_matches_an_independent_implementation():
    rng = np.random.default_rng(3)
    z = rng.normal(0.0, 2.0, 200)
    z[rng.integers(0, 200, 30)] = NAN
    for window, min_readings in ((6, 4), (3, 1), (12, 6)):
        np.testing.assert_allclose(
            detectors.trailing_median(z, window, min_readings),
            trailing_median_reference(z.tolist(), window, min_readings),
            equal_nan=True,
        )


def test_trailing_median_works_row_by_row_on_a_matrix():
    z = np.array([[1.0, 2.0, 3.0, 4.0], [10.0, NAN, 30.0, 40.0]])
    result = detectors.trailing_median(z, 2)
    np.testing.assert_allclose(result, [[1.0, 1.5, 2.5, 3.5], [10.0, 10.0, 30.0, 35.0]])


# --- Isolation Forest ---------------------------------------------------------------------------------------------
def test_iforest_features_are_z_delta_and_three_trailing_statistics():
    z = np.array([1.0, 3.0, NAN, 2.0, 6.0])
    features = detectors.iforest_features(z)
    assert features.shape == (5, 5)
    assert detectors.IFOREST_FEATURES == ("z", "delta_z", "mean_3h", "std_3h", "mean_12h")
    np.testing.assert_allclose(features[0], [1.0, 0.0, 1.0, 0.0, 1.0])  # no previous reading: change is zero
    np.testing.assert_allclose(features[1], [3.0, 2.0, 2.0, 1.0, 2.0])
    assert np.isnan(features[2]).all()  # no reading: no features
    np.testing.assert_allclose(features[3], [2.0, 0.0, 2.5, 0.5, 2.0])  # previous reading missing
    np.testing.assert_allclose(features[4], [6.0, 4.0, 4.0, 2.0, 3.0])


def test_iforest_scores_are_the_paper_score_and_deterministic_for_a_seed():
    rng = np.random.default_rng(11)
    z = rng.normal(0.0, 1.0, (4, 400))
    z[1, 200:206] = 14.0
    z[2, 50:60] = NAN
    scores = detectors.isolation_forest_scores(z, seed=42)
    assert scores.shape == z.shape
    assert np.isnan(scores[2, 50:60]).all() and np.isfinite(scores[~np.isnan(z)]).all()
    finite = scores[np.isfinite(scores)]
    assert finite.min() > 0.0 and finite.max() <= 1.0  # the original-paper score, not min-max rescaled
    assert 0.35 <= np.median(finite) <= 0.5  # ordinary readings sit below 0.5
    assert scores[1, 200:206].min() > np.quantile(finite, 0.99)  # the planted block is isolated first
    assert scores[1, 200] >= 0.62
    np.testing.assert_array_equal(detectors.isolation_forest_scores(z, seed=42), scores)
    assert not np.array_equal(detectors.isolation_forest_scores(z, seed=43), scores, equal_nan=True)


def test_iforest_uses_the_parameters_of_the_contract():
    assert (detectors.IFOREST_ESTIMATORS, detectors.IFOREST_MAX_SAMPLES) == (100, 256)


def test_iforest_without_enough_readings_scores_nothing():
    scores = detectors.isolation_forest_scores(np.array([[NAN, 1.0, NAN]]), seed=1)
    assert np.isnan(scores).all()


def test_iforest_is_positive_only_with_a_high_score_and_a_moderate_z():
    scores = np.array([0.70, 0.70, 0.61, 0.62, 0.90, NAN])
    z = np.array([2.99, 3.0, 9.0, -3.5, NAN, 8.0])
    assert detectors.corroborating(scores, z, 0.62, 3.0).tolist() == [False, True, False, True, False, False]


# --- baseline: work domain, profile, robust z ---------------------------------------------------------------------
def hourly_axis(days: int, tz: str = "America/Chicago") -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    start = datetime(2026, 9, 1, 5, tzinfo=UTC)  # local midnight in Dodge City (CDT)
    stamps = [start + timedelta(hours=i) for i in range(days * 24)]
    hours, weekend = baseline.local_time_keys(stamps, ZoneInfo(tz))
    return hours, weekend, stamps


def test_local_time_keys_use_the_study_area_clock():
    hours, weekend, _ = hourly_axis(7)
    assert hours[:3].tolist() == [0, 1, 2]  # 05:00 UTC is midnight local
    assert not weekend[:24].any()  # 2026-09-01 is a Tuesday
    saturday = 4 * 24
    assert weekend[saturday : saturday + 48].all() and not weekend[saturday + 48 :].any()
    utc_hours, _, _ = hourly_axis(1, "UTC")
    assert utc_hours[0] == 5


def test_vibration_is_modelled_in_log_units_and_the_others_natively():
    values = np.array([[0.5, 1.0, np.e, NAN]])
    np.testing.assert_allclose(baseline.to_work_domain(values, True), [[np.log(0.5), 0.0, 1.0, NAN]], equal_nan=True)
    np.testing.assert_allclose(baseline.to_work_domain(values, False), values, equal_nan=True)
    np.testing.assert_allclose(baseline.from_work_domain(baseline.to_work_domain(values, True), True), values, equal_nan=True)
    assert {"vibration"} == baseline.LOG_DOMAIN_TYPES
    assert {"temperature", "moisture"} == baseline.PEER_ADJUSTED_TYPES


def test_profile_is_the_median_by_local_hour_of_day():
    hours, weekend, _ = hourly_axis(10)
    pattern = 60.0 - 4.0 * np.exp(-((np.arange(24) - 7) ** 2) / 4.0)  # a morning dip
    values = np.tile(pattern, 10)[None, :].copy()
    values[0, 100] += 30.0  # one outlier does not move the median of its hour
    profile = baseline.hourly_profile(values, hours)
    np.testing.assert_allclose(profile[0], np.tile(pattern, 10))


def test_vibration_profile_also_splits_weekdays_and_weekends():
    hours, weekend, _ = hourly_axis(14)
    values = np.where(weekend, 0.7, 1.0)[None, :] * np.tile(1.0 + 0.5 * np.sin(np.arange(24) / 24 * 2 * np.pi), 14)
    profile = baseline.hourly_profile(values, hours, weekend)
    np.testing.assert_allclose(profile, values)
    pooled = baseline.hourly_profile(values, hours)
    assert not np.allclose(pooled, values)  # without the split the weekend level is missed


def test_robust_z_of_a_pressure_series_uses_median_mad_and_the_floor():
    rng = np.random.default_rng(5)
    hours, weekend, _ = hourly_axis(30)
    noise = rng.normal(0.0, 1.0, 720)
    values = (62.0 + noise)[None, :].copy()
    values[0, 400] += 12.0
    fit = baseline.fit_type_baseline(values, ["water_main"], "pressure", hours, weekend)
    residual = values[0] - baseline.hourly_profile(values, hours)[0]
    centre = np.median(residual)
    scale = max(1.4826 * np.median(np.abs(residual - centre)), 0.5)
    np.testing.assert_allclose(fit.z[0], (residual - centre) / scale)
    assert fit.scale[0] == pytest.approx(scale) and fit.floor[0] == 0.5
    assert fit.z[0, 400] > 8.0
    assert not fit.log_domain and fit.peer_classes == ()
    # expected band = expected +- 3 robust sigma, in native units
    np.testing.assert_allclose(fit.expected_high[0] - fit.expected[0], 3.0 * scale)
    np.testing.assert_allclose(fit.expected[0] - fit.expected_low[0], 3.0 * scale)


@pytest.mark.parametrize(
    ("sensor_type", "class_size", "floor"),
    [("temperature", 4, 0.5), ("temperature", 3, 1.5), ("temperature", 1, 1.5), ("vibration", 1, 0.10),
     ("vibration", 30, 0.10), ("moisture", 2, 0.5), ("moisture", 30, 0.5), ("pressure", 1, 0.5)],
)  # fmt: skip
def test_scale_floors(sensor_type, class_size, floor):
    assert baseline.scale_floor(sensor_type, class_size) == floor


def test_the_floor_applies_to_a_very_quiet_sensor():
    hours, weekend, _ = hourly_axis(10)
    values = np.full((1, 240), 20.0) + np.random.default_rng(1).normal(0.0, 0.01, 240)
    fit = baseline.fit_type_baseline(values, ["road_subgrade"], "moisture", hours, weekend)
    assert fit.scale[0] == 0.5  # 1.4826 * MAD would be about 0.01
    assert np.nanmax(np.abs(fit.z)) < 0.2


def test_vibration_band_is_multiplicative():
    rng = np.random.default_rng(2)
    hours, weekend, _ = hourly_axis(28)
    values = np.exp(rng.normal(0.0, 0.15, 672))[None, :]
    fit = baseline.fit_type_baseline(values, ["bridge_deck"], "vibration", hours, weekend)
    assert fit.log_domain
    np.testing.assert_allclose(fit.expected_high[0], fit.expected[0] * np.exp(3.0 * fit.scale[0]))
    np.testing.assert_allclose(fit.expected_low[0], fit.expected[0] * np.exp(-3.0 * fit.scale[0]))
    assert fit.scale[0] == pytest.approx(0.15, abs=0.04)
    work = np.log(values[0])
    np.testing.assert_allclose(fit.z[0], (work - np.log(fit.expected[0])) / fit.scale[0])


def test_peer_adjustment_removes_what_the_class_does_together_and_keeps_what_one_sensor_does_alone():
    """Six sensors warm up together (weather); one of them also has its own excursion."""
    rng = np.random.default_rng(8)
    hours, weekend, _ = hourly_axis(30)
    diurnal = 5.0 * np.cos((hours - 16) / 24 * 2 * np.pi)
    weather = np.zeros(720)
    weather[300:340] = 7.0  # a regional hot spell: every sensor sees it
    gains = np.array([0.9, 1.0, 1.1, 0.95, 1.05, 1.0])
    values = 20.0 + diurnal[None, :] + gains[:, None] * weather[None, :] + rng.normal(0.0, 0.5, (6, 720))
    values[2, 500:506] += 9.0  # only sensor 2
    placements = ["road_surface"] * 6
    fit = baseline.fit_type_baseline(values, placements, "temperature", hours, weekend)
    assert fit.peer_classes == ("road_surface",)
    assert np.abs(fit.z[:, 300:340]).max() < 3.5  # the shared hot spell is expected, not flagged
    assert fit.z[2, 500:506].min() > 8.0  # the lone excursion stands out
    others = np.delete(fit.z[:, 500:506], 2, axis=0)
    assert np.abs(others).max() < 3.5
    # without peers (pressure is not peer-adjusted) the same hot spell would look abnormal
    alone = baseline.fit_type_baseline(values, placements, "pressure", hours, weekend)
    assert np.abs(alone.z[:, 300:340]).max() > 6.0


def test_classes_with_fewer_than_four_sensors_provide_no_peer_median():
    hours, weekend, _ = hourly_axis(10)
    values = 20.0 + np.random.default_rng(4).normal(0.0, 0.5, (3, 240))
    fit = baseline.fit_type_baseline(values, ["bridge_deck"] * 3, "temperature", hours, weekend)
    assert fit.peer_classes == ()
    assert fit.floor.tolist() == [1.5, 1.5, 1.5]
    assert baseline.MIN_CLASS_SENSORS == 4


def test_huber_fit_is_not_pulled_by_outliers():
    rng = np.random.default_rng(6)
    x = rng.normal(0.0, 1.0, (300, 1))
    y = 2.0 * x[:, 0] + rng.normal(0.0, 0.05, 300)
    y[:15] += 40.0
    assert baseline.huber_fit(x, y)[0] == pytest.approx(2.0, abs=0.05)
    ordinary = np.linalg.lstsq(x, y, rcond=None)[0][0]
    assert abs(ordinary - 2.0) > abs(baseline.huber_fit(x, y)[0] - 2.0)
    assert (baseline.HUBER_C, baseline.HUBER_ITERATIONS) == (1.345, 8)
    assert baseline.huber_fit(np.ones((1, 2)), np.array([1.0])).tolist() == [0.0, 0.0]  # too few rows: no fit


def test_a_sensor_without_readings_has_no_baseline_and_gaps_have_no_z():
    hours, weekend, _ = hourly_axis(10)
    values = 60.0 + np.random.default_rng(9).normal(0.0, 1.0, (2, 240))
    values[0, 50:60] = NAN
    values[1, :] = NAN
    fit = baseline.fit_type_baseline(values, ["water_main"] * 2, "pressure", hours, weekend)
    assert np.isnan(fit.z[0, 50:60]).all() and np.isfinite(fit.z[0, :50]).all()
    assert np.isfinite(fit.expected[0]).all()  # the expected value is defined for every hour, also in a gap
    assert np.isnan(fit.z[1]).all() and np.isnan(fit.scale[1])


def test_mismatching_shapes_are_rejected():
    hours, weekend, _ = hourly_axis(2)
    with pytest.raises(ValueError):
        baseline.fit_type_baseline(np.zeros((2, 48)), ["water_main"], "pressure", hours, weekend)
    with pytest.raises(ValueError):
        baseline.fit_type_baseline(np.zeros((1, 40)), ["water_main"], "pressure", hours, weekend)


def test_ema_starts_at_zero_and_holds_through_missing_values():
    series = np.array([0.0, 1.0, 1.0, NAN, 1.0])
    out = baseline.ema(series, span_hours=24.0)
    alpha = 1.0 - np.exp(-1.0 / 24.0)
    expected = [0.0, alpha, alpha + alpha * (1 - alpha)]
    expected.append(expected[-1])
    expected.append(expected[-1] + alpha * (1 - expected[-1]))
    np.testing.assert_allclose(out, expected)
    assert baseline.PEER_EMA_HOURS == {"moisture": (24.0, 72.0)}
