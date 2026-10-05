"""FHWA National Bridge Inventory (NBI): download for the study area and parsing of the recorded fields.

Field names follow the 1995 FHWA Recording and Coding Guide (item number as suffix). The parsed attributes
are shown as recorded; they are never turned into a health score, a status colour or a safety statement.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pipeline import geo
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

SOURCE_ID = "nbi"
FILE_NAME = "nbi_bridges.json"
LAYER_URL = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
    "NTAD_National_Bridge_Inventory/FeatureServer/0"
)
QUERY_URL = LAYER_URL + "/query"

# Fields this project reads. The downloader refuses a response that lacks any of them (FHWA is moving the
# inventory to the SNBI schema, which renames items) and keeps the existing cache instead.
REQUIRED_FIELDS: tuple[str, ...] = (
    "STRUCTURE_NUMBER_008",
    "FACILITY_CARRIED_007",
    "FEATURES_DESC_006A",
    "LOCATION_009",
    "OWNER_022",
    "YEAR_BUILT_027",
    "YEAR_RECONSTRUCTED_106",
    "ADT_029",
    "YEAR_ADT_030",
    "STRUCTURE_LEN_MT_049",
    "DATE_OF_INSPECT_090",
    "DECK_COND_058",
    "SUPERSTRUCTURE_COND_059",
    "SUBSTRUCTURE_COND_060",
    "CULVERT_COND_062",
    "BRIDGE_CONDITION",
    "LOWEST_RATING",
    "LAT_016",
    "LONG_017",
)

OWNER_LABELS = {"01": "State Highway Agency", "04": "City or Municipal Highway Agency"}
# BRIDGE_CONDITION is FHWA's Good / Fair / Poor classification. The label always carries its basis so it cannot
# be read as a safety verdict or as this project's own assessment.
BRIDGE_CONDITION_LABELS = {"G": "Good", "F": "Fair", "P": "Poor"}
BRIDGE_CONDITION_BASIS = "FHWA classification from the lowest component rating"
NOT_APPLICABLE = "N"
LOCATION_MISMATCH_M = 500.0
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip

_AS_OF = re.compile(r"as of ([A-Z][a-z]+ \d{1,2}, \d{4})")
_WORD_START = re.compile(r"(?<![0-9A-Za-z'])[a-z]")


# --- download -----------------------------------------------------------------------------------------------
def query_params(bbox: BBox) -> dict[str, str]:
    """Query parameters selecting every record whose point lies in the bounding box."""
    return {
        "where": "1=1",
        "geometry": f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "f": "json",
    }


def missing_fields(payload: dict[str, Any]) -> list[str]:
    """Required field names that the response does not provide."""
    present = {f.get("name") for f in payload.get("fields", []) if isinstance(f, dict)}
    for feature in payload.get("features", [])[:1]:
        present.update(feature.get("attributes", {}).keys())
    return [name for name in REQUIRED_FIELDS if name not in present]


def vintage_from_description(description: str | None) -> tuple[str | None, str]:
    """``(vintage, attribution_text)`` from the layer description ("... dataset is as of June 20, 2025 ...")."""
    base = "FHWA National Bridge Inventory"
    match = _AS_OF.search(description or "")
    if match:
        return f"data as of {match.group(1)}", f"{base} (data as of {match.group(1)}), distributed by USDOT/BTS NTAD"
    return None, f"{base}, distributed by USDOT/BTS NTAD"


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
        attribution_text=previous.get("attribution_text") or info.attribution_text,
        vintage=previous.get("vintage"),
        terms_url=info.terms_url,
    )


def download(
    raw_dir: Path,
    bbox: BBox,
    *,
    refresh: bool = False,
    previous: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Make sure ``raw_dir/nbi_bridges.json`` exists; return its SOURCES.json entry or None (layer absent).

    Cache is used unless ``refresh``. Any failure (network, service error, unexpected field names) keeps the
    existing cache with a warning; without a cache the layer is skipped with a warning.
    """
    path = raw_dir / FILE_NAME
    if path.exists() and not refresh:
        logger.info("using cached NBI data: %s", path.name)
        return describe_cache(path, previous)

    own_client = client is None
    http = client or http_client()
    try:
        layer = http.get(LAYER_URL, params={"f": "json"})
        layer.raise_for_status()
        layer_info = layer.json()
        response = http.get(QUERY_URL, params=query_params(bbox))
        response.raise_for_status()
        payload = json.loads(response.content)
        if "error" in payload or "error" in layer_info:
            raise ValueError(f"service error: {payload.get('error') or layer_info.get('error')}")
        if not isinstance(payload.get("features"), list):
            raise ValueError("response has no 'features' list")
        missing = missing_fields(payload)
        if missing:
            raise ValueError(f"expected NBI field names are missing: {', '.join(missing)}")
    except (httpx.HTTPError, ValueError) as exc:
        if path.exists():
            logger.warning("NBI download failed, keeping the cached file: %s", exc)
            return describe_cache(path, previous)
        logger.warning("NBI download failed and there is no cache; the bridge inventory layer is skipped: %s", exc)
        return None
    finally:
        if own_client:
            http.close()

    if payload.get("exceededTransferLimit"):
        logger.warning("NBI response was truncated by the service (exceededTransferLimit); use a smaller area")
    write_bytes_atomic(path, response.content)
    vintage, attribution = vintage_from_description(layer_info.get("description"))
    info = SOURCE_REGISTRY[SOURCE_ID]
    entry = make_entry(
        source_id=SOURCE_ID,
        file=FILE_NAME,
        url=str(response.request.url),
        retrieved_at=utc_now_iso(),
        sha256=sha256_file(path),
        feature_count=len(payload["features"]),
        license=info.license,
        attribution_text=attribution,
        vintage=vintage,
        terms_url=info.terms_url,
    )
    logger.info("downloaded %d NBI records (%s)", entry["feature_count"], vintage or "vintage unknown")
    return entry


# --- parsing ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class NbiRecord:
    """One parsed NBI record."""

    structure_number: str
    lon: float
    lat: float
    location_check: str  # ok | mismatch | unverified
    recorded_lon: float | None  # decoded from LAT_016 / LONG_017
    recorded_lat: float | None
    check_distance_m: float | None  # distance between the recorded coordinates and the point geometry
    fields: dict[str, Any] = field(default_factory=dict)  # parsed attributes stored as properties.nbi

    @property
    def is_culvert(self) -> bool:
        """A record is a culvert when item 62 (culvert condition) carries a rating."""
        return self.fields.get("culvert_condition") not in (None, NOT_APPLICABLE)


def dms_to_decimal(lat_016: Any, long_017: Any) -> tuple[float, float] | None:
    """Decode items 16/17 (DDMMSSss north, DDDMMSSss west) to decimal ``(lon, lat)``; None when unusable."""

    def decode(raw: Any, width: int) -> float | None:
        text = str(raw).strip() if raw is not None else ""
        if not text.isdigit() or len(text) > width or int(text) == 0:
            return None
        text = text.zfill(width)
        degrees, minutes, hundredths = int(text[: width - 6]), int(text[width - 6 : width - 4]), int(text[width - 4 :])
        if minutes >= 60 or hundredths >= 6000:
            return None
        return degrees + minutes / 60.0 + (hundredths / 100.0) / 3600.0

    lat, lon = decode(lat_016, 8), decode(long_017, 9)
    if lat is None or lon is None or lat > 90.0 or lon > 180.0:
        return None
    return (-lon, lat)


def parse_inspection_date(raw: Any, pivot_year: int) -> tuple[int, int] | None:
    """Item 90 is MMYY with the leading zero dropped ('223' = February 2023). Returns ``(year, month)``."""
    text = str(raw).strip() if raw is not None else ""
    if not text.isdigit() or not 1 <= len(text) <= 4:
        return None
    text = text.zfill(4)
    month, yy = int(text[:2]), int(text[2:])
    if not 1 <= month <= 12:
        return None
    year = 2000 + yy
    if year > pivot_year:
        year -= 100
    return (year, month)


def title_case(text: str) -> str:
    """'WEST TRAIL ST.' -> 'West Trail St.', '2nd. AVENUE' -> '2nd. Avenue' (presentation of recorded names)."""
    return _WORD_START.sub(lambda m: m.group(0).upper(), text.strip().lower())


def _clean_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _clean_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _condition_code(value: Any) -> str | None:
    """Condition rating exactly as recorded: '0'..'9', or 'N' (not applicable). None when missing."""
    text = _clean_text(value)
    return text.upper() if text else None


def bridge_condition_label(code: str | None) -> str | None:
    """'F' -> 'Fair (FHWA classification from the lowest component rating)'; None for an unknown code."""
    word = BRIDGE_CONDITION_LABELS.get(code or "")
    return f"{word} ({BRIDGE_CONDITION_BASIS})" if word else None


def parse_record(feature: dict[str, Any], pivot_year: int | None = None) -> NbiRecord | None:
    """Parse one ArcGIS feature of the NBI layer. Returns None when it has no usable identifier or point.

    ``pivot_year`` resolves the two-digit inspection year (a year after it is read as 19YY); it defaults to
    the current year.
    """
    attrs = feature.get("attributes") or {}
    geometry = feature.get("geometry") or {}
    number = _clean_text(attrs.get("STRUCTURE_NUMBER_008"))
    lon, lat = _clean_float(geometry.get("x")), _clean_float(geometry.get("y"))
    if number is None or lon is None or lat is None:
        return None
    pivot = pivot_year if pivot_year is not None else datetime.now(UTC).year

    recorded = dms_to_decimal(attrs.get("LAT_016"), attrs.get("LONG_017"))
    if recorded is None:
        check, distance, rec_lon, rec_lat = "unverified", None, None, None
    else:
        rec_lon, rec_lat = recorded
        distance = round(geo.haversine(lon, lat, rec_lon, rec_lat), 1)
        check = "mismatch" if distance > LOCATION_MISMATCH_M else "ok"

    reconstructed = _clean_int(attrs.get("YEAR_RECONSTRUCTED_106"))
    owner_code = _clean_text(attrs.get("OWNER_022"))
    if owner_code and owner_code.isdigit():
        owner_code = owner_code.zfill(2)
    inspection = parse_inspection_date(attrs.get("DATE_OF_INSPECT_090"), pivot)
    condition = _clean_text(attrs.get("BRIDGE_CONDITION"))
    condition = condition.upper() if condition else None

    fields: dict[str, Any] = {
        "structure_number": number,
        "facility_carried": _clean_text(attrs.get("FACILITY_CARRIED_007")),
        "features_intersected": _clean_text(attrs.get("FEATURES_DESC_006A")),
        "location": _clean_text(attrs.get("LOCATION_009")),
        "owner_code": owner_code,
        "owner": OWNER_LABELS.get(owner_code or "", f"Owner code {owner_code}" if owner_code else None),
        "year_built": _clean_int(attrs.get("YEAR_BUILT_027")) or None,
        "year_reconstructed": reconstructed if reconstructed else None,  # 0 / null = none recorded
        "adt": _clean_int(attrs.get("ADT_029")),
        "adt_year": _clean_int(attrs.get("YEAR_ADT_030")) or None,
        "structure_length_m": _clean_float(attrs.get("STRUCTURE_LEN_MT_049")),
        "inspection_date": f"{inspection[0]:04d}-{inspection[1]:02d}" if inspection else None,
        "inspection_label": f"{MONTH_NAMES[inspection[1] - 1]} {inspection[0]}" if inspection else None,
        "deck_condition": _condition_code(attrs.get("DECK_COND_058")),
        "superstructure_condition": _condition_code(attrs.get("SUPERSTRUCTURE_COND_059")),
        "substructure_condition": _condition_code(attrs.get("SUBSTRUCTURE_COND_060")),
        "culvert_condition": _condition_code(attrs.get("CULVERT_COND_062")),
        "lowest_rating": _clean_int(attrs.get("LOWEST_RATING")),
        "bridge_condition": condition,
        "bridge_condition_label": bridge_condition_label(condition),
        "location_check": check,
    }
    return NbiRecord(
        structure_number=number,
        lon=lon,
        lat=lat,
        location_check=check,
        recorded_lon=rec_lon,
        recorded_lat=rec_lat,
        check_distance_m=distance,
        fields=fields,
    )


def record_name(record: NbiRecord) -> str | None:
    """Display name from the recorded items 7 and 6A: '<facility carried> over <feature intersected>'."""
    facility = record.fields.get("facility_carried")
    feature = record.fields.get("features_intersected")
    if facility and feature:
        return f"{title_case(facility)} over {title_case(feature)}"
    if facility:
        return title_case(facility)
    return None


def parse_records(payload: dict[str, Any], pivot_year: int | None = None) -> list[NbiRecord]:
    """Parse every feature of a cached NBI response, sorted by structure number."""
    records = [parse_record(feature, pivot_year) for feature in payload.get("features", [])]
    return sorted((r for r in records if r is not None), key=lambda r: r.structure_number)
