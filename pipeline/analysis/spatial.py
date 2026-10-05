"""Reusable spatial queries: proximity, nearest neighbour, within-radius and density (build contract 9).

PostGIS does the work. Distances are metres on the spheroid (``geography``): within a radius is
``ST_DWithin(a.geom::geography, p::geography, r)``, nearest is ``ORDER BY a.geom::geography <-> p::geography``
(a geometry KNN would rank by degrees and return the wrong neighbour at this latitude); both use the
expression indexes on ``(geom::geography)``. Distances are rounded to 0.1 m.

Every function takes the connection and returns plain dictionaries, ready for the API layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

DISTANCE_DECIMALS = 1
# Severity weights of the density and risk layers (low 1, medium 2, high 4, critical 7) as a SQL expression.
SEVERITY_WEIGHT_SQL = (
    "CASE an.severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 4 WHEN 'critical' THEN 7 END"
)


def _rows(conn: psycopg.Connection, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def assets_within_radius(conn: psycopg.Connection, lon: float, lat: float, radius_m: float) -> list[dict[str, Any]]:
    """Assets within ``radius_m`` metres of a point, nearest first (``infra.assets_within_radius``)."""
    return _rows(
        conn,
        """
        SELECT asset_id, asset_type, category, name, is_simulated, distance_m
        FROM infra.assets_within_radius(%(lon)s, %(lat)s, %(radius_m)s)
        """,
        {"lon": lon, "lat": lat, "radius_m": radius_m},
    )


def nearest_asset(
    conn: psycopg.Connection, lon: float, lat: float, asset_type: str | None = None
) -> dict[str, Any] | None:
    """The asset nearest to a point, optionally of one asset type (``infra.nearest_asset``); None if there is none."""
    rows = _rows(
        conn,
        """
        SELECT asset_id, asset_type, category, name, is_simulated, distance_m
        FROM infra.nearest_asset(%(lon)s, %(lat)s, %(asset_type)s)
        """,
        {"lon": lon, "lat": lat, "asset_type": asset_type},
    )
    return rows[0] if rows else None


def sensors_in_asset_area(conn: psycopg.Connection, asset_id: str, buffer_m: float = 25.0) -> list[dict[str, Any]]:
    """Sensors within ``buffer_m`` metres of an asset's geometry, nearest first (``infra.sensors_in_asset_area``)."""
    return _rows(
        conn,
        """
        SELECT sensor_id, asset_id, sensor_type, placement, unit, distance_m
        FROM infra.sensors_in_asset_area(%(asset_id)s, %(buffer_m)s)
        """,
        {"asset_id": asset_id, "buffer_m": buffer_m},
    )


def assets_near_anomaly(
    conn: psycopg.Connection, anomaly_id: str, radius_m: float, include_own_asset: bool = False
) -> list[dict[str, Any]]:
    """Infrastructure near an anomaly: assets within ``radius_m`` of its point, nearest first.

    The asset the anomalous sensor sits on is left out unless ``include_own_asset`` is set.
    """
    return _rows(
        conn,
        f"""
        SELECT a.asset_id,
               a.name,
               a.asset_type,
               a.is_simulated,
               round(ST_Distance(a.geom::geography, an.geom::geography)::numeric, {DISTANCE_DECIMALS})::float8
                   AS distance_m
        FROM infra.anomalies an
        JOIN infra.infrastructure_assets a
          ON ST_DWithin(a.geom::geography, an.geom::geography, %(radius_m)s)
        WHERE an.anomaly_id = %(anomaly_id)s
          AND (%(include_own)s OR a.asset_id <> an.asset_id)
        ORDER BY ST_Distance(a.geom::geography, an.geom::geography), a.asset_id
        """,
        {"anomaly_id": anomaly_id, "radius_m": radius_m, "include_own": include_own_asset},
    )


def nearby_asset_counts(conn: psycopg.Connection, radius_m: float) -> dict[str, int]:
    """Anomaly id -> number of other assets within ``radius_m`` of the anomaly's point (every anomaly listed)."""
    rows = conn.execute(
        """
        SELECT an.anomaly_id, count(a.asset_id)
        FROM infra.anomalies an
        LEFT JOIN infra.infrastructure_assets a
               ON ST_DWithin(a.geom::geography, an.geom::geography, %(radius_m)s)
              AND a.asset_id <> an.asset_id
        GROUP BY an.anomaly_id
        """,
        {"radius_m": radius_m},
    ).fetchall()
    return {row[0]: int(row[1]) for row in rows}


def assets_near_active_anomalies(conn: psycopg.Connection, as_of: datetime, radius_m: float) -> list[dict[str, Any]]:
    """Proximity analysis: assets within ``radius_m`` of a sensor whose anomaly is active at ``as_of``.

    One row per asset with the nearest such anomaly, the number of active anomalies in range and the highest
    severity among them; the assets the anomalous sensors sit on are included (distance 0 or a few metres).
    """
    return _rows(
        conn,
        f"""
        SELECT a.asset_id,
               a.name,
               a.asset_type,
               a.is_simulated,
               count(*)::int AS active_anomalies,
               (ARRAY['low', 'medium', 'high', 'critical'])[
                   max(array_position(ARRAY['low', 'medium', 'high', 'critical'], an.severity))
               ] AS max_severity,
               (array_agg(an.anomaly_id ORDER BY ST_Distance(a.geom::geography, an.geom::geography),
                          an.anomaly_id))[1] AS nearest_anomaly_id,
               round(min(ST_Distance(a.geom::geography, an.geom::geography))::numeric, {DISTANCE_DECIMALS})::float8
                   AS distance_m
        FROM infra.anomalies an
        JOIN infra.infrastructure_assets a
          ON ST_DWithin(a.geom::geography, an.geom::geography, %(radius_m)s)
        WHERE an.started_at <= %(as_of)s::timestamptz AND an.ended_at >= %(as_of)s::timestamptz
        GROUP BY a.asset_id, a.name, a.asset_type, a.is_simulated
        ORDER BY distance_m, a.asset_id
        """,
        {"as_of": as_of, "radius_m": radius_m},
    )


def anomaly_density(
    conn: psycopg.Connection,
    start: datetime | None = None,
    end: datetime | None = None,
    with_geometry: bool = False,
) -> list[dict[str, Any]]:
    """Anomaly density per hexagonal cell of ``infra.risk_zones``: count and severity-weighted count.

    An anomaly counts when its interval overlaps [start, end] (open-ended when a bound is None). Every cell
    is returned, with zeros where there is no anomaly; ``with_geometry`` adds the cell polygon as GeoJSON.
    """
    geometry = ", ST_AsGeoJSON(z.geom, 6)::jsonb AS geometry" if with_geometry else ""
    return _rows(
        conn,
        f"""
        SELECT z.cell_id,
               count(an.anomaly_id)::int AS anomaly_count,
               COALESCE(sum({SEVERITY_WEIGHT_SQL}), 0)::int AS weighted_severity,
               round((count(an.anomaly_id) / (ST_Area(z.geom::geography) / 10000.0))::numeric, 3)::float8
                   AS anomalies_per_hectare
               {geometry}
        FROM infra.risk_zones z
        LEFT JOIN infra.anomalies an
               ON ST_Covers(z.geom, an.geom)
              AND (%(end)s::timestamptz IS NULL OR an.started_at <= %(end)s::timestamptz)
              AND (%(start)s::timestamptz IS NULL OR an.ended_at >= %(start)s::timestamptz)
        GROUP BY z.cell_id, z.geom
        ORDER BY z.cell_id
        """,
        {"start": start, "end": end},
    )


def anomalies_per_asset_type(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Spatial aggregation: anomalies, affected assets and severity-weighted count per asset type."""
    return _rows(
        conn,
        f"""
        SELECT a.asset_type,
               count(an.anomaly_id)::int AS anomaly_count,
               count(DISTINCT an.asset_id)::int AS assets_with_anomalies,
               COALESCE(sum({SEVERITY_WEIGHT_SQL}), 0)::int AS weighted_severity
        FROM infra.infrastructure_assets a
        JOIN infra.anomalies an ON an.asset_id = a.asset_id
        GROUP BY a.asset_type
        ORDER BY anomaly_count DESC, a.asset_type
        """,
        {},
    )
