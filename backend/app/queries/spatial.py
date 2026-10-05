"""Queries of ``/spatial/*``: proximity, nearest asset, sensors around an asset, density, risk zones, clusters.

The proximity and density SQL lives in ``pipeline.analysis.spatial`` (and the SQL functions of migration 002);
this module turns its rows into the response shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from backend.app.queries import anomalies as anomaly_queries
from backend.app.queries import assets as asset_queries
from backend.app.queries import sensors as sensor_queries
from backend.app.queries.common import (
    COORD_DECIMALS,
    DISTANCE_DECIMALS,
    SCORE_DECIMALS,
    NotFoundError,
    feature,
    feature_collection,
    fetch_all,
    rounded,
)
from backend.app.timeutil import iso_z
from pipeline.analysis import spatial as spatial_analysis
from pipeline.analysis.risk_zones import LEVEL_LOW

RISK_ZONES_SQL = f"""
SELECT z.cell_id,
       ST_AsGeoJSON(z.geom, {COORD_DECIMALS})::json AS geometry,
       COALESCE(sc.risk_score, 0)::float8 AS risk_score,
       COALESCE(sc.risk_level, %(lowest_level)s) AS risk_level,
       COALESCE(sc.anomaly_count, 0) AS anomaly_count
FROM infra.risk_zones z
LEFT JOIN infra.risk_zone_scores sc ON sc.cell_id = z.cell_id AND sc.as_of = %(as_of)s::timestamptz
ORDER BY z.cell_id
"""


def _with_distance(asset_feature: dict[str, Any], distance_m: Any) -> dict[str, Any]:
    properties = {**asset_feature["properties"], "distance_m": rounded(distance_m, DISTANCE_DECIMALS)}
    return feature(asset_feature["geometry"], properties)


def assets_within(conn: psycopg.Connection, t_end: datetime, lon: float, lat: float, radius_m: float) -> dict[str, Any]:
    """Assets within ``radius_m`` metres of a point as asset features with ``distance_m``, nearest first."""
    near = spatial_analysis.assets_within_radius(conn, lon, lat, radius_m)
    features = asset_queries.features_by_id(conn, t_end, [row["asset_id"] for row in near])
    found = [
        _with_distance(features[row["asset_id"]], row["distance_m"]) for row in near if row["asset_id"] in features
    ]
    return feature_collection(found, numberMatched=len(found), numberReturned=len(found))


def nearest_asset(
    conn: psycopg.Connection, t_end: datetime, lon: float, lat: float, asset_type: str | None = None
) -> dict[str, Any]:
    """The asset nearest to a point (optionally of one type) as an asset feature with ``distance_m``."""
    near = spatial_analysis.nearest_asset(conn, lon, lat, asset_type)
    features = asset_queries.features_by_id(conn, t_end, [near["asset_id"]]) if near is not None else {}
    if near is None or near["asset_id"] not in features:
        kind = f"of type '{asset_type}' " if asset_type else ""
        raise NotFoundError(f"no asset {kind}in the database")
    return _with_distance(features[near["asset_id"]], near["distance_m"])


def sensors_in_asset_area(conn: psycopg.Connection, as_of: datetime, asset_id: str, buffer_m: float) -> dict[str, Any]:
    """Sensors within ``buffer_m`` metres of an asset's geometry (its own and its neighbours'), nearest first."""
    asset_queries.require_asset(conn, asset_id)
    near = spatial_analysis.sensors_in_asset_area(conn, asset_id, buffer_m)
    _, items = sensor_queries.list_sensors(conn, as_of, sensor_ids=[row["sensor_id"] for row in near])
    by_id = {item["sensor_id"]: item for item in items}
    return {
        "asset_id": asset_id,
        "buffer_m": buffer_m,
        "items": [
            {**by_id[row["sensor_id"]], "distance_m": rounded(row["distance_m"], DISTANCE_DECIMALS)}
            for row in near
            if row["sensor_id"] in by_id
        ],
    }


def anomaly_density(conn: psycopg.Connection, start: datetime | None, end: datetime | None) -> dict[str, Any]:
    """Anomaly count and severity-weighted count per hexagonal cell (every cell; zeros where there is none).

    An anomaly counts when its interval overlaps [start, end]; a missing bound is open-ended.
    """
    cells = spatial_analysis.anomaly_density(conn, start, end, with_geometry=True)
    return feature_collection(
        [
            feature(
                cell["geometry"],
                {
                    "cell_id": cell["cell_id"],
                    "anomaly_count": int(cell["anomaly_count"]),
                    "weighted_severity": int(cell["weighted_severity"]),
                },
            )
            for cell in cells
        ]
    )


def risk_zones(conn: psycopg.Connection, as_of: datetime) -> dict[str, Any]:
    """Risk score, level and active-anomaly count of every hexagonal cell at ``as_of`` (0 where none)."""
    rows = fetch_all(conn, RISK_ZONES_SQL, {"as_of": as_of, "lowest_level": LEVEL_LOW})
    return feature_collection(
        [
            feature(
                row["geometry"],
                {
                    "cell_id": row["cell_id"],
                    "risk_score": rounded(row["risk_score"], SCORE_DECIMALS),
                    "risk_level": row["risk_level"],
                    "anomaly_count": int(row["anomaly_count"]),
                },
            )
            for row in rows
        ],
        as_of=iso_z(as_of),
    )


def clusters(conn: psycopg.Connection) -> dict[str, Any]:
    """Hulls of the co-occurrence clusters of anomalies."""
    records = anomaly_queries.cluster_records(conn)
    return feature_collection([feature(record["geometry"], record["properties"]) for record in records])
