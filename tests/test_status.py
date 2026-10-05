"""Time and status semantics (build contract section 10.1): the pure rules. No database.

The same rules evaluated in SQL against stored data are checked in the ``db`` modules
(``test_api_parity``, ``test_api_time``, ``test_pipeline_db``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from pipeline.analysis import status
from pipeline.analysis.status import TimeAxis

START = datetime(2026, 9, 1, 5, tzinfo=UTC)
END = datetime(2026, 10, 1, 4, tzinfo=UTC)
AXIS = TimeAxis(START, END, timedelta(hours=1), run_id=1)


# --- time axis and as_of ------------------------------------------------------------------------------------------
def test_axis_is_the_hourly_grid_from_the_first_to_the_last_reading():
    assert AXIS.count == 720 and AXIS.step_seconds == 3600 and AXIS.step_hours == 1.0
    stamps = AXIS.timestamps()
    assert stamps[0] == START and stamps[-1] == END and len(stamps) == 720
    assert AXIS.at(1) == START + timedelta(hours=1)
    np.testing.assert_array_equal(np.diff(AXIS.epochs()), 3600.0)


def test_as_of_defaults_to_t_end():
    assert AXIS.resolve(None) == END
    assert AXIS.index_of(None) == 719


@pytest.mark.parametrize(
    ("given", "effective"),
    [
        (datetime(2026, 9, 15, 12, 0, tzinfo=UTC), datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
        (datetime(2026, 9, 15, 12, 59, 59, tzinfo=UTC), datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),  # floored
        (datetime(2026, 9, 15, 12, 0, 1, tzinfo=UTC), datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
        (datetime(2026, 9, 15, 12, 30), datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),  # naive = UTC
        (datetime(2026, 9, 15, 7, 30, tzinfo=timezone(timedelta(hours=-5))), datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
        (datetime(2020, 1, 1, tzinfo=UTC), START),  # clamped to the start
        (START - timedelta(seconds=1), START),
        (datetime(2030, 1, 1, tzinfo=UTC), END),  # clamped to T_end
        (END + timedelta(minutes=59), END),
        (END, END),
        (START, START),
    ],
)
def test_as_of_is_floored_to_the_step_and_clamped_to_the_window(given, effective):
    assert AXIS.resolve(given) == effective
    assert status.resolve_as_of(AXIS, given) == effective
    assert AXIS.at(AXIS.index_of(given)) == effective
    assert AXIS.resolve(given).utcoffset() == timedelta(0)


@pytest.mark.parametrize("before_the_hour", [timedelta(microseconds=1), timedelta(milliseconds=1), timedelta(milliseconds=3), timedelta(seconds=1)])
def test_as_of_just_before_a_grid_point_belongs_to_the_previous_step(before_the_hour):
    """The floor is exact: 12:59:59.999 is still the 12:00 step (no rounding tolerance towards the next hour)."""
    grid_point = START + timedelta(hours=463)
    assert AXIS.index_of(grid_point) == 463
    assert AXIS.index_of(grid_point - before_the_hour) == 462
    assert AXIS.resolve(grid_point - before_the_hour) == grid_point - timedelta(hours=1)
    assert AXIS.index_of(START - before_the_hour) == 0 and AXIS.index_of(END + before_the_hour) == 719  # clamped


def test_axis_from_bounds_ends_on_the_last_grid_point_at_or_before_the_last_reading():
    axis = status.axis_from_bounds(START, START + timedelta(hours=10, minutes=20), timedelta(hours=1))
    assert (axis.start, axis.end, axis.count) == (START, START + timedelta(hours=10), 11)
    single = status.axis_from_bounds(START, START, timedelta(hours=1))
    assert single.count == 1 and single.resolve(None) == START
    with pytest.raises(ValueError):
        status.axis_from_bounds(START, END, timedelta(0))


def test_a_reading_belongs_to_the_first_grid_point_at_or_after_it():
    """Offline rule: a reading is current at t when its timestamp lies in (t - step, t]."""
    epochs = np.array([START.timestamp() + seconds for seconds in (0, 1, 3599, 3600, 3601, 7200, -1, 720 * 3600)])
    assert AXIS.cell_indices(epochs).tolist() == [0, 1, 1, 1, 2, 2, 0, 720]


# --- anomaly status -----------------------------------------------------------------------------------------------
def test_anomaly_is_active_from_started_at_to_ended_at_inclusive():
    started, ended = START + timedelta(hours=10), START + timedelta(hours=20)
    at = lambda hour: status.anomaly_status_at(started, ended, START + timedelta(hours=hour))  # noqa: E731
    assert at(9) is None  # not yet visible
    assert at(10) == "active" and at(15) == "active" and at(20) == "active"
    assert at(21) == "resolved" and at(500) == "resolved"


def test_active_matrix_is_the_same_rule_over_the_whole_axis():
    started = np.array([10.0, 30.0]) * 3600
    ended = np.array([20.0, 30.0]) * 3600
    grid = np.array([9, 10, 20, 21, 30, 31]) * 3600.0
    assert status.active_matrix(started, ended, grid).tolist() == [
        [False, True, True, False, False, False],
        [False, False, False, False, True, False],
    ]


def test_sql_fragments_state_the_same_rule():
    assert status.ANOMALY_VISIBLE_SQL == "an.started_at <= %(as_of)s::timestamptz"
    assert "an.started_at <= %(as_of)s::timestamptz AND an.ended_at >= %(as_of)s::timestamptz" in status.ANOMALY_ACTIVE_SQL
    assert status.ANOMALY_STATUS_SQL == "CASE WHEN an.ended_at >= %(as_of)s::timestamptz THEN 'active' ELSE 'resolved' END"


# --- sensor status ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reporting", [True, False])
@pytest.mark.parametrize("anomaly", [True, False])
@pytest.mark.parametrize("flagged", [True, False])
@pytest.mark.parametrize("outside_warn", [True, False])
def test_sensor_status_precedence_offline_anomaly_warning_normal(reporting, anomaly, flagged, outside_warn):
    code = status.status_codes(np.array([[reporting]]), np.array([[anomaly]]), np.array([[flagged]]), np.array([[outside_warn]]))[0, 0]
    if not reporting:
        expected = "offline"
    elif anomaly:
        expected = "anomaly"
    elif flagged or outside_warn:
        expected = "warning"
    else:
        expected = "normal"
    assert status.SENSOR_STATUSES[code] == expected


def test_status_characters_are_n_w_a_o():
    assert status.SENSOR_STATUS_CHARS == "nwao"
    assert status.SENSOR_STATUSES == ("normal", "warning", "anomaly", "offline")
    assert status.status_string(np.array([0, 1, 2, 3, 0], dtype=np.uint8)) == "nwaon"


def test_a_reading_is_current_for_one_sampling_interval():
    has_reading = np.array([[True, False, False, True, True, False]])
    hourly = status.current_cells(has_reading, np.array([1]))
    assert hourly.tolist() == [[0, -1, -1, 3, 4, -1]]  # offline as soon as an hour has no reading
    three_hourly = status.current_cells(has_reading, np.array([3]))
    assert three_hourly.tolist() == [[0, 0, 0, 3, 4, 4]]  # a 3-hourly sensor stays current for three steps
    never = status.current_cells(np.zeros((1, 4), dtype=bool), np.array([1]))
    assert never.tolist() == [[-1, -1, -1, -1]]


def test_sensor_activity_marks_the_sensor_of_each_active_anomaly():
    anomalies = status.AnomalyIntervals(
        anomaly_ids=["ANM-0001", "ANM-0002", "ANM-0003"],
        sensor_ids=["S2", "S2", "S9"],
        asset_ids=["A", "A", "Z"],
        severities=["low", "high", "low"],
        started=np.array([1.0, 4.0, 0.0]) * 3600,
        ended=np.array([2.0, 4.0, 5.0]) * 3600,
    )
    grid = np.arange(6) * 3600.0
    activity = status.sensor_activity(["S1", "S2"], anomalies, grid)
    assert activity.tolist() == [[False] * 6, [False, True, True, False, True, False]]
    assert len(anomalies) == 3


# --- recency weights (shared by health and risk) ------------------------------------------------------------------
def test_recency_weight_is_one_while_active_then_halves_every_half_life_up_to_the_maximum_age():
    started = np.array([10.0]) * 3600
    ended = np.array([20.0]) * 3600
    grid = np.array([9, 10, 20, 44, 68, 20 + 168, 20 + 169]) * 3600.0
    weights = status.recency_weights(started, ended, grid, half_life_hours=48.0, max_age_hours=168.0)[0]
    assert weights.tolist() == pytest.approx([0.0, 1.0, 1.0, 0.5**0.5, 0.5, 0.5**3.5, 0.0])


# --- KPI definitions ----------------------------------------------------------------------------------------------
def test_kpi_series_keys_and_thresholds():
    assert status.KPI_SERIES_KEYS == (
        "active_sensors", "offline_sensors", "warning_sensors", "active_anomalies", "critical_alerts", "assets_at_risk",
    )  # fmt: skip
    assert status.AT_RISK_BELOW == 70 and status.CRITICAL_SEVERITY == "critical"


def test_kpi_sql_counts_what_the_contract_defines():
    sql = " ".join(status.KPI_SQL.split())
    assert "count(*) FROM infra.infrastructure_assets) AS total_assets" in sql
    assert "WHERE NOT is_simulated) AS real_assets" in sql and "WHERE is_simulated) AS simulated_assets" in sql
    assert "count(DISTINCT asset_id) FROM infra.sensors) AS monitored_assets" in sql
    assert "WHERE status <> 'offline') AS active_sensors" in sql  # active = total - offline
    assert "WHERE status = 'offline') AS offline_sensors" in sql
    assert "WHERE status = 'warning') AS warning_sensors" in sql
    assert "h.health_score < %(at_risk_below)s) AS assets_at_risk" in sql
    offline = " ".join(status.SENSOR_STATUS_SQL.split())
    # no reading with ts in (t - sampling interval, t]
    assert "sr.ts <= %(as_of)s::timestamptz AND sr.ts > %(as_of)s::timestamptz - make_interval(secs => s.sampling_interval_s)" in offline
    assert offline.index("'offline'") < offline.index("'anomaly'") < offline.index("'warning'") < offline.index("'normal'")
