"""Database pool, request dependencies and validated query parameters of the API.

All database access is synchronous psycopg 3 through one connection pool per application. Read endpoints
get a read-only REPEATABLE READ transaction, so every query of a request sees the same state of the data.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from psycopg_pool import ConnectionPool
from pydantic import AfterValidator, BeforeValidator

from backend.app import schemas
from backend.app.queries.common import BBox
from backend.app.queries.playback import PlaybackCache
from backend.app.timeutil import iso_z, parse_datetime
from pipeline.analysis import status
from pipeline.config import Settings
from pipeline.db.connection import connection_pool

HEALTH_CHECK_TIMEOUT_S = 2.0
MAX_RADIUS_M = 5000.0
DATETIME_EXAMPLE = "2026-09-20T12:00:00Z"


# --- database ---------------------------------------------------------------------------------------------------
class Database:
    """Owns the connection pool of one application.

    The pool is opened with ``wait=False``: the application starts while the database is down, and requests
    made in the meantime fail with ``PoolTimeout`` (answered with 503). The lifespan opens and closes it; it
    is also opened on first use when the ASGI server runs without lifespan events.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    def open(self) -> ConnectionPool:
        """Return the open pool, creating it when needed."""
        with self._lock:
            if self._pool is None:
                pool = connection_pool(self._settings)
                pool.open(wait=False)
                self._pool = pool
            return self._pool

    def close(self) -> None:
        """Close the pool; a later ``open`` creates a new one."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    @contextmanager
    def connection(self, *, read_only: bool, timeout: float | None = None) -> Iterator[psycopg.Connection]:
        """A pooled connection inside one transaction (committed on success, rolled back on error)."""
        with self.open().connection(timeout=timeout) as conn:
            conn.read_only = read_only
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ if read_only else None
            yield conn


def get_app_settings(request: Request) -> Settings:
    """Settings of the application that serves the request."""
    return request.app.state.settings


def get_database(request: Request) -> Database:
    """Database of the application that serves the request."""
    return request.app.state.database


def get_playback_cache(request: Request) -> PlaybackCache:
    """Playback cache of the application that serves the request."""
    return request.app.state.playback_cache


def get_conn(request: Request) -> Iterator[psycopg.Connection]:
    """Read-only connection for one request."""
    with get_database(request).connection(read_only=True) as conn:
        yield conn


def get_write_conn(request: Request) -> Iterator[psycopg.Connection]:
    """Read-write connection for one request (ingestion)."""
    with get_database(request).connection(read_only=False) as conn:
        yield conn


Conn = Annotated[psycopg.Connection, Depends(get_conn)]
WriteConn = Annotated[psycopg.Connection, Depends(get_write_conn)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
Cache = Annotated[PlaybackCache, Depends(get_playback_cache)]


def get_axis(conn: Conn) -> status.TimeAxis:
    """Time axis of the latest detection run (``NoDetectionRunError`` -> 503 when there is none)."""
    return status.load_time_axis(conn)


Axis = Annotated[status.TimeAxis, Depends(get_axis)]


# --- validated query parameters ---------------------------------------------------------------------------------
def query_error(name: str, message: str, value: Any) -> RequestValidationError:
    """A 422 answer for one query parameter, in FastAPI's own error format."""
    return RequestValidationError([{"type": "value_error", "loc": ("query", name), "msg": message, "input": value}])


def _to_datetime(value: Any) -> Any:
    """Parse an ISO 8601 query value; naive values are UTC. Other inputs are left to pydantic."""
    return parse_datetime(value) if isinstance(value, str) else value


def _split_commas(value: Any) -> Any:
    """Accept ``a,b`` as well as repeated parameters; an empty list means "no filter"."""
    if value is None:
        return None
    raw = [value] if isinstance(value, str) else list(value)
    parts = [part.strip() for item in raw for part in str(item).split(",")]
    return [part for part in parts if part] or None


def _no_control_characters(value: str) -> str:
    """Refuse control characters in an id or a text filter (``ValueError`` -> 422)."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("control characters are not allowed")
    return value


# Ids and text filters are bound as query parameters. No stored value contains a control character and
# PostgreSQL text cannot hold NUL, so such input is refused as invalid (422) before it reaches the database.
PlainText = Annotated[str, AfterValidator(_no_control_characters)]
OptionalDatetime = Annotated[datetime | None, BeforeValidator(_to_datetime)]
AsOf = Annotated[
    OptionalDatetime,
    Query(
        description=(
            "Point in time, ISO 8601 (a value without an offset is UTC). It is floored to the time step and "
            "clamped to the analysed window; the response echoes the effective value. Default: the end of the "
            "window."
        ),
        examples=[DATETIME_EXAMPLE],
    ),
]
SeverityList = Annotated[list[schemas.Severity] | None, BeforeValidator(_split_commas)]
SensorTypeList = Annotated[list[schemas.SensorType] | None, BeforeValidator(_split_commas)]
BBoxText = Annotated[
    str | None,
    Query(
        description="Bounding box as west,south,east,north in WGS84 degrees.",
        examples=["-100.025,37.750,-100.010,37.758"],
        max_length=120,
    ),
]
Longitude = Annotated[
    float, Query(ge=-180.0, le=180.0, description="Longitude in WGS84 degrees.", examples=[-100.0172])
]
Latitude = Annotated[float, Query(ge=-90.0, le=90.0, description="Latitude in WGS84 degrees.", examples=[37.7528])]
Offset = Annotated[int, Query(ge=0, description="Number of items to skip.")]


def datetime_query(description: str) -> Any:
    """``Query`` metadata of an optional ISO 8601 datetime parameter."""
    return Query(description=f"{description} ISO 8601; a value without an offset is UTC.", examples=[DATETIME_EXAMPLE])


def parse_bbox(value: str | None) -> BBox | None:
    """Parse ``west,south,east,north``; 422 when it is not four numbers describing a box."""
    if value is None or not value.strip():
        return None
    try:
        west, south, east, north = (float(part) for part in value.split(","))
    except ValueError:
        raise query_error("bbox", "bbox must be four numbers: west,south,east,north", value) from None
    if not all(map(math.isfinite, (west, south, east, north))):
        raise query_error("bbox", "bbox values must be finite numbers", value)
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        raise query_error("bbox", "bbox must satisfy -180 <= west < east <= 180 and -90 <= south < north <= 90", value)
    return (west, south, east, north)


def grid_range(axis: status.TimeAxis, start: datetime | None, end: datetime | None) -> tuple[int, int]:
    """Indices of the grid points for ``start`` .. ``end`` (floored, clamped; defaults: the whole axis)."""
    first = 0 if start is None else axis.index_of(start)
    last = axis.index_of(end)
    if last < first:
        raise query_error("end", "end must not be before start", iso_z(end))
    return first, last
