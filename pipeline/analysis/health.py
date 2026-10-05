"""Derived Asset Health Score (build contract section 9): the formula and its hourly computation.

For every monitored asset (at least one sensor) and every hour t::

    window  = anomalies on the asset's sensors with started_at <= t and ended_at >= t - HEALTH_WINDOW_DAYS
    w_i     = 1 while active at t, else 0.5 ** ((t - ended_at_i) / HEALTH_HALF_LIFE_HOURS)
    s_i     = {low: 1, medium: 2, high: 4, critical: 7}[severity_i]
    frequency_penalty = min(20, 6 * sum(w_i))
    severity_penalty  = min(45, 6 * sum(w_i * s_i))
    reading_penalty   = min(10, 2 * mean over reporting sensors of clip(median(|z|, trailing 6 h) - 3, 0, 5))
                        (0 when no sensor reports)
    sensor_penalty    = 20 * (sensors with no reading at t / sensors_total)
    health  = clamp(round(100 - frequency - severity - reading - sensor), 0, 100)
    status  = normal >= 90 > watch >= 70 > at_risk >= 45 > critical

The score is evaluated at every hour t from the anomalies that had started by t and the readings up to t;
anomaly severities and baselines come from the retrospective detection run. It is derived from simulated
sensors and is not an assessment of the real condition of an asset. Assets without sensors have no score.

``penalties`` is the one implementation of the formula; ``health_score`` (one asset, one moment) and
``compute_health`` (all assets, all hours) both call it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import psycopg
from numpy.typing import NDArray

from pipeline.analysis.status import (
    AT_RISK_BELOW,
    AnomalyIntervals,
    SensorMatrix,
    TimeAxis,
    active_matrix,
    recency_weights,
)
from pipeline.config import Settings
from pipeline.db.loaders import copy_rows
from pipeline.detection.baseline import FloatArray
from pipeline.detection.detectors import trailing_median

logger = logging.getLogger(__name__)

SEVERITY_WEIGHTS: dict[str, float] = {"low": 1.0, "medium": 2.0, "high": 4.0, "critical": 7.0}
FREQUENCY_FACTOR = 6.0
FREQUENCY_CAP = 20.0
SEVERITY_FACTOR = 6.0
SEVERITY_CAP = 45.0
READING_FACTOR = 2.0
READING_CAP = 10.0
READING_Z_OFFSET = 3.0  # a trailing median |z| up to this level costs nothing
READING_Z_RANGE = 5.0  # ... and the excess is counted up to this much
READING_WINDOW_HOURS = 6.0
SENSOR_FACTOR = 20.0
HOURS_PER_DAY = 24.0
PENALTY_DECIMALS = 3

STATUS_NORMAL = "normal"
STATUS_WATCH = "watch"
STATUS_AT_RISK = "at_risk"
STATUS_CRITICAL = "critical"
STATUS_NOT_MONITORED = "not_monitored"  # assets without sensors: no score is computed
# Lower bound of every band, best first. "Assets at Risk" = score below the watch bound.
STATUS_BANDS: tuple[tuple[str, int], ...] = (
    (STATUS_NORMAL, 90),
    (STATUS_WATCH, AT_RISK_BELOW),
    (STATUS_AT_RISK, 45),
    (STATUS_CRITICAL, 0),
)
ASSET_STATUSES: tuple[str, ...] = tuple(name for name, _ in STATUS_BANDS)
# One character per time step in the playback bundle.
ASSET_STATUS_CHARS: dict[str, str] = {
    STATUS_NORMAL: "n", STATUS_WATCH: "w", STATUS_AT_RISK: "r", STATUS_CRITICAL: "c",
}  # fmt: skip
FORMULA_TEXT = (
    "health = clamp(round(100 - frequency - severity - reading - sensor), 0, 100); "
    "frequency = min(20, 6 * sum(w)); severity = min(45, 6 * sum(w * s)) with s = low 1, medium 2, high 4, "
    "critical 7 and w = 1 while an anomaly is active, else 0.5 ** (hours since it ended / half-life), counted "
    "for the look-back window; reading = min(10, 2 * mean over reporting sensors of clip(median |z| of the "
    "last 6 h - 3, 0, 5)); sensor = 20 * share of the asset's sensors without a reading. "
    "Status: normal >= 90 > watch >= 70 > at_risk >= 45 > critical."
)


@dataclass(frozen=True, slots=True)
class AnomalyState:
    """One anomaly of an asset as seen at the moment of evaluation."""

    severity: str
    hours_since_end: float | None = None  # None = active at that moment


@dataclass(frozen=True, slots=True)
class HealthResult:
    """Health of one asset at one moment, with the four penalties that explain it."""

    health_score: int
    status: str
    frequency_penalty: float
    severity_penalty: float
    reading_penalty: float
    sensor_penalty: float
    anomalies_in_window: int
    active_anomalies: int
    sensors_reporting: int
    sensors_total: int


@dataclass(frozen=True, slots=True)
class HealthSeries:
    """Health of every monitored asset at every time step (rows ordered by asset id)."""

    asset_ids: list[str]
    health_score: NDArray[np.int64]
    frequency_penalty: FloatArray
    severity_penalty: FloatArray
    reading_penalty: FloatArray
    sensor_penalty: FloatArray
    anomalies_in_window: NDArray[np.int64]
    active_anomalies: NDArray[np.int64]
    sensors_reporting: NDArray[np.int64]
    sensors_total: NDArray[np.int64]  # per asset

    def status_strings(self) -> dict[str, str]:
        """Asset id -> one status character per time step (n normal, w watch, r at_risk, c critical)."""
        return {
            asset_id: "".join(ASSET_STATUS_CHARS[status_for(int(score))] for score in self.health_score[i])
            for i, asset_id in enumerate(self.asset_ids)
        }


# --- the formula ------------------------------------------------------------------------------------------------
def penalties(
    weight_sum: Any, weighted_severity_sum: Any, reading_level: Any, offline_share: Any
) -> tuple[Any, Any, Any, Any]:
    """The four penalties (frequency, severity, reading, sensor) from their inputs; floats or arrays.

    ``weight_sum`` = sum of w_i, ``weighted_severity_sum`` = sum of w_i * s_i, ``reading_level`` = mean over
    the reporting sensors of clip(trailing median |z| - 3, 0, 5), ``offline_share`` = share of the asset's
    sensors without a reading.
    """
    frequency = np.minimum(FREQUENCY_CAP, FREQUENCY_FACTOR * np.asarray(weight_sum, dtype=float))
    severity = np.minimum(SEVERITY_CAP, SEVERITY_FACTOR * np.asarray(weighted_severity_sum, dtype=float))
    reading = np.minimum(READING_CAP, READING_FACTOR * np.asarray(reading_level, dtype=float))
    sensor = SENSOR_FACTOR * np.asarray(offline_share, dtype=float)
    return frequency, severity, reading, sensor


def score_from_penalties(frequency: Any, severity: Any, reading: Any, sensor: Any) -> Any:
    """``clamp(round(100 - sum of penalties), 0, 100)``; halves round up."""
    total = 100.0 - (frequency + severity + reading + sensor)
    return np.clip(np.floor(total + 0.5), 0, 100).astype(np.int64)


def reading_excess(median_abs_z: Any) -> Any:
    """``clip(median |z| - 3, 0, 5)``: how far a sensor's recent readings sit beyond the ordinary range."""
    return np.clip(np.asarray(median_abs_z, dtype=float) - READING_Z_OFFSET, 0.0, READING_Z_RANGE)


def status_for(score: int) -> str:
    """Status band of a health score."""
    for name, minimum in STATUS_BANDS:
        if score >= minimum:
            return name
    return STATUS_CRITICAL


def anomaly_weight(hours_since_end: float | None, half_life_hours: float) -> float:
    """w_i: 1 while the anomaly is active, else halved every ``half_life_hours`` since it ended."""
    if hours_since_end is None or hours_since_end <= 0:
        return 1.0
    return float(0.5 ** (hours_since_end / half_life_hours))


def health_score(
    anomalies: Sequence[AnomalyState],
    sensor_levels: Sequence[float | None],
    half_life_hours: float = 48.0,
    window_hours: float = 168.0,
) -> HealthResult:
    """Health of one asset at one moment.

    ``anomalies``: the asset's anomalies that have started. ``sensor_levels``: one entry per sensor of the
    asset - the median |z| of its last 6 h for a reporting sensor, ``None`` for a sensor without a reading.
    """
    if not sensor_levels:
        raise ValueError("an asset without sensors has no health score (status 'not_monitored')")
    in_window = [a for a in anomalies if a.hours_since_end is None or a.hours_since_end <= window_hours]
    weights = [anomaly_weight(a.hours_since_end, half_life_hours) for a in in_window]
    reporting = [level for level in sensor_levels if level is not None]
    reading_level = float(np.mean(reading_excess(reporting))) if reporting else 0.0
    offline_share = (len(sensor_levels) - len(reporting)) / len(sensor_levels)
    frequency, severity, reading, sensor = penalties(
        sum(weights),
        sum(weight * SEVERITY_WEIGHTS[a.severity] for weight, a in zip(weights, in_window)),
        reading_level,
        offline_share,
    )
    score = int(score_from_penalties(frequency, severity, reading, sensor))
    return HealthResult(
        health_score=score,
        status=status_for(score),
        frequency_penalty=round(float(frequency), PENALTY_DECIMALS),
        severity_penalty=round(float(severity), PENALTY_DECIMALS),
        reading_penalty=round(float(reading), PENALTY_DECIMALS),
        sensor_penalty=round(float(sensor), PENALTY_DECIMALS),
        anomalies_in_window=len(in_window),
        active_anomalies=sum(1 for a in in_window if a.hours_since_end is None or a.hours_since_end <= 0),
        sensors_reporting=len(reporting),
        sensors_total=len(sensor_levels),
    )


# --- every monitored asset, every hour --------------------------------------------------------------------------
def _membership(groups: Sequence[str], members: Sequence[str]) -> FloatArray:
    """(groups x members) indicator matrix: member j belongs to group i."""
    row = {name: i for i, name in enumerate(groups)}
    matrix = np.zeros((len(groups), len(members)))
    for j, name in enumerate(members):
        if name in row:
            matrix[row[name], j] = 1.0
    return matrix


def compute_health(matrix: SensorMatrix, anomalies: AnomalyIntervals, settings: Settings) -> HealthSeries:
    """Evaluate the formula for every monitored asset at every time step of the axis."""
    axis = matrix.axis
    grid = axis.epochs()
    asset_ids = sorted(set(matrix.asset_ids))
    by_sensor = _membership(asset_ids, matrix.asset_ids)
    by_anomaly = _membership(asset_ids, anomalies.asset_ids)
    sensors_total = by_sensor.sum(axis=1)

    window_hours = settings.HEALTH_WINDOW_DAYS * HOURS_PER_DAY
    weights = recency_weights(anomalies.started, anomalies.ended, grid, settings.HEALTH_HALF_LIFE_HOURS, window_hours)
    severity = np.array([SEVERITY_WEIGHTS[name] for name in anomalies.severities], dtype=float)
    active = active_matrix(anomalies.started, anomalies.ended, grid)

    window_steps = max(int(round(READING_WINDOW_HOURS / axis.step_hours)), 1)
    recent = np.nan_to_num(trailing_median(np.abs(matrix.robust_z), window_steps), nan=0.0)
    excess = np.where(matrix.reporting, reading_excess(recent), 0.0)
    reporting = by_sensor @ matrix.reporting.astype(float)
    reading_level = np.divide(by_sensor @ excess, reporting, out=np.zeros_like(reporting), where=reporting > 0)
    offline_share = (sensors_total[:, None] - reporting) / sensors_total[:, None]

    frequency, severity_penalty, reading, sensor = penalties(
        by_anomaly @ weights, by_anomaly @ (weights * severity[:, None]), reading_level, offline_share
    )
    return HealthSeries(
        asset_ids=asset_ids,
        health_score=score_from_penalties(frequency, severity_penalty, reading, sensor),
        frequency_penalty=np.round(frequency, PENALTY_DECIMALS),
        severity_penalty=np.round(severity_penalty, PENALTY_DECIMALS),
        reading_penalty=np.round(reading, PENALTY_DECIMALS),
        sensor_penalty=np.round(sensor, PENALTY_DECIMALS),
        anomalies_in_window=np.rint(by_anomaly @ (weights > 0).astype(float)).astype(np.int64),
        active_anomalies=np.rint(by_anomaly @ active.astype(float)).astype(np.int64),
        sensors_reporting=np.rint(reporting).astype(np.int64),
        sensors_total=np.rint(sensors_total).astype(np.int64),
    )


def _health_rows(series: HealthSeries, matrix: SensorMatrix, run_id: int) -> Iterator[tuple[Any, ...]]:
    stamps = matrix.axis.timestamps()
    for i, asset_id in enumerate(series.asset_ids):
        total = int(series.sensors_total[i])
        for t, as_of in enumerate(stamps):
            score = int(series.health_score[i, t])
            yield (
                asset_id, as_of, run_id, score, status_for(score),
                float(series.frequency_penalty[i, t]), float(series.severity_penalty[i, t]),
                float(series.reading_penalty[i, t]), float(series.sensor_penalty[i, t]),
                int(series.anomalies_in_window[i, t]), int(series.active_anomalies[i, t]),
                int(series.sensors_reporting[i, t]), total,
            )  # fmt: skip


def write_health(conn: psycopg.Connection, series: HealthSeries, matrix: SensorMatrix, run_id: int) -> int:
    """COPY one row per (monitored asset, time step) into ``infra.asset_health``."""
    columns = ("asset_id", "as_of", "run_id", "health_score", "status", "frequency_penalty", "severity_penalty",
               "reading_penalty", "sensor_penalty", "anomalies_in_window", "active_anomalies", "sensors_reporting",
               "sensors_total")  # fmt: skip
    return copy_rows(conn, "asset_health", columns, _health_rows(series, matrix, run_id))


def run_health(
    conn: psycopg.Connection, settings: Settings, matrix: SensorMatrix, anomalies: AnomalyIntervals, run_id: int
) -> dict[str, Any]:
    """Compute and store the health of every monitored asset at every time step; returns a summary."""
    series = compute_health(matrix, anomalies, settings)
    rows = write_health(conn, series, matrix, run_id)
    final = series.health_score[:, -1]
    counts = dict.fromkeys(ASSET_STATUSES, 0)
    for score in final:
        counts[status_for(int(score))] += 1
    logger.info(
        "asset health: %d rows for %d monitored assets; at the last time step %s; %d below %d (assets at risk)",
        rows, len(series.asset_ids), ", ".join(f"{name} {count}" for name, count in counts.items()),
        int((final < AT_RISK_BELOW).sum()), AT_RISK_BELOW,
    )  # fmt: skip
    return {
        "rows": rows,
        "monitored_assets": len(series.asset_ids),
        "status_at_end": counts,
        "assets_at_risk_at_end": int((final < AT_RISK_BELOW).sum()),
    }


def load_health_series(conn: psycopg.Connection, axis: TimeAxis) -> dict[str, dict[str, Any]]:
    """Stored health of every monitored asset over the axis: ``{asset_id: {"health": [...], "status": "nnwrc..."}}``.

    One score and one status character per time step, in axis order (the form the playback bundle uses).
    """
    cases = " ".join(f"WHEN '{name}' THEN '{char}'" for name, char in ASSET_STATUS_CHARS.items())
    rows = conn.execute(
        f"""
        SELECT asset_id,
               array_agg(health_score::int ORDER BY as_of),
               string_agg(CASE status {cases} END, '' ORDER BY as_of)
        FROM infra.asset_health
        WHERE as_of >= %s AND as_of <= %s
        GROUP BY asset_id
        ORDER BY asset_id
        """,
        (axis.start, axis.end),
    ).fetchall()
    return {row[0]: {"health": list(row[1]), "status": row[2]} for row in rows}
