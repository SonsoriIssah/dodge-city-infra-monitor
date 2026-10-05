"""Rule-based sensor placement and the simulated water mains (build contract section 7).

Sensors are placed by rule on the real assets of the registry; nothing here invents an attribute of a real
feature. The planning functions are pure (asset records in, a ``PlacementPlan`` out); ``load_assets`` and
``write_placement`` are the thin database layer around them.

Rules, in the order that also fixes the sensor ids (``VIB-001`` ...):

    vibration    bridge_deck           every bridge and culvert; 2 on structures of 100 m or more, else 1
                 building_structure    22 buildings
                 road_pavement         8 roads, highest road class first
    moisture     road_subgrade         22 roads
                 foundation_perimeter  8 buildings
                 abutment_backfill     every bridge and culvert, 1 each
    temperature  bridge_deck           highway and rail bridges (not culverts); 2 on 100 m or more, else 1
                 road_surface          12 roads
                 building_envelope     12 buildings
                 equipment             every power substation
    pressure     water_main            SIM_WATER_MAINS simulated mains, 1 each

All counts are caps (``min(target, pool)``). Showcase assets carry three sensor types: every bridge matched
to a National Bridge Inventory record (the bridge rules above already give it all three) and up to three
buildings picked by the civic-building rule, which host one sensor of each building class. The remaining
hosts are chosen by seeded farthest-point sampling so that the network covers the whole study area; apart
from the showcase assets a building or road hosts one sensor class (while enough hosts are free).

A simulated water main exists only where a pressure sensor is hosted: the host road's clipped centre line
offset by 4.5 m and clipped to the study area again. It has no attributes besides its host road and length.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import zlib
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import psycopg

from pipeline import geo
from pipeline.config import BBox, Settings
from pipeline.db.loaders import jsonb
from pipeline.gis.process import ASSET_CATEGORIES, clip_line, make_asset_id
from pipeline.models import TAG_NBI_BRIDGE, TAG_SHOWCASE, SensorSpec
from pipeline.sensors.thresholds import SENSOR_ID_PREFIXES, UNITS, threshold_for

logger = logging.getLogger(__name__)

Coord = tuple[float, float]

SIMULATOR_SOURCE_ID = "simulator"
WATER_MAIN_OFFSET_M = 4.5
MIN_HOST_ROAD_M = 40.0  # shorter road segments are skipped as hosts while enough longer ones exist
LONG_STRUCTURE_M = 100.0  # bridges at least this long carry two deck sensors
MIN_WATER_MAIN_M = MIN_HOST_ROAD_M / 2.0  # an offset line shorter than this (after clipping) is not used
MIN_SENSOR_SPACING_M = 4.0  # sensors sharing an asset are at least this far apart
SPACING_TOLERANCE_M = 0.05  # rounding allowance when that spacing is checked
SEPARATION_ATTEMPTS = 60  # shifts tried along a line before two sensors are left closer together
ABUTMENT_OFFSET_M = 3.0  # abutment sensors sit this far from the start of the structure ...
ABUTMENT_OFFSET_SHARE = 0.05  # ... but not further than this share of its length
# Sensors sharing a polygon or point asset: the first at the centre, the others on a circle around it. The
# radii are tried in this order (the smaller ones only matter for small footprints), in RING_DIRECTIONS steps.
RING_RADII_M = (MIN_SENSOR_SPACING_M, 1.5 * MIN_SENSOR_SPACING_M, 0.75 * MIN_SENSOR_SPACING_M, 2.0, 1.0)
RING_DIRECTIONS = 12
BUILDING_POOL_FACTOR = 4  # building hosts are sampled from the (cap x factor) largest footprints
SHOWCASE_BUILDINGS = 3
COORD_DECIMALS = 7
INSTALLED_DAYS_BEFORE = (180, 1460)  # simulated installation date: 6 months to 4 years before the window

CIVIC_TAG_VALUES = frozenset(
    {"courthouse", "townhall", "government", "school", "hospital", "public", "civic", "police", "fire_station"}
)
CIVIC_TAG_KEYS = ("amenity", "office", "building_type")
CIVIC_NAME = re.compile(r"court|government|school|city hall", re.IGNORECASE)

_ROAD_BASE_RANK = {"motorway": 6, "trunk": 5, "primary": 4, "secondary": 3, "tertiary": 2}


@dataclass(frozen=True, slots=True)
class AssetRecord:
    """One row of ``infra.infrastructure_assets`` as the placement rules need it."""

    asset_id: str
    asset_type: str
    name: str | None
    geometry: dict[str, Any]  # GeoJSON Point / LineString / Polygon
    centroid: Coord
    properties: dict[str, Any] = field(default_factory=dict)
    source_id: str = "osm"

    @property
    def label(self) -> str:
        """Name for descriptions: the asset's name, else its id."""
        return self.name or self.asset_id


@dataclass(frozen=True, slots=True)
class WaterMain:
    """A simulated water main (asset type ``water_main``) hosted by a road asset."""

    asset_id: str
    name: str
    host_road_id: str
    coords: tuple[Coord, ...]
    centroid: Coord
    length_m: float

    @property
    def properties(self) -> dict[str, Any]:
        """Asset properties: the host road and measures of the generated geometry (nothing invented)."""
        return {"host_road_id": self.host_road_id, "length_m": self.length_m, "offset_m": WATER_MAIN_OFFSET_M}

    @property
    def geometry(self) -> dict[str, Any]:
        """GeoJSON LineString of the main."""
        return {"type": "LineString", "coordinates": [list(point) for point in self.coords]}


@dataclass(frozen=True, slots=True)
class PlacementRule:
    """One row of the placement table: which assets host which sensor class."""

    sensor_type: str
    placement: str
    host_type: str  # asset_type of the hosts
    cap: int | None  # None = every eligible asset
    device: str  # what the simulated device is, for the sensor description


# Table order defines the sensor numbering within each sensor type.
_RMS = "(hourly RMS velocity)"
_VWC = "(volumetric water content)"
RULES: tuple[PlacementRule, ...] = (
    PlacementRule("vibration", "bridge_deck", "bridge", None, f"deck vibration sensor {_RMS}"),
    PlacementRule("vibration", "building_structure", "building", 22, f"structural vibration sensor {_RMS}"),
    PlacementRule("vibration", "road_pavement", "road", 8, f"pavement vibration sensor {_RMS}"),
    PlacementRule("moisture", "road_subgrade", "road", 22, f"subgrade moisture probe {_VWC}"),
    PlacementRule("moisture", "foundation_perimeter", "building", 8, f"foundation moisture probe {_VWC}"),
    PlacementRule("moisture", "abutment_backfill", "bridge", None, f"abutment backfill moisture probe {_VWC}"),
    PlacementRule("temperature", "bridge_deck", "bridge", None, "deck surface temperature sensor"),
    PlacementRule("temperature", "road_surface", "road", 12, "road surface temperature sensor"),
    PlacementRule("temperature", "building_envelope", "building", 12, "envelope temperature sensor"),
    PlacementRule("temperature", "equipment", "power", None, "equipment temperature sensor"),
    PlacementRule("pressure", "water_main", "water_main", None, "pressure logger"),
)  # fmt: skip


@dataclass(slots=True)
class PlacementPlan:
    """Result of the placement rules: the sensors, the simulated mains they need and how hosts were chosen."""

    sensors: list[SensorSpec]
    water_mains: list[WaterMain]
    showcase_asset_ids: list[str]
    hosts: dict[tuple[str, str], list[str]]  # (sensor_type, placement) -> host asset ids

    def sensors_by_class(self) -> dict[tuple[str, str], int]:
        """Number of sensors per (sensor_type, placement), in rule order."""
        counts: dict[tuple[str, str], int] = {}
        for sensor in self.sensors:
            key = (sensor.sensor_type, sensor.placement)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def sensors_by_type(self) -> dict[str, int]:
        """Number of sensors per sensor type."""
        counts: dict[str, int] = {}
        for sensor in self.sensors:
            counts[sensor.sensor_type] = counts.get(sensor.sensor_type, 0) + 1
        return counts

    def monitored_asset_ids(self) -> list[str]:
        """Ids of the assets that host at least one sensor."""
        return sorted({sensor.asset_id for sensor in self.sensors})


# --- asset classification -----------------------------------------------------------------------------------
def _line_coords(asset: AssetRecord) -> list[Coord] | None:
    if asset.geometry.get("type") != "LineString":
        return None
    return [(float(p[0]), float(p[1])) for p in asset.geometry["coordinates"]]


def _outer_ring(asset: AssetRecord) -> list[Coord] | None:
    if asset.geometry.get("type") != "Polygon":
        return None
    return [(float(p[0]), float(p[1])) for p in asset.geometry["coordinates"][0]]


def footprint_m2(asset: AssetRecord) -> float:
    """Footprint of a building asset (0 when unknown)."""
    try:
        return float(asset.properties.get("footprint_m2") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def structure_length_m(asset: AssetRecord) -> float:
    """Length of a line asset or bridge: the computed ``length_m``, else the inventory's structure length."""
    for value in (asset.properties.get("length_m"), (asset.properties.get("nbi") or {}).get("structure_length_m")):
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    coords = _line_coords(asset)
    return geo.line_length(coords) if coords else 0.0


def is_culvert(asset: AssetRecord) -> bool:
    """True for bridge-registry assets recorded as culverts."""
    return asset.asset_type == "bridge" and asset.properties.get("structure_kind") == "culvert"


def is_nbi_matched_bridge(asset: AssetRecord) -> bool:
    """True for a mapped (OpenStreetMap) bridge matched to a National Bridge Inventory record."""
    return (
        asset.asset_type == "bridge"
        and not is_culvert(asset)
        and asset.source_id != "nbi"
        and isinstance(asset.properties.get("nbi"), dict)
    )


def is_substation(asset: AssetRecord) -> bool:
    """True for power assets mapped as substations."""
    return asset.asset_type == "power" and asset.properties.get("power") == "substation"


def road_class_rank(asset: AssetRecord) -> float:
    """Rank of a road's highway class (higher = more important); links rank just below their class."""
    highway = str(asset.properties.get("highway_class") or "")
    base = highway.removesuffix("_link")
    rank = float(_ROAD_BASE_RANK.get(base, 1))
    return rank - 0.5 if highway.endswith("_link") else rank


def is_civic_building(asset: AssetRecord) -> bool:
    """Showcase rule: a named building that is tagged or named as a civic facility."""
    if asset.asset_type != "building" or not asset.name or asset.name.strip().isdigit():
        return False
    tagged = any(str(asset.properties.get(key) or "").lower() in CIVIC_TAG_VALUES for key in CIVIC_TAG_KEYS)
    return tagged or bool(CIVIC_NAME.search(asset.name))


def select_showcase_buildings(buildings: Sequence[AssetRecord], limit: int = SHOWCASE_BUILDINGS) -> list[AssetRecord]:
    """Up to ``limit`` showcase buildings: civic buildings by footprint, one per name, filled up by footprint."""

    def by_footprint(items: Iterable[AssetRecord]) -> list[AssetRecord]:
        return sorted(items, key=lambda a: (-footprint_m2(a), a.asset_id))

    chosen: list[AssetRecord] = []
    names: set[str] = set()
    for group in (by_footprint(b for b in buildings if is_civic_building(b)), by_footprint(buildings)):
        for building in group:
            if len(chosen) >= limit:
                return chosen
            key = (building.name or building.asset_id).strip().lower()
            if key in names or building in chosen:
                continue  # several footprints of one named facility collapse to the largest
            names.add(key)
            chosen.append(building)
    return chosen


# --- host selection -----------------------------------------------------------------------------------------
def class_rng(seed: int, *parts: str) -> random.Random:
    """Seeded generator for one selection step (independent of Python's hash randomisation)."""
    return random.Random(zlib.crc32(":".join(parts).encode("utf-8")) ^ (seed & 0xFFFFFFFF))


def spread_sample(
    candidates: Sequence[AssetRecord], n: int, rng: random.Random, fixed: Sequence[AssetRecord] = ()
) -> list[AssetRecord]:
    """Greedy farthest-point sampling: ``n`` hosts that cover the area, starting from the ``fixed`` ones.

    Without fixed hosts the first one is drawn with ``rng``; every further host is the candidate farthest
    from all hosts chosen so far (ties resolved by asset id). Returns the fixed hosts first.
    """
    chosen = list(fixed)[: max(n, 0)]
    taken = {asset.asset_id for asset in chosen}
    pool = sorted((a for a in candidates if a.asset_id not in taken), key=lambda a: a.asset_id)
    if len(chosen) + len(pool) <= n:
        return chosen + pool
    if not chosen and n > 0:
        chosen.append(pool.pop(rng.randrange(len(pool))))
    nearest = {
        a.asset_id: min(geo.haversine(*a.centroid, *c.centroid) for c in chosen) if chosen else math.inf for a in pool
    }
    while len(chosen) < n:
        best = max(pool, key=lambda a: nearest[a.asset_id])  # max() keeps the first of equals: lowest id
        pool.remove(best)
        chosen.append(best)
        for asset in pool:
            nearest[asset.asset_id] = min(nearest[asset.asset_id], geo.haversine(*asset.centroid, *best.centroid))
    return chosen


def _unused(assets: Sequence[AssetRecord], taken: Collection[str], needed: int) -> list[AssetRecord]:
    """Assets that do not host a sensor yet, or all of them when fewer than ``needed`` are still free."""
    free = [asset for asset in assets if asset.asset_id not in taken]
    return free if len(free) >= needed else list(assets)


def _road_pool(roads: Sequence[AssetRecord], cap: int, taken: Collection[str] = ()) -> list[AssetRecord]:
    """Road hosts: segments of at least MIN_HOST_ROAD_M that host no sensor yet (relaxed when too few)."""
    lines = [road for road in roads if _line_coords(road)]
    long_enough = [road for road in lines if structure_length_m(road) >= MIN_HOST_ROAD_M]
    return _unused(long_enough if len(long_enough) >= cap else lines, taken, cap)


def _building_pool(buildings: Sequence[AssetRecord], cap: int, taken: Collection[str] = ()) -> list[AssetRecord]:
    """Building hosts are sampled from the largest footprints (cap x BUILDING_POOL_FACTOR) without a sensor yet."""
    ranked = sorted(_unused(buildings, taken, cap), key=lambda a: (-footprint_m2(a), a.asset_id))
    return ranked[: cap * BUILDING_POOL_FACTOR]


def _highest_class_first(roads: Sequence[AssetRecord], cap: int, rng: random.Random) -> list[AssetRecord]:
    """Fill the cap class by class from the most important road class down, spread within each class."""
    chosen: list[AssetRecord] = []
    for rank in sorted({road_class_rank(road) for road in roads}, reverse=True):
        if len(chosen) >= cap:
            break
        tier = [road for road in roads if road_class_rank(road) == rank]
        chosen = (
            spread_sample(tier + chosen, cap, rng, fixed=chosen) if len(chosen) + len(tier) > cap else chosen + tier
        )
    return chosen[:cap]


# --- simulated water mains ----------------------------------------------------------------------------------
def build_water_main_line(road: AssetRecord, bbox: BBox) -> tuple[list[Coord], float] | None:
    """Offset the road's clipped centre line by 4.5 m and clip it to the study area; None when nothing is left.

    The left side of the digitised direction is tried first, then the right side (a road running along the
    edge of the study area may have only one side inside it).
    """
    coords = _line_coords(road)
    if not coords or len(coords) < 2:
        return None
    for side in (WATER_MAIN_OFFSET_M, -WATER_MAIN_OFFSET_M):
        clipped = clip_line([(x, y) for x, y in geo.offset_line(coords, side)], bbox)
        if clipped is not None and clipped.length_m >= MIN_WATER_MAIN_M:
            return [(float(x), float(y)) for x, y in clipped.coords], clipped.length_m
    return None


def plan_water_mains(roads: Sequence[AssetRecord], settings: Settings) -> list[WaterMain]:
    """The simulated mains: one per sampled host road, numbered in host-road order."""
    target = settings.SIM_WATER_MAINS
    if target <= 0:
        return []
    lines: dict[str, tuple[list[Coord], float]] = {}
    for road in roads:
        line = build_water_main_line(road, settings.bbox)
        if line is not None:
            lines[road.asset_id] = line
    pool = _road_pool([road for road in roads if road.asset_id in lines], target)
    hosts = spread_sample(pool, min(target, len(pool)), class_rng(settings.SIM_SEED, "pressure", "water_main"))
    mains: list[WaterMain] = []
    for number, road in enumerate(sorted(hosts, key=lambda a: a.asset_id), start=1):
        coords, length_m = lines[road.asset_id]
        mains.append(
            WaterMain(
                asset_id=make_asset_id("water_main", number),
                name=f"Simulated water main along {road.label}",
                host_road_id=road.asset_id,
                coords=tuple(coords),
                centroid=_rounded(geo.point_along(coords, 0.5), settings.bbox),
                length_m=length_m,
            )
        )
    return mains


# --- sensor positions ---------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Slot:
    """One sensor waiting for a position on its host asset."""

    rule: PlacementRule
    index: int  # 0-based index among the sensors of this rule on the asset
    count: int  # sensors of this rule on the asset


def _rounded(point: Coord, bbox: BBox) -> Coord:
    """Round to COORD_DECIMALS and keep the point inside the study-area rectangle."""
    lon = min(max(round(point[0], COORD_DECIMALS), bbox.west), bbox.east)
    lat = min(max(round(point[1], COORD_DECIMALS), bbox.south), bbox.north)
    return (lon, lat)


def _separate(target: float, placed: Sequence[float], length: float) -> float:
    """Move a distance along a line until it is MIN_SENSOR_SPACING_M away from the ones already placed."""
    candidate, step = target, 0
    while any(abs(candidate - other) < MIN_SENSOR_SPACING_M for other in placed) and step < SEPARATION_ATTEMPTS:
        step += 1
        shift = MIN_SENSOR_SPACING_M * ((step + 1) // 2) * (1 if step % 2 else -1)
        candidate = min(max(target + shift, 0.0), length)
    return candidate


def _line_positions(coords: list[Coord], slots: Sequence[_Slot]) -> list[Coord]:
    """Positions along a line asset: mid-point, third-points for two deck sensors, the line end for abutments."""
    length = geo.line_length(coords)
    placed: list[float] = []
    for slot in slots:
        if slot.rule.placement == "abutment_backfill":
            target = min(ABUTMENT_OFFSET_M, ABUTMENT_OFFSET_SHARE * length)
        elif slot.count > 1:
            target = length * (slot.index + 1) / (slot.count + 1)
        else:
            target = length / 2.0
        placed.append(_separate(target, placed, length))
    return [geo.point_at_distance(coords, distance) for distance in placed]


def _ring_positions(centre: Coord, count: int, inside: Callable[[Coord], bool]) -> list[Coord]:
    """The first sensor at the centre, the others on a small circle around it (kept inside the asset)."""
    positions = [centre]
    others = count - 1
    for k in range(others):
        preferred = math.pi / 2.0 + 2.0 * math.pi * k / others
        point: Coord | None = None
        # Preferred direction first, then the other directions; a smaller circle only for small footprints.
        for radius in RING_RADII_M:
            for turn in range(RING_DIRECTIONS):
                angle = preferred + turn * math.pi / (RING_DIRECTIONS / 2)
                candidate = geo.offset_point(centre[0], centre[1], radius * math.cos(angle), radius * math.sin(angle))
                clearance = min(geo.haversine(*candidate, *other) for other in positions)
                if inside(candidate) and clearance >= min(radius, MIN_SENSOR_SPACING_M) - SPACING_TOLERANCE_M:
                    point = candidate
                    break
            if point is not None:
                break
        positions.append(point if point is not None else centre)
    return positions


def sensor_positions(asset: AssetRecord, slots: Sequence[_Slot], bbox: BBox) -> list[Coord]:
    """Points on the asset for its sensors, a few metres apart, always inside the study area."""
    coords = _line_coords(asset)
    ring = _outer_ring(asset)
    if coords and len(coords) >= 2:
        points = _line_positions(coords, slots)
    elif ring and len(ring) >= 4:
        points = _ring_positions(geo.interior_point(ring), len(slots), lambda p: geo.point_in_polygon(p[0], p[1], ring))
    else:
        points = _ring_positions(asset.centroid, len(slots), lambda _p: True)
    return [_rounded(point, bbox) for point in points]


# --- the plan -----------------------------------------------------------------------------------------------
def _installed_at(sensor_id: str, settings: Settings) -> date:
    """A plausible simulated installation date: 6 months to 4 years before the simulated window."""
    rng = random.Random(zlib.crc32(sensor_id.encode("utf-8")) ^ (settings.SIM_SEED & 0xFFFFFFFF))
    return settings.SIM_START.astimezone(settings.tz).date() - timedelta(days=rng.randint(*INSTALLED_DAYS_BEFORE))


def _describe(rule: PlacementRule, asset: AssetRecord, index: int, count: int, simulated: bool) -> str:
    prefix = "Simulated " if simulated else ""
    host = asset.label
    if asset.asset_type == "water_main" and host.startswith("Simulated "):
        host = "the s" + host[1:]
    text = f"{prefix}{rule.device} on {host}"
    text = text[0].upper() + text[1:]
    return f"{text} ({index + 1} of {count})" if count > 1 else text


HostSelection = dict[tuple[str, str], list[tuple[AssetRecord, int]]]  # rule -> (host asset, sensors on it)


def _deck_sensor_count(bridge: AssetRecord) -> int:
    """Deck sensors of one kind on a structure: two on bridges of LONG_STRUCTURE_M or more, else one."""
    return 2 if not is_culvert(bridge) and structure_length_m(bridge) >= LONG_STRUCTURE_M else 1


def select_hosts(
    by_type: dict[str, list[AssetRecord]],
    main_assets: Sequence[AssetRecord],
    showcase_buildings: Sequence[AssetRecord],
    seed: int,
) -> HostSelection:
    """Hosts of every placement rule, each list sorted by asset id.

    Apart from the showcase assets, a building or road hosts one sensor class, so the network covers as many
    assets as the caps allow.
    """
    buildings, roads, bridges = by_type.get("building", []), by_type.get("road", []), by_type.get("bridge", [])
    selections: HostSelection = {}
    taken: set[str] = set()
    for rule in RULES:
        key = (rule.sensor_type, rule.placement)
        rng = class_rng(seed, rule.sensor_type, rule.placement)
        if key == ("vibration", "bridge_deck"):
            chosen = [(bridge, _deck_sensor_count(bridge)) for bridge in bridges]
        elif key == ("temperature", "bridge_deck"):
            chosen = [(bridge, _deck_sensor_count(bridge)) for bridge in bridges if not is_culvert(bridge)]
        elif key == ("moisture", "abutment_backfill"):
            chosen = [(bridge, 1) for bridge in bridges]
        elif key == ("temperature", "equipment"):
            chosen = [(asset, 1) for asset in by_type.get("power", []) if is_substation(asset)]
        elif key == ("pressure", "water_main"):
            chosen = [(asset, 1) for asset in main_assets]
        elif rule.host_type == "building":
            cap = min(rule.cap or 0, len(buildings))
            pool = _building_pool(buildings, cap, taken)
            chosen = [(b, 1) for b in spread_sample(pool, cap, rng, fixed=showcase_buildings[:cap])]
        elif key == ("vibration", "road_pavement"):
            cap = min(rule.cap or 0, len(roads))
            chosen = [(road, 1) for road in _highest_class_first(_road_pool(roads, cap, taken), cap, rng)]
        else:  # the other road classes
            cap = min(rule.cap or 0, len(roads))
            chosen = [(road, 1) for road in spread_sample(_road_pool(roads, cap, taken), cap, rng)]
        if rule.host_type in ("building", "road"):
            taken.update(asset.asset_id for asset, _count in chosen)
        selections[key] = sorted(chosen, key=lambda item: item[0].asset_id)
    return selections


def _number_sensors(selections: HostSelection) -> list[tuple[str, PlacementRule, AssetRecord, _Slot]]:
    """Sensor ids (per sensor type, in rule order) with the rule, the host and the slot on the host."""
    pending: list[tuple[str, PlacementRule, AssetRecord, _Slot]] = []
    counters: dict[str, int] = {}
    for rule in RULES:
        for asset, count in selections[(rule.sensor_type, rule.placement)]:
            for index in range(count):
                counters[rule.sensor_type] = counters.get(rule.sensor_type, 0) + 1
                sensor_id = f"{SENSOR_ID_PREFIXES[rule.sensor_type]}-{counters[rule.sensor_type]:03d}"
                pending.append((sensor_id, rule, asset, _Slot(rule, index, count)))
    return pending


def plan_placement(assets: Sequence[AssetRecord], settings: Settings) -> PlacementPlan:
    """Apply the placement rules to the real assets; pure and deterministic for a given SIM_SEED."""
    real = sorted((a for a in assets if a.asset_type != "water_main"), key=lambda a: a.asset_id)
    by_type: dict[str, list[AssetRecord]] = {}
    for asset in real:
        by_type.setdefault(asset.asset_type, []).append(asset)
    simulated = settings.SENSOR_SOURCE.strip().lower() == "simulated"
    source_name = SIMULATOR_SOURCE_ID if simulated else settings.SENSOR_SOURCE.strip().lower()

    showcase_buildings = select_showcase_buildings(by_type.get("building", []))
    showcase_bridges = [bridge for bridge in by_type.get("bridge", []) if is_nbi_matched_bridge(bridge)]
    showcase_ids = [a.asset_id for a in showcase_bridges] + [a.asset_id for a in showcase_buildings]
    nbi_bridge_ids = {bridge.asset_id for bridge in showcase_bridges}

    mains = plan_water_mains(by_type.get("road", []), settings)
    main_assets = [
        AssetRecord(m.asset_id, "water_main", m.name, m.geometry, m.centroid, m.properties, SIMULATOR_SOURCE_ID)
        for m in mains
    ]
    selections = select_hosts(by_type, main_assets, showcase_buildings, settings.SIM_SEED)
    pending = _number_sensors(selections)

    slots_by_asset: dict[str, list[_Slot]] = {}
    for _sensor_id, _rule, asset, slot in pending:
        slots_by_asset.setdefault(asset.asset_id, []).append(slot)
    asset_by_id = {asset.asset_id: asset for asset in [*real, *main_assets]}
    positions = {
        asset_id: dict(zip(slots, sensor_positions(asset_by_id[asset_id], slots, settings.bbox)))
        for asset_id, slots in slots_by_asset.items()
    }

    sensors: list[SensorSpec] = []
    for sensor_id, rule, asset, slot in pending:
        threshold_for(rule.sensor_type, rule.placement)  # the class must exist in infra.sensor_thresholds
        lon, lat = positions[asset.asset_id][slot]
        tagged = ((TAG_SHOWCASE, asset.asset_id in showcase_ids), (TAG_NBI_BRIDGE, asset.asset_id in nbi_bridge_ids))
        sensors.append(
            SensorSpec(
                sensor_id=sensor_id,
                asset_id=asset.asset_id,
                sensor_type=rule.sensor_type,
                placement=rule.placement,
                unit=UNITS[rule.sensor_type],
                lon=lon,
                lat=lat,
                description=_describe(rule, asset, slot.index, slot.count, simulated),
                is_simulated=simulated,
                source=source_name,
                installed_at=_installed_at(sensor_id, settings),
                sampling_interval_s=settings.SIM_STEP_MINUTES * 60,
                asset_type=asset.asset_type,
                asset_tags=tuple(tag for tag, present in tagged if present),
            )
        )
    hosts = {key: [asset.asset_id for asset, _count in chosen] for key, chosen in selections.items()}
    return PlacementPlan(sensors=sensors, water_mains=mains, showcase_asset_ids=showcase_ids, hosts=hosts)


# --- database layer -----------------------------------------------------------------------------------------
def load_assets(conn: psycopg.Connection) -> list[AssetRecord]:
    """The real assets of the registry, ordered by asset id."""
    rows = conn.execute(
        """
        SELECT asset_id, asset_type, name, ST_AsGeoJSON(geom, 9), ST_X(centroid), ST_Y(centroid), properties,
               source_id
        FROM infra.infrastructure_assets
        WHERE NOT is_simulated
        ORDER BY asset_id
        """
    ).fetchall()
    return [
        AssetRecord(
            asset_id=row[0],
            asset_type=row[1],
            name=row[2],
            geometry=json.loads(row[3]),
            centroid=(float(row[4]), float(row[5])),
            properties=row[6] or {},
            source_id=row[7],
        )
        for row in rows
    ]


def write_placement(conn: psycopg.Connection, plan: PlacementPlan) -> dict[str, int]:
    """Insert the simulated water mains and the sensors (the caller owns the transaction and has cleared both)."""
    row = conn.execute("SELECT study_area_id FROM infra.study_areas ORDER BY study_area_id LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("no study area in the database - run scripts/seed_database.py first")
    study_area_id = row[0]
    with conn.cursor() as cur:
        if plan.water_mains:
            cur.executemany(
                """
                INSERT INTO infra.infrastructure_assets
                    (asset_id, study_area_id, asset_type, category, name, is_simulated, source_id, properties,
                     geom, centroid)
                VALUES (%s, %s, 'water_main', %s, %s, true, %s, %s,
                        ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                """,
                [
                    (
                        main.asset_id, study_area_id, ASSET_CATEGORIES["water_main"], main.name, SIMULATOR_SOURCE_ID,
                        jsonb(main.properties), json.dumps(main.geometry, separators=(",", ":")), main.centroid[0],
                        main.centroid[1],
                    )
                    for main in plan.water_mains
                ],
            )  # fmt: skip
        if plan.sensors:
            cur.executemany(
                """
                INSERT INTO infra.sensors
                    (sensor_id, asset_id, sensor_type, placement, unit, description, is_simulated, source,
                     installed_at, sampling_interval_s, geom)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                """,
                [
                    (
                        s.sensor_id, s.asset_id, s.sensor_type, s.placement, s.unit, s.description, s.is_simulated,
                        s.source, s.installed_at, s.sampling_interval_s, s.lon, s.lat,
                    )
                    for s in plan.sensors
                ],
            )  # fmt: skip
    outside = conn.execute(
        """
        SELECT count(*) FROM infra.sensors s
        WHERE NOT EXISTS (SELECT 1 FROM infra.study_areas a WHERE ST_Covers(a.geom, s.geom))
        """
    ).fetchone()[0]
    if outside:
        raise RuntimeError(f"{outside} sensor(s) were placed outside the study area")
    return {"water_mains": len(plan.water_mains), "sensors": len(plan.sensors)}
