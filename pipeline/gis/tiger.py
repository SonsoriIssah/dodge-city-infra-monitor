"""U.S. Census Bureau TIGERweb: boundary of the incorporated place (drawn as a context outline).

The place is selected by GEOID (``TIGER_PLACE_GEOID``); when that setting is ``auto`` the place containing the
centre of the study area is used. The layer id has been renumbered by the Census Bureau in the past, so the
downloader asserts the layer name before trusting the result and records the layer description as vintage.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from pipeline.config import BBox
from pipeline.gis.sources import (
    SOURCE_REGISTRY,
    file_mtime_iso,
    http_client,
    make_entry,
    read_json,
    sha256_file,
    utc_now_iso,
    write_bytes_atomic,
)

logger = logging.getLogger(__name__)

SOURCE_ID = "tiger"
FILE_NAME = "city_boundary.geojson"
LAYER_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4"
QUERY_URL = LAYER_URL + "/query"
EXPECTED_LAYER_NAME = "Incorporated Places"
AUTO_PLACE = "auto"  # TIGER_PLACE_GEOID value meaning "the place that contains the centre of the study area"


def query_params(geoid: str, bbox: BBox) -> dict[str, str]:
    """Query parameters: by GEOID when given; ``auto`` (or empty) = the place containing the study-area centre."""
    params = {"outFields": "*", "outSR": "4326", "f": "geojson"}
    geoid = geoid.strip()
    if geoid and geoid.lower() != AUTO_PLACE:
        if not geoid.isdigit():  # the value is placed in a WHERE clause: digits only, nothing to escape
            raise ValueError(f"TIGER_PLACE_GEOID must be digits only or '{AUTO_PLACE}'")
        params["where"] = f"GEOID='{geoid}'"
    else:
        lon, lat = bbox.center
        params.update(
            {
                "where": "1=1",
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
            }
        )
    return params


def validate_boundary(payload: Any) -> list[dict[str, Any]]:
    """Features of a boundary GeoJSON, or ``ValueError`` when it holds no polygon feature."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError("response is not a GeoJSON FeatureCollection")
    features = [
        f
        for f in payload.get("features", [])
        if (f.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon")
    ]
    if not features:
        raise ValueError("no polygon feature returned for the requested place")
    return features


def describe_cache(path: Path, previous: dict[str, Any] | None) -> dict[str, Any]:
    """SOURCES.json entry for an existing cache file (dynamic fields come from the previous entry)."""
    previous = previous or {}
    info = SOURCE_REGISTRY[SOURCE_ID]
    payload = read_json(path)
    return make_entry(
        source_id=SOURCE_ID,
        file=FILE_NAME,
        url=previous.get("url") or QUERY_URL,
        retrieved_at=previous.get("retrieved_at") or file_mtime_iso(path),
        sha256=sha256_file(path),
        feature_count=len(payload.get("features", [])),
        license=info.license,
        attribution_text=info.attribution_text,
        vintage=previous.get("vintage"),
        terms_url=info.terms_url,
    )


def download(
    raw_dir: Path,
    bbox: BBox,
    geoid: str,
    *,
    refresh: bool = False,
    previous: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Make sure ``raw_dir/city_boundary.geojson`` exists; return its SOURCES.json entry or None (layer absent).

    Cache is used unless ``refresh``. A failure keeps the existing cache with a warning; without a cache the
    boundary layer is skipped with a warning.
    """
    path = raw_dir / FILE_NAME
    if path.exists() and not refresh:
        logger.info("using cached city boundary: %s", path.name)
        return describe_cache(path, previous)

    own_client = client is None
    http = client or http_client()
    try:
        layer = http.get(LAYER_URL, params={"f": "json"})
        layer.raise_for_status()
        layer_info = layer.json()
        if layer_info.get("name") != EXPECTED_LAYER_NAME:
            raise ValueError(
                f"TIGERweb layer 4 is named {layer_info.get('name')!r}, expected {EXPECTED_LAYER_NAME!r} "
                "(the service layout changed)"
            )
        response = http.get(QUERY_URL, params=query_params(geoid, bbox))
        response.raise_for_status()
        payload = json.loads(response.content)
        features = validate_boundary(payload)
    except (httpx.HTTPError, ValueError) as exc:
        if path.exists():
            logger.warning("city boundary download failed, keeping the cached file: %s", exc)
            return describe_cache(path, previous)
        logger.warning("city boundary download failed and there is no cache; the layer is skipped: %s", exc)
        return None
    finally:
        if own_client:
            http.close()

    write_bytes_atomic(path, response.content)
    info = SOURCE_REGISTRY[SOURCE_ID]
    entry = make_entry(
        source_id=SOURCE_ID,
        file=FILE_NAME,
        url=str(response.request.url),
        retrieved_at=utc_now_iso(),
        sha256=sha256_file(path),
        feature_count=len(features),
        license=info.license,
        attribution_text=info.attribution_text,
        vintage=layer_info.get("description") or None,
        terms_url=info.terms_url,
    )
    names = ", ".join(str((f.get("properties") or {}).get("NAME")) for f in features)
    logger.info("downloaded city boundary: %s (%s)", names, entry["vintage"])
    return entry
