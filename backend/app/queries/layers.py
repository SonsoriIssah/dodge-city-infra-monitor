"""Queries of ``/layers/*``: the base-map layers of real geographic data as GeoJSON."""

from __future__ import annotations

from typing import Any

import psycopg

from backend.app.queries.common import (
    COORD_DECIMALS,
    DISTANCE_DECIMALS,
    feature,
    feature_collection,
    fetch_all,
    rounded,
    text_or_none,
)

ROADS_SQL = f"""
SELECT road_id, osm_id, name, highway_class, surface, lanes, maxspeed, oneway, is_bridge,
       length_m::float8 AS length_m, source_id, ST_AsGeoJSON(geom, {COORD_DECIMALS})::json AS geometry
FROM infra.roads
ORDER BY road_id
"""

STUDY_AREA_SQL = f"""
SELECT slug, name, description, timezone, utm_srid, ST_AsGeoJSON(geom, {COORD_DECIMALS})::json AS geometry
FROM infra.study_areas
ORDER BY study_area_id
"""

BOUNDARIES_SQL = f"""
SELECT kind, name, source_id, ST_AsGeoJSON(geom, {COORD_DECIMALS})::json AS geometry
FROM infra.reference_boundaries
ORDER BY boundary_id
"""


def roads(conn: psycopg.Connection) -> dict[str, Any]:
    """Every road of the base map (all OSM highway classes, clipped to the study area)."""
    return feature_collection(
        [
            feature(
                row["geometry"],
                {
                    "road_id": int(row["road_id"]),
                    "osm_id": int(row["osm_id"]),
                    "name": text_or_none(row["name"]),
                    "highway_class": row["highway_class"],
                    "surface": text_or_none(row["surface"]),
                    "lanes": row["lanes"],
                    "maxspeed": text_or_none(row["maxspeed"]),
                    "oneway": row["oneway"],
                    "is_bridge": bool(row["is_bridge"]),
                    "length_m": rounded(row["length_m"], DISTANCE_DECIMALS),
                    "source_id": row["source_id"],
                },
            )
            for row in fetch_all(conn, ROADS_SQL)
        ]
    )


def study_area(conn: psycopg.Connection) -> dict[str, Any]:
    """Outline of the study area."""
    return feature_collection(
        [
            feature(
                row["geometry"],
                {
                    "slug": row["slug"],
                    "name": row["name"],
                    "description": text_or_none(row["description"]),
                    "timezone": row["timezone"],
                    "utm_srid": int(row["utm_srid"]),
                },
            )
            for row in fetch_all(conn, STUDY_AREA_SQL)
        ]
    )


def city_boundary(conn: psycopg.Connection) -> dict[str, Any]:
    """Context outline(s) of the city (empty when the boundary source was not available)."""
    return feature_collection(
        [
            feature(
                row["geometry"],
                {"kind": row["kind"], "name": text_or_none(row["name"]), "source_id": row["source_id"]},
            )
            for row in fetch_all(conn, BOUNDARIES_SQL)
        ]
    )
