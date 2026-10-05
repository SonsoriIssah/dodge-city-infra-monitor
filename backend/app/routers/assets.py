"""Asset endpoints: ``/assets``, ``/assets/{asset_id}``, ``/assets/{asset_id}/health``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import (
    AppSettings,
    AsOf,
    Axis,
    BBoxText,
    Conn,
    Offset,
    OptionalDatetime,
    PlainText,
    datetime_query,
    grid_range,
    parse_bbox,
)
from backend.app.queries import assets as asset_queries
from backend.app.schemas import CanonicalJSONResponse

router = APIRouter(tags=["Assets"])

AssetId = Annotated[
    PlainText, Path(min_length=1, max_length=64, description="Asset id, for example BRG-001.", examples=["BRG-001"])
]


@router.get(
    "/assets",
    response_model=schemas.AssetCollection,
    summary="List infrastructure assets (GeoJSON)",
    description=(
        "The asset registry as a GeoJSON FeatureCollection with `numberMatched` and `numberReturned`: buildings, "
        "roads, bridges, rail, power and street lights recorded in public data, plus the simulated water mains "
        "(`is_simulated: true`). Every feature carries its monitoring summary at the end of the analysed "
        "window: sensor count and types, anomaly count, Derived Asset Health Score and status. Assets without "
        "sensors have no score (`status: not_monitored`). All assets are returned when `limit` is omitted."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def list_assets(
    conn: Conn,
    axis: Axis,
    asset_type: Annotated[schemas.AssetType | None, Query(description="Only assets of this type.")] = None,
    category: Annotated[
        PlainText | None,
        Query(max_length=64, description="Only assets of this category.", examples=["Transportation"]),
    ] = None,
    monitored: Annotated[
        bool | None, Query(description="true: only assets with sensors; false: only assets without sensors.")
    ] = None,
    asset_status: Annotated[
        schemas.AssetStatus | None,
        Query(alias="status", description="Only assets with this health status at the end of the analysed window."),
    ] = None,
    bbox: BBoxText = None,
    limit: Annotated[
        int | None,
        Query(ge=1, le=asset_queries.MAX_ASSETS, description="Maximum number of features; all assets when omitted."),
    ] = None,
    offset: Offset = 0,
) -> Response:
    """List assets as GeoJSON."""
    return CanonicalJSONResponse(
        asset_queries.list_assets(
            conn,
            axis.end,
            asset_type=asset_type,
            category=category,
            monitored=monitored,
            asset_status=asset_status,
            bbox=parse_bbox(bbox),
            limit=limit,
            offset=offset,
        )
    )


@router.get(
    "/assets/{asset_id}",
    response_model=schemas.AssetDetail,
    summary="One asset with its health, sensors, recent anomalies and provenance",
    description=(
        "The asset feature plus: `health` (Derived Asset Health Score, status and the four penalty components "
        "at `as_of`), `sensors` (each with its status and current reading at `as_of`), `recent_anomalies` (the "
        "ten most recent that had started by `as_of`) and `provenance` (the data sources and every recorded "
        "attribute of the feature). The properties of the feature itself stay at the end of the analysed window."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_asset(conn: Conn, axis: Axis, settings: AppSettings, asset_id: AssetId, as_of: AsOf = None) -> Response:
    """Describe one asset at ``as_of``."""
    return CanonicalJSONResponse(
        asset_queries.asset_detail(conn, asset_id, axis, axis.resolve(as_of), settings.PROXIMITY_RADIUS_M)
    )


@router.get(
    "/assets/{asset_id}/health",
    response_model=schemas.AssetHealthSeries,
    summary="Health history of one monitored asset (columnar)",
    description=(
        "The Derived Asset Health Score of a monitored asset at every time step between `start` and `end`, as "
        "parallel arrays: score, status (one character per step: n normal, w watch, r at_risk, c critical), "
        "the four penalties, active anomalies and reporting sensors. Answers 404 for an asset without sensors, "
        "which has no score."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_asset_health(
    conn: Conn,
    axis: Axis,
    asset_id: AssetId,
    start: Annotated[OptionalDatetime, datetime_query("First time step; default: the start of the window.")] = None,
    end: Annotated[OptionalDatetime, datetime_query("Last time step; default: the end of the window.")] = None,
) -> Response:
    """Return the health series of one asset."""
    first, last = grid_range(axis, start, end)
    return CanonicalJSONResponse(asset_queries.asset_health_series(conn, asset_id, axis, first, last))
