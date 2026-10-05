"""Spatial analysis endpoints: ``/spatial/*`` (PostGIS proximity, density, risk zones, clusters)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import (
    MAX_RADIUS_M,
    AppSettings,
    AsOf,
    Axis,
    Conn,
    Latitude,
    Longitude,
    OptionalDatetime,
    PlainText,
    datetime_query,
    query_error,
)
from backend.app.queries import spatial as spatial_queries
from backend.app.schemas import CanonicalJSONResponse
from backend.app.timeutil import iso_z

router = APIRouter(prefix="/spatial", tags=["Spatial analysis"])

DEFAULT_BUFFER_M = 25.0


@router.get(
    "/assets-within",
    response_model=schemas.AssetCollection,
    summary="Assets within a radius of a point",
    description=(
        "Asset features within `radius_m` metres of a point, nearest first, each with `distance_m` (metres on "
        "the spheroid, 0.1 m). PostGIS `ST_DWithin` on geography."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_assets_within(
    conn: Conn,
    axis: Axis,
    settings: AppSettings,
    lon: Longitude,
    lat: Latitude,
    radius_m: Annotated[
        float | None,
        Query(gt=0, le=MAX_RADIUS_M, description="Search radius in metres; default: PROXIMITY_RADIUS_M."),
    ] = None,
) -> Response:
    """Find the assets around a point."""
    radius = settings.PROXIMITY_RADIUS_M if radius_m is None else radius_m
    return CanonicalJSONResponse(spatial_queries.assets_within(conn, axis.end, lon, lat, radius))


@router.get(
    "/nearest-asset",
    response_model=schemas.AssetFeature,
    summary="Nearest asset to a point",
    description=(
        "The asset nearest to a point, optionally of one asset type, as an asset feature with `distance_m`. "
        "Nearest-neighbour search on geography (ordering by geometry would rank by degrees)."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_nearest_asset(
    conn: Conn,
    axis: Axis,
    lon: Longitude,
    lat: Latitude,
    asset_type: Annotated[schemas.AssetType | None, Query(description="Only consider assets of this type.")] = None,
) -> Response:
    """Find the nearest asset."""
    return CanonicalJSONResponse(spatial_queries.nearest_asset(conn, axis.end, lon, lat, asset_type))


@router.get(
    "/sensors-in-asset-area",
    response_model=schemas.SensorsInArea,
    summary="Sensors within an infrastructure area",
    description=(
        "Sensors within `buffer_m` metres of an asset's geometry, nearest first: its own sensors and those of "
        "neighbouring assets. Each item is a sensor (status at the end of the analysed window) with `distance_m`."
    ),
    responses=schemas.LOOKUP_RESPONSES,
)
def get_sensors_in_asset_area(
    conn: Conn,
    axis: Axis,
    asset_id: Annotated[
        PlainText, Query(min_length=1, max_length=64, description="Asset id (required).", examples=["BRG-001"])
    ],
    buffer_m: Annotated[
        float, Query(ge=0, le=MAX_RADIUS_M, description="Buffer around the asset geometry in metres.")
    ] = DEFAULT_BUFFER_M,
) -> Response:
    """Find the sensors around an asset."""
    return CanonicalJSONResponse(spatial_queries.sensors_in_asset_area(conn, axis.end, asset_id, buffer_m))


@router.get(
    "/anomaly-density",
    response_model=schemas.DensityCollection,
    summary="Anomaly density per hexagonal cell",
    description=(
        "Every cell of the hexagonal grid with the number of anomalies whose sensor lies in it and their "
        "severity-weighted count (low 1, medium 2, high 4, critical 7). An anomaly counts when its interval "
        "overlaps `start`..`end`; a missing bound is open-ended."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_anomaly_density(
    conn: Conn,
    start: Annotated[OptionalDatetime, datetime_query("Start of the period; default: no lower bound.")] = None,
    end: Annotated[OptionalDatetime, datetime_query("End of the period; default: no upper bound.")] = None,
) -> Response:
    """Aggregate anomalies per hexagonal cell."""
    if start is not None and end is not None and end < start:
        raise query_error("end", "end must not be before start", iso_z(end))
    return CanonicalJSONResponse(spatial_queries.anomaly_density(conn, start, end))


@router.get(
    "/risk-zones",
    response_model=schemas.RiskCollection,
    summary="Risk zones at a point in time",
    description=(
        "Every cell of the hexagonal grid with its risk score (0..100), level (low, moderate, high, very_high) "
        "and the number of anomalies active in it at `as_of`; 0 where there is none. The score is a kernel-"
        "weighted sum of anomaly severities that decays after an anomaly ends. It is derived from simulated "
        "anomalies and says nothing about risk on the ground."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_risk_zones(conn: Conn, axis: Axis, as_of: AsOf = None) -> Response:
    """Return the risk layer at ``as_of``."""
    return CanonicalJSONResponse(spatial_queries.risk_zones(conn, axis.resolve(as_of)))


@router.get(
    "/clusters",
    response_model=schemas.ClusterCollection,
    summary="Co-occurrence clusters of anomalies",
    description=(
        "Hulls of the groups of anomalies that are close in space and time (spatio-temporal DBSCAN), with the "
        "number of anomalies, sensors and assets, the sensor types, the highest severity, the time span and the "
        "anomaly ids. A cluster is descriptive, not a causal finding."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_clusters(conn: Conn) -> Response:
    """Return the anomaly clusters."""
    return CanonicalJSONResponse(spatial_queries.clusters(conn))
