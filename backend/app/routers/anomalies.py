"""Anomaly endpoints: ``/anomalies``, ``/anomalies/{anomaly_id}``, ``/simulation-events``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import (
    MAX_RADIUS_M,
    AppSettings,
    AsOf,
    Axis,
    BBoxText,
    Conn,
    Offset,
    OptionalDatetime,
    PlainText,
    SensorTypeList,
    SeverityList,
    datetime_query,
    parse_bbox,
    query_error,
)
from backend.app.queries import anomalies as anomaly_queries
from backend.app.queries import assets as asset_queries
from backend.app.queries import sensors as sensor_queries
from backend.app.schemas import CanonicalJSONResponse
from backend.app.timeutil import iso_z
from pipeline.config import DATA_NOTICE

router = APIRouter(tags=["Anomalies"])

DEFAULT_ANOMALY_LIMIT = 500
MAX_ANOMALY_LIMIT = 1000
INCLUDE_NEARBY = "nearby_assets"


@router.get(
    "/anomalies",
    response_model=schemas.AnomalyList,
    summary="List detected anomalies",
    description=(
        "Anomalies found by the Prototype Anomaly Detection (a retrospective batch analysis of simulated "
        "readings) that had started by `as_of`, with the observed and expected value, robust z-score, anomaly "
        "score and its components, severity, detection method and a plain-language explanation. `status` is "
        "evaluated at `as_of`: active while `started_at <= as_of <= ended_at`, resolved afterwards. "
        "`nearby_asset_count` counts the other assets within the proximity radius; `include=nearby_assets` "
        "lists them. `start` and `end` filter on `started_at`."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def list_anomalies(
    conn: Conn,
    axis: Axis,
    settings: AppSettings,
    severity: Annotated[
        SeverityList, Query(description="Only these severities; a comma-separated list is accepted.")
    ] = None,
    sensor_type: Annotated[
        SensorTypeList, Query(description="Only these sensor types; a comma-separated list is accepted.")
    ] = None,
    asset_id: Annotated[
        PlainText | None, Query(min_length=1, max_length=64, description="Only anomalies on this asset.")
    ] = None,
    sensor_id: Annotated[
        PlainText | None, Query(min_length=1, max_length=64, description="Only anomalies of this sensor.")
    ] = None,
    anomaly_status: Annotated[
        schemas.AnomalyStatus | None, Query(alias="status", description="Only anomalies with this status at `as_of`.")
    ] = None,
    start: Annotated[OptionalDatetime, datetime_query("Only anomalies that started at or after this time.")] = None,
    end: Annotated[OptionalDatetime, datetime_query("Only anomalies that started at or before this time.")] = None,
    as_of: AsOf = None,
    bbox: BBoxText = None,
    include: Annotated[
        schemas.AnomalyInclude | None,
        Query(description="`nearby_assets` adds the list of assets within the proximity radius to every item."),
    ] = None,
    sort: Annotated[
        schemas.AnomalySort,
        Query(description="Order of the list; `severity` puts critical first, `-` means descending."),
    ] = anomaly_queries.DEFAULT_SORT,
    limit: Annotated[
        int, Query(ge=1, le=MAX_ANOMALY_LIMIT, description="Maximum number of items.")
    ] = DEFAULT_ANOMALY_LIMIT,
    offset: Offset = 0,
) -> Response:
    """List anomalies visible at ``as_of``."""
    if start is not None and end is not None and end < start:
        raise query_error("end", "end must not be before start", iso_z(end))
    if asset_id is not None:
        asset_queries.require_asset(conn, asset_id)
    if sensor_id is not None:
        sensor_queries.require_sensor(conn, sensor_id)
    moment = axis.resolve(as_of)
    total, items = anomaly_queries.list_anomalies(
        conn,
        moment,
        settings.PROXIMITY_RADIUS_M,
        severity=severity,
        sensor_type=sensor_type,
        asset_id=asset_id,
        sensor_id=sensor_id,
        anomaly_status=anomaly_status,
        start=start,
        end=end,
        bbox=parse_bbox(bbox),
        include_nearby=include == INCLUDE_NEARBY,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return CanonicalJSONResponse(
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "as_of": iso_z(moment),
            "data_notice": DATA_NOTICE,
            "items": items,
        }
    )


@router.get(
    "/anomalies/{anomaly_id}",
    response_model=schemas.AnomalyDetail,
    summary="One anomaly with the infrastructure around it and its cluster",
    description=(
        "The anomaly (status at the end of the analysed window) plus `nearby_assets`, the other assets within "
        "`radius_m` metres of the sensor, nearest first, and `cluster`, the co-occurrence cluster it belongs to "
        "(null when it is in none). A cluster is descriptive: anomalies close in space and time, not a causal "
        "finding."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_anomaly(
    conn: Conn,
    axis: Axis,
    settings: AppSettings,
    anomaly_id: Annotated[
        PlainText,
        Path(min_length=1, max_length=64, description="Anomaly id, for example ANM-0001.", examples=["ANM-0001"]),
    ],
    radius_m: Annotated[
        float | None,
        Query(gt=0, le=MAX_RADIUS_M, description="Proximity radius in metres; default: PROXIMITY_RADIUS_M."),
    ] = None,
) -> Response:
    """Describe one anomaly."""
    radius = settings.PROXIMITY_RADIUS_M if radius_m is None else radius_m
    return CanonicalJSONResponse(anomaly_queries.anomaly_detail(conn, anomaly_id, axis.end, radius))


@router.get(
    "/simulation-events",
    response_model=schemas.SimulationEventList,
    summary="Ground truth of the simulator",
    description=(
        "The events the simulator put into the readings: injected abnormal events (`is_anomaly: true`, with "
        "sensor and asset) and benign regional events such as simulated rain or a simulated hot spell "
        "(`is_anomaly: false`, no sensor). The detector never reads this table; it is used only to score a "
        "detection run."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def list_simulation_events(
    conn: Conn,
    is_anomaly: Annotated[
        bool | None, Query(description="true: injected abnormal events; false: benign regional events.")
    ] = None,
) -> Response:
    """List the simulator's ground truth."""
    return CanonicalJSONResponse(anomaly_queries.simulation_events(conn, is_anomaly))
