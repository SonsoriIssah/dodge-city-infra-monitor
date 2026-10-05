"""Risk zones: a hexagonal grid over the study area and an hourly risk score per cell (build contract 9).

Grid: ``ST_HexagonGrid(RISK_HEX_EDGE_M, study area)`` in the study area's UTM zone, clipped to the study
area; ``cell_id = 'i_j'`` (the grid's own column / row numbers, so ids are stable between runs).

Score, for every cell and every hour t on the time axis::

    risk_raw(cell, t) = sum_i s_i * exp(-d_i^2 / (2 * RISK_BANDWIDTH_M^2)) * w_i(t)
    risk_score        = min(100, 100 * risk_raw / RISK_REFERENCE)

over the anomalies with ``started_at <= t``; ``d_i`` = distance from the cell centroid (metres, UTM),
``s_i`` = severity weight (low 1, medium 2, high 4, critical 7), ``w_i`` = 1 while the anomaly is active,
else ``0.5 ** (hours since it ended / RISK_HALF_LIFE_HOURS)``; anomalies that ended more than 14 days before
t are ignored. With the default reference of 14, two active critical anomalies at the cell centre give 100,
one gives 50, one active high 29, one active medium 14.
Levels: low < 25 <= moderate < 50 <= high < 75 <= very_high. Only rows with a score of at least 0.5 are
stored. The layer is derived from simulated anomalies; it says nothing about real-world risk.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import numpy as np
import psycopg
from numpy.typing import NDArray

from pipeline.analysis.health import SEVERITY_WEIGHTS
from pipeline.analysis.status import AnomalyIntervals, TimeAxis, active_matrix, recency_weights
from pipeline.config import Settings
from pipeline.db.loaders import copy_rows
from pipeline.detection.baseline import FloatArray

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 14.0  # anomalies that ended longer ago do not count
MIN_STORED_SCORE = 0.5  # sparse storage: lower scores are not written
SCORE_DECIMALS = 2
MIN_CELL_AREA_M2 = 1.0  # slivers left by clipping the grid to the study area are dropped
HOURS_PER_DAY = 24.0
LEVEL_LOW = "low"
# Lower bound of every level, highest first.
RISK_LEVELS: tuple[tuple[str, float], ...] = (("very_high", 75.0), ("high", 50.0), ("moderate", 25.0), (LEVEL_LOW, 0.0))

GRID_SQL = """
WITH area AS (
    SELECT sa.study_area_id, sa.utm_srid, ST_Transform(sa.geom, sa.utm_srid) AS geom_utm
    FROM infra.study_areas sa
    LIMIT 1
),
cells AS (
    SELECT h.i, h.j, area.study_area_id, ST_Intersection(h.geom, area.geom_utm) AS clipped
    FROM area
    CROSS JOIN LATERAL ST_HexagonGrid(%(edge_m)s, area.geom_utm) AS h
    WHERE ST_Intersects(h.geom, area.geom_utm)
),
parts AS (
    -- the clipped cell is one polygon for a convex study area; otherwise its largest part is kept
    SELECT DISTINCT ON (c.i, c.j) c.i, c.j, c.study_area_id, d.geom
    FROM cells c
    CROSS JOIN LATERAL ST_Dump(ST_CollectionExtract(c.clipped, 3)) AS d
    WHERE ST_Area(d.geom) >= %(min_area_m2)s
    ORDER BY c.i, c.j, ST_Area(d.geom) DESC
)
INSERT INTO infra.risk_zones (cell_id, study_area_id, geom, centroid)
SELECT p.i || '_' || p.j, p.study_area_id, ST_Transform(p.geom, 4326), ST_Transform(ST_Centroid(p.geom), 4326)
FROM parts p
ORDER BY p.i, p.j
"""

CELL_POINTS_SQL = """
SELECT z.cell_id,
       ST_X(ST_Transform(z.centroid, sa.utm_srid)),
       ST_Y(ST_Transform(z.centroid, sa.utm_srid))
FROM infra.risk_zones z
JOIN infra.study_areas sa ON sa.study_area_id = z.study_area_id
ORDER BY z.cell_id
"""

ANOMALY_POINTS_SQL = """
SELECT an.anomaly_id,
       ST_X(ST_Transform(an.geom, sa.utm_srid)),
       ST_Y(ST_Transform(an.geom, sa.utm_srid)),
       (SELECT z.cell_id FROM infra.risk_zones z WHERE ST_Covers(z.geom, an.geom) ORDER BY z.cell_id LIMIT 1)
FROM infra.anomalies an
CROSS JOIN (SELECT utm_srid FROM infra.study_areas LIMIT 1) sa
ORDER BY an.anomaly_id
"""


def build_grid(conn: psycopg.Connection, settings: Settings) -> int:
    """Fill ``infra.risk_zones`` with the hexagonal grid of the study area; returns the number of cells."""
    return conn.execute(GRID_SQL, {"edge_m": settings.RISK_HEX_EDGE_M, "min_area_m2": MIN_CELL_AREA_M2}).rowcount


def risk_level(score: float) -> str:
    """Level of a risk score."""
    for name, minimum in RISK_LEVELS:
        if score >= minimum:
            return name
    return LEVEL_LOW


def kernel(cell_xy: FloatArray, anomaly_xy: FloatArray, severity_weights: FloatArray, bandwidth_m: float) -> FloatArray:
    """(cells x anomalies): ``s_i * exp(-d^2 / (2 * bandwidth^2))`` with d in metres."""
    delta = cell_xy[:, None, :] - anomaly_xy[None, :, :]
    squared = (delta**2).sum(axis=2)
    return severity_weights[None, :] * np.exp(-squared / (2.0 * bandwidth_m**2))


def risk_scores(
    cell_xy: FloatArray,
    anomaly_xy: FloatArray,
    severity_weights: FloatArray,
    started: FloatArray,
    ended: FloatArray,
    grid: FloatArray,
    settings: Settings,
) -> FloatArray:
    """Risk score (0..100) of every cell at every grid time: (cells x steps). Times are epoch seconds."""
    weights = recency_weights(started, ended, grid, settings.RISK_HALF_LIFE_HOURS, MAX_AGE_DAYS * HOURS_PER_DAY)
    raw = kernel(cell_xy, anomaly_xy, severity_weights, settings.RISK_BANDWIDTH_M) @ weights
    return np.minimum(100.0, 100.0 * raw / settings.RISK_REFERENCE)


def active_counts(cell_ids: list[str], anomaly_cells: list[str | None], active: NDArray[np.bool_]) -> NDArray[np.int64]:
    """(cells x steps): number of anomalies active at each time whose point lies in the cell."""
    row = {cell_id: i for i, cell_id in enumerate(cell_ids)}
    counts = np.zeros((len(cell_ids), active.shape[1]), dtype=np.int64)
    for j, cell_id in enumerate(anomaly_cells):
        if cell_id in row:
            counts[row[cell_id]] += active[j]
    return counts


def _score_rows(
    cell_ids: list[str], axis: TimeAxis, scores: FloatArray, counts: NDArray[np.int64], run_id: int
) -> Iterator[tuple[Any, ...]]:
    stamps = axis.timestamps()
    for cell, step in zip(*np.nonzero(scores >= MIN_STORED_SCORE)):
        score = float(scores[cell, step])
        yield (cell_ids[cell], stamps[step], run_id, score, risk_level(score), int(counts[cell, step]))


def run_risk_zones(
    conn: psycopg.Connection, settings: Settings, axis: TimeAxis, anomalies: AnomalyIntervals, run_id: int
) -> dict[str, Any]:
    """Build the grid, compute the hourly risk of every cell and store it sparsely; returns a summary."""
    cells = build_grid(conn, settings)
    if cells == 0:
        raise RuntimeError("the hexagonal grid has no cell: check RISK_HEX_EDGE_M and the study area")
    cell_rows = conn.execute(CELL_POINTS_SQL).fetchall()
    cell_ids = [row[0] for row in cell_rows]
    cell_xy = np.array([[row[1], row[2]] for row in cell_rows], dtype=float)
    point_rows = {row[0]: row for row in conn.execute(ANOMALY_POINTS_SQL).fetchall()}
    ordered = [point_rows[anomaly_id] for anomaly_id in anomalies.anomaly_ids]
    anomaly_xy = np.array([[row[1], row[2]] for row in ordered], dtype=float).reshape(-1, 2)
    anomaly_cells: list[str | None] = [row[3] for row in ordered]
    for j, cell_id in enumerate(anomaly_cells):
        if cell_id is None:  # a point on the outline of the study area: use the nearest cell centre
            anomaly_cells[j] = cell_ids[int(np.argmin(((cell_xy - anomaly_xy[j]) ** 2).sum(axis=1)))]

    grid = axis.epochs()
    severity = np.array([SEVERITY_WEIGHTS[name] for name in anomalies.severities], dtype=float)
    scores = np.round(
        risk_scores(cell_xy, anomaly_xy, severity, anomalies.started, anomalies.ended, grid, settings),
        SCORE_DECIMALS,
    )
    counts = active_counts(cell_ids, anomaly_cells, active_matrix(anomalies.started, anomalies.ended, grid))
    columns = ("cell_id", "as_of", "run_id", "risk_score", "risk_level", "anomaly_count")
    rows = copy_rows(conn, "risk_zone_scores", columns, _score_rows(cell_ids, axis, scores, counts, run_id))

    final = scores[:, -1]
    levels = dict.fromkeys((name for name, _ in reversed(RISK_LEVELS)), 0)
    for score in final:
        levels[risk_level(float(score))] += 1
    logger.info(
        "risk zones: %d hexagonal cells (edge %.0f m), %d stored (cell, hour) scores; at the last time step: "
        "max %.1f, %d cells at or above %.1f, levels %s",
        cells, settings.RISK_HEX_EDGE_M, rows, float(final.max()), int((final >= MIN_STORED_SCORE).sum()),
        MIN_STORED_SCORE, ", ".join(f"{name} {count}" for name, count in levels.items()),
    )  # fmt: skip
    return {
        "cells": cells,
        "score_rows": rows,
        "max_score_at_end": float(final.max()),
        "levels_at_end": levels,
        "max_score_overall": float(scores.max()),
    }


def load_risk_series(conn: psycopg.Connection, axis: TimeAxis) -> dict[str, list[int]]:
    """Stored risk of every cell that is ever above zero: ``{cell_id: [integer score per time step]}``.

    Hours without a stored row are 0 (sparse storage); the form the playback bundle uses.
    """
    rows = conn.execute(
        """
        SELECT cell_id,
               array_agg(extract(epoch FROM as_of)::float8 ORDER BY as_of),
               array_agg(risk_score ORDER BY as_of)
        FROM infra.risk_zone_scores
        WHERE as_of >= %s AND as_of <= %s
        GROUP BY cell_id
        ORDER BY cell_id
        """,
        (axis.start, axis.end),
    ).fetchall()
    series: dict[str, list[int]] = {}
    for cell_id, epochs, scores in rows:
        values = np.zeros(axis.count, dtype=np.int64)
        values[axis.cell_indices(np.array(epochs, dtype=float))] = np.floor(np.array(scores, dtype=float) + 0.5)
        series[cell_id] = [int(value) for value in values]
    return series
