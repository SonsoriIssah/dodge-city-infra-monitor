"""Queries of ``/assets``, ``/assets/{id}`` and ``/assets/{id}/health``.

Asset features carry the monitoring summary at the end of the analysed window (``health_score``, ``status``,
``anomaly_count``); ``/assets/{id}?as_of=`` adds the health at ``as_of`` from the stored hourly scores, the
same rows the playback bundle is built from. Assets without sensors have no score (``not_monitored``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from backend.app.queries import anomalies as anomaly_queries
from backend.app.queries import meta as meta_queries
from backend.app.queries import sensors as sensor_queries
from backend.app.queries.common import (
    BBOX_FILTER_SQL,
    COORD_DECIMALS,
    BBox,
    DataNotReadyError,
    NotFoundError,
    bbox_params,
    feature,
    fetch_all,
    fetch_one,
    rounded,
    scrub,
    split_page,
    text_or_none,
)
from backend.app.timeutil import iso_z
from pipeline.analysis import status
from pipeline.analysis.health import ASSET_STATUS_CHARS, PENALTY_DECIMALS, STATUS_NOT_MONITORED

MAX_ASSETS = 20_000
RECENT_ANOMALIES = 10
ADDRESS_PREFIX = "addr:"
MISSING_STATUS_CHAR = "-"
NOT_ANALYSED = "asset health has not been computed for the latest detection run; run scripts/analyze_spatial.py"
# Stored attributes shown on the feature of each asset type (always present there, null when not recorded).
TYPE_PROPERTIES: dict[str, tuple[str, ...]] = {
    "building": ("height_m", "height_source", "building_type", "levels", "footprint_m2"),
    "road": ("highway_class", "surface", "lanes", "length_m"),
    "bridge": ("structure_kind", "length_m", "nbi"),
    "water_main": ("host_road_id",),
}
# Sources that contribute recorded attributes to an asset besides its own source.
NBI_SOURCE = "nbi"
LIDAR_SOURCE = "usgs_3dep"
LIDAR_HEIGHT_SOURCE = "lidar_3dep"

ASSETS_SQL = f"""
WITH base AS (
    SELECT a.asset_id, a.asset_type, a.category, a.name, a.is_simulated, a.source_id, a.properties,
           ST_AsGeoJSON(a.geom, {COORD_DECIMALS})::json AS geometry,
           ST_X(a.centroid) AS lon, ST_Y(a.centroid) AS lat,
           s.sensor_count IS NOT NULL AS monitored,
           COALESCE(s.sensor_count, 0) AS sensor_count,
           COALESCE(s.sensor_types, ARRAY[]::text[]) AS sensor_types,
           COALESCE(n.anomaly_count, 0) AS anomaly_count,
           CASE WHEN s.sensor_count IS NOT NULL THEN h.health_score END AS health_score,
           CASE WHEN s.sensor_count IS NOT NULL THEN h.status ELSE '{STATUS_NOT_MONITORED}' END AS status
    FROM infra.infrastructure_assets a
    LEFT JOIN (
        SELECT se.asset_id, count(*)::int AS sensor_count,
               array_agg(DISTINCT se.sensor_type ORDER BY se.sensor_type) AS sensor_types
        FROM infra.sensors se
        GROUP BY se.asset_id
    ) s ON s.asset_id = a.asset_id
    LEFT JOIN (
        SELECT an.asset_id, count(*)::int AS anomaly_count
        FROM infra.anomalies an
        GROUP BY an.asset_id
    ) n ON n.asset_id = a.asset_id
    LEFT JOIN infra.asset_health h ON h.asset_id = a.asset_id AND h.as_of = %(t_end)s::timestamptz
    WHERE (%(asset_type)s::text IS NULL OR a.asset_type = %(asset_type)s::text)
      AND (%(category)s::text IS NULL OR a.category = %(category)s::text)
      AND (%(asset_ids)s::text[] IS NULL OR a.asset_id = ANY(%(asset_ids)s::text[]))
      AND {BBOX_FILTER_SQL.format(geom="a.geom")}
),
filtered AS (
    SELECT * FROM base
    WHERE (%(monitored)s::boolean IS NULL OR monitored = %(monitored)s::boolean)
      AND (%(status)s::text IS NULL OR status = %(status)s::text)
),
page AS (
    SELECT * FROM filtered ORDER BY asset_id LIMIT %(limit)s OFFSET %(offset)s
)
SELECT t.total, p.*
FROM (SELECT count(*) AS total FROM filtered) t
LEFT JOIN page p ON true
ORDER BY p.asset_id
"""

HEALTH_AT_SQL = """
SELECT health_score, status, frequency_penalty::float8 AS frequency_penalty,
       severity_penalty::float8 AS severity_penalty, reading_penalty::float8 AS reading_penalty,
       sensor_penalty::float8 AS sensor_penalty
FROM infra.asset_health
WHERE asset_id = %(asset_id)s AND as_of = %(as_of)s::timestamptz
"""

HEALTH_SERIES_SQL = """
SELECT extract(epoch FROM as_of)::float8, health_score, status, frequency_penalty::float8,
       severity_penalty::float8, reading_penalty::float8, sensor_penalty::float8, active_anomalies,
       sensors_reporting, sensors_total
FROM infra.asset_health
WHERE asset_id = %(asset_id)s AND as_of >= %(start)s::timestamptz AND as_of <= %(end)s::timestamptz
ORDER BY as_of
"""


def asset_properties(row: dict[str, Any]) -> dict[str, Any]:
    """Feature properties of one asset row: the common keys, the keys of its type and its OSM address tags."""
    if row["status"] is None:  # a monitored asset without a stored score: stage 6 has not run for this run
        raise DataNotReadyError(NOT_ANALYSED)
    stored = scrub(row["properties"] or {})
    properties = {
        "asset_id": row["asset_id"],
        "asset_type": row["asset_type"],
        "category": row["category"],
        "name": text_or_none(row["name"]),
        "is_simulated": bool(row["is_simulated"]),
        "source_id": row["source_id"],
        "monitored": bool(row["monitored"]),
        "sensor_count": int(row["sensor_count"]),
        "sensor_types": list(row["sensor_types"]),
        "anomaly_count": int(row["anomaly_count"]),
        "health_score": None if row["health_score"] is None else int(row["health_score"]),
        "status": row["status"],
        "centroid": [rounded(row["lon"], COORD_DECIMALS), rounded(row["lat"], COORD_DECIMALS)],
    }
    for key in TYPE_PROPERTIES.get(row["asset_type"], ()):
        properties[key] = stored.get(key)
    for key in sorted(stored):
        if key.startswith(ADDRESS_PREFIX) and stored[key] is not None:
            properties[key] = stored[key]
    return properties


def asset_feature(row: dict[str, Any]) -> dict[str, Any]:
    """One asset row as a GeoJSON feature."""
    return feature(row["geometry"], asset_properties(row))


def asset_rows(
    conn: psycopg.Connection,
    t_end: datetime,
    *,
    asset_type: str | None = None,
    category: str | None = None,
    monitored: bool | None = None,
    asset_status: str | None = None,
    bbox: BBox | None = None,
    asset_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    """Asset rows that match the filters, ordered by id: (total, page). ``t_end`` dates the health columns."""
    params = {
        "t_end": t_end,
        "asset_type": asset_type,
        "category": category,
        "monitored": monitored,
        "status": asset_status,
        "asset_ids": asset_ids,
        "limit": MAX_ASSETS if limit is None else limit,
        "offset": offset,
        **bbox_params(bbox),
    }
    return split_page(fetch_all(conn, ASSETS_SQL, params), "asset_id")


def list_assets(conn: psycopg.Connection, t_end: datetime, **filters: Any) -> dict[str, Any]:
    """Body of ``GET /assets``: a GeoJSON feature collection with ``numberMatched`` and ``numberReturned``."""
    total, rows = asset_rows(conn, t_end, **filters)
    return {
        "type": "FeatureCollection",
        "features": [asset_feature(row) for row in rows],
        "numberMatched": total,
        "numberReturned": len(rows),
    }


def features_by_id(conn: psycopg.Connection, t_end: datetime, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Asset features of the given ids, keyed by asset id."""
    if not asset_ids:
        return {}
    _, rows = asset_rows(conn, t_end, asset_ids=asset_ids)
    return {row["asset_id"]: asset_feature(row) for row in rows}


def asset_exists(conn: psycopg.Connection, asset_id: str) -> bool:
    """True when the asset is in the registry."""
    found = conn.execute("SELECT 1 FROM infra.infrastructure_assets WHERE asset_id = %s", (asset_id,)).fetchone()
    return found is not None


def require_asset(conn: psycopg.Connection, asset_id: str) -> None:
    """Raise ``NotFoundError`` when the asset is not in the registry."""
    if not asset_exists(conn, asset_id):
        raise NotFoundError(f"asset '{asset_id}' not found; list the assets with GET /assets")


def _health_at(conn: psycopg.Connection, asset_id: str, as_of: datetime, monitored: bool) -> dict[str, Any]:
    if not monitored:
        return {"score": None, "status": STATUS_NOT_MONITORED, "components": None}
    row = fetch_one(conn, HEALTH_AT_SQL, {"asset_id": asset_id, "as_of": as_of})
    if row is None:
        raise DataNotReadyError(NOT_ANALYSED)
    return {
        "score": int(row["health_score"]),
        "status": row["status"],
        "components": {
            key: rounded(row[key], PENALTY_DECIMALS)
            for key in ("frequency_penalty", "severity_penalty", "reading_penalty", "sensor_penalty")
        },
    }


def _provenance(conn: psycopg.Connection, row: dict[str, Any]) -> dict[str, Any]:
    stored = scrub(row["properties"] or {})
    source_ids = [row["source_id"]]
    if stored.get("nbi") is not None:
        source_ids.append(NBI_SOURCE)
    if stored.get("height_source") == LIDAR_HEIGHT_SOURCE:
        source_ids.append(LIDAR_SOURCE)
    by_id = {item["source_id"]: item for item in meta_queries.data_sources(conn, source_ids)}
    return {
        "is_simulated": bool(row["is_simulated"]),
        "sources": [by_id[source_id] for source_id in dict.fromkeys(source_ids) if source_id in by_id],
        "attributes": stored,
    }


def asset_detail(
    conn: psycopg.Connection, asset_id: str, axis: status.TimeAxis, as_of: datetime, radius_m: float
) -> dict[str, Any]:
    """Body of ``GET /assets/{id}``: the feature, its health at ``as_of``, sensors, recent anomalies, provenance."""
    _, rows = asset_rows(conn, axis.end, asset_ids=[asset_id])
    if not rows:
        raise NotFoundError(f"asset '{asset_id}' not found; list the assets with GET /assets")
    row = rows[0]
    _, sensors = sensor_queries.list_sensors(conn, as_of, asset_id=asset_id)
    _, recent = anomaly_queries.list_anomalies(conn, as_of, radius_m, asset_id=asset_id, limit=RECENT_ANOMALIES)
    return {
        **asset_feature(row),
        "as_of": iso_z(as_of),
        "health": _health_at(conn, asset_id, as_of, bool(row["monitored"])),
        "sensors": sensors,
        "recent_anomalies": recent,
        "provenance": _provenance(conn, row),
    }


def asset_health_series(
    conn: psycopg.Connection, asset_id: str, axis: status.TimeAxis, first: int, last: int
) -> dict[str, Any]:
    """Health of one monitored asset for the grid points ``first`` .. ``last`` (indices), columnar.

    ``NotFoundError`` for an unknown asset and for an asset without sensors (it has no score).
    """
    require_asset(conn, asset_id)
    params = {"asset_id": asset_id, "start": axis.at(first), "end": axis.at(last)}
    rows = conn.execute(HEALTH_SERIES_SQL, params).fetchall()
    if not rows:
        monitored = conn.execute("SELECT 1 FROM infra.sensors WHERE asset_id = %s LIMIT 1", (asset_id,)).fetchone()
        if monitored is not None:
            raise DataNotReadyError(NOT_ANALYSED)
        raise NotFoundError(
            f"asset '{asset_id}' is not monitored (it has no sensors), so it has no health score; "
            "monitored assets are listed by GET /assets?monitored=true"
        )
    count = last - first + 1
    names = ("health_score", "frequency_penalty", "severity_penalty", "reading_penalty", "sensor_penalty",
             "active_anomalies", "sensors_reporting")  # fmt: skip
    columns: dict[str, list[Any]] = {name: [None] * count for name in names}
    chars = [MISSING_STATUS_CHAR] * count
    for epoch, score, state, frequency, severity, reading, sensor, active, reporting, _ in rows:
        cell = round((epoch - axis.at(first).timestamp()) / axis.step_seconds)
        if not 0 <= cell < count:
            continue
        columns["health_score"][cell] = int(score)
        columns["frequency_penalty"][cell] = rounded(frequency, PENALTY_DECIMALS)
        columns["severity_penalty"][cell] = rounded(severity, PENALTY_DECIMALS)
        columns["reading_penalty"][cell] = rounded(reading, PENALTY_DECIMALS)
        columns["sensor_penalty"][cell] = rounded(sensor, PENALTY_DECIMALS)
        columns["active_anomalies"][cell] = None if active is None else int(active)
        columns["sensors_reporting"][cell] = None if reporting is None else int(reporting)
        chars[cell] = ASSET_STATUS_CHARS[state]
    return {
        "asset_id": asset_id,
        "start": iso_z(axis.at(first)),
        "step_minutes": axis.step_seconds / 60.0,
        "count": count,
        "health_score": columns["health_score"],
        "status": "".join(chars),
        "frequency_penalty": columns["frequency_penalty"],
        "severity_penalty": columns["severity_penalty"],
        "reading_penalty": columns["reading_penalty"],
        "sensor_penalty": columns["sensor_penalty"],
        "active_anomalies": columns["active_anomalies"],
        "sensors_reporting": columns["sensors_reporting"],
        "sensors_total": int(rows[-1][9] or 0),
    }
