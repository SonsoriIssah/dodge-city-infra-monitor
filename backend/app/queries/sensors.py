"""Queries of ``/sensors``, ``/sensors/{id}`` and ``/sensor-readings``.

The status of a sensor at ``as_of`` and its current reading come from ``status.SENSOR_STATUS_SQL``, the
single implementation of those rules; this module only joins the descriptive columns to it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import psycopg

from backend.app.queries import anomalies as anomaly_queries
from backend.app.queries.common import (
    COORD_DECIMALS,
    VALUE_DECIMALS,
    Z_DECIMALS,
    NotFoundError,
    fetch_all,
    fetch_one,
    rounded,
    split_page,
    text_or_none,
)
from backend.app.timeutil import iso_z
from pipeline.analysis import status
from pipeline.config import DATA_NOTICE

MAX_READING_POINTS = 5000

SENSOR_ITEMS_SQL = f"""
WITH st AS ({status.SENSOR_STATUS_SQL}),
filtered AS (
    SELECT s.sensor_id, s.asset_id, a.name AS asset_name, a.asset_type, s.sensor_type, s.placement, s.unit,
           s.description, s.is_simulated, s.source, ST_X(s.geom) AS lon, ST_Y(s.geom) AS lat,
           st.status, st.reading_ts, st.reading_value, st.reading_status, st.expected,
           st.robust_z::float8 AS robust_z,
           (SELECT count(*) FROM infra.anomalies an
            WHERE an.sensor_id = s.sensor_id AND {status.ANOMALY_VISIBLE_SQL}) AS anomaly_count
    FROM st
    JOIN infra.sensors s ON s.sensor_id = st.sensor_id
    JOIN infra.infrastructure_assets a ON a.asset_id = s.asset_id
    WHERE (%(sensor_types)s::text[] IS NULL OR s.sensor_type = ANY(%(sensor_types)s::text[]))
      AND (%(asset_id)s::text IS NULL OR s.asset_id = %(asset_id)s::text)
      AND (%(sensor_ids)s::text[] IS NULL OR s.sensor_id = ANY(%(sensor_ids)s::text[]))
      AND (%(status)s::text IS NULL OR st.status = %(status)s::text)
),
page AS (
    SELECT * FROM filtered ORDER BY sensor_id LIMIT %(limit)s OFFSET %(offset)s
)
SELECT t.total, p.*
FROM (SELECT count(*) AS total FROM filtered) t
LEFT JOIN page p ON true
ORDER BY p.sensor_id
"""

SENSOR_HEAD_SQL = """
SELECT s.sensor_id, s.sensor_type, s.unit, s.placement, s.is_simulated, s.source,
       th.warn_low, th.warn_high, th.crit_low, th.crit_high,
       (SELECT r.params -> 'sensor_baselines' -> s.sensor_id
        FROM infra.detection_runs r
        WHERE r.finished_at IS NOT NULL
        ORDER BY r.run_id DESC
        LIMIT 1) AS baseline
FROM infra.sensors s
JOIN infra.sensor_thresholds th ON th.sensor_type = s.sensor_type AND th.placement = s.placement
WHERE s.sensor_id = %(sensor_id)s
"""

# Readings of one sensor with the detector's output, in time order. The lower bound is exclusive or
# inclusive depending on the caller (%(after)s is the instant just before the first wanted reading).
READINGS_SQL = """
SELECT r.ts, extract(epoch FROM r.ts)::float8, r.value, r.status, sc.expected, sc.expected_low, sc.expected_high,
       sc.robust_z::float8, COALESCE(sc.flagged, false)
FROM infra.sensor_readings r
LEFT JOIN infra.reading_scores sc ON sc.sensor_id = r.sensor_id AND sc.ts = r.ts
WHERE r.sensor_id = %(sensor_id)s
  AND (CASE WHEN %(inclusive)s::boolean THEN r.ts >= %(start)s::timestamptz ELSE r.ts > %(start)s::timestamptz END)
  AND r.ts <= %(end)s::timestamptz
ORDER BY r.ts
LIMIT %(limit)s
"""


def sensor_item(row: dict[str, Any]) -> dict[str, Any]:
    """One row of ``SENSOR_ITEMS_SQL`` in the response shape."""
    latest = None
    if row["reading_ts"] is not None:
        latest = {
            "ts": iso_z(row["reading_ts"]),
            "value": rounded(row["reading_value"], VALUE_DECIMALS),
            "status": row["reading_status"],
            "expected": rounded(row["expected"], VALUE_DECIMALS),
            "robust_z": rounded(row["robust_z"], Z_DECIMALS),
        }
    return {
        "sensor_id": row["sensor_id"],
        "asset_id": row["asset_id"],
        "asset_name": text_or_none(row["asset_name"]),
        "asset_type": row["asset_type"],
        "sensor_type": row["sensor_type"],
        "placement": row["placement"],
        "unit": row["unit"],
        "description": text_or_none(row["description"]),
        "is_simulated": bool(row["is_simulated"]),
        "source": row["source"],
        "lon": rounded(row["lon"], COORD_DECIMALS),
        "lat": rounded(row["lat"], COORD_DECIMALS),
        "status": row["status"],
        "latest": latest,
        "anomaly_count": int(row["anomaly_count"]),
    }


def list_sensors(
    conn: psycopg.Connection,
    as_of: datetime,
    *,
    sensor_types: list[str] | None = None,
    asset_id: str | None = None,
    sensor_ids: list[str] | None = None,
    sensor_status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Sensors with their status and current reading at ``as_of``: (total, page of sensor items)."""
    params = {
        "as_of": as_of,
        "sensor_types": sensor_types or None,
        "asset_id": asset_id,
        "sensor_ids": sensor_ids,
        "status": sensor_status,
        "limit": limit,
        "offset": offset,
    }
    total, rows = split_page(fetch_all(conn, SENSOR_ITEMS_SQL, params), "sensor_id")
    return total, [sensor_item(row) for row in rows]


def sensor_exists(conn: psycopg.Connection, sensor_id: str) -> bool:
    """True when the sensor is registered."""
    found = conn.execute("SELECT 1 FROM infra.sensors WHERE sensor_id = %s", (sensor_id,)).fetchone()
    return found is not None


def require_sensor(conn: psycopg.Connection, sensor_id: str) -> None:
    """Raise ``NotFoundError`` when the sensor is not registered."""
    if not sensor_exists(conn, sensor_id):
        raise NotFoundError(f"sensor '{sensor_id}' not found; list the sensors with GET /sensors")


def _thresholds(head: dict[str, Any]) -> dict[str, Any]:
    return {key: head[key] for key in ("warn_low", "warn_high", "crit_low", "crit_high")}


def _sensor_head(conn: psycopg.Connection, sensor_id: str) -> dict[str, Any]:
    head = fetch_one(conn, SENSOR_HEAD_SQL, {"sensor_id": sensor_id})
    if head is None:
        raise NotFoundError(f"sensor '{sensor_id}' not found; list the sensors with GET /sensors")
    return head


def sensor_detail(conn: psycopg.Connection, sensor_id: str, as_of: datetime, radius_m: float) -> dict[str, Any]:
    """One sensor at ``as_of`` with its limits, baseline and anomalies (newest first)."""
    head = _sensor_head(conn, sensor_id)
    _, items = list_sensors(conn, as_of, sensor_ids=[sensor_id])
    _, anomalies = anomaly_queries.list_anomalies(conn, as_of, radius_m, sensor_id=sensor_id, limit=None)
    baseline = head["baseline"]
    return {
        **items[0],
        "as_of": iso_z(as_of),
        "thresholds": _thresholds(head),
        "baseline": None if not baseline else {"scale": baseline.get("scale"), "floor": baseline.get("floor")},
        "anomalies": anomalies,
    }


def _reading_head(head: dict[str, Any]) -> dict[str, Any]:
    return {
        "sensor_id": head["sensor_id"],
        "sensor_type": head["sensor_type"],
        "unit": head["unit"],
        "placement": head["placement"],
        "is_simulated": bool(head["is_simulated"]),
        "source": head["source"],
        "data_notice": DATA_NOTICE,
    }


def readings_records(
    conn: psycopg.Connection, sensor_id: str, start: datetime, end: datetime, limit: int = MAX_READING_POINTS
) -> dict[str, Any]:
    """Readings with ``start <= ts <= end`` as a list of records, oldest first (at most ``limit``)."""
    head = _sensor_head(conn, sensor_id)
    params = {"sensor_id": sensor_id, "start": start, "end": end, "inclusive": True, "limit": limit}
    readings = [
        {
            "ts": iso_z(ts),
            "value": rounded(value, VALUE_DECIMALS),
            "status": reading_status,
            "expected": rounded(expected, VALUE_DECIMALS),
            "expected_low": rounded(low, VALUE_DECIMALS),
            "expected_high": rounded(high, VALUE_DECIMALS),
            "robust_z": rounded(z, Z_DECIMALS),
            "flagged": bool(flagged),
        }
        for ts, _, value, reading_status, expected, low, high, z, flagged in conn.execute(READINGS_SQL, params)
    ]
    return {
        **_reading_head(head),
        "start": iso_z(start),
        "end": iso_z(end),
        "count": len(readings),
        "readings": readings,
    }


def readings_columns(
    conn: psycopg.Connection, sensor_id: str, axis: status.TimeAxis, first: int, last: int
) -> dict[str, Any]:
    """Readings as arrays aligned to the time grid, for the grid points ``first`` .. ``last`` (indices).

    A grid point holds the reading whose timestamp lies in (t - step, t] - the rule of
    ``TimeAxis.cell_indices`` - and null when there is none. ``flagged`` lists the positions of the flagged
    readings in the returned arrays.
    """
    head = _sensor_head(conn, sensor_id)
    count = last - first + 1
    columns: dict[str, list[float | None]] = {
        name: [None] * count for name in ("value", "expected", "expected_low", "expected_high", "robust_z")
    }
    flagged_cells = [False] * count
    params = {
        "sensor_id": sensor_id,
        "start": axis.at(first) - axis.step,
        "end": axis.at(last),
        "inclusive": False,
        "limit": None,
    }
    rows = conn.execute(READINGS_SQL, params).fetchall()
    if rows:
        cells = axis.cell_indices(np.array([row[1] for row in rows], dtype=float)) - first
        for cell, (_, _, value, _, expected, low, high, z, flagged) in zip(cells.tolist(), rows, strict=True):
            if not 0 <= cell < count:
                continue
            columns["value"][cell] = rounded(value, VALUE_DECIMALS)
            columns["expected"][cell] = rounded(expected, VALUE_DECIMALS)
            columns["expected_low"][cell] = rounded(low, VALUE_DECIMALS)
            columns["expected_high"][cell] = rounded(high, VALUE_DECIMALS)
            columns["robust_z"][cell] = rounded(z, Z_DECIMALS)
            flagged_cells[cell] = bool(flagged)
    return {
        **_reading_head(head),
        "thresholds": _thresholds(head),
        "start": iso_z(axis.at(first)),
        "step_minutes": axis.step_seconds / 60.0,
        "count": count,
        **columns,
        "flagged": [cell for cell, flag in enumerate(flagged_cells) if flag],
    }
