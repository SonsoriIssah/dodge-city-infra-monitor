"""Shared helpers of the query modules: row access, rounding rules, text clean-up, GeoJSON builders."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

# Rounding rules of the API (build contract 10.2).
VALUE_DECIMALS = 3
Z_DECIMALS = 2
SCORE_DECIMALS = 3
COORD_DECIMALS = 6
DISTANCE_DECIMALS = 1

BBox = tuple[float, float, float, float]  # west, south, east, north
Params = Mapping[str, Any] | Sequence[Any] | None


class DataNotReadyError(RuntimeError):
    """The database is reachable but does not hold what the request needs yet (answered with 503)."""


class NotFoundError(LookupError):
    """The requested asset, sensor, anomaly or other record does not exist (answered with 404)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def fetch_all(conn: psycopg.Connection, query: str, params: Params = None) -> list[dict[str, Any]]:
    """Run a query and return its rows as dictionaries."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def fetch_one(conn: psycopg.Connection, query: str, params: Params = None) -> dict[str, Any] | None:
    """Run a query and return its first row as a dictionary, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def rounded(value: Any, decimals: int) -> float | None:
    """Round a number for output; None and NaN become None."""
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, decimals)


def text_or_none(value: Any) -> str | None:
    """Missing text is null, never an empty string."""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def scrub(value: Any) -> Any:
    """Recursively replace empty strings by None in a JSON value (stored attributes of real features)."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def bbox_params(bbox: BBox | None) -> dict[str, float | None]:
    """Query parameters ``west``, ``south``, ``east``, ``north`` of an optional bounding box."""
    west, south, east, north = bbox if bbox is not None else (None, None, None, None)
    return {"west": west, "south": south, "east": east, "north": north}


# SQL condition for an optional bounding-box filter on the geometry column given as {geom}.
BBOX_FILTER_SQL = (
    "(%(west)s::float8 IS NULL OR ST_Intersects({geom}, ST_MakeEnvelope("
    "%(west)s::float8, %(south)s::float8, %(east)s::float8, %(north)s::float8, 4326)))"
)


def feature(geometry: Any, properties: dict[str, Any]) -> dict[str, Any]:
    """A GeoJSON feature."""
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def feature_collection(features: list[dict[str, Any]], **members: Any) -> dict[str, Any]:
    """A GeoJSON feature collection; ``members`` are added at the top level (for example ``as_of``)."""
    return {"type": "FeatureCollection", "features": features, **members}


def split_page(rows: list[dict[str, Any]], key: str) -> tuple[int, list[dict[str, Any]]]:
    """Total and page rows of a ``total LEFT JOIN page`` result (an empty page yields one row of NULLs)."""
    total = int(rows[0]["total"]) if rows else 0
    return total, [row for row in rows if row[key] is not None]
