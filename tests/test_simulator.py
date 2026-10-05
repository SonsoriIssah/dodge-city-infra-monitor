"""The sensor simulator (build contract section 7): determinism, event schedule, sigma rule, realism.

Runs on the default study area in memory (placement on the committed raw data, 30 days, seed 42).
No database.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pipeline import geo
from pipeline.models import TAG_NBI_BRIDGE, Reading
from pipeline.sensors import simulator
from pipeline.sensors.ingestion import PLAUSIBLE_RANGES
from tests.support import EVENT_MIX_40, THRESHOLDS

LOCAL = ZoneInfo("America/Chicago")
# Duration (h) and magnitude ranges of the injected events, contract section 7 (moisture: A6, 12-72 h in total).
EVENT_RANGES = {
    "vibration_spike": ((1, 2), (4.0, 8.0)),
    "sustained_high_vibration": ((6, 48), (2.2, 3.5)),
    "moisture_increase": ((12, 72), (6.0, 14.0)),
    "pressure_drop": ((4, 24), (12.0, 40.0)),
    "pressure_spike": ((1, 3), (15.0, 30.0)),
    "pressure_decline": ((48, 96), (8.0, 15.0)),
    "temperature_spike": ((3, 8), (8.0, 15.0)),
    "temperature_drift": ((48, 120), (5.0, 10.0)),
}
SHORT_EVENTS = {"vibration_spike", "pressure_drop", "pressure_spike", "temperature_spike"}  # >= 8 sigma at the peak
STEP_EVENTS = {"pressure_drop", "moisture_increase", "sustained_high_vibration"}
RAMP_EVENTS = {"pressure_decline", "temperature_drift"}
SIGN = {"pressure_drop": -1, "pressure_decline": -1}


@pytest.fixture(scope="module")
def sim(default_simulation):
    return default_simulation


@pytest.fixture(scope="module")
def sensors(sim):
    return {s.sensor_id: s for s in sim.sensors}


def local_hours(sim) -> np.ndarray:
    return np.array([ts.astimezone(LOCAL).hour for ts in sim.timestamps])


def profile_by_hour(values: np.ndarray, hours: np.ndarray, keep: np.ndarray | None = None) -> np.ndarray:
    keep = np.ones(len(values), dtype=bool) if keep is None else keep
    return np.array([np.nanmedian(values[(hours == h) & keep]) for h in range(24)])


# --- time axis, determinism ---------------------------------------------------------------------------------------
def test_readings_are_hourly_tz_aware_utc_over_the_configured_window(sim, default_settings):
    assert len(sim.timestamps) == 720
    assert sim.timestamps[0] == datetime(2026, 9, 1, 5, tzinfo=UTC) and sim.timestamps[-1] == datetime(2026, 10, 1, 4, tzinfo=UTC)
    assert all(ts.utcoffset() == timedelta(0) for ts in sim.timestamps)
    assert sim.timestamps == default_settings.time_axis()
    assert set(sim.values) == {s.sensor_id for s in sim.sensors}
    assert all(len(series) == 720 for series in sim.values.values())


def test_same_seed_gives_identical_readings_and_ground_truth(sim, default_plan, default_settings):
    again = simulator.simulate(list(reversed(default_plan.sensors)), default_settings)  # sensor order must not matter
    assert set(again.values) == set(sim.values)
    for sensor_id, series in sim.values.items():
        np.testing.assert_array_equal(again.values[sensor_id], series)
    assert again.ground_truth() == sim.ground_truth()
    assert again.dropouts == sim.dropouts and again.colocated_group == sim.colocated_group


def test_another_seed_gives_different_readings(sim, default_plan, settings_factory):
    other = simulator.simulate(default_plan.sensors, settings_factory(SIM_SEED=43))
    different = [sid for sid in sim.values if not np.array_equal(other.values[sid], sim.values[sid], equal_nan=True)]
    assert len(different) == len(sim.values)  # every sensor has its own seeded generator
    assert other.ground_truth() != sim.ground_truth()


def test_each_sensor_has_its_own_generator_seeded_from_crc32_xor_seed():
    import zlib

    expected = np.random.default_rng(zlib.crc32(b"VIB-001") ^ 42).normal(size=4)
    np.testing.assert_array_equal(simulator.sensor_rng("VIB-001", 42).normal(size=4), expected)
    assert not np.array_equal(simulator.sensor_rng("VIB-002", 42).normal(size=4), expected)


def test_the_readings_iterator_yields_rounded_readings_and_skips_dropouts(sim, sensors):
    readings = list(sim.readings())
    assert len(readings) == sim.reading_count() == sum(int(np.isfinite(v).sum()) for v in sim.values.values())
    assert all(isinstance(r, Reading) and r.ts.tzinfo is not None and math.isfinite(r.value) for r in readings[:500])
    assert {r.unit for r in readings} == {"°C", "mm/s", "%", "psi"}
    first = readings[0]
    assert first.value == round(float(sim.values[first.sensor_id][0]), 4)
    assert first.unit == sensors[first.sensor_id].unit
    start, end = sim.timestamps[10], sim.timestamps[12]
    window = list(sim.readings(start, end))
    assert {r.ts for r in window} <= {sim.timestamps[10], sim.timestamps[11], sim.timestamps[12]}  # both ends inclusive
    assert {r.ts for r in window} == {sim.timestamps[10], sim.timestamps[11], sim.timestamps[12]}


# --- event schedule -----------------------------------------------------------------------------------------------
def test_event_mix_for_forty_events(sim):
    assert Counter(d.event.event_type for d in sim.events) == Counter(EVENT_MIX_40)
    assert len(sim.events) == 40 and all(d.event.is_anomaly for d in sim.events)


@pytest.mark.parametrize("total", [0, 1, 8, 20, 39, 40, 41, 80, 100])
def test_event_mix_is_scaled_proportionally(total):
    mix = simulator.scaled_event_mix(total)
    assert sum(mix.values()) == total
    assert set(mix) == set(EVENT_MIX_40)
    for name, count in mix.items():
        exact = EVENT_MIX_40[name] * total / 40
        assert abs(count - exact) < 1.0, (name, count, exact)
    if total % 40 == 0:
        assert mix == {name: count * total // 40 for name, count in EVENT_MIX_40.items()}


def test_a_smaller_event_budget_is_simulated_with_the_scaled_mix(default_plan, settings_factory):
    result = simulator.simulate(default_plan.sensors, settings_factory(SIM_ANOMALY_EVENTS=20))
    assert Counter(d.event.event_type for d in result.events) == Counter(
        {name: count for name, count in simulator.scaled_event_mix(20).items() if count}
    )
    none = simulator.simulate(default_plan.sensors, settings_factory(SIM_ANOMALY_EVENTS=0))
    assert none.events == [] and none.colocated_group == []
    assert len(none.regional_events) == 4  # the benign weather is still simulated


def test_the_first_72_hours_are_quiet(sim):
    assert min(d.start_index for d in sim.events) >= 72
    lead_in_end = sim.timestamps[0] + timedelta(hours=72)
    assert all(d.event.started_at >= lead_in_end for d in sim.events)


def test_start_times_are_spread_over_the_window(sim):
    starts = sorted(d.start_index for d in sim.events)
    quarters = Counter(min((s - 72) * 4 // (720 - 72), 3) for s in starts)
    assert set(quarters) == {0, 1, 2, 3} and min(quarters.values()) >= 5  # no quarter of the window is left empty
    assert max(b - a for a, b in zip(starts, starts[1:])) <= 72


def test_at_most_two_events_and_144_event_hours_per_sensor(sim):
    per_sensor = Counter(d.event.sensor_id for d in sim.events)
    hours = Counter()
    for detail in sim.events:
        hours[detail.event.sensor_id] += detail.end_index - detail.start_index + 1
    assert max(per_sensor.values()) <= 2
    assert max(hours.values()) <= 144
    for a, b in combinations(sim.events, 2):
        if a.event.sensor_id == b.event.sensor_id:
            assert a.end_index < b.start_index or b.end_index < a.start_index  # never overlapping


def test_events_sit_on_a_sensor_of_their_type_and_its_asset(sim, sensors):
    for detail in sim.events:
        sensor = sensors[detail.event.sensor_id]
        assert detail.event.event_type.split("_")[0] in (sensor.sensor_type, "sustained")
        assert detail.event.sensor_type == sensor.sensor_type and detail.event.asset_id == sensor.asset_id
        assert detail.event.started_at == sim.timestamps[detail.start_index]
        assert detail.event.ended_at == sim.timestamps[detail.end_index]


def test_durations_and_magnitudes_stay_in_their_ranges(sim):
    last = len(sim.timestamps) - 1
    for detail in sim.events:
        (low_h, high_h), (low_m, high_m) = EVENT_RANGES[detail.event.event_type]
        hours = detail.end_index - detail.start_index + 1
        magnitude = abs(detail.event.magnitude)
        sized_by_hand = detail.role != "scheduled"  # final-hour and co-located events are sized for their purpose
        if not sized_by_hand:
            assert low_h <= hours <= high_h, (detail.event.event_type, hours)
            assert low_m - 1e-6 <= magnitude, (detail.event.event_type, magnitude)
        if detail.end_index < last:
            assert hours <= high_h
        assert np.sign(detail.event.magnitude) == SIGN.get(detail.event.event_type, 1)


# --- the final hour -----------------------------------------------------------------------------------------------
def test_exactly_six_events_are_ongoing_at_the_last_timestamp(sim, sensors):
    ongoing = sim.ongoing_at_end()
    last = len(sim.timestamps) - 1
    assert len(ongoing) == 6
    assert {d.event.ended_at for d in ongoing} == {sim.timestamps[-1]}
    assert [d for d in sim.events if d.end_index == last] == ongoing  # the hook agrees with the ground truth
    group = set(sim.colocated_group)
    in_group = [d for d in ongoing if d.event.sensor_id in group]
    outside = Counter(d.event.event_type for d in ongoing if d.event.sensor_id not in group)
    assert len(in_group) == 2
    assert outside == {"pressure_drop": 1, "moisture_increase": 1, "sustained_high_vibration": 1, "temperature_drift": 1}
    for detail in ongoing:
        assert not math.isnan(sim.values[detail.event.sensor_id][-1])  # an ongoing event is never hidden by an outage


def test_the_final_pressure_drop_falls_below_the_critical_limit(sim):
    drop = next(d for d in sim.ongoing_at_end() if d.event.event_type == "pressure_drop")
    crit_low = THRESHOLDS[("pressure", "water_main")][2]
    values = sim.values[drop.event.sensor_id]
    assert crit_low == 20.0
    assert np.nanmax(values[drop.start_index :]) < crit_low  # below the limit for the whole event
    assert np.nanmin(values[: drop.start_index]) > crit_low  # ... and never before
    assert 25 <= drop.end_index - drop.start_index + 1 <= 29  # amendment A6


def test_the_final_vibration_event_is_on_the_nbi_matched_highway_bridge(sim, sensors):
    group = set(sim.colocated_group)
    event = next(d for d in sim.ongoing_at_end()
                 if d.event.event_type == "sustained_high_vibration" and d.event.sensor_id not in group)  # fmt: skip
    sensor = sensors[event.event.sensor_id]
    assert TAG_NBI_BRIDGE in sensor.asset_tags and sensor.asset_type == "bridge" and sensor.placement == "bridge_deck"


def test_ongoing_events_start_early_enough_to_be_detected(sim):
    last = len(sim.timestamps) - 1
    for detail in sim.ongoing_at_end():
        lead = last - detail.start_index
        if detail.event.event_type in STEP_EVENTS:
            assert lead >= 24, (detail.event.event_type, lead)
        if detail.event.event_type in RAMP_EVENTS:
            assert lead >= 60, (detail.event.event_type, lead)


def test_only_the_designated_pressure_drop_reaches_a_critical_limit(sim, sensors):
    final_drop = next(d for d in sim.ongoing_at_end() if d.event.event_type == "pressure_drop").event.sensor_id
    for sensor_id, series in sim.values.items():
        sensor = sensors[sensor_id]
        if sensor.sensor_type != "pressure" or sensor_id == final_drop:
            continue
        assert np.nanmin(series) > 20.0, sensor_id


# --- co-located group ---------------------------------------------------------------------------------------------
def test_colocated_group_is_four_sensors_on_neighbouring_assets_within_150_m(sim, sensors):
    group = [sensors[sensor_id] for sensor_id in sim.colocated_group]
    assert len(group) == 4 and len({s.sensor_id for s in group}) == 4
    assert 3 <= len({s.asset_id for s in group}) <= 4
    for a, b in combinations(group, 2):
        assert geo.haversine(a.lon, a.lat, b.lon, b.lat) <= 150.0


def test_colocated_events_start_within_36_hours_and_overlap_the_final_five_days(sim):
    events = [d for d in sim.events if d.event.sensor_id in set(sim.colocated_group) and d.role.startswith("colocated")]
    assert len(events) == 4
    assert len({d.event.sensor_id for d in events}) == 4
    starts = [d.start_index for d in events]
    assert max(starts) - min(starts) <= 36
    last = len(sim.timestamps) - 1
    assert all(d.end_index >= last - 120 for d in events)
    assert all("co-located group" in d.event.description for d in events)
    assert sum(d.end_index == last for d in events) == 2


# --- dropouts -----------------------------------------------------------------------------------------------------
def test_two_outages_run_through_the_final_hour(sim):
    last = len(sim.timestamps) - 1
    final = {sid: gaps for sid, gaps in sim.dropouts.items() if any(end == last for _, end in gaps)}
    lengths = sorted(end - start + 1 for gaps in final.values() for start, end in gaps)
    assert lengths == [6, 30]
    assert sorted(sim.offline_at_end()) == sorted(final)
    with_events = {d.event.sensor_id for d in sim.events}
    assert not set(final) & with_events
    for sensor_id, gaps in final.items():
        start, end = gaps[0]
        assert np.isnan(sim.values[sensor_id][start : end + 1]).all()
        assert not np.isnan(sim.values[sensor_id][start - 1])


def test_dropout_rate_of_sensors_lose_two_to_ten_consecutive_hours(sim, default_settings):
    last = len(sim.timestamps) - 1
    short = {sid: gaps for sid, gaps in sim.dropouts.items() if all(end < last for _, end in gaps)}
    assert len(short) == round(default_settings.SIM_DROPOUT_RATE * len(sim.sensors))  # 12 % of the sensors
    for sensor_id, gaps in short.items():
        assert len(gaps) == 1
        start, end = gaps[0]
        assert 2 <= end - start + 1 <= 10
        assert np.isnan(sim.values[sensor_id][start : end + 1]).all()
    # a missing hour is an absent reading: nothing else is NaN
    for sensor_id, series in sim.values.items():
        missing = np.flatnonzero(np.isnan(series))
        expected = [i for start, end in sim.dropouts.get(sensor_id, []) for i in range(start, end + 1)]
        assert missing.tolist() == expected


def test_a_dropout_never_hides_a_short_event_or_the_onset_of_a_long_one(sim):
    for detail in sim.events:
        series = sim.values[detail.event.sensor_id]
        hours = detail.end_index - detail.start_index + 1
        visible_until = detail.end_index if hours <= 12 else detail.start_index + 5
        assert not np.isnan(series[detail.start_index : visible_until + 1]).any(), detail.event


def test_dropout_rate_zero_leaves_only_the_two_final_outages(default_plan, settings_factory):
    result = simulator.simulate(default_plan.sensors, settings_factory(SIM_DROPOUT_RATE=0.0))
    assert len(result.dropouts) == 2 and len(result.offline_at_end()) == 2


# --- sigma rule ---------------------------------------------------------------------------------------------------
def test_the_simulator_reports_no_violation_of_the_sigma_rule(sim):
    assert sim.sigma_rule_violations() == []


def test_injected_events_are_large_and_benign_events_small_in_noise_sigma(sim):
    for detail in sim.events:
        if detail.event.event_type in SHORT_EVENTS:
            assert detail.short and detail.peak_sigma >= 8.0, (detail.event.event_type, detail.peak_sigma)
        else:
            assert not detail.short and detail.sustained_hours(4.0) >= 6.0, detail.event.event_type
    assert len(sim.benign_bursts) > 100  # about six per vibration sensor and month
    assert max(d.peak_sigma for d in sim.benign_bursts) <= 3.5
    assert len(sim.regional_events) == len(sim.regional_sigma) == 4
    assert max(size for sizes in sim.regional_sigma for size in sizes.values()) <= 3.5


def test_work_sigma_is_the_noise_scale_floored_at_the_detector_floor(sim, sensors):
    floors = {"temperature": 0.5, "vibration": 0.10, "moisture": 0.5, "pressure": 0.5}
    class_size = Counter((s.sensor_type, s.placement) for s in sim.sensors)
    for sensor_id, sigma in sim.work_sigma.items():
        sensor = sensors[sensor_id]
        floor = floors[sensor.sensor_type]
        if sensor.sensor_type == "temperature" and class_size[(sensor.sensor_type, sensor.placement)] < 4:
            floor = 1.5
        assert sigma == pytest.approx(max(sim.noise_sigma[sensor_id], floor))
    assert {round(sim.noise_sigma[s.sensor_id], 6) for s in sim.sensors if s.sensor_type == "vibration"} == {0.15}
    assert {round(sim.noise_sigma[s.sensor_id], 6) for s in sim.sensors if s.sensor_type == "moisture"} == {0.15}
    temperature = [sim.noise_sigma[s.sensor_id] for s in sim.sensors if s.sensor_type == "temperature"]
    assert min(temperature) >= 0.3 and max(temperature) <= 0.6


def robust_sigma(residual: np.ndarray) -> float:
    residual = residual[np.isfinite(residual)]
    return float(1.4826 * np.median(np.abs(residual - np.median(residual))))


def quiet_mask(sim, sensor_id: str, margin: int = 3) -> np.ndarray:
    """Time steps of a sensor away from its injected events and benign bursts."""
    keep = np.ones(len(sim.timestamps), dtype=bool)
    for detail in [*sim.events, *sim.benign_bursts]:
        if detail.event.sensor_id == sensor_id:
            keep[max(detail.start_index - margin, 0) : detail.end_index + margin + 1] = False
    return keep


def test_spot_re_estimate_of_the_vibration_noise_and_event_sizes(sim, sensors):
    """Independent check: estimate each sensor's ln-noise from its readings and size its events against it."""
    hours = local_hours(sim)
    weekend = np.array([ts.astimezone(LOCAL).weekday() >= 5 for ts in sim.timestamps])
    checked = 0
    for detail in sim.events:
        sensor = sensors[detail.event.sensor_id]
        if sensor.sensor_type != "vibration":
            continue
        log_values = np.log(sim.values[sensor.sensor_id])
        keep = quiet_mask(sim, sensor.sensor_id)
        residual = np.full(len(log_values), np.nan)
        for part in (False, True):
            for hour in range(24):
                bucket = (hours == hour) & (weekend == part)
                residual[bucket & keep] = log_values[bucket & keep] - np.nanmedian(log_values[bucket & keep])
        sigma = max(robust_sigma(residual), 0.10)
        assert sigma == pytest.approx(0.15, abs=0.035)  # the simulator's ln-noise, recovered from the data
        size = math.log(detail.event.magnitude) / sigma
        if detail.event.event_type == "vibration_spike":
            assert size >= 8.0 * 0.85, (sensor.sensor_id, size)
        else:
            assert size >= 4.0, (sensor.sensor_id, size)
        checked += 1
    assert checked == 12
    for burst in sim.benign_bursts:
        assert 1.3 <= burst.event.magnitude <= 1.5
        assert math.log(burst.event.magnitude) / 0.15 <= 3.5


def test_spot_re_estimate_of_the_pressure_noise_and_event_sizes(sim, sensors):
    hours = local_hours(sim)
    checked = 0
    for detail in sim.events:
        sensor = sensors[detail.event.sensor_id]
        if sensor.sensor_type != "pressure":
            continue
        values = sim.values[sensor.sensor_id]
        keep = quiet_mask(sim, sensor.sensor_id)
        residual = np.full(len(values), np.nan)
        for hour in range(24):
            bucket = (hours == hour) & keep
            residual[bucket] = values[bucket] - np.nanmedian(values[bucket])
        sigma = max(robust_sigma(residual), 0.5)
        assert 0.5 <= sigma <= 1.1  # white noise 0.5 psi plus the benign slow drift
        size = abs(detail.event.magnitude) / sigma
        if detail.event.event_type in ("pressure_drop", "pressure_spike"):
            assert size >= 8.0, (sensor.sensor_id, detail.event.event_type, size)
            inside = values[detail.start_index : detail.end_index + 1]
            before = np.nanmedian(values[max(detail.start_index - 24, 0) : detail.start_index])
            observed = np.nanmedian(inside) - before  # the step is really in the readings
            assert observed == pytest.approx(detail.event.magnitude, abs=4.0)
        else:  # pressure_decline: a ramp that spends at least 6 h beyond 4 sigma
            assert abs(detail.event.magnitude) >= 4.0 * sigma
            assert detail.sustained_hours(4.0) >= 6.0
        checked += 1
    assert checked == 12


# --- physical realism ---------------------------------------------------------------------------------------------
def test_values_are_physically_plausible_for_their_sensor_type(sim, sensors):
    for sensor_id, series in sim.values.items():
        sensor = sensors[sensor_id]
        low, high = PLAUSIBLE_RANGES[sensor.sensor_type]
        assert np.nanmin(series) >= low and np.nanmax(series) <= high, sensor_id  # nothing would be stored as suspect
        if sensor.sensor_type == "vibration":
            assert np.nanmin(series) > 0.0
        if sensor.sensor_type == "moisture":
            assert np.nanmin(series) >= 5.0 and np.nanmax(series) <= 60.0
        if sensor.sensor_type == "temperature":
            assert np.nanmin(series) >= -5.0 and np.nanmax(series) <= 80.0


def test_baselines_are_in_the_ranges_of_the_contract(sim, sensors):
    hours = local_hours(sim)
    for sensor_id, series in sim.values.items():
        sensor = sensors[sensor_id]
        keep = quiet_mask(sim, sensor_id, margin=0)
        typical = float(np.nanmedian(series[keep]))
        if sensor.sensor_type == "pressure":
            assert 48.0 <= typical <= 76.0, (sensor_id, typical)  # 55-75 psi operating level less the demand dips
        elif sensor.sensor_type == "moisture":
            assert 13.0 <= typical <= 34.0, (sensor_id, typical)  # 14-26 % baseline plus rain response
        elif sensor.sensor_type == "vibration":
            weekday_peak = float(np.nanmedian(series[keep & (hours == 17)]))
            low, high = {"building_structure": (0.05, 0.30), "road_pavement": (0.3, 0.8), "bridge_deck": (0.8, 2.0)}[
                sensor.placement
            ]
            assert 0.6 * low <= weekday_peak <= 1.15 * high, (sensor_id, weekday_peak)


def test_no_benign_reading_crosses_a_warning_limit(sim, sensors):
    """Weather, bursts and drift stay inside the warning limits: only injected events may cross them."""
    for sensor_id, series in sim.values.items():
        sensor = sensors[sensor_id]
        warn_low, warn_high, _, _ = THRESHOLDS[(sensor.sensor_type, sensor.placement)]
        keep = np.ones(len(series), dtype=bool)
        for detail in sim.events:
            if detail.event.sensor_id == sensor_id:
                keep[detail.start_index : detail.end_index + 1] = False
        benign = series[keep]
        if warn_high is not None:
            assert np.nanmax(benign) <= warn_high, (sensor_id, float(np.nanmax(benign)))
        if warn_low is not None:
            assert np.nanmin(benign) >= warn_low, (sensor_id, float(np.nanmin(benign)))


def test_temperature_follows_the_local_afternoon(sim, sensors):
    hours = local_hours(sim)
    for sensor in sim.sensors:
        if sensor.sensor_type != "temperature" or sensor.placement not in ("road_surface", "bridge_deck"):
            continue
        profile = profile_by_hour(sim.values[sensor.sensor_id], hours, quiet_mask(sim, sensor.sensor_id, 0))
        assert 12 <= int(np.argmax(profile)) <= 17, sensor.sensor_id  # warmest in the local afternoon
        assert 3 <= int(np.argmin(profile)) <= 8, sensor.sensor_id  # coolest around local dawn
        assert profile.max() - profile.min() >= 10.0  # a clear daily cycle (air swing plus solar gain)


def test_temperature_cools_over_the_window(sim, sensors):
    outdoor = [s.sensor_id for s in sim.sensors if s.placement in ("road_surface", "bridge_deck") and s.sensor_type == "temperature"]
    first = np.nanmean([np.nanmean(sim.values[sid][: 7 * 24]) for sid in outdoor])
    last = np.nanmean([np.nanmean(sim.values[sid][-7 * 24 :]) for sid in outdoor])
    assert first - last >= 2.0  # the simulated air mean falls from 22.5 to 16.5 deg C


def test_vibration_follows_local_traffic_hours_and_is_lower_at_weekends(sim, sensors):
    hours = local_hours(sim)
    weekend = np.array([ts.astimezone(LOCAL).weekday() >= 5 for ts in sim.timestamps])
    ratios = []
    for sensor in sim.sensors:
        if sensor.sensor_type != "vibration":
            continue
        series, keep = sim.values[sensor.sensor_id], quiet_mask(sim, sensor.sensor_id, 0)
        weekday_profile = profile_by_hour(series, hours, keep & ~weekend)
        assert 15 <= int(np.argmax(weekday_profile)) <= 18, sensor.sensor_id  # evening peak, local time
        assert 0 <= int(np.argmin(weekday_profile)) <= 4 or int(np.argmin(weekday_profile)) == 23
        assert weekday_profile.max() / weekday_profile.min() >= 2.0
        ratios.append(np.nanmedian(series[keep & weekend]) / np.nanmedian(series[keep & ~weekend]))
    assert np.median(ratios) == pytest.approx(0.7, abs=0.08)


def test_pressure_dips_with_local_morning_and_evening_demand(sim, sensors):
    hours = local_hours(sim)
    for sensor in sim.sensors:
        if sensor.sensor_type != "pressure":
            continue
        profile = profile_by_hour(sim.values[sensor.sensor_id], hours, quiet_mask(sim, sensor.sensor_id, 0))
        night = float(np.mean(profile[[1, 2, 3]]))
        assert int(np.argmin(profile)) in (6, 7, 8), sensor.sensor_id  # the morning dip is the deepest
        assert profile[7] < night - 1.5 and profile[19] < night - 1.0


def test_daily_patterns_follow_the_configured_time_zone(default_plan, settings_factory):
    """The same clock times in another zone: the afternoon peak moves with the local clock, not with UTC."""
    tokyo = ZoneInfo("Asia/Tokyo")
    result = simulator.simulate(default_plan.sensors, settings_factory(TIMEZONE="Asia/Tokyo", SIM_ANOMALY_EVENTS=0))
    hours = np.array([ts.astimezone(tokyo).hour for ts in result.timestamps])
    sensor = next(s for s in result.sensors if s.placement == "road_surface")
    assert 12 <= int(np.argmax(profile_by_hour(result.values[sensor.sensor_id], hours))) <= 17


def test_rain_raises_every_moisture_sensor_and_is_recorded_as_benign(sim, sensors):
    rain = [e for e in sim.regional_events if e.event_type == "regional_rain"]
    hot = [e for e in sim.regional_events if e.event_type == "regional_hot_spell"]
    assert len(rain) == 3 and len(hot) == 1
    assert all(not e.is_anomaly and e.sensor_id is None and e.asset_id is None for e in sim.regional_events)
    assert all(9.0 <= e.magnitude <= 26.0 for e in rain)
    index = {ts: i for i, ts in enumerate(sim.timestamps)}
    with_events = {d.event.sensor_id for d in sim.events}
    rose = total = 0
    for event in rain:
        start = index[event.started_at]
        for sensor in sim.sensors:
            if sensor.sensor_type != "moisture" or sensor.sensor_id in with_events:
                continue
            series = sim.values[sensor.sensor_id]
            before = np.nanmedian(series[start - 12 : start])
            after = np.nanmax(series[start : start + 12])
            if np.isfinite(before) and np.isfinite(after):
                total += 1
                rose += after - before >= 1.0
    assert total > 30 and rose == total  # +1.5 .. +7 % at every moisture sensor
    assert hot[0].magnitude == 7.0 and hot[0].ended_at - hot[0].started_at == timedelta(hours=35)


def test_ground_truth_lists_injected_events_then_benign_regional_events(sim):
    truth = sim.ground_truth()
    assert len(truth) == 44
    assert [e.is_anomaly for e in truth] == [True] * 40 + [False] * 4
    assert all(e.sensor_id and e.asset_id and e.sensor_type for e in truth[:40])
    assert all(e.started_at <= e.ended_at for e in truth)
    assert not any(e.event_type == "vibration_burst" for e in truth)  # local benign bursts are not stored


def test_unknown_sensor_classes_and_duplicate_ids_are_rejected(default_plan, default_settings):
    from dataclasses import replace

    spec = default_plan.sensors[0]
    with pytest.raises(ValueError, match="unique"):
        simulator.simulate([spec, spec], default_settings)
    with pytest.raises(KeyError):
        simulator.simulate([replace(spec, placement="no_such_placement")], default_settings)
