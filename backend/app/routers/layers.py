"""Map layer endpoints: ``/layers/*`` (real geographic data as GeoJSON)."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import Conn
from backend.app.queries import layers as layer_queries
from backend.app.schemas import CanonicalJSONResponse

router = APIRouter(prefix="/layers", tags=["Map layers"])


@router.get(
    "/roads",
    response_model=schemas.FeatureCollection,
    summary="Road network of the base map",
    description=(
        "Every OpenStreetMap highway way clipped to the study area, including service roads and footways that "
        "are not infrastructure assets. Properties: road_id, osm_id, name, highway_class, surface, lanes, "
        "maxspeed, oneway, is_bridge, length_m, source_id."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_roads(conn: Conn) -> Response:
    """Return the road layer."""
    return CanonicalJSONResponse(layer_queries.roads(conn))


@router.get(
    "/study-area",
    response_model=schemas.FeatureCollection,
    summary="Outline of the study area",
    description="The configured study area as one polygon. Properties: slug, name, description, timezone, utm_srid.",
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_study_area(conn: Conn) -> Response:
    """Return the study-area outline."""
    return CanonicalJSONResponse(layer_queries.study_area(conn))


@router.get(
    "/city-boundary",
    response_model=schemas.FeatureCollection,
    summary="City limits (context outline)",
    description=(
        "The incorporated-place boundary from U.S. Census Bureau TIGERweb, drawn as context. It is a "
        "statistical boundary, not a legal land description. Properties: kind, name, source_id. The collection "
        "is empty when the boundary was not available."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_city_boundary(conn: Conn) -> Response:
    """Return the city boundary."""
    return CanonicalJSONResponse(layer_queries.city_boundary(conn))
