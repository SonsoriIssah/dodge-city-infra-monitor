"""Pure-Python GIS processing: raw OSM / NBI / TIGER files -> study-area layers and the asset registry.

Rules (build contract sections 2, 3 and 5):

* every line feature is clipped to the study-area rectangle; of several parts the longest is kept; parts
  shorter than 5 m are dropped; ``length_m`` is the clipped length;
* a building is kept whole when its centroid lies inside the rectangle and its footprint is at least 25 m2;
* each OSM way becomes exactly one asset; asset ids are deterministic (sorted by source id);
* contiguous ``bridge`` ways of the same class are merged into one bridge asset, which is never also a
  road or rail asset;
* NBI records are attached to the nearest highway bridge within 60 m when their recorded coordinates agree
  with their point geometry; other valid records become point assets; a record that fails the coordinate
  check creates no asset and is listed in the processing report;
* no attribute is invented: asset properties hold OSM tags of interest, measures computed from the geometry,
  the building height with its source, and parsed NBI fields.

Simulated water mains are not created here (the sensor stage does that).
"""
from __future__ import annotations

import csv
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline import geo
from pipeline.config import BBox, Settings
from pipeline.gis import nbi as nbi_mod
from pipeline.gis import osm as osm_mod
from pipeline.gis import tiger as tiger_mod
from pipeline.gis.sources import HEIGHTS_CSV, read_json, read_sources, write_json

logger = logging.getLogger(__name__)

COORD_DECIMALS = 7  # OSM native precision (about 1 cm)
MIN_BUILDING_AREA_M2 = 25.0
MIN_LINE_PART_M = 5.0
LEVEL_HEIGHT_M = 3.6
NBI_MATCH_RADIUS_M = 60.0
# Range of a usable measured (lidar) building height; the same limits tools/derive_building_heights.py applies.
LIDAR_MIN_HEIGHT_M = 2.5
LIDAR_MAX_HEIGHT_M = 60.0
LIDAR_CSV_COLUMNS = ("osm_id", "height_m")

HEIGHT_SOURCES = ("osm_height", "lidar_3dep", "osm_levels", "estimated")  # precedence order

# Highway classes that become road assets (the others stay in the base-map road layer only).
_ROAD_BASE_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")
ROAD_ASSET_CLASSES = frozenset(
    {*_ROAD_BASE_CLASSES, *(f"{c}_link" for c in _ROAD_BASE_CLASSES), "unclassified", "residential"}
)
# ``highway`` values that are not travelled ways and are left out of the road layer altogether.
NON_ROAD_HIGHWAYS = frozenset(
    {"proposed", "construction", "abandoned", "razed", "platform", "rest_area", "services", "elevator",
     "corridor", "bus_stop", "street_lamp"}
)  # fmt: skip
RAIL_ASSET_VALUES = frozenset({"rail"})
POWER_LINE_VALUES = frozenset({"line", "minor_line"})

ASSET_ID_FORMATS: dict[str, tuple[str, int]] = {
    "building": ("BLD", 4),
    "road": ("RD", 4),
    "bridge": ("BRG", 3),
    "rail": ("RAIL", 3),
    "power": ("PWR", 3),
    "street_light": ("SL", 3),
    "water_main": ("WM", 3),
}
ASSET_CATEGORIES: dict[str, str] = {
    "building": "Buildings",
    "road": "Transportation",
    "bridge": "Transportation",
    "rail": "Transportation",
    "power": "Utilities",
    "street_light": "Utilities",
    "water_main": "Simulated network",
}
ASSET_TYPE_ORDER = ("building", "road", "bridge", "rail", "power", "street_light")

# OSM tags copied into asset properties when present (nothing is defaulted).
BUILDING_TAGS = ("amenity", "office", "operator")
ROAD_TAGS = ("surface", "maxspeed", "ref")
RAIL_TAGS = ("operator", "usage", "service")
POWER_TAGS = ("operator", "voltage", "substation", "cables")
LAMP_TAGS = ("lamp_type", "lamp_mount", "light:method", "light:count", "operator", "ref")

_HEIGHT_TAG = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(m|meters?|metres?|ft|feet|')?\s*$", re.IGNORECASE)
_NUMERIC_NAME = re.compile(r"^[0-9]+$")
FEET_TO_M = 0.3048

OUTPUT_FILES = {
    "study_area": "study_area.geojson",
    "buildings": "buildings.geojson",
    "roads": "roads.geojson",
    "assets": "assets.geojson",
    "city_boundary": "city_boundary.geojson",
    "report": "processing_report.json",
}

Coord = tuple[float, float]


# --- small parsers ------------------------------------------------------------------------------------------
def make_asset_id(asset_type: str, number: int) -> str:
    """Zero-padded asset id, e.g. ``make_asset_id('building', 7) -> 'BLD-0007'``."""
    prefix, width = ASSET_ID_FORMATS[asset_type]
    return f"{prefix}-{number:0{width}d}"


def clean_name(value: Any) -> str | None:
    """OSM name, or None when missing, blank or purely numeric ("1" .. "10" are mapping artefacts)."""
    text = str(value).strip() if value is not None else ""
    if not text or _NUMERIC_NAME.match(text):
        return None
    return text


def parse_height_tag(value: Any) -> float | None:
    """Metres from an OSM ``height`` tag ("12", "12.5 m", "40 ft", "40'"); None when unusable."""
    match = _HEIGHT_TAG.match(str(value)) if value is not None else None
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "m").lower()
    metres = number * FEET_TO_M if unit in ("ft", "feet", "'") else number
    return metres if metres > 0 else None


def parse_levels(value: Any) -> float | None:
    """Number of storeys from ``building:levels``; None when missing or not a positive number."""
    try:
        levels = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return levels if math.isfinite(levels) and levels > 0 else None


def parse_lanes(value: Any) -> int | None:
    """Lane count from the ``lanes`` tag; None unless it is a plain positive integer."""
    text = str(value).strip() if value is not None else ""
    if text.isdigit() and 1 <= int(text) <= 20:
        return int(text)
    return None


def parse_oneway(value: Any) -> bool | None:
    """``oneway`` tag as a boolean; None when untagged or not a yes/no value."""
    text = str(value).strip().lower() if value is not None else ""
    if text in ("yes", "true", "1", "-1"):
        return True
    if text in ("no", "false", "0"):
        return False
    return None


def estimate_height(building_type: str | None, footprint_m2: float, man_made: str | None = None) -> float:
    """Deterministic height estimate for a building without a tagged or measured height (no randomness)."""
    kind = (building_type or "").lower()
    if kind in ("house", "residential", "detached", "cabin"):
        return 5.0
    if kind in ("garage", "shed", "roof", "pavilion"):
        return 3.5
    if kind in ("industrial", "warehouse"):
        return 9.0
    if kind == "storage_tank" or (man_made or "").lower() == "storage_tank":
        return 10.0
    if footprint_m2 < 400.0:
        return 4.5
    if footprint_m2 < 1500.0:
        return 6.0
    return 8.0


def resolve_height(tags: dict[str, Any], footprint_m2: float, lidar_height: float | None) -> tuple[float, str]:
    """Building height and its source, by precedence: osm_height > lidar_3dep > osm_levels > estimated."""
    tagged = parse_height_tag(tags.get("height"))
    if tagged is not None:
        return round(tagged, 1), "osm_height"
    if lidar_height is not None and LIDAR_MIN_HEIGHT_M <= lidar_height <= LIDAR_MAX_HEIGHT_M:
        return round(lidar_height, 1), "lidar_3dep"
    levels = parse_levels(tags.get("building:levels"))
    if levels is not None:
        return round(levels * LEVEL_HEIGHT_M, 1), "osm_levels"
    return estimate_height(tags.get("building"), footprint_m2, tags.get("man_made")), "estimated"


def load_lidar_heights(csv_path: Path) -> dict[int, float]:
    """Measured heights by OSM way id from ``building_heights_3dep.csv`` (empty when the file is absent).

    Expected header: ``osm_id,height_m,n_pixels,lidar_project,collected``. A file without the ``osm_id`` and
    ``height_m`` columns raises ``ValueError`` (a wrong file must not silently turn every height into an
    estimate). Rows that are not usable - unreadable numbers, a height outside 2.5-60 m, a repeated
    ``osm_id`` - are ignored and counted in a warning.
    """
    if not csv_path.exists():
        return {}
    heights: dict[int, float] = {}
    ignored = 0
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in LIDAR_CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{csv_path.name}: missing column(s) {', '.join(missing)} in the header")
        for row in reader:
            try:
                osm_id, height = int(row["osm_id"]), float(row["height_m"])
            except (TypeError, ValueError):
                ignored += 1
                continue
            if osm_id in heights or not LIDAR_MIN_HEIGHT_M <= height <= LIDAR_MAX_HEIGHT_M:  # NaN fails the range
                ignored += 1
                continue
            heights[osm_id] = height
    if ignored:
        logger.warning("%s: %d row(s) ignored (unreadable, out of range or repeated osm_id)", csv_path.name, ignored)
    return heights


# --- geometry helpers ---------------------------------------------------------------------------------------
def way_coords(element: dict[str, Any]) -> list[Coord]:
    """(lon, lat) vertices of an Overpass way returned with ``out geom``."""
    return [(p["lon"], p["lat"]) for p in element.get("geometry") or [] if p is not None]


def _round_coord(lon: float, lat: float) -> list[float]:
    return [round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS)]


def _dedupe(coords: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for point in coords:
        if not out or point != out[-1]:
            out.append(point)
    return out


@dataclass(slots=True)
class ClippedLine:
    """Result of clipping one polyline to the study area."""

    coords: list[list[float]]  # rounded [lon, lat] pairs, all inside the box
    length_m: float
    parts: int  # number of parts of at least MIN_LINE_PART_M the clip produced
    dropped_m: float  # length of the parts that were not kept
    was_clipped: bool


def clip_line(coords: list[Coord], bbox: BBox) -> ClippedLine | None:
    """Clip a polyline to the study area: keep the longest part, drop parts shorter than 5 m.

    Returns None when nothing (long enough) lies inside the box.
    """
    box = (bbox.west, bbox.south, bbox.east, bbox.north)
    measured: list[tuple[float, list[list[float]]]] = []
    for part in geo.clip_line_to_bbox(coords, box):
        rounded = _dedupe(
            [
                [min(max(lon, bbox.west), bbox.east), min(max(lat, bbox.south), bbox.north)]
                for lon, lat in (_round_coord(x, y) for x, y in part)
            ]
        )
        if len(rounded) < 2:
            continue
        measured.append((geo.line_length(rounded), rounded))
    kept = [item for item in measured if item[0] >= MIN_LINE_PART_M]
    if not kept:
        return None
    best_index = max(range(len(kept)), key=lambda i: kept[i][0])  # first of equal lengths: deterministic
    length, best = kept[best_index]
    original = geo.line_length(coords)
    return ClippedLine(
        coords=best,
        length_m=round(length, 1),
        parts=len(kept),
        dropped_m=round(sum(item[0] for item in measured) - length, 1),
        was_clipped=abs(original - length) > 0.05,
    )


def closed_ring(coords: list[Coord]) -> list[list[float]] | None:
    """Rounded, counter-clockwise closed ring of a closed way; None when the way is not a usable polygon."""
    if len(coords) < 4 or coords[0] != coords[-1]:
        return None
    ring = _dedupe([_round_coord(lon, lat) for lon, lat in coords])
    if len(ring) < 4 or ring[0] != ring[-1]:
        return None
    if geo.ring_signed_area_deg(ring[:-1]) < 0:
        ring.reverse()
    return ring


# --- bridges ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class BridgeChain:
    """Contiguous bridge ways of one class merged into a single line."""

    way_ids: list[int]  # in chain order
    kind: tuple[str, str]  # ("highway", "<class>") or ("railway", "rail")
    coords: list[Coord]
    names: list[str] = field(default_factory=list)  # distinct OSM names in chain order
    tags: dict[str, Any] = field(default_factory=dict)  # tags of the lowest-id way


def bridge_kind(tags: dict[str, Any]) -> tuple[str, str] | None:
    """Class of a bridge way, or None when the way is not a bridge asset candidate.

    A candidate has a ``bridge`` tag other than "no", is not an area, and is a travelled highway or a
    ``railway=rail`` track.
    """
    if tags.get("bridge") in (None, "no") or tags.get("area") == "yes":
        return None
    highway = tags.get("highway")
    if highway and highway not in NON_ROAD_HIGHWAYS:
        return ("highway", highway)
    if tags.get("railway") in RAIL_ASSET_VALUES:
        return ("railway", tags["railway"])
    return None


def _end_keys(element: dict[str, Any], coords: list[Coord]) -> tuple[Any, Any]:
    """Identity of the first and last node of a way (node id when available, else the coordinate)."""
    nodes = element.get("nodes")
    if nodes and len(nodes) == len(coords):
        return ("n", nodes[0]), ("n", nodes[-1])
    return ("c", *coords[0]), ("c", *coords[-1])


def merge_bridge_ways(ways: list[dict[str, Any]]) -> list[BridgeChain]:
    """Merge bridge ways of the same class that share an end node into chains (deterministic order)."""
    by_kind: dict[tuple[str, str], dict[int, tuple[list[Coord], Any, Any, dict[str, Any]]]] = {}
    for element in ways:
        tags = element.get("tags") or {}
        kind = bridge_kind(tags)
        coords = way_coords(element)
        if kind is None or len(coords) < 2:
            continue
        start, end = _end_keys(element, coords)
        by_kind.setdefault(kind, {})[int(element["id"])] = (coords, start, end, tags)

    chains: list[BridgeChain] = []
    for kind in sorted(by_kind):
        remaining = by_kind[kind]
        while remaining:
            seed = min(remaining)
            coords, head_key, tail_key, tags = remaining.pop(seed)
            line, ids = list(coords), [seed]
            names = [n for n in [clean_name(tags.get("name"))] if n]
            for at_tail in (True, False):
                grown = True
                while grown:
                    grown = False
                    key = tail_key if at_tail else head_key
                    for way_id in sorted(remaining):
                        w_coords, w_start, w_end, w_tags = remaining[way_id]
                        if key not in (w_start, w_end):
                            continue
                        del remaining[way_id]
                        name = clean_name(w_tags.get("name"))
                        if at_tail:
                            oriented = w_coords if w_start == key else w_coords[::-1]
                            line.extend(oriented[1:])
                            tail_key = w_end if w_start == key else w_start
                            ids.append(way_id)
                            if name and name not in names:
                                names.append(name)
                        else:
                            oriented = w_coords if w_end == key else w_coords[::-1]
                            line[:0] = oriented[:-1]
                            head_key = w_start if w_end == key else w_end
                            ids.insert(0, way_id)
                            if name and name not in names:
                                names.insert(0, name)
                        grown = True
                        break
            chains.append(BridgeChain(way_ids=ids, kind=kind, coords=line, names=names, tags=tags))
    chains.sort(key=lambda chain: min(chain.way_ids))
    return chains


def match_nbi(
    records: list[nbi_mod.NbiRecord], bridge_lines: dict[str, list[Coord]], radius_m: float = NBI_MATCH_RADIUS_M
) -> dict[str, tuple[str, float]]:
    """Attach NBI records to bridge lines: ``{structure_number: (bridge key, distance_m)}``.

    Only records that passed the coordinate check take part. Each record attaches to at most one bridge and
    each bridge takes at most one record; pairs are assigned nearest first.
    """
    pairs: list[tuple[float, str, str]] = []
    for record in records:
        if record.location_check != "ok":
            continue
        for key, coords in bridge_lines.items():
            distance = geo.point_to_line_m(record.lon, record.lat, coords)
            if distance <= radius_m:
                pairs.append((distance, record.structure_number, key))
    matches: dict[str, tuple[str, float]] = {}
    taken: set[str] = set()
    for distance, number, key in sorted(pairs):
        if number in matches or key in taken:
            continue
        matches[number] = (key, round(distance, 1))
        taken.add(key)
    return matches


# --- processing ---------------------------------------------------------------------------------------------
@dataclass(slots=True)
class ProcessedData:
    """Everything the processing stage writes to ``data/processed``."""

    study_area: dict[str, Any]
    buildings: dict[str, Any]
    roads: dict[str, Any]
    assets: dict[str, Any]
    city_boundary: dict[str, Any] | None
    report: dict[str, Any]


def _feature(geometry: dict[str, Any], properties: dict[str, Any], feature_id: Any = None) -> dict[str, Any]:
    feature: dict[str, Any] = {"type": "Feature"}
    if feature_id is not None:
        feature["id"] = feature_id
    feature["geometry"] = geometry
    feature["properties"] = properties
    return feature


def _collection(name: str, features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "name": name, "features": features}


def _tags_of_interest(tags: dict[str, Any], keys: tuple[str, ...], with_addr: bool = False) -> dict[str, Any]:
    picked = {key: tags[key] for key in keys if tags.get(key) not in (None, "")}
    if with_addr:
        picked.update({k: v for k, v in sorted(tags.items()) if k.startswith("addr:") and v not in (None, "")})
    return picked


def _pending_asset(
    asset_type: str,
    sort_key: tuple[Any, ...],
    name: str | None,
    source_id: str,
    geometry: dict[str, Any],
    centroid: list[float],
    attributes: dict[str, Any],
    link_osm_id: int | None = None,
) -> dict[str, Any]:
    return {
        "asset_type": asset_type,
        "sort_key": sort_key,
        "name": name,
        "source_id": source_id,
        "geometry": geometry,
        "centroid": centroid,
        "attributes": attributes,
        "link_osm_id": link_osm_id,
    }


def study_area_feature(settings: Settings) -> dict[str, Any]:
    """The study area (bbox rectangle) as a GeoJSON feature."""
    bbox = settings.bbox
    return _feature(
        settings.study_area_geometry(),
        {
            "slug": settings.STUDY_AREA_SLUG,
            "name": settings.STUDY_AREA_NAME,
            "description": "Rectangular study area of the monitoring prototype (configured by STUDY_AREA_BBOX).",
            "bbox": [bbox.west, bbox.south, bbox.east, bbox.north],
            "center": [round(c, 6) for c in bbox.center],
            "utm_srid": settings.STUDY_AREA_UTM_SRID,
            "timezone": settings.TIMEZONE,
        },
    )


def process_city_boundary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise the TIGERweb boundary into MultiPolygon features with a small property set."""
    features = []
    for raw in tiger_mod.validate_boundary(payload):
        geometry = raw["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        rounded = [[_dedupe([_round_coord(p[0], p[1]) for p in ring]) for ring in polygon] for polygon in polygons]
        props = raw.get("properties") or {}
        features.append(
            _feature(
                {"type": "MultiPolygon", "coordinates": rounded},
                {
                    "kind": "city_limits",
                    "name": props.get("NAME") or props.get("BASENAME"),
                    "geoid": props.get("GEOID"),
                    "source_id": tiger_mod.SOURCE_ID,
                },
            )
        )
    return features


def _nbi_pivot_year(raw_dir: Path) -> int:
    """Year used to resolve two-digit inspection years: the year the NBI file was retrieved."""
    retrieved = (read_sources(raw_dir).get(nbi_mod.SOURCE_ID) or {}).get("retrieved_at")
    try:
        return datetime.fromisoformat(str(retrieved).replace("Z", "+00:00")).year
    except ValueError:
        return datetime.now(UTC).year


def process(raw_dir: Path, settings: Settings) -> ProcessedData:
    """Turn the cached raw files into the processed layers and the asset registry (no database access)."""
    bbox = settings.bbox
    box = (bbox.west, bbox.south, bbox.east, bbox.north)
    osm_path = raw_dir / osm_mod.FILE_NAME
    if not osm_path.exists():
        raise FileNotFoundError(f"{osm_path} is missing - run scripts/download_data.py first")
    osm = read_json(osm_path)
    elements: dict[tuple[str, int], dict[str, Any]] = {}
    for element in osm.get("elements", []):
        if element.get("type") in ("way", "node") and "id" in element:
            elements.setdefault((element["type"], int(element["id"])), element)
    ways = [e for (kind, _), e in sorted(elements.items()) if kind == "way" and e.get("geometry")]
    nodes = [e for (kind, _), e in sorted(elements.items()) if kind == "node" and "lon" in e and "lat" in e]

    lidar = load_lidar_heights(raw_dir / HEIGHTS_CSV)
    counts: Counter[str] = Counter()
    pending: list[dict[str, Any]] = []
    building_features: list[dict[str, Any]] = []
    road_features: list[dict[str, Any]] = []
    multi_part: list[dict[str, Any]] = []
    bridge_ways: list[dict[str, Any]] = []
    height_sources: Counter[str] = Counter()
    road_km_before = road_km_after = 0.0

    def clip_logged(osm_id: int, layer: str, coords: list[Coord]) -> ClippedLine | None:
        clipped = clip_line(coords, bbox)
        if clipped is None:
            counts[f"{layer}_dropped_outside_or_short"] += 1
            return None
        if clipped.was_clipped:
            counts[f"{layer}_clipped"] += 1
        if clipped.parts > 1:
            multi_part.append(
                {"layer": layer, "osm_id": osm_id, "parts": clipped.parts, "kept_m": clipped.length_m,
                 "dropped_m": clipped.dropped_m}
            )  # fmt: skip
            logger.info(
                "%s way %d crosses the study area %d times; kept the longest part (%.0f m, dropped %.0f m)",
                layer, osm_id, clipped.parts, clipped.length_m, clipped.dropped_m,
            )  # fmt: skip
        return clipped

    for element in ways:
        osm_id = int(element["id"])
        tags: dict[str, Any] = element.get("tags") or {}
        coords = way_coords(element)

        # -- buildings -----------------------------------------------------------------------------------
        if tags.get("building") not in (None, "no"):
            counts["buildings_input"] += 1
            ring = closed_ring(coords)
            if ring is None:
                counts["buildings_dropped_not_closed"] += 1
                continue
            footprint = geo.polygon_area_m2([tuple(p) for p in ring[:-1]])
            if footprint < MIN_BUILDING_AREA_M2:
                counts["buildings_dropped_small"] += 1
                continue
            centroid = geo.polygon_area_centroid([tuple(p) for p in ring])
            if not geo.point_in_bbox(centroid[0], centroid[1], box):
                counts["buildings_dropped_centroid_outside"] += 1
                continue
            raw_name = tags.get("name")
            name = clean_name(raw_name)
            if raw_name and name is None:
                counts["buildings_numeric_names_cleared"] += 1
            height, height_source = resolve_height(tags, footprint, lidar.get(osm_id))
            height_sources[height_source] += 1
            levels = parse_levels(tags.get("building:levels"))
            footprint = round(footprint, 1)
            geometry = {"type": "Polygon", "coordinates": [ring]}
            building_features.append(
                _feature(
                    geometry,
                    {
                        "osm_id": osm_id,
                        "name": name,
                        "building_type": tags.get("building"),
                        "levels": levels,
                        "height_m": height,
                        "height_source": height_source,
                        "footprint_m2": footprint,
                        "tags": tags,
                    },
                )
            )
            attributes: dict[str, Any] = {"osm_id": osm_id, "building_type": tags.get("building")}
            if levels is not None:
                attributes["levels"] = levels
            attributes.update({"height_m": height, "height_source": height_source, "footprint_m2": footprint})
            attributes.update(_tags_of_interest(tags, BUILDING_TAGS, with_addr=True))
            pending.append(
                _pending_asset("building", (osm_id,), name, osm_mod.SOURCE_ID, geometry,
                               _round_coord(*centroid), attributes, link_osm_id=osm_id)
            )  # fmt: skip
            continue

        # -- power ---------------------------------------------------------------------------------------
        power = tags.get("power")
        if power == "substation":
            ring = closed_ring(coords)
            centre = geo.polygon_area_centroid([tuple(p) for p in ring]) if ring else geo.polygon_centroid(coords)
            if not geo.point_in_bbox(centre[0], centre[1], box):
                counts["power_dropped_outside"] += 1
                continue
            attributes = {"osm_id": osm_id, "osm_type": "way", "power": power}
            if ring:
                attributes["footprint_m2"] = round(geo.polygon_area_m2([tuple(p) for p in ring[:-1]]), 1)
            attributes.update(_tags_of_interest(tags, POWER_TAGS))
            point = _round_coord(*centre)
            pending.append(
                _pending_asset("power", (osm_id, "way"), clean_name(tags.get("name")), osm_mod.SOURCE_ID,
                               {"type": "Point", "coordinates": point}, point, attributes)
            )  # fmt: skip
            continue
        if power in POWER_LINE_VALUES:
            clipped = clip_logged(osm_id, "power", coords) if len(coords) >= 2 else None
            if clipped is None:
                continue
            attributes = {"osm_id": osm_id, "osm_type": "way", "power": power, "length_m": clipped.length_m}
            attributes.update(_tags_of_interest(tags, POWER_TAGS))
            pending.append(
                _pending_asset("power", (osm_id, "way"), clean_name(tags.get("name")), osm_mod.SOURCE_ID,
                               {"type": "LineString", "coordinates": clipped.coords},
                               _round_coord(*geo.point_along(clipped.coords)), attributes)
            )  # fmt: skip
            continue

        # -- highways (base-map road layer; road assets; bridge candidates) ------------------------------
        is_bridge = bridge_kind(tags) is not None
        if is_bridge:
            bridge_ways.append(element)
        highway = tags.get("highway")
        if highway:
            counts["highway_ways_input"] += 1
            if highway in NON_ROAD_HIGHWAYS:
                counts["highway_skipped_class"] += 1
                continue
            if tags.get("area") == "yes":
                counts["highway_skipped_area"] += 1
                continue
            if len(coords) < 2:
                counts["highway_dropped_degenerate"] += 1
                continue
            is_asset_class = highway in ROAD_ASSET_CLASSES and not is_bridge
            if is_asset_class:
                road_km_before += geo.line_length(coords) / 1000.0
            clipped = clip_logged(osm_id, "roads", coords)
            if clipped is None:
                continue
            name = clean_name(tags.get("name"))
            lanes, oneway = parse_lanes(tags.get("lanes")), parse_oneway(tags.get("oneway"))
            geometry = {"type": "LineString", "coordinates": clipped.coords}
            road_features.append(
                _feature(
                    geometry,
                    {
                        "osm_id": osm_id,
                        "name": name,
                        "highway_class": highway,
                        "surface": tags.get("surface") or None,
                        "lanes": lanes,
                        "maxspeed": tags.get("maxspeed") or None,
                        "oneway": oneway,
                        "is_bridge": is_bridge,
                        "length_m": clipped.length_m,
                        "tags": tags,
                    },
                )
            )
            if is_asset_class:
                road_km_after += clipped.length_m / 1000.0
                attributes = {"osm_id": osm_id, "highway_class": highway}
                if lanes is not None:
                    attributes["lanes"] = lanes
                if oneway is not None:
                    attributes["oneway"] = oneway
                attributes.update(_tags_of_interest(tags, ROAD_TAGS))
                attributes["length_m"] = clipped.length_m
                pending.append(
                    _pending_asset("road", (osm_id,), name, osm_mod.SOURCE_ID, geometry,
                                   _round_coord(*geo.point_along(clipped.coords)), attributes, link_osm_id=osm_id)
                )  # fmt: skip
            continue

        # -- rail ----------------------------------------------------------------------------------------
        if tags.get("railway") in RAIL_ASSET_VALUES and not is_bridge:
            counts["rail_ways_input"] += 1
            clipped = clip_logged(osm_id, "rail", coords) if len(coords) >= 2 else None
            if clipped is None:
                continue
            attributes = {"osm_id": osm_id, "railway": tags["railway"]}
            attributes.update(_tags_of_interest(tags, RAIL_TAGS))
            attributes["length_m"] = clipped.length_m
            pending.append(
                _pending_asset("rail", (osm_id,), clean_name(tags.get("name")), osm_mod.SOURCE_ID,
                               {"type": "LineString", "coordinates": clipped.coords},
                               _round_coord(*geo.point_along(clipped.coords)), attributes)
            )  # fmt: skip
            continue
        if not is_bridge:
            counts["ways_not_used"] += 1

    # -- point features ----------------------------------------------------------------------------------
    for element in nodes:
        osm_id = int(element["id"])
        tags = element.get("tags") or {}
        lon, lat = element["lon"], element["lat"]
        if not geo.point_in_bbox(lon, lat, box):
            continue
        point = _round_coord(lon, lat)
        if tags.get("highway") == "street_lamp":
            attributes = {"osm_id": osm_id, "osm_type": "node"}
            attributes.update(_tags_of_interest(tags, LAMP_TAGS))
            pending.append(
                _pending_asset("street_light", (osm_id,), clean_name(tags.get("name")), osm_mod.SOURCE_ID,
                               {"type": "Point", "coordinates": point}, point, attributes)
            )  # fmt: skip
        elif tags.get("power") == "substation":
            attributes = {"osm_id": osm_id, "osm_type": "node", "power": "substation"}
            attributes.update(_tags_of_interest(tags, POWER_TAGS))
            pending.append(
                _pending_asset("power", (osm_id, "node"), clean_name(tags.get("name")), osm_mod.SOURCE_ID,
                               {"type": "Point", "coordinates": point}, point, attributes)
            )  # fmt: skip

    # -- bridges (merged OSM ways) and the National Bridge Inventory -------------------------------------
    layers_absent: list[str] = []
    bridge_lines: dict[str, list[Coord]] = {}
    bridges: dict[str, dict[str, Any]] = {}
    for chain in merge_bridge_ways(bridge_ways):
        clipped = clip_logged(min(chain.way_ids), "bridges", chain.coords)
        if clipped is None:
            continue
        key = str(min(chain.way_ids))
        on_rail = chain.kind[0] == "railway"
        attributes = {
            "structure_kind": "bridge",
            "railway" if on_rail else "highway_class": chain.kind[1],
            "osm_way_ids": sorted(chain.way_ids),
        }
        attributes.update(_tags_of_interest(chain.tags, RAIL_TAGS if on_rail else ROAD_TAGS))
        attributes["length_m"] = clipped.length_m
        # A single-way highway bridge keeps the link to its base-map road row; merged bridges list their ways.
        single_road_way = chain.way_ids[0] if len(chain.way_ids) == 1 and not on_rail else None
        bridges[key] = _pending_asset(
            "bridge",
            (0, min(chain.way_ids)),
            " / ".join(chain.names) or None,
            osm_mod.SOURCE_ID,
            {"type": "LineString", "coordinates": clipped.coords},
            _round_coord(*geo.point_along(clipped.coords)),
            attributes,
            link_osm_id=single_road_way,
        )
        if not on_rail:
            bridge_lines[key] = [tuple(p) for p in clipped.coords]

    nbi_report: dict[str, Any] = {"records": 0, "matched": [], "point_assets": [], "mismatched": [],
                                  "unverified": [], "outside_study_area": []}  # fmt: skip
    nbi_path = raw_dir / nbi_mod.FILE_NAME
    if nbi_path.exists():
        records = nbi_mod.parse_records(read_json(nbi_path), _nbi_pivot_year(raw_dir))
        nbi_report["records"] = len(records)
        usable: list[nbi_mod.NbiRecord] = []
        for record in records:
            summary = {
                "structure_number": record.structure_number,
                "facility_carried": record.fields.get("facility_carried"),
                "features_intersected": record.fields.get("features_intersected"),
            }
            if record.location_check == "mismatch":
                nbi_report["mismatched"].append(
                    {
                        **summary,
                        "point": [round(record.lon, 6), round(record.lat, 6)],
                        "recorded_coordinates": [
                            round(record.recorded_lon or 0.0, 6),
                            round(record.recorded_lat or 0.0, 6),
                        ],
                        "distance_m": record.check_distance_m,
                        "action": "no asset created: recorded coordinates (items 16/17) disagree with the "
                        "point geometry",
                    }
                )
                logger.warning(
                    "NBI record %s (%s): recorded coordinates are %.0f m from its point geometry; no asset created",
                    record.structure_number, record.fields.get("facility_carried"), record.check_distance_m or 0.0,
                )  # fmt: skip
            elif record.location_check != "ok":
                nbi_report["unverified"].append({**summary, "action": "no asset created: coordinates not checkable"})
                logger.warning("NBI record %s has no usable recorded coordinates; no asset created",
                               record.structure_number)  # fmt: skip
            elif not geo.point_in_bbox(record.lon, record.lat, box):
                nbi_report["outside_study_area"].append(summary)
            else:
                usable.append(record)
        matches = match_nbi(usable, bridge_lines)
        for record in usable:
            parsed = dict(record.fields)
            if record.structure_number in matches:
                key, distance = matches[record.structure_number]
                parsed["match_distance_m"] = distance
                bridges[key]["attributes"]["nbi"] = parsed
                bridges[key]["name"] = nbi_mod.record_name(record) or bridges[key]["name"]
                nbi_report["matched"].append(
                    {"structure_number": record.structure_number, "osm_way_ids":
                     bridges[key]["attributes"]["osm_way_ids"], "distance_m": distance}
                )  # fmt: skip
            else:
                point = _round_coord(record.lon, record.lat)
                kind = "culvert" if record.is_culvert else "bridge"
                bridges[f"nbi:{record.structure_number}"] = _pending_asset(
                    "bridge", (1, record.structure_number), nbi_mod.record_name(record), nbi_mod.SOURCE_ID,
                    {"type": "Point", "coordinates": point}, point, {"structure_kind": kind, "nbi": parsed},
                )  # fmt: skip
                nbi_report["point_assets"].append({"structure_number": record.structure_number,
                                                   "structure_kind": kind})  # fmt: skip
    else:
        layers_absent.append(nbi_mod.SOURCE_ID)
        logger.warning("%s not found: no bridge inventory attributes (layer absent)", nbi_mod.FILE_NAME)
    pending.extend(bridges.values())

    # -- city boundary -------------------------------------------------------------------------------------
    boundary: dict[str, Any] | None = None
    boundary_path = raw_dir / tiger_mod.FILE_NAME
    if boundary_path.exists():
        boundary = _collection("city_boundary", process_city_boundary(read_json(boundary_path)))
    else:
        layers_absent.append(tiger_mod.SOURCE_ID)
        logger.warning("%s not found: no city boundary outline (layer absent)", tiger_mod.FILE_NAME)

    # -- asset registry: deterministic ids -----------------------------------------------------------------
    asset_features: list[dict[str, Any]] = []
    by_type: Counter[str] = Counter()
    for asset_type in ASSET_TYPE_ORDER:
        group = sorted((a for a in pending if a["asset_type"] == asset_type), key=lambda a: a["sort_key"])
        for number, asset in enumerate(group, start=1):
            asset_id = make_asset_id(asset_type, number)
            by_type[asset_type] += 1
            asset_features.append(
                _feature(
                    asset["geometry"],
                    {
                        "asset_id": asset_id,
                        "asset_type": asset_type,
                        "category": ASSET_CATEGORIES[asset_type],
                        "name": asset["name"],
                        "is_simulated": False,
                        "source_id": asset["source_id"],
                        "link_osm_id": asset["link_osm_id"],
                        "centroid": asset["centroid"],
                        "attributes": asset["attributes"],
                    },
                    feature_id=asset_id,
                )
            )

    bridge_summary = [
        {
            "asset_id": f["properties"]["asset_id"],
            "name": f["properties"]["name"],
            "source_id": f["properties"]["source_id"],
            "structure_kind": f["properties"]["attributes"].get("structure_kind"),
            "osm_way_ids": f["properties"]["attributes"].get("osm_way_ids"),
            "length_m": f["properties"]["attributes"].get("length_m"),
            "nbi_structure_number": (f["properties"]["attributes"].get("nbi") or {}).get("structure_number"),
        }
        for f in asset_features
        if f["properties"]["asset_type"] == "bridge"
    ]
    bbox_list = [bbox.west, bbox.south, bbox.east, bbox.north]
    report = {
        "study_area": {"slug": settings.STUDY_AREA_SLUG, "name": settings.STUDY_AREA_NAME, "bbox": bbox_list},
        "inputs": {
            "osm_elements": len(osm.get("elements", [])),
            "osm_timestamp": (osm.get("osm3s") or {}).get("timestamp_osm_base"),
            "osm_ways_not_used": counts["ways_not_used"],
            "nbi_records": nbi_report["records"],
            "city_boundary_features": len(boundary["features"]) if boundary else 0,
            "lidar_heights_available": len(lidar),
        },
        "layers_absent": layers_absent,
        "buildings": {
            "input": counts["buildings_input"],
            "kept": len(building_features),
            "dropped_not_closed": counts["buildings_dropped_not_closed"],
            "dropped_footprint_under_25_m2": counts["buildings_dropped_small"],
            "dropped_centroid_outside": counts["buildings_dropped_centroid_outside"],
            "numeric_names_cleared": counts["buildings_numeric_names_cleared"],
            "height_sources": {source: height_sources.get(source, 0) for source in HEIGHT_SOURCES},
        },
        "roads": {
            "highway_ways_input": counts["highway_ways_input"],
            "base_layer_rows": len(road_features),
            "skipped_non_road_class": counts["highway_skipped_class"],
            "skipped_area": counts["highway_skipped_area"],
            "dropped_outside_or_short": counts["roads_dropped_outside_or_short"],
            "clipped_to_study_area": counts["roads_clipped"],
            "asset_class_km_before_clip": round(road_km_before, 2),
            "asset_class_km_after_clip": round(road_km_after, 2),
        },
        "rail": {
            "ways_input": counts["rail_ways_input"],
            "dropped_outside_or_short": counts["rail_dropped_outside_or_short"],
            "clipped_to_study_area": counts["rail_clipped"],
        },
        "power": {
            "dropped_outside_or_short": counts["power_dropped_outside_or_short"] + counts["power_dropped_outside"],
        },
        "lines_kept_longest_part": multi_part,
        "bridges": bridge_summary,
        "nbi": nbi_report,
        "assets": {"total": len(asset_features), "by_type": {t: by_type.get(t, 0) for t in ASSET_TYPE_ORDER}},
        "notes": [
            "Line features are clipped to the study-area rectangle; length_m is the clipped length.",
            "Buildings are kept whole when their centroid is inside the study area.",
            "Building relations (multipolygons) are not fetched; buildings come from closed OSM ways only.",
            "Simulated water mains are created by the sensor stage, not here.",
        ],
    }
    logger.info(
        "processed: %d buildings, %d base-map roads, %d assets (%s)",
        len(building_features), len(road_features), len(asset_features),
        ", ".join(f"{t}={by_type.get(t, 0)}" for t in ASSET_TYPE_ORDER),
    )  # fmt: skip
    logger.info("building height sources: %s", ", ".join(f"{s}={height_sources.get(s, 0)}" for s in HEIGHT_SOURCES))
    return ProcessedData(
        study_area=_collection("study_area", [study_area_feature(settings)]),
        buildings=_collection("buildings", building_features),
        roads=_collection("roads", road_features),
        assets=_collection("assets", asset_features),
        city_boundary=boundary,
        report=report,
    )


def write_outputs(data: ProcessedData, out_dir: Path) -> dict[str, Path]:
    """Write the processed layers and the report; a stale optional layer from an earlier run is removed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for key in ("study_area", "buildings", "roads", "assets"):
        written[key] = out_dir / OUTPUT_FILES[key]
        write_json(written[key], getattr(data, key))
    boundary_path = out_dir / OUTPUT_FILES["city_boundary"]
    if data.city_boundary is not None:
        write_json(boundary_path, data.city_boundary)
        written["city_boundary"] = boundary_path
    elif boundary_path.exists():
        boundary_path.unlink()
    written["report"] = out_dir / OUTPUT_FILES["report"]
    write_json(written["report"], data.report, indent=2)
    return written
