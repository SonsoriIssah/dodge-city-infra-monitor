"""Sensor endpoints: ``/sensors``, ``/sensors/{sensor_id}``, ``/sensor-readings``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import (
    AppSettings,
    AsOf,
    Axis,
    Conn,
    Offset,
    OptionalDatetime,
    PlainText,
    SensorTypeList,
    datetime_query,
    grid_range,
    query_error,
)
from backend.app.queries import assets as asset_queries
from backend.app.queries import sensors as sensor_queries
from backend.app.schemas import CanonicalJSONResponse
from backend.app.timeutil import iso_z

router = APIRouter(tags=["Sensors"])

DEFAULT_SENSOR_LIMIT = 1000
MAX_SENSOR_LIMIT = 5000
STATUS_RULE = (
    "Status at `as_of`, first match wins: `offline` (no reading within the sampling interval), `anomaly` (an "
    "anomaly of the sensor is active), `warning` (the current reading is flagged or outside the warning "
    "limits), else `normal`."
)


@router.get(
    "/sensors",
    response_model=schemas.SensorList,
    summary="List sensors with their status and current reading",
    description=(
        "Sensors of the monitoring network (Simulated Sensor Data unless another source is configured) with "
        f"their location, host asset, status and current reading at `as_of`. {STATUS_RULE} `anomaly_count` "
        "counts the sensor's anomalies that had started by `as_of`."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def list_sensors(
    conn: Conn,
    axis: Axis,
    sensor_type: Annotated[
        SensorTypeList, Query(description="Only these sensor types; a comma-separated list is accepted.")
    ] = None,
    asset_id: Annotated[
        PlainText | None, Query(min_length=1, max_length=64, description="Only the sensors of this asset.")
    ] = None,
    sensor_status: Annotated[
        schemas.SensorStatus | None, Query(alias="status", description="Only sensors with this status at `as_of`.")
    ] = None,
    as_of: AsOf = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_SENSOR_LIMIT, description="Maximum number of items.")
    ] = DEFAULT_SENSOR_LIMIT,
    offset: Offset = 0,
) -> Response:
    """List sensors at ``as_of``."""
    if asset_id is not None:
        asset_queries.require_asset(conn, asset_id)
    moment = axis.resolve(as_of)
    total, items = sensor_queries.list_sensors(
        conn,
        moment,
        sensor_types=sensor_type,
        asset_id=asset_id,
        sensor_status=sensor_status,
        limit=limit,
        offset=offset,
    )
    return CanonicalJSONResponse(
        {"total": total, "limit": limit, "offset": offset, "as_of": iso_z(moment), "items": items}
    )


@router.get(
    "/sensors/{sensor_id}",
    response_model=schemas.SensorDetail,
    summary="One sensor with its limits, baseline and anomalies",
    description=(
        "The sensor at `as_of` plus its warning and critical limits, the robust scale of its baseline (in the "
        "detector's work domain: ln(mm/s) for vibration, native units otherwise) with the floor that applies, "
        "and its anomalies that had started by `as_of`, newest first."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_sensor(
    conn: Conn,
    axis: Axis,
    settings: AppSettings,
    sensor_id: Annotated[
        PlainText,
        Path(min_length=1, max_length=64, description="Sensor id, for example VIB-001.", examples=["VIB-001"]),
    ],
    as_of: AsOf = None,
) -> Response:
    """Describe one sensor at ``as_of``."""
    return CanonicalJSONResponse(
        sensor_queries.sensor_detail(conn, sensor_id, axis.resolve(as_of), settings.PROXIMITY_RADIUS_M)
    )


@router.get(
    "/sensor-readings",
    response_model=schemas.ReadingsRecords | schemas.ReadingsColumns,
    summary="Historical readings of one sensor with the detector's output",
    description=(
        "Readings of one sensor between `start` and `end` (default: the analysed window) with the expected "
        "value, the expected band, the robust z-score and the flag of every reading. `shape=records` returns "
        "a list of readings; `shape=columns` returns parallel arrays aligned to the time grid (null where "
        "the sensor did not report; `flagged` lists the indices of the flagged readings; `start` and `end` "
        "are floored to the time step and clamped to the window). At most 5000 points per call."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_sensor_readings(
    conn: Conn,
    axis: Axis,
    sensor_id: Annotated[
        PlainText, Query(min_length=1, max_length=64, description="Sensor id (required).", examples=["VIB-001"])
    ],
    start: Annotated[OptionalDatetime, datetime_query("First timestamp; default: the start of the window.")] = None,
    end: Annotated[OptionalDatetime, datetime_query("Last timestamp; default: the end of the window.")] = None,
    shape: Annotated[schemas.ReadingShape, Query(description="Layout of the response.")] = "records",
    limit: Annotated[
        int, Query(ge=1, le=sensor_queries.MAX_READING_POINTS, description="Maximum number of points.")
    ] = sensor_queries.MAX_READING_POINTS,
) -> Response:
    """Return the readings of one sensor."""
    if shape == "columns":
        first, last = grid_range(axis, start, end)
        last = min(last, first + limit - 1)
        return CanonicalJSONResponse(sensor_queries.readings_columns(conn, sensor_id, axis, first, last))
    if start is not None and end is not None and end < start:
        raise query_error("end", "end must not be before start", iso_z(end))
    # A single bound outside the analysed window selects an empty range (no readings), not an error about
    # the bound the client did not send.
    window_start = axis.start if start is None else start
    window_end = axis.end if end is None else end
    if start is None:
        window_start = min(window_start, window_end)
    if end is None:
        window_end = max(window_end, window_start)
    return CanonicalJSONResponse(sensor_queries.readings_records(conn, sensor_id, window_start, window_end, limit))
