"""Time axis, ``as_of`` handling, sensor status and KPIs: the single implementation (build contract 10.1).

Used by the detection run (grid of the analysed window), the health and risk computations, and the API
(``/sensors``, ``/statistics``, ``/playback``). The same rules exist in two forms that must agree:

* vectorised over the whole time axis (``load_sensor_matrix``, ``kpi_series``) - playback and asset health;
* for a single ``as_of`` in SQL (``SENSOR_STATUS_SQL``, ``sensor_status_at``, ``kpis_at``).

Rules
-----
* Time axis: a regular grid from the first to the last reading timestamp of the latest detection run,
  anchored at the first reading. ``T_end`` is its last point. ``as_of`` defaults to ``T_end``; a given value
  is floored to the step and clamped to the axis. Naive datetimes are UTC.
* Anomaly: active at t when ``started_at <= t <= ended_at``; resolved when ``ended_at < t``; not yet visible
  when ``started_at > t``.
* Sensor status at t, first match wins: ``offline`` (no reading with ts in (t - sampling interval, t]),
  ``anomaly`` (an anomaly of the sensor is active at t), ``warning`` (the current reading is flagged or lies
  outside the warning limits), else ``normal``.
* KPIs at t: offline / active / warning sensors, active anomalies, critical alerts (active anomalies of
  severity critical), assets at risk (monitored assets with a health score below 70 at t).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import psycopg
from numpy.typing import NDArray
from psycopg.rows import dict_row

from pipeline.detection.baseline import BoolArray, FloatArray

STATUS_NORMAL = "normal"
STATUS_WARNING = "warning"
STATUS_ANOMALY = "anomaly"
STATUS_OFFLINE = "offline"
SENSOR_STATUSES: tuple[str, ...] = (STATUS_NORMAL, STATUS_WARNING, STATUS_ANOMALY, STATUS_OFFLINE)
# One character per time step in the playback bundle; the position in this string is the status code.
SENSOR_STATUS_CHARS = "nwao"
CODE_NORMAL, CODE_WARNING, CODE_ANOMALY, CODE_OFFLINE = range(4)

ANOMALY_ACTIVE = "active"
ANOMALY_RESOLVED = "resolved"
# The anomaly rules as SQL, for statements that alias infra.anomalies as "an" and bind %(as_of)s.
ANOMALY_VISIBLE_SQL = "an.started_at <= %(as_of)s::timestamptz"
ANOMALY_ACTIVE_SQL = "(an.started_at <= %(as_of)s::timestamptz AND an.ended_at >= %(as_of)s::timestamptz)"
ANOMALY_STATUS_SQL = "CASE WHEN an.ended_at >= %(as_of)s::timestamptz THEN 'active' ELSE 'resolved' END"

AT_RISK_BELOW = 70  # "Assets at Risk" = monitored assets with a health score below this value
CRITICAL_SEVERITY = "critical"
DEFAULT_STEP_SECONDS = 3600
GRID_TOLERANCE = 1e-6  # steps; absorbs floating-point noise when timestamps are mapped to the grid
KPI_SERIES_KEYS: tuple[str, ...] = (
    "active_sensors", "offline_sensors", "warning_sensors", "active_anomalies", "critical_alerts", "assets_at_risk",
)  # fmt: skip


class NoDetectionRunError(RuntimeError):
    """The database holds no finished detection run (stage 5 has not run)."""


# --- time axis --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TimeAxis:
    """Regular grid of the analysed window: ``start``, ``start + step``, ..., ``end`` (all aware, UTC)."""

    start: datetime
    end: datetime
    step: timedelta
    run_id: int | None = None

    @property
    def step_seconds(self) -> float:
        """Length of one step in seconds."""
        return self.step.total_seconds()

    @property
    def step_hours(self) -> float:
        """Length of one step in hours."""
        return self.step_seconds / 3600.0

    @property
    def count(self) -> int:
        """Number of points on the axis."""
        return int(round((self.end - self.start) / self.step)) + 1

    def at(self, index: int) -> datetime:
        """Timestamp of a grid point."""
        return self.start + index * self.step

    def timestamps(self) -> list[datetime]:
        """Every grid point, first to last."""
        return [self.at(i) for i in range(self.count)]

    def epochs(self) -> FloatArray:
        """Every grid point as seconds since the Unix epoch."""
        return self.start.timestamp() + np.arange(self.count) * self.step_seconds

    def resolve(self, value: datetime | None) -> datetime:
        """The effective ``as_of``: default ``end``; floored to the step; clamped to the axis; naive = UTC."""
        return self.at(self.index_of(value))

    def index_of(self, value: datetime | None) -> int:
        """Index of the effective ``as_of`` (see ``resolve``)."""
        if value is None:
            return self.count - 1
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        # Exact floor in whole microseconds (timedelta // timedelta): 12:59:59.999 belongs to the 12:00 step.
        steps = (moment - self.start) // self.step
        return min(max(steps, 0), self.count - 1)

    def cell_indices(self, epochs: FloatArray) -> NDArray[np.int64]:
        """Grid index a reading belongs to: the first grid point at or after its timestamp.

        A reading is "current" at grid time t when its timestamp lies in (t - step, t], which is the offline
        rule of the status semantics. Indices outside 0..count-1 are readings outside the axis.
        """
        position = (np.asarray(epochs, dtype=float) - self.start.timestamp()) / self.step_seconds
        return np.ceil(position - GRID_TOLERANCE).astype(np.int64)


def axis_from_bounds(first: datetime, last: datetime, step: timedelta, run_id: int | None = None) -> TimeAxis:
    """Axis anchored at the first reading; its end is the last grid point at or before the last reading."""
    if step <= timedelta(0):
        raise ValueError("the time step must be positive")
    first, last = first.astimezone(UTC), last.astimezone(UTC)
    steps = max(math.floor((last - first) / step + GRID_TOLERANCE), 0)
    return TimeAxis(start=first, end=first + steps * step, step=step, run_id=run_id)


def sampling_step(conn: psycopg.Connection) -> timedelta:
    """Step of the grid: the most common sampling interval of the sensors (the shortest one on a tie)."""
    rows = conn.execute("SELECT sampling_interval_s FROM infra.sensors").fetchall()
    if not rows:
        return timedelta(seconds=DEFAULT_STEP_SECONDS)
    counts = Counter(int(row[0]) for row in rows)
    seconds = min(counts, key=lambda value: (-counts[value], value))
    return timedelta(seconds=seconds)


def load_time_axis(conn: psycopg.Connection) -> TimeAxis:
    """Time axis of the latest finished detection run; ``NoDetectionRunError`` when there is none."""
    row = conn.execute(
        """
        SELECT run_id, window_start, window_end, params
        FROM infra.detection_runs
        WHERE finished_at IS NOT NULL AND window_start IS NOT NULL AND window_end IS NOT NULL
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise NoDetectionRunError("no finished detection run in the database; run scripts/detect_anomalies.py first")
    run_id, start, end, params = row
    seconds = (params or {}).get("step_seconds")
    step = timedelta(seconds=float(seconds)) if seconds else sampling_step(conn)
    return axis_from_bounds(start, end, step, run_id=int(run_id))


def resolve_as_of(axis: TimeAxis, value: datetime | None) -> datetime:
    """The effective ``as_of`` for a request value (None = ``T_end``); every response echoes this value."""
    return axis.resolve(value)


# --- pure status rules ------------------------------------------------------------------------------------------
def current_cells(has_reading: BoolArray, window_steps: NDArray[np.int64]) -> NDArray[np.int64]:
    """Index of the grid cell holding each sensor's current reading at every time step, -1 when offline.

    ``has_reading`` is (sensors x steps): the cell holds a reading. ``window_steps`` is the sampling
    interval of every sensor in steps; a reading stays current for that many steps.
    """
    steps = has_reading.shape[1]
    position = np.arange(steps)
    latest = np.maximum.accumulate(np.where(has_reading, position, -1), axis=1)
    fresh = (latest >= 0) & (position - latest < np.maximum(window_steps, 1)[:, None])
    return np.where(fresh, latest, -1)


def active_matrix(started: FloatArray, ended: FloatArray, grid: FloatArray) -> BoolArray:
    """(intervals x steps): the interval [started, ended] contains the grid time (all in epoch seconds)."""
    return (started[:, None] <= grid[None, :]) & (grid[None, :] <= ended[:, None])


def status_codes(
    reporting: BoolArray, anomaly_active: BoolArray, flagged: BoolArray, outside_warn: BoolArray
) -> NDArray[np.uint8]:
    """Sensor status code per (sensor, step): offline > anomaly > warning > normal."""
    codes = np.full(reporting.shape, CODE_NORMAL, dtype=np.uint8)
    codes[flagged | outside_warn] = CODE_WARNING
    codes[anomaly_active] = CODE_ANOMALY
    codes[~reporting] = CODE_OFFLINE
    return codes


def anomaly_status_at(started_at: datetime, ended_at: datetime, as_of: datetime) -> str | None:
    """Status of an anomaly at ``as_of``: 'active', 'resolved', or None while it is not yet visible."""
    if started_at > as_of:
        return None
    return ANOMALY_ACTIVE if as_of <= ended_at else ANOMALY_RESOLVED


def status_string(codes: NDArray[np.uint8]) -> str:
    """One status character per time step (``n`` normal, ``w`` warning, ``a`` anomaly, ``o`` offline)."""
    return "".join(SENSOR_STATUS_CHARS[code] for code in codes)


def recency_weights(
    started: FloatArray, ended: FloatArray, grid: FloatArray, half_life_hours: float, max_age_hours: float
) -> FloatArray:
    """Weight of every anomaly at every grid time (anomalies x steps).

    1 while the anomaly is active; ``0.5 ** (hours since it ended / half_life_hours)`` afterwards, for at
    most ``max_age_hours``; 0 before it started and after that age.
    """
    age_hours = (grid[None, :] - ended[:, None]) / 3600.0
    with np.errstate(over="ignore"):
        decayed = np.where(age_hours <= max_age_hours, 0.5 ** (np.maximum(age_hours, 0.0) / half_life_hours), 0.0)
    weights = np.where(age_hours > 0, decayed, 1.0)
    return np.where(started[:, None] <= grid[None, :], weights, 0.0)


# --- vectorised over the whole time axis -------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AnomalyIntervals:
    """The anomalies of the latest run as arrays (ordered by anomaly id); times are epoch seconds."""

    anomaly_ids: list[str]
    sensor_ids: list[str]
    asset_ids: list[str]
    severities: list[str]
    started: FloatArray
    ended: FloatArray

    def __len__(self) -> int:
        return len(self.anomaly_ids)


@dataclass(frozen=True, slots=True)
class SensorMatrix:
    """State of every sensor at every time step (rows ordered by sensor id)."""

    axis: TimeAxis
    sensor_ids: list[str]
    asset_ids: list[str]
    sensor_types: list[str]
    values: FloatArray  # current reading, NaN when offline
    robust_z: FloatArray  # robust z of the reading stored in each grid cell, NaN where the cell is empty
    reporting: BoolArray  # a reading within the sampling interval
    flagged: BoolArray  # the current reading is flagged
    outside_warn: BoolArray  # the current reading lies outside the warning limits
    anomaly_active: BoolArray  # an anomaly of the sensor is active
    status: NDArray[np.uint8]  # CODE_* per (sensor, step)

    def status_strings(self) -> dict[str, str]:
        """Sensor id -> one status character per time step."""
        return {sensor_id: status_string(self.status[i]) for i, sensor_id in enumerate(self.sensor_ids)}


def load_anomaly_intervals(conn: psycopg.Connection) -> AnomalyIntervals:
    """Every stored anomaly with its interval."""
    rows = conn.execute(
        """
        SELECT anomaly_id, sensor_id, asset_id, severity,
               extract(epoch FROM started_at)::float8, extract(epoch FROM ended_at)::float8
        FROM infra.anomalies
        ORDER BY anomaly_id
        """
    ).fetchall()
    return AnomalyIntervals(
        anomaly_ids=[row[0] for row in rows],
        sensor_ids=[row[1] for row in rows],
        asset_ids=[row[2] for row in rows],
        severities=[row[3] for row in rows],
        started=np.array([row[4] for row in rows], dtype=float),
        ended=np.array([row[5] for row in rows], dtype=float),
    )


# One row per sensor with its readings of the window as arrays (time order), joined to their scores.
SENSOR_SERIES_SQL = """
SELECT s.sensor_id, s.asset_id, s.sensor_type, s.sampling_interval_s, th.warn_low, th.warn_high,
       r.epochs, r.reading_values, r.zs, r.flags
FROM infra.sensors s
JOIN infra.sensor_thresholds th ON th.sensor_type = s.sensor_type AND th.placement = s.placement
LEFT JOIN LATERAL (
    SELECT array_agg(extract(epoch FROM sr.ts)::float8 ORDER BY sr.ts) AS epochs,
           array_agg(sr.value ORDER BY sr.ts) AS reading_values,
           array_agg(sc.robust_z::float8 ORDER BY sr.ts) AS zs,
           array_agg(COALESCE(sc.flagged, false) ORDER BY sr.ts) AS flags
    FROM infra.sensor_readings sr
    LEFT JOIN infra.reading_scores sc ON sc.sensor_id = sr.sensor_id AND sc.ts = sr.ts
    WHERE sr.sensor_id = s.sensor_id AND sr.ts >= %(start)s AND sr.ts <= %(end)s
) r ON true
ORDER BY s.sensor_id
"""


def sensor_activity(sensor_ids: list[str], anomalies: AnomalyIntervals, grid: FloatArray) -> BoolArray:
    """(sensors x steps): an anomaly of the sensor is active at the grid time."""
    position = {sensor_id: i for i, sensor_id in enumerate(sensor_ids)}
    activity = np.zeros((len(sensor_ids), len(grid)), dtype=bool)
    active = active_matrix(anomalies.started, anomalies.ended, grid)
    for j, sensor_id in enumerate(anomalies.sensor_ids):
        if sensor_id in position:
            activity[position[sensor_id]] |= active[j]
    return activity


def load_sensor_matrix(
    conn: psycopg.Connection, axis: TimeAxis, anomalies: AnomalyIntervals | None = None
) -> SensorMatrix:
    """Read sensors, readings, scores and anomalies and evaluate the status rules at every time step."""
    anomalies = anomalies if anomalies is not None else load_anomaly_intervals(conn)
    rows = conn.execute(SENSOR_SERIES_SQL, {"start": axis.start, "end": axis.end}).fetchall()
    sensors, steps = len(rows), axis.count
    cell_values = np.full((sensors, steps), np.nan)
    cell_z = np.full((sensors, steps), np.nan)
    cell_flagged = np.zeros((sensors, steps), dtype=bool)
    has_reading = np.zeros((sensors, steps), dtype=bool)
    outside = np.zeros((sensors, steps), dtype=bool)
    window_steps = np.ones(sensors, dtype=np.int64)
    for i, (_, _, _, interval_s, warn_low, warn_high, epochs, reading_values, zs, flags) in enumerate(rows):
        window_steps[i] = max(int(round(interval_s / axis.step_seconds)), 1)
        if not epochs:
            continue
        cells = axis.cell_indices(np.array(epochs, dtype=float))
        inside = (cells >= 0) & (cells < steps)
        cells = cells[inside]  # ascending in time: a later reading in the same cell replaces the earlier one
        cell_values[i, cells] = np.array(reading_values, dtype=float)[inside]
        cell_z[i, cells] = np.array([np.nan if z is None else z for z in zs], dtype=float)[inside]
        cell_flagged[i, cells] = np.array(flags, dtype=bool)[inside]
        has_reading[i, cells] = True
        with np.errstate(invalid="ignore"):
            if warn_low is not None:
                outside[i] |= cell_values[i] < warn_low
            if warn_high is not None:
                outside[i] |= cell_values[i] > warn_high

    current = current_cells(has_reading, window_steps)
    reporting = current >= 0
    pick = np.maximum(current, 0)
    values = np.where(reporting, np.take_along_axis(cell_values, pick, axis=1), np.nan)
    flagged = reporting & np.take_along_axis(cell_flagged, pick, axis=1)
    outside_warn = reporting & np.take_along_axis(outside, pick, axis=1)
    sensor_ids = [row[0] for row in rows]
    anomaly_active = sensor_activity(sensor_ids, anomalies, axis.epochs())
    return SensorMatrix(
        axis=axis,
        sensor_ids=sensor_ids,
        asset_ids=[row[1] for row in rows],
        sensor_types=[row[2] for row in rows],
        values=values,
        robust_z=cell_z,
        reporting=reporting,
        flagged=flagged,
        outside_warn=outside_warn,
        anomaly_active=anomaly_active,
        status=status_codes(reporting, anomaly_active, flagged, outside_warn),
    )


def assets_at_risk_series(conn: psycopg.Connection, axis: TimeAxis) -> NDArray[np.int64]:
    """Number of monitored assets with a health score below ``AT_RISK_BELOW`` at every time step."""
    counts = np.zeros(axis.count, dtype=np.int64)
    rows = conn.execute(
        """
        SELECT extract(epoch FROM as_of)::float8, count(*)
        FROM infra.asset_health
        WHERE health_score < %s AND as_of >= %s AND as_of <= %s
        GROUP BY as_of
        """,
        (AT_RISK_BELOW, axis.start, axis.end),
    ).fetchall()
    if rows:
        cells = axis.cell_indices(np.array([row[0] for row in rows], dtype=float))
        counts[cells] = np.array([row[1] for row in rows], dtype=np.int64)
    return counts


def kpi_series(conn: psycopg.Connection, axis: TimeAxis, matrix: SensorMatrix | None = None) -> dict[str, list[int]]:
    """The time-dependent KPIs at every time step; element i equals ``kpis_at(conn, axis.at(i))``."""
    anomalies = load_anomaly_intervals(conn)
    matrix = matrix if matrix is not None else load_sensor_matrix(conn, axis, anomalies)
    active = active_matrix(anomalies.started, anomalies.ended, axis.epochs())
    critical = np.array([severity == CRITICAL_SEVERITY for severity in anomalies.severities], dtype=bool)
    offline = (matrix.status == CODE_OFFLINE).sum(axis=0)
    series = {
        "active_sensors": len(matrix.sensor_ids) - offline,
        "offline_sensors": offline,
        "warning_sensors": (matrix.status == CODE_WARNING).sum(axis=0),
        "active_anomalies": active.sum(axis=0),
        "critical_alerts": active[critical].sum(axis=0),
        "assets_at_risk": assets_at_risk_series(conn, axis),
    }
    return {key: [int(value) for value in series[key]] for key in KPI_SERIES_KEYS}


# --- single as_of (SQL) -----------------------------------------------------------------------------------------
# One row per sensor: its status at %(as_of)s and the reading that is current at that time (NULL when offline).
SENSOR_STATUS_SQL = f"""
SELECT s.sensor_id,
       s.asset_id,
       s.sensor_type,
       CASE
           WHEN r.ts IS NULL THEN 'offline'
           WHEN EXISTS (
               SELECT 1 FROM infra.anomalies an WHERE an.sensor_id = s.sensor_id AND {ANOMALY_ACTIVE_SQL}
           ) THEN 'anomaly'
           WHEN COALESCE(sc.flagged, false) OR r.value < th.warn_low OR r.value > th.warn_high THEN 'warning'
           ELSE 'normal'
       END AS status,
       r.ts AS reading_ts,
       r.value AS reading_value,
       r.status AS reading_status,
       sc.expected,
       sc.expected_low,
       sc.expected_high,
       sc.robust_z,
       COALESCE(sc.flagged, false) AS flagged
FROM infra.sensors s
JOIN infra.sensor_thresholds th ON th.sensor_type = s.sensor_type AND th.placement = s.placement
LEFT JOIN LATERAL (
    SELECT sr.ts, sr.value, sr.status
    FROM infra.sensor_readings sr
    WHERE sr.sensor_id = s.sensor_id
      AND sr.ts <= %(as_of)s::timestamptz
      AND sr.ts > %(as_of)s::timestamptz - make_interval(secs => s.sampling_interval_s)
    ORDER BY sr.ts DESC
    LIMIT 1
) r ON true
LEFT JOIN infra.reading_scores sc ON sc.sensor_id = s.sensor_id AND sc.ts = r.ts
"""

KPI_SQL = f"""
WITH sensor_status AS ({SENSOR_STATUS_SQL}),
active AS (
    SELECT an.severity
    FROM infra.anomalies an
    WHERE {ANOMALY_ACTIVE_SQL}
)
SELECT (SELECT count(*) FROM infra.infrastructure_assets) AS total_assets,
       (SELECT count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated) AS real_assets,
       (SELECT count(*) FROM infra.infrastructure_assets WHERE is_simulated) AS simulated_assets,
       (SELECT count(DISTINCT asset_id) FROM infra.sensors) AS monitored_assets,
       (SELECT count(*) FROM sensor_status) AS total_sensors,
       (SELECT count(*) FROM sensor_status WHERE status <> 'offline') AS active_sensors,
       (SELECT count(*) FROM sensor_status WHERE status = 'offline') AS offline_sensors,
       (SELECT count(*) FROM sensor_status WHERE status = 'warning') AS warning_sensors,
       (SELECT count(*) FROM active) AS active_anomalies,
       (SELECT count(*) FROM active WHERE severity = %(critical)s) AS critical_alerts,
       (SELECT count(*) FROM infra.asset_health h
        WHERE h.as_of = %(as_of)s::timestamptz AND h.health_score < %(at_risk_below)s) AS assets_at_risk
"""


def sensor_status_at(conn: psycopg.Connection, as_of: datetime) -> list[dict[str, Any]]:
    """Status and current reading of every sensor at ``as_of`` (an effective, resolved timestamp)."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(SENSOR_STATUS_SQL + " ORDER BY s.sensor_id", {"as_of": as_of}).fetchall()


def kpis_at(conn: psycopg.Connection, as_of: datetime) -> dict[str, int]:
    """The KPI values at ``as_of`` (an effective, resolved timestamp)."""
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            KPI_SQL, {"as_of": as_of, "critical": CRITICAL_SEVERITY, "at_risk_below": AT_RISK_BELOW}
        ).fetchone()
    return {key: int(value) for key, value in row.items()}
