"""Queries of ``/anomalies``, ``/anomalies/{id}`` and ``/simulation-events``; cluster records.

An anomaly is visible at ``as_of`` once it has started; its ``status`` (active / resolved) is evaluated at
``as_of`` with the rules of ``pipeline.analysis.status``. Proximity comes from ``pipeline.analysis.spatial``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql

from backend.app.queries.common import (
    BBOX_FILTER_SQL,
    COORD_DECIMALS,
    DISTANCE_DECIMALS,
    SCORE_DECIMALS,
    VALUE_DECIMALS,
    Z_DECIMALS,
    BBox,
    NotFoundError,
    bbox_params,
    fetch_all,
    rounded,
    split_page,
    text_or_none,
)
from backend.app.timeutil import iso_z
from pipeline.analysis import spatial as spatial_analysis
from pipeline.analysis import status
from pipeline.detection.events import anomaly_label

DEFAULT_SORT = "-started_at"
# Whitelisted orderings of the list (the request value only selects one of these constants).
ORDERINGS: dict[str, str] = {
    "-started_at": "an.started_at DESC, an.anomaly_id DESC",
    "started_at": "an.started_at ASC, an.anomaly_id ASC",
    "-anomaly_score": "an.anomaly_score DESC, an.started_at DESC, an.anomaly_id ASC",
    "severity": (
        "array_position(ARRAY['critical', 'high', 'medium', 'low'], an.severity) ASC, "
        "an.started_at DESC, an.anomaly_id ASC"
    ),
}

ANOMALY_COLUMNS_SQL = f"""
       an.anomaly_id, an.sensor_id, an.asset_id, a.name AS asset_name, a.asset_type, an.sensor_type, s.placement,
       an.anomaly_type, an.started_at, an.ended_at, an.peak_at, an.duration_hours, an.observed_value,
       an.expected_value, an.unit, an.robust_z::float8 AS robust_z, an.anomaly_score::float8 AS anomaly_score,
       an.score_components, an.severity, an.detection_method, an.explanation,
       {status.ANOMALY_STATUS_SQL} AS status, s.is_simulated, an.cluster_id,
       ST_X(an.geom) AS lon, ST_Y(an.geom) AS lat
"""

ANOMALY_FROM_SQL = """
FROM infra.anomalies an
JOIN infra.sensors s ON s.sensor_id = an.sensor_id
JOIN infra.infrastructure_assets a ON a.asset_id = an.asset_id
"""

ANOMALY_LIST_SQL = f"""
WITH filtered AS (
    SELECT row_number() OVER (ORDER BY {{order}}) AS position,
           {ANOMALY_COLUMNS_SQL}
    {ANOMALY_FROM_SQL}
    WHERE {status.ANOMALY_VISIBLE_SQL}
      AND (%(severity)s::text[] IS NULL OR an.severity = ANY(%(severity)s::text[]))
      AND (%(sensor_type)s::text[] IS NULL OR an.sensor_type = ANY(%(sensor_type)s::text[]))
      AND (%(asset_id)s::text IS NULL OR an.asset_id = %(asset_id)s::text)
      AND (%(sensor_id)s::text IS NULL OR an.sensor_id = %(sensor_id)s::text)
      AND (%(status)s::text IS NULL OR {status.ANOMALY_STATUS_SQL} = %(status)s::text)
      AND (%(start)s::timestamptz IS NULL OR an.started_at >= %(start)s::timestamptz)
      AND (%(end)s::timestamptz IS NULL OR an.started_at <= %(end)s::timestamptz)
      AND {BBOX_FILTER_SQL.format(geom="an.geom")}
),
page AS (
    SELECT * FROM filtered ORDER BY position LIMIT %(limit)s OFFSET %(offset)s
)
SELECT t.total, p.*
FROM (SELECT count(*) AS total FROM filtered) t
LEFT JOIN page p ON true
ORDER BY p.position
"""

ANOMALY_ONE_SQL = f"""
SELECT {ANOMALY_COLUMNS_SQL}
{ANOMALY_FROM_SQL}
WHERE an.anomaly_id = %(anomaly_id)s
"""

CLUSTERS_SQL = """
SELECT c.cluster_id, c.n_anomalies, c.n_sensors, c.n_assets, c.sensor_types, c.max_severity,
       c.first_started_at, c.last_ended_at,
       COALESCE((SELECT array_agg(an.anomaly_id ORDER BY an.anomaly_id)
                 FROM infra.anomalies an
                 WHERE an.cluster_id = c.cluster_id), ARRAY[]::text[]) AS anomaly_ids,
       ST_AsGeoJSON(c.geom, 6)::json AS geometry
FROM infra.anomaly_clusters c
WHERE %(cluster_id)s::int IS NULL OR c.cluster_id = %(cluster_id)s::int
ORDER BY c.cluster_id
"""

SIMULATION_EVENTS_SQL = """
SELECT event_id, sensor_id, asset_id, sensor_type, event_type, is_anomaly, started_at, ended_at,
       magnitude::float8 AS magnitude, description
FROM infra.simulation_events
WHERE %(is_anomaly)s::boolean IS NULL OR is_anomaly = %(is_anomaly)s::boolean
ORDER BY event_id
"""


def nearby_item(row: dict[str, Any]) -> dict[str, Any]:
    """An asset near an anomaly in the response shape."""
    return {
        "asset_id": row["asset_id"],
        "name": text_or_none(row["name"]),
        "asset_type": row["asset_type"],
        "distance_m": rounded(row["distance_m"], DISTANCE_DECIMALS),
    }


def nearby_assets(conn: psycopg.Connection, anomaly_id: str, radius_m: float) -> list[dict[str, Any]]:
    """Other assets within ``radius_m`` of the anomaly's point, nearest first."""
    return [nearby_item(row) for row in spatial_analysis.assets_near_anomaly(conn, anomaly_id, radius_m)]


def anomaly_item(row: dict[str, Any], nearby_asset_count: int) -> dict[str, Any]:
    """One anomaly row in the response shape."""
    components = row["score_components"] or {}
    return {
        "anomaly_id": row["anomaly_id"],
        "sensor_id": row["sensor_id"],
        "asset_id": row["asset_id"],
        "asset_name": text_or_none(row["asset_name"]),
        "asset_type": row["asset_type"],
        "sensor_type": row["sensor_type"],
        "placement": row["placement"],
        "anomaly_type": row["anomaly_type"],
        "anomaly_label": anomaly_label(row["anomaly_type"]),
        "started_at": iso_z(row["started_at"]),
        "ended_at": iso_z(row["ended_at"]),
        "peak_at": iso_z(row["peak_at"]),
        "duration_hours": int(row["duration_hours"]),
        "observed_value": rounded(row["observed_value"], VALUE_DECIMALS),
        "expected_value": rounded(row["expected_value"], VALUE_DECIMALS),
        "unit": row["unit"],
        "robust_z": rounded(row["robust_z"], Z_DECIMALS),
        "anomaly_score": rounded(row["anomaly_score"], SCORE_DECIMALS),
        "score_components": {key: rounded(value, SCORE_DECIMALS) for key, value in components.items()},
        "severity": row["severity"],
        "detection_method": row["detection_method"],
        "explanation": row["explanation"],
        "status": row["status"],
        "is_simulated": bool(row["is_simulated"]),
        "cluster_id": row["cluster_id"],
        "lon": rounded(row["lon"], COORD_DECIMALS),
        "lat": rounded(row["lat"], COORD_DECIMALS),
        "nearby_asset_count": nearby_asset_count,
    }


def list_anomalies(
    conn: psycopg.Connection,
    as_of: datetime,
    radius_m: float,
    *,
    severity: list[str] | None = None,
    sensor_type: list[str] | None = None,
    asset_id: str | None = None,
    sensor_id: str | None = None,
    anomaly_status: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    bbox: BBox | None = None,
    include_nearby: bool = False,
    sort: str = DEFAULT_SORT,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Anomalies visible at ``as_of`` that match the filters: (total, page of anomaly items).

    ``start`` / ``end`` filter on ``started_at``; ``anomaly_status`` is evaluated at ``as_of``; ``radius_m``
    is the radius of the nearby-asset count (and of ``nearby_assets`` when ``include_nearby`` is set).
    """
    query = sql.SQL(ANOMALY_LIST_SQL).format(order=sql.SQL(ORDERINGS[sort]))
    params = {
        "as_of": as_of,
        "severity": severity or None,
        "sensor_type": sensor_type or None,
        "asset_id": asset_id,
        "sensor_id": sensor_id,
        "status": anomaly_status,
        "start": start,
        "end": end,
        "limit": limit,
        "offset": offset,
        **bbox_params(bbox),
    }
    total, rows = split_page(fetch_all(conn, query, params), "anomaly_id")
    if not rows:
        return total, []
    if include_nearby:
        items = []
        for row in rows:
            nearby = nearby_assets(conn, row["anomaly_id"], radius_m)
            items.append({**anomaly_item(row, len(nearby)), "nearby_assets": nearby})
        return total, items
    counts = spatial_analysis.nearby_asset_counts(conn, radius_m)
    return total, [anomaly_item(row, counts.get(row["anomaly_id"], 0)) for row in rows]


def cluster_records(conn: psycopg.Connection, cluster_id: int | None = None) -> list[dict[str, Any]]:
    """Co-occurrence clusters (all, or one): response properties plus the hull under ``geometry``."""
    records = []
    for row in fetch_all(conn, CLUSTERS_SQL, {"cluster_id": cluster_id}):
        records.append(
            {
                "geometry": row["geometry"],
                "properties": {
                    "cluster_id": int(row["cluster_id"]),
                    "n_anomalies": int(row["n_anomalies"]),
                    "n_sensors": int(row["n_sensors"]),
                    "n_assets": int(row["n_assets"]),
                    "sensor_types": list(row["sensor_types"] or []),
                    "max_severity": row["max_severity"],
                    "first_started_at": iso_z(row["first_started_at"]),
                    "last_ended_at": iso_z(row["last_ended_at"]),
                    "anomaly_ids": list(row["anomaly_ids"]),
                },
            }
        )
    return records


def anomaly_detail(conn: psycopg.Connection, anomaly_id: str, as_of: datetime, radius_m: float) -> dict[str, Any]:
    """One anomaly with the assets within ``radius_m`` and its cluster; ``NotFoundError`` when unknown."""
    rows = fetch_all(conn, ANOMALY_ONE_SQL, {"anomaly_id": anomaly_id, "as_of": as_of})
    if not rows:
        raise NotFoundError(f"anomaly '{anomaly_id}' not found; list the anomalies with GET /anomalies")
    row = rows[0]
    nearby = nearby_assets(conn, anomaly_id, radius_m)
    cluster = None
    if row["cluster_id"] is not None:
        records = cluster_records(conn, int(row["cluster_id"]))
        cluster = records[0]["properties"] if records else None
    return {**anomaly_item(row, len(nearby)), "nearby_assets": nearby, "cluster": cluster}


def simulation_events(conn: psycopg.Connection, is_anomaly: bool | None = None) -> dict[str, Any]:
    """The simulator's ground truth: injected abnormal events and benign regional events."""
    items = [
        {
            "event_id": int(row["event_id"]),
            "sensor_id": row["sensor_id"],
            "asset_id": row["asset_id"],
            "sensor_type": text_or_none(row["sensor_type"]),
            "event_type": row["event_type"],
            "is_anomaly": bool(row["is_anomaly"]),
            "started_at": iso_z(row["started_at"]),
            "ended_at": iso_z(row["ended_at"]),
            "magnitude": rounded(row["magnitude"], VALUE_DECIMALS),
            "description": text_or_none(row["description"]),
        }
        for row in fetch_all(conn, SIMULATION_EVENTS_SQL, {"is_anomaly": is_anomaly})
    ]
    return {"total": len(items), "items": items}
