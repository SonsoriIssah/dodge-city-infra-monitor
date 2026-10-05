"""Detection run: database in, database out, one transaction (build contract section 8 and 4.1 row 5).

Reads ``infra.sensors``, ``infra.sensor_thresholds`` and ``infra.sensor_readings`` (and
``infra.simulation_events`` for the evaluation only), and writes ``infra.detection_runs`` (parameters and
metrics), ``infra.reading_scores`` (one row per analysed reading) and ``infra.anomalies`` (numbered
``ANM-0001``... by start time, then sensor id). Re-running replaces the previous run.

``detect`` is the database-free part: readings on a grid in, scores and anomaly records out.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import psycopg
from psycopg import sql

from pipeline.analysis.status import ANOMALY_ACTIVE, TimeAxis, anomaly_status_at, axis_from_bounds, sampling_step
from pipeline.config import Settings
from pipeline.db.loaders import copy_rows, jsonb
from pipeline.detection import baseline, detectors, events, explain
from pipeline.detection.baseline import BoolArray, FloatArray
from pipeline.detection.evaluate import Evaluation, evaluate
from pipeline.models import AnomalyRecord, InjectedEvent, Reading, SensorSpec
from pipeline.sensors.thresholds import Threshold

logger = logging.getLogger(__name__)

REQUIRED_TABLES = ("sensors", "sensor_thresholds", "sensor_readings", "simulation_events", "detection_runs",
                   "reading_scores", "anomalies")  # fmt: skip
ANALYZED_TABLES = ("detection_runs", "reading_scores", "anomalies")
SIMULATED_SOURCE = "simulated"
ANOMALY_ID_FORMAT = "ANM-{:04d}"
VALUE_DECIMALS = 4  # expected values and band limits
Z_DECIMALS = 3
FOREST_DECIMALS = 4
METHOD_SUMMARY = (
    "Retrospective batch analysis. Per sensor: local-hour profile, peer adjustment on placement-class medians "
    "(temperature, moisture), robust z-score. Detectors: threshold, robust z-score, rolling median; "
    "Isolation Forest as corroborating evidence only."
)


class DetectionInputError(RuntimeError):
    """The inputs of the detection are missing (an earlier pipeline stage has not run)."""


class DetectionTargetError(RuntimeError):
    """The run does not show what the simulated scenario guarantees at the end of the window."""


@dataclass(frozen=True, slots=True)
class SensorRow:
    """The columns of ``infra.sensors`` the detection needs."""

    sensor_id: str
    asset_id: str
    sensor_type: str
    placement: str
    unit: str
    lon: float
    lat: float

    @classmethod
    def from_spec(cls, spec: SensorSpec) -> SensorRow:
        """The same sensor as described by the placement rules (no database needed)."""
        return cls(spec.sensor_id, spec.asset_id, spec.sensor_type, spec.placement, spec.unit, spec.lon, spec.lat)


@dataclass(slots=True)
class ReadingGrid:
    """Readings of every sensor on the time axis (rows follow ``sensors``; NaN / None = no reading)."""

    axis: TimeAxis
    sensors: list[SensorRow]
    values: FloatArray
    times: np.ndarray  # object array: the timestamp of the reading held by each cell, or None
    n_readings: int = 0  # readings placed on the grid
    n_superseded: int = 0  # readings replaced by a later reading of the same grid cell
    n_outside: int = 0  # readings after the last grid point


@dataclass(slots=True)
class DetectionResult:
    """Everything a detection run computed (no database involved)."""

    expected: FloatArray
    expected_low: FloatArray
    expected_high: FloatArray
    z: FloatArray
    forest: FloatArray
    flagged: BoolArray
    anomalies: list[AnomalyRecord]
    sensor_baselines: dict[str, dict[str, Any]] = field(default_factory=dict)
    peer_classes: dict[str, list[str]] = field(default_factory=dict)


# --- inputs -----------------------------------------------------------------------------------------------------
def check_inputs(conn: psycopg.Connection) -> None:
    """Fail loudly when the schema, the sensors or the readings are missing."""
    missing = [
        table
        for table in REQUIRED_TABLES
        if conn.execute("SELECT to_regclass(%s)", (f"infra.{table}",)).fetchone()[0] is None
    ]
    if missing:
        raise DetectionInputError(
            f"the database has no schema yet (missing infra.{missing[0]}); run scripts/seed_database.py first"
        )
    hint = "run scripts/generate_sensors.py (stage 4) first"
    if conn.execute("SELECT count(*) FROM infra.sensors").fetchone()[0] == 0:
        raise DetectionInputError(f"no sensors in the database; {hint}")
    if conn.execute("SELECT EXISTS (SELECT 1 FROM infra.sensor_readings)").fetchone()[0] is not True:
        raise DetectionInputError(f"no sensor readings in the database; {hint}")


def load_sensors(conn: psycopg.Connection) -> list[SensorRow]:
    """Every sensor, ordered by id."""
    rows = conn.execute(
        """
        SELECT sensor_id, asset_id, sensor_type, placement, unit, ST_X(geom), ST_Y(geom)
        FROM infra.sensors
        ORDER BY sensor_id
        """
    ).fetchall()
    return [SensorRow(*row) for row in rows]


def load_thresholds(conn: psycopg.Connection) -> dict[tuple[str, str], Threshold]:
    """The configured limits per (sensor_type, placement), read from ``infra.sensor_thresholds``."""
    rows = conn.execute(
        """
        SELECT sensor_type, placement, unit, warn_low, warn_high, crit_low, crit_high, COALESCE(description, '')
        FROM infra.sensor_thresholds
        """
    ).fetchall()
    return {(row[0], row[1]): Threshold(*row) for row in rows}


def build_reading_grid(
    sensors: Sequence[SensorRow],
    series: Mapping[str, tuple[Sequence[datetime], Sequence[float]]],
    step: timedelta,
) -> ReadingGrid:
    """Place readings on the regular grid from the first to the last reading timestamp.

    ``series`` maps a sensor id to its (timestamps in ascending order, values). A sensor without an entry
    has no reading; ids that are not in ``sensors`` are ignored.
    """
    position = {sensor.sensor_id: i for i, sensor in enumerate(sensors)}
    known = {sensor_id: data for sensor_id, data in series.items() if sensor_id in position and len(data[0])}
    if not known:
        raise DetectionInputError("no sensor readings to analyse")
    first = min(stamps[0] for stamps, _ in known.values())
    last = max(stamps[-1] for stamps, _ in known.values())
    axis = axis_from_bounds(first, last, step)
    grid = ReadingGrid(
        axis=axis,
        sensors=list(sensors),
        values=np.full((len(sensors), axis.count), np.nan),
        times=np.full((len(sensors), axis.count), None, dtype=object),
    )
    for sensor_id, (stamps, reading_values) in known.items():
        row = position[sensor_id]
        cells = axis.cell_indices(np.array([stamp.timestamp() for stamp in stamps], dtype=float))
        inside = (cells >= 0) & (cells < axis.count)
        grid.n_outside += int((~inside).sum())
        cells = cells[inside]  # ascending in time: a later reading of the same cell replaces the earlier one
        grid.values[row, cells] = np.array(reading_values, dtype=float)[inside]
        grid.times[row, cells] = np.array(stamps, dtype=object)[inside]
        placed = int(np.isfinite(grid.values[row]).sum())
        grid.n_readings += placed
        grid.n_superseded += len(cells) - placed
    return grid


def grid_from_readings(sensors: Sequence[SensorRow], readings: Iterable[Reading], step: timedelta) -> ReadingGrid:
    """Grid from ``Reading`` objects in any order (straight from a sensor source, without a database)."""
    collected: dict[str, list[tuple[datetime, float]]] = {}
    for reading in readings:
        collected.setdefault(reading.sensor_id, []).append((reading.ts, reading.value))
    series: dict[str, tuple[list[datetime], list[float]]] = {}
    for sensor_id, pairs in collected.items():
        pairs.sort(key=lambda pair: pair[0])
        series[sensor_id] = ([ts for ts, _ in pairs], [value for _, value in pairs])
    return build_reading_grid(sensors, series, step)


def load_reading_grid(conn: psycopg.Connection, sensors: Sequence[SensorRow], step: timedelta) -> ReadingGrid:
    """Place every stored reading on the grid (one query; the readings arrive as one array per sensor)."""
    rows = conn.execute(
        """
        SELECT sensor_id, array_agg(ts ORDER BY ts), array_agg(value ORDER BY ts)
        FROM infra.sensor_readings
        GROUP BY sensor_id
        """
    ).fetchall()
    return build_reading_grid(sensors, {row[0]: (row[1], row[2]) for row in rows}, step)


def load_ground_truth(conn: psycopg.Connection) -> list[InjectedEvent]:
    """The simulator's injected and benign regional events (empty for a real sensor source)."""
    rows = conn.execute(
        """
        SELECT event_type, is_anomaly, started_at, ended_at, sensor_id, asset_id, sensor_type, magnitude, description
        FROM infra.simulation_events
        ORDER BY event_id
        """
    ).fetchall()
    return [InjectedEvent(*row) for row in rows]


# --- detection (no database) ------------------------------------------------------------------------------------
def _anomaly_record(
    sensor: SensorRow,
    event: events.Event,
    grid: ReadingGrid,
    row: int,
    expected: FloatArray,
    log_domain: bool,
    peer_adjusted: bool,
) -> AnomalyRecord:
    """Turn one event of one sensor into the record stored in ``infra.anomalies`` (the id is set later)."""
    observed = float(grid.values[row, event.peak])
    expected_value = round(float(expected[event.peak]), VALUE_DECIMALS)
    started_at, ended_at = grid.times[row, event.start], grid.times[row, event.end]
    # Stored status = the time rule of the status module evaluated at T_end (the end of the axis): active when
    # ended_at >= T_end, else resolved. The API applies the same rule at any as_of.
    status = anomaly_status_at(started_at, ended_at, grid.axis.end) or events.STATUS_RESOLVED
    text = explain.explain(
        label=events.anomaly_label(event.anomaly_type),
        sensor_id=sensor.sensor_id,
        placement=sensor.placement,
        unit=sensor.unit,
        observed=observed,
        expected=expected_value,
        robust_z=event.robust_z,
        log_domain=log_domain,
        peer_adjusted=peer_adjusted,
        duration_hours=event.duration_hours,
        active=status == ANOMALY_ACTIVE,  # "still present at the end of the analysed window"
        methods=event.methods,
        severity=event.severity,
        score=event.score,
        components=event.components,
        breach=event.breach,
    )
    explain.assert_descriptive(text)
    return AnomalyRecord(
        anomaly_id="",
        sensor_id=sensor.sensor_id,
        asset_id=sensor.asset_id,
        sensor_type=sensor.sensor_type,
        anomaly_type=event.anomaly_type,
        started_at=started_at,
        ended_at=ended_at,
        peak_at=grid.times[row, event.peak],
        duration_hours=event.duration_hours,
        observed_value=observed,
        expected_value=expected_value,
        unit=sensor.unit,
        robust_z=round(event.robust_z, Z_DECIMALS),
        anomaly_score=event.score,
        severity=event.severity,
        detection_method="+".join(event.methods),
        explanation=text,
        status=status,
        lon=sensor.lon,
        lat=sensor.lat,
        score_components=dict(event.components),
        extra={"start_index": event.start, "end_index": event.end, "peak_index": event.peak,
               "flagged_hours": event.flagged_hours, "placement": sensor.placement},
    )  # fmt: skip


def detect(grid: ReadingGrid, thresholds: dict[tuple[str, str], Threshold], settings: Settings) -> DetectionResult:
    """Baselines, detectors and events for every sensor on the grid; anomalies come back numbered."""
    axis = grid.axis
    shape = grid.values.shape
    result = DetectionResult(
        expected=np.full(shape, np.nan),
        expected_low=np.full(shape, np.nan),
        expected_high=np.full(shape, np.nan),
        z=np.full(shape, np.nan),
        forest=np.full(shape, np.nan),
        flagged=np.zeros(shape, dtype=bool),
        anomalies=[],
    )
    params = events.DetectionParams.from_settings(settings, axis.step_hours)
    local_hour, weekend = baseline.local_time_keys(axis.timestamps(), settings.tz)
    for sensor_type in sorted({sensor.sensor_type for sensor in grid.sensors}):
        rows = [i for i, sensor in enumerate(grid.sensors) if sensor.sensor_type == sensor_type]
        placements = [grid.sensors[i].placement for i in rows]
        fit = baseline.fit_type_baseline(
            grid.values[rows], placements, sensor_type, local_hour, weekend, axis.step_hours
        )
        # Every decision below uses the scores at the precision they are stored with, so the flags, events and
        # detection methods can be reproduced exactly from infra.reading_scores.
        z = np.round(fit.z, Z_DECIMALS)
        forest = np.round(detectors.isolation_forest_scores(z, settings.SIM_SEED, axis.step_hours), FOREST_DECIMALS)
        result.expected[rows], result.expected_low[rows] = fit.expected, fit.expected_low
        result.expected_high[rows], result.z[rows], result.forest[rows] = fit.expected_high, z, forest
        result.peer_classes[sensor_type] = list(fit.peer_classes)
        for local, row in enumerate(rows):
            sensor = grid.sensors[row]
            limits = thresholds[(sensor.sensor_type, sensor.placement)]
            flags = events.hour_flags(grid.values[row], z[local], forest[local], limits, params)
            result.flagged[row] = flags.flagged
            result.sensor_baselines[sensor.sensor_id] = {
                "scale": None if np.isnan(fit.scale[local]) else round(float(fit.scale[local]), 4),
                "floor": float(fit.floor[local]),
                "domain": "ln" if fit.log_domain else "native",
            }
            for event in events.find_events(sensor_type, grid.values[row], z[local], flags, limits, params):
                result.anomalies.append(
                    _anomaly_record(sensor, event, grid, row, fit.expected[local], fit.log_domain,
                                    bool(fit.peer_classes))
                )  # fmt: skip
    result.anomalies.sort(key=lambda record: (record.started_at, record.sensor_id))
    for number, record in enumerate(result.anomalies, start=1):
        record.anomaly_id = ANOMALY_ID_FORMAT.format(number)
    return result


def run_parameters(settings: Settings, axis: TimeAxis, result: DetectionResult) -> dict[str, Any]:
    """The parameters of the run as stored in ``detection_runs.params``."""
    return {
        "method": METHOD_SUMMARY,
        "retrospective": True,
        "sensor_source": settings.SENSOR_SOURCE,
        "timezone": settings.TIMEZONE,
        "step_seconds": axis.step_seconds,
        "step_minutes": axis.step_seconds / 60.0,
        "z_strong": settings.DETECT_Z_STRONG,
        "z_min": settings.DETECT_Z_MIN,
        "rolling_hours": settings.DETECT_ROLLING_HOURS,
        "rolling_level": detectors.ROLLING_MEDIAN_LEVEL,
        "rolling_min_readings": detectors.ROLLING_MIN_READINGS,
        "merge_gap_hours": settings.DETECT_MERGE_GAP_HOURS,
        "min_flagged_hours": events.MIN_FLAGGED_HOURS,
        "iforest": {
            "threshold": settings.DETECT_IFOREST_THRESHOLD,
            "n_estimators": detectors.IFOREST_ESTIMATORS,
            "max_samples": detectors.IFOREST_MAX_SAMPLES,
            "random_state": settings.SIM_SEED,
            "features": list(detectors.IFOREST_FEATURES),
            "role": "corroborating evidence only",
        },
        "baseline": {
            "log_domain_types": sorted(baseline.LOG_DOMAIN_TYPES),
            "weekpart_profile_types": sorted(baseline.WEEKPART_PROFILE_TYPES),
            "peer_adjusted_types": sorted(baseline.PEER_ADJUSTED_TYPES),
            "peer_classes": result.peer_classes,
            "peer_ema_hours": {name: list(spans) for name, spans in baseline.PEER_EMA_HOURS.items()},
            "min_class_sensors": baseline.MIN_CLASS_SENSORS,
            "scale_floors": baseline.SCALE_FLOORS,
            "small_class_floors": baseline.SMALL_CLASS_FLOORS,
            "huber_c": baseline.HUBER_C,
            "huber_iterations": baseline.HUBER_ITERATIONS,
            "band_sigmas": baseline.BAND_SIGMAS,
        },
        "score": {
            "weights": events.SCORE_WEIGHTS,
            "severity_min_score": dict(events.SEVERITY_MIN_SCORE),
        },
        "sensor_baselines": result.sensor_baselines,
    }


# --- outputs ----------------------------------------------------------------------------------------------------
def clear_previous_run(conn: psycopg.Connection) -> None:
    """Remove the previous run and everything derived from it (build contract section 4.1, row 5)."""
    conn.execute("TRUNCATE infra.detection_runs RESTART IDENTITY CASCADE")


def _rounded(value: float, decimals: int) -> float | None:
    return None if np.isnan(value) else round(float(value), decimals)


def _score_rows(grid: ReadingGrid, result: DetectionResult, run_id: int) -> Iterator[tuple[Any, ...]]:
    for row, sensor in enumerate(grid.sensors):
        for cell in np.flatnonzero(np.isfinite(grid.values[row])):
            yield (
                sensor.sensor_id,
                grid.times[row, cell],
                run_id,
                _rounded(result.expected[row, cell], VALUE_DECIMALS),
                _rounded(result.expected_low[row, cell], VALUE_DECIMALS),
                _rounded(result.expected_high[row, cell], VALUE_DECIMALS),
                _rounded(result.z[row, cell], Z_DECIMALS),
                _rounded(result.forest[row, cell], FOREST_DECIMALS),
                bool(result.flagged[row, cell]),
            )


def write_reading_scores(conn: psycopg.Connection, grid: ReadingGrid, result: DetectionResult, run_id: int) -> int:
    """COPY one score row per analysed reading into ``infra.reading_scores``."""
    columns = ("sensor_id", "ts", "run_id", "expected", "expected_low", "expected_high", "robust_z", "iforest_score",
               "flagged")  # fmt: skip
    return copy_rows(conn, "reading_scores", columns, _score_rows(grid, result, run_id))


def write_anomalies(conn: psycopg.Connection, anomalies: Sequence[AnomalyRecord], run_id: int) -> int:
    """Insert the anomaly records; the point geometry is the sensor's location."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO infra.anomalies
                (anomaly_id, run_id, sensor_id, asset_id, sensor_type, anomaly_type, started_at, ended_at, peak_at,
                 duration_hours, observed_value, expected_value, unit, robust_z, anomaly_score, score_components,
                 severity, detection_method, explanation, status, geom)
            SELECT %s, %s, s.sensor_id, s.asset_id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   s.geom
            FROM infra.sensors s
            WHERE s.sensor_id = %s
            """,
            [
                (
                    a.anomaly_id, run_id, a.sensor_type, a.anomaly_type, a.started_at, a.ended_at, a.peak_at,
                    a.duration_hours, a.observed_value, a.expected_value, a.unit, a.robust_z, a.anomaly_score,
                    jsonb(a.score_components), a.severity, a.detection_method, a.explanation, a.status, a.sensor_id,
                )
                for a in anomalies
            ],
        )  # fmt: skip
    return len(anomalies)


def assert_final_hour(anomalies: Sequence[AnomalyRecord], truth: Sequence[InjectedEvent], axis: TimeAxis) -> None:
    """The simulated scenario ends with ongoing events: at least one critical and one high anomaly must be active.

    Skipped (with a log line) when the ground truth holds no injected event that is ongoing at the end.
    """
    if not any(event.is_anomaly and event.ended_at >= axis.end for event in truth):
        logger.info("no injected event is ongoing at the end of the window: final-hour check skipped")
        return
    active = Counter(a.severity for a in anomalies if a.status == events.STATUS_ACTIVE)
    if active["critical"] < 1 or active["high"] < 1:
        raise DetectionTargetError(
            "the simulated scenario must end with at least one critical and one high anomaly active at "
            f"{axis.end.isoformat()}, found {dict(active) or 'none'}; check the simulator and detector settings"
        )


def _log_summary(result: DetectionResult, evaluation: Evaluation, grid: ReadingGrid) -> None:
    metrics = evaluation.metrics
    logger.info(
        "detected %d anomalies on %d sensors; severity %s; %d flagged readings in total",
        len(result.anomalies), len({a.sensor_id for a in result.anomalies}), metrics["severity_counts"],
        int(result.flagged.sum()),
    )  # fmt: skip
    active = [a for a in result.anomalies if a.status == events.STATUS_ACTIVE]
    logger.info(
        "active at %s: %d (%s)", grid.axis.end.isoformat(), len(active),
        ", ".join(f"{a.anomaly_id} {a.sensor_id} {a.severity}" for a in active) or "none",
    )  # fmt: skip
    if metrics["injected_events"]:
        precision = metrics["anomaly_precision"]
        logger.info(
            "evaluation against %d injected events (self-consistency check): recall %.3f, precision %s, "
            "%d false anomalies (%d during benign regional events), %d split events",
            metrics["injected_events"], metrics["event_recall"],
            "n/a (no anomalies)" if precision is None else f"{precision:.3f}",
            metrics["false_anomalies"], metrics["false_anomalies_during_benign_events"], metrics["split_events"],
        )  # fmt: skip
        for event in evaluation.missed_events:
            logger.warning("  injected event not detected: %s on %s from %s", event.event_type, event.sensor_id,
                           event.started_at.isoformat())  # fmt: skip
    else:
        logger.info("no injected events in infra.simulation_events: the run is not evaluated")


def run_detection(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """Run the detection through ``conn`` (the caller owns the transaction); returns a summary."""
    clock = time.perf_counter()
    started_at = datetime.now(UTC)
    check_inputs(conn)
    clear_previous_run(conn)

    sensors = load_sensors(conn)
    thresholds = load_thresholds(conn)
    grid = load_reading_grid(conn, sensors, sampling_step(conn))
    if grid.n_superseded or grid.n_outside:
        logger.warning(
            "%d readings share a grid cell with a later reading and %d lie after the last grid point; "
            "they are not scored", grid.n_superseded, grid.n_outside,
        )  # fmt: skip
    logger.info(
        "analysing %d readings of %d sensors, %s .. %s (%d steps of %.0f min)",
        grid.n_readings, len(sensors), grid.axis.start.isoformat(), grid.axis.end.isoformat(), grid.axis.count,
        grid.axis.step_seconds / 60.0,
    )  # fmt: skip

    result = detect(grid, thresholds, settings)
    truth = load_ground_truth(conn)
    evaluation = evaluate(result.anomalies, truth)
    _log_summary(result, evaluation, grid)
    if settings.SENSOR_SOURCE == SIMULATED_SOURCE:
        assert_final_hour(result.anomalies, truth, grid.axis)

    run_id = conn.execute(
        """
        INSERT INTO infra.detection_runs (started_at, window_start, window_end, params, n_readings, n_anomalies)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING run_id
        """,
        (started_at, grid.axis.start, grid.axis.end, jsonb(run_parameters(settings, grid.axis, result)),
         grid.n_readings, len(result.anomalies)),
    ).fetchone()[0]  # fmt: skip
    scores = write_reading_scores(conn, grid, result, run_id)
    write_anomalies(conn, result.anomalies, run_id)
    conn.execute(
        "UPDATE infra.detection_runs SET metrics = %s, finished_at = %s WHERE run_id = %s",
        (jsonb(evaluation.metrics), datetime.now(UTC), run_id),
    )
    for table in ANALYZED_TABLES:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier("infra", table)))

    seconds = round(time.perf_counter() - clock, 2)
    logger.info("stage 5 wrote run %d: %d reading scores and %d anomalies in %.1f s", run_id, scores,
                len(result.anomalies), seconds)  # fmt: skip
    return {
        "run_id": run_id,
        "window_start": grid.axis.start,
        "window_end": grid.axis.end,
        "readings": grid.n_readings,
        "reading_scores": scores,
        "flagged_readings": int(result.flagged.sum()),
        "anomalies": len(result.anomalies),
        "active_at_end": [a.anomaly_id for a in result.anomalies if a.status == events.STATUS_ACTIVE],
        "metrics": evaluation.metrics,
        "seconds": seconds,
    }
