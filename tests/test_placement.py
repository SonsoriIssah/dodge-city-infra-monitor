"""Sensor placement rules (build contract section 7) on a synthetic asset registry. No database.

The registry below has no name of the default study area in it: the rules must work on any area.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from itertools import combinations

import pytest

from pipeline import geo
from pipeline.models import TAG_NBI_BRIDGE, TAG_SHOWCASE
from pipeline.sensors import placement
from pipeline.sensors.placement import AssetRecord
from tests.support import SENSOR_ID_PREFIX, THRESHOLDS, UNITS

BBOX = "37.7500,-100.0200,37.7600,-100.0100"  # south,west,north,east: about 880 m x 1105 m
WEST, SOUTH, EAST, NORTH = -100.02, 37.75, -100.01, 37.76
EAST_M, NORTH_M = geo.metres_per_degree(37.755)
# caps of the placement table (contract section 7)
BUILDING_CAPS = {("vibration", "building_structure"): 22, ("moisture", "foundation_perimeter"): 8,
                 ("temperature", "building_envelope"): 12}  # fmt: skip
ROAD_CAPS = {("vibration", "road_pavement"): 8, ("moisture", "road_subgrade"): 22, ("temperature", "road_surface"): 12}


def square(lon: float, lat: float, side_m: float) -> dict:
    dx, dy = side_m / EAST_M, side_m / NORTH_M
    ring = [[lon, lat], [lon + dx, lat], [lon + dx, lat + dy], [lon, lat + dy], [lon, lat]]
    return {"type": "Polygon", "coordinates": [ring]}


def make_building(number: int, lon: float, lat: float, side_m: float, name: str | None = None, **tags) -> AssetRecord:
    geometry = square(lon, lat, side_m)
    centre = (lon + side_m / EAST_M / 2, lat + side_m / NORTH_M / 2)
    properties = {"osm_id": 10_000 + number, "building_type": "yes", "footprint_m2": round(side_m * side_m, 1), **tags}
    return AssetRecord(f"BLD-{number:04d}", "building", name, geometry, centre, properties)


def make_road(number: int, lat: float, lon_from: float, length_m: float, highway: str) -> AssetRecord:
    coords = [[lon_from, lat], [lon_from + length_m / EAST_M / 2, lat], [lon_from + length_m / EAST_M, lat]]
    properties = {"osm_id": 20_000 + number, "highway_class": highway, "length_m": round(length_m, 1)}
    centre = (coords[1][0], lat)
    return AssetRecord(f"RD-{number:04d}", "road", f"Road {number}", {"type": "LineString", "coordinates": coords},
                       centre, properties)  # fmt: skip


def synthetic_registry(buildings: int = 64, roads: int = 50) -> list[AssetRecord]:
    """A regular town: a grid of footprints, east-west roads, three bridge structures, power and lamps."""
    assets: list[AssetRecord] = []
    for number in range(1, buildings + 1):
        row, column = divmod(number - 1, 8)
        side = 12.0 + (number * 7) % 23  # 12 .. 34 m, deterministic variety
        assets.append(make_building(number, WEST + 0.0010 + column * 0.0010, SOUTH + 0.0006 + row * 0.0009, side))
    classes = ["primary"] * 3 + ["secondary"] * 4 + ["tertiary"] * 6
    for number in range(1, roads + 1):
        highway = classes[number - 1] if number <= len(classes) else "residential"
        lat = SOUTH + 0.0003 + (number - 1) * 0.00019
        assets.append(make_road(number, lat, WEST + 0.0012 + (number % 5) * 0.0011, 180.0 + (number % 4) * 60.0, highway))
    long_bridge = [[-100.0150, 37.7560], [-100.0150, 37.7566], [-100.0150, 37.7572]]  # about 133 m
    assets.append(
        AssetRecord("BRG-001", "bridge", "Avenue over River", {"type": "LineString", "coordinates": long_bridge},
                    (-100.0150, 37.7566), {"structure_kind": "bridge", "highway_class": "primary",
                                           "osm_way_ids": [1, 2], "length_m": 133.4,
                                           "nbi": {"structure_number": "X1", "structure_length_m": 131.0}})
    )  # fmt: skip
    rail_bridge = [[-100.0172, 37.7510], [-100.0167, 37.7510]]  # about 44 m
    assets.append(
        AssetRecord("BRG-002", "bridge", "Railroad", {"type": "LineString", "coordinates": rail_bridge},
                    (-100.01695, 37.7510), {"structure_kind": "bridge", "railway": "rail", "osm_way_ids": [3],
                                            "length_m": 44.0})
    )  # fmt: skip
    assets.append(
        AssetRecord("BRG-003", "bridge", "Street over Ditch", {"type": "Point", "coordinates": [-100.0185, 37.7550]},
                    (-100.0185, 37.7550), {"structure_kind": "culvert", "nbi": {"structure_number": "X3"}}, "nbi")
    )  # fmt: skip
    assets.append(
        AssetRecord("PWR-001", "power", "Substation", {"type": "Point", "coordinates": [-100.0125, 37.7586]},
                    (-100.0125, 37.7586), {"power": "substation"})
    )  # fmt: skip
    assets.append(
        AssetRecord("PWR-002", "power", None,
                    {"type": "LineString", "coordinates": [[-100.0124, 37.7587], [-100.0124, 37.7599]]},
                    (-100.0124, 37.7593), {"power": "line", "length_m": 133.0})
    )  # fmt: skip
    for number in range(1, 4):
        point = [-100.0160 + number * 0.0005, 37.7531]
        assets.append(AssetRecord(f"SL-{number:03d}", "street_light", None, {"type": "Point", "coordinates": point},
                                  tuple(point), {"osm_id": 5000 + number}))  # fmt: skip
    return assets


def renamed(assets: list[AssetRecord], names: dict[str, tuple[str | None, dict]]) -> list[AssetRecord]:
    """Copy of the registry with new names / extra tags for some assets."""
    out = []
    for asset in assets:
        if asset.asset_id in names:
            name, tags = names[asset.asset_id]
            asset = AssetRecord(asset.asset_id, asset.asset_type, name, asset.geometry, asset.centroid,
                                {**asset.properties, **tags}, asset.source_id)  # fmt: skip
        out.append(asset)
    return out


@pytest.fixture(scope="module")
def town_settings(settings_factory):
    return settings_factory(STUDY_AREA_BBOX=BBOX, SIM_WATER_MAINS=6)


@pytest.fixture(scope="module")
def town() -> list[AssetRecord]:
    return synthetic_registry()


@pytest.fixture(scope="module")
def plan(town, town_settings):
    return placement.plan_placement(town, town_settings)


def by_class(plan) -> Counter:
    return Counter((s.sensor_type, s.placement) for s in plan.sensors)


def sensors_on(plan, asset_id: str) -> list:
    return [s for s in plan.sensors if s.asset_id == asset_id]


# --- counts -------------------------------------------------------------------------------------------------------
def test_counts_per_class_on_a_town_with_enough_hosts(plan):
    assert by_class(plan) == Counter({
        ("vibration", "bridge_deck"): 4,  # 2 on the 133 m bridge, 1 on the rail bridge, 1 on the culvert
        ("vibration", "building_structure"): 22,
        ("vibration", "road_pavement"): 8,
        ("moisture", "road_subgrade"): 22,
        ("moisture", "foundation_perimeter"): 8,
        ("moisture", "abutment_backfill"): 3,  # bridges and culverts, one each
        ("temperature", "bridge_deck"): 3,  # highway and rail bridges only: 2 + 1, none on the culvert
        ("temperature", "road_surface"): 12,
        ("temperature", "building_envelope"): 12,
        ("temperature", "equipment"): 1,  # the substation, not the power line
        ("pressure", "water_main"): 6,  # SIM_WATER_MAINS
    })  # fmt: skip


@pytest.mark.parametrize(("buildings", "roads"), [(5, 3), (10, 9), (30, 30), (2, 1)])
def test_every_count_is_a_cap_min_target_pool(settings_factory, buildings, roads):
    assets = [a for a in synthetic_registry(buildings, roads) if a.asset_type in ("building", "road")]
    settings = settings_factory(STUDY_AREA_BBOX=BBOX)  # SIM_WATER_MAINS = 28
    counts = by_class(placement.plan_placement(assets, settings))
    for key, cap in BUILDING_CAPS.items():
        assert counts[key] == min(cap, buildings), key
    for key, cap in ROAD_CAPS.items():
        assert counts[key] == min(cap, roads), key
    assert counts[("pressure", "water_main")] == min(28, roads)
    for key in (("vibration", "bridge_deck"), ("temperature", "bridge_deck"), ("moisture", "abutment_backfill"),
                ("temperature", "equipment")):  # fmt: skip
        assert counts[key] == 0  # no such assets: no such sensors, and no error


def test_an_empty_registry_gives_an_empty_plan(town_settings):
    empty = placement.plan_placement([], town_settings)
    assert empty.sensors == [] and empty.water_mains == [] and empty.showcase_asset_ids == []


def test_bridge_rules(plan):
    long_bridge = Counter((s.sensor_type, s.placement) for s in sensors_on(plan, "BRG-001"))
    assert long_bridge == {("vibration", "bridge_deck"): 2, ("temperature", "bridge_deck"): 2,
                           ("moisture", "abutment_backfill"): 1}  # fmt: skip
    rail_bridge = Counter((s.sensor_type, s.placement) for s in sensors_on(plan, "BRG-002"))
    assert rail_bridge == {("vibration", "bridge_deck"): 1, ("temperature", "bridge_deck"): 1,
                           ("moisture", "abutment_backfill"): 1}  # fmt: skip
    culvert = Counter((s.sensor_type, s.placement) for s in sensors_on(plan, "BRG-003"))
    assert culvert == {("vibration", "bridge_deck"): 1, ("moisture", "abutment_backfill"): 1}


@pytest.mark.parametrize(("length_m", "deck_sensors"), [(99.9, 1), (100.0, 2), (250.0, 2), (12.0, 1)])
def test_two_deck_sensors_from_one_hundred_metres(town_settings, length_m, deck_sensors):
    end_lat = 37.7560 + length_m / NORTH_M
    bridge = AssetRecord("BRG-001", "bridge", None,
                         {"type": "LineString", "coordinates": [[-100.015, 37.7560], [-100.015, end_lat]]},
                         (-100.015, (37.7560 + end_lat) / 2), {"structure_kind": "bridge", "length_m": length_m})  # fmt: skip
    counts = by_class(placement.plan_placement([bridge], town_settings))
    assert counts[("vibration", "bridge_deck")] == deck_sensors
    assert counts[("temperature", "bridge_deck")] == deck_sensors
    assert counts[("moisture", "abutment_backfill")] == 1


def test_only_substations_host_equipment_sensors(plan):
    hosts = {s.asset_id for s in plan.sensors if s.placement == "equipment"}
    assert hosts == {"PWR-001"}
    assert not sensors_on(plan, "PWR-002")
    assert not [s for s in plan.sensors if s.asset_type == "street_light"]


# --- identifiers, units, classes ----------------------------------------------------------------------------------
def test_sensor_ids_units_and_classes(plan, town_settings):
    for sensor_type, prefix in SENSOR_ID_PREFIX.items():
        ids = [s.sensor_id for s in plan.sensors if s.sensor_type == sensor_type]
        assert ids == [f"{prefix}-{n:03d}" for n in range(1, len(ids) + 1)]
    for sensor in plan.sensors:
        assert re.fullmatch(r"(TMP|VIB|MST|PRS)-\d{3}", sensor.sensor_id)
        assert sensor.unit == UNITS[sensor.sensor_type]
        assert (sensor.sensor_type, sensor.placement) in THRESHOLDS
        assert sensor.is_simulated is True and sensor.source == "simulator"
        assert sensor.sampling_interval_s == 3600
        assert sensor.description.startswith("Simulated ")
        assert sensor.installed_at < town_settings.SIM_START.date()
    assert len({s.sensor_id for s in plan.sensors}) == len(plan.sensors)


def test_numbering_follows_the_order_of_the_placement_table(plan):
    vibration = [s.placement for s in plan.sensors if s.sensor_type == "vibration"]
    assert vibration == ["bridge_deck"] * 4 + ["building_structure"] * 22 + ["road_pavement"] * 8
    moisture = [s.placement for s in plan.sensors if s.sensor_type == "moisture"]
    assert moisture == ["road_subgrade"] * 22 + ["foundation_perimeter"] * 8 + ["abutment_backfill"] * 3
    temperature = [s.placement for s in plan.sensors if s.sensor_type == "temperature"]
    assert temperature == ["bridge_deck"] * 3 + ["road_surface"] * 12 + ["building_envelope"] * 12 + ["equipment"]


def test_sensor_classes_match_their_host_type(plan, town):
    asset_type = {a.asset_id: a.asset_type for a in town} | {m.asset_id: "water_main" for m in plan.water_mains}
    expected_host = {"bridge_deck": "bridge", "abutment_backfill": "bridge", "building_structure": "building",
                     "foundation_perimeter": "building", "building_envelope": "building", "road_pavement": "road",
                     "road_subgrade": "road", "road_surface": "road", "equipment": "power", "water_main": "water_main"}  # fmt: skip
    for sensor in plan.sensors:
        assert asset_type[sensor.asset_id] == expected_host[sensor.placement]
        assert sensor.asset_type == asset_type[sensor.asset_id]


def test_a_real_sensor_source_is_not_labelled_simulated(town, settings_factory):
    settings = settings_factory(STUDY_AREA_BBOX=BBOX, SENSOR_SOURCE="http", HTTP_SOURCE_URL="https://gateway.example/r")
    sensors = placement.plan_placement(town, settings).sensors
    assert {s.is_simulated for s in sensors} == {False}
    assert {s.source for s in sensors} == {"http"}
    assert not any(s.description.startswith("Simulated ") for s in sensors if s.sensor_type != "pressure")


# --- showcase -----------------------------------------------------------------------------------------------------
CIVIC = {
    "BLD-0010": ("Riverside School", {"footprint_m2": 900.0}),
    "BLD-0011": ("Riverside School", {"footprint_m2": 400.0}),  # second footprint of the same facility
    "BLD-0020": ("Municipal Court", {"footprint_m2": 500.0}),
    "BLD-0030": ("Engine House", {"footprint_m2": 300.0, "amenity": "fire_station"}),  # civic by tag, not by name
    "BLD-0040": ("Grain Warehouse", {"footprint_m2": 5000.0}),  # the largest footprint, not civic
    "BLD-0041": (None, {"footprint_m2": 4000.0, "amenity": "school"}),  # civic tag but unnamed
}


def test_showcase_is_every_nbi_matched_bridge_plus_three_civic_buildings_by_footprint(town, town_settings):
    plan = placement.plan_placement(renamed(town, CIVIC), town_settings)
    assert plan.showcase_asset_ids == ["BRG-001", "BLD-0010", "BLD-0020", "BLD-0030"]
    for asset_id in plan.showcase_asset_ids:
        assert {s.sensor_type for s in sensors_on(plan, asset_id)} == {"vibration", "moisture", "temperature"}
        assert all(TAG_SHOWCASE in s.asset_tags for s in sensors_on(plan, asset_id))
    assert {s.asset_id for s in plan.sensors if TAG_NBI_BRIDGE in s.asset_tags} == {"BRG-001"}
    # the NBI culvert is a point record without a mapped bridge: not a showcase asset
    assert "BRG-003" not in plan.showcase_asset_ids and "BRG-002" not in plan.showcase_asset_ids


def test_showcase_buildings_are_filled_up_by_footprint_when_fewer_than_three_are_civic(town, town_settings):
    only_court = {key: CIVIC[key] for key in ("BLD-0020", "BLD-0040")}
    plan = placement.plan_placement(renamed(town, only_court), town_settings)
    largest_other = max(
        (a for a in town if a.asset_type == "building" and a.asset_id not in only_court),
        key=lambda a: (a.properties["footprint_m2"], -int(a.asset_id[-4:])),
    )
    assert plan.showcase_asset_ids == ["BRG-001", "BLD-0020", "BLD-0040", largest_other.asset_id]


def test_showcase_rule_needs_no_names_at_all(town, town_settings):
    unnamed = renamed(town, {a.asset_id: (None, {}) for a in town})
    plan = placement.plan_placement(unnamed, town_settings)
    buildings = sorted((a for a in town if a.asset_type == "building"),
                       key=lambda a: (-a.properties["footprint_m2"], a.asset_id))  # fmt: skip
    assert plan.showcase_asset_ids == ["BRG-001"] + [b.asset_id for b in buildings[:3]]
    assert by_class(plan) == by_class(placement.plan_placement(town, town_settings))  # same counts with or without names


@pytest.mark.parametrize(
    ("name", "tags", "civic"),
    [
        ("County Courthouse", {}, True),
        ("City Hall", {}, True),
        ("Government Center", {}, True),
        ("Lincoln Elementary School", {}, True),
        ("Annex", {"amenity": "townhall"}, True),
        ("Annex", {"office": "government"}, True),
        ("Annex", {"building_type": "hospital"}, True),
        ("Annex", {"amenity": "police"}, True),
        ("Annex", {"building_type": "public"}, True),
        ("Annex", {"building_type": "civic"}, True),
        ("Annex", {"amenity": "restaurant"}, False),
        ("Grain Warehouse", {}, False),
        (None, {"amenity": "school"}, False),
        ("12", {"amenity": "school"}, False),
    ],
)
def test_civic_building_rule(name, tags, civic):
    asset = AssetRecord("BLD-0001", "building", name, square(WEST, SOUTH, 20), (WEST, SOUTH), tags)
    assert placement.is_civic_building(asset) is civic


# --- host selection -----------------------------------------------------------------------------------------------
def test_pavement_vibration_goes_to_the_highest_road_classes_first(plan, town):
    hosts = {s.asset_id for s in plan.sensors if s.placement == "road_pavement"}
    highway = {a.asset_id: a.properties["highway_class"] for a in town if a.asset_type == "road"}
    chosen = Counter(highway[asset_id] for asset_id in hosts)
    assert chosen == {"primary": 3, "secondary": 4, "tertiary": 1}  # 3 + 4 exhaust the top classes, 1 more


def test_a_road_or_building_hosts_one_class_while_free_hosts_exist(plan):
    showcase = set(plan.showcase_asset_ids)
    per_asset: dict[str, set] = {}
    for sensor in plan.sensors:
        if sensor.asset_type in ("building", "road") and sensor.asset_id not in showcase:
            per_asset.setdefault(sensor.asset_id, set()).add(sensor.placement)
    assert per_asset and all(len(classes) == 1 for classes in per_asset.values())


def test_placement_is_deterministic_for_a_seed_and_differs_for_another(town, town_settings, settings_factory, plan):
    again = placement.plan_placement(list(reversed(town)), town_settings)  # input order must not matter
    assert again.sensors == plan.sensors and again.water_mains == plan.water_mains
    other = placement.plan_placement(town, settings_factory(STUDY_AREA_BBOX=BBOX, SIM_WATER_MAINS=6, SIM_SEED=7))
    assert by_class(other) == by_class(plan)
    assert other.showcase_asset_ids == plan.showcase_asset_ids  # chosen by rule, not by the seed
    assert other.hosts[("moisture", "road_subgrade")] != plan.hosts[("moisture", "road_subgrade")]


def test_farthest_point_sampling_spreads_the_hosts():
    def at(number: int, x_m: float) -> AssetRecord:
        return AssetRecord(f"A-{number}", "building", None, {}, (WEST + x_m / EAST_M, SOUTH))

    line = [at(1, 0), at(2, 10), at(3, 20), at(4, 30), at(5, 1000)]
    picked = placement.spread_sample(line, 2, random.Random(1), fixed=[line[0]])
    assert [a.asset_id for a in picked] == ["A-1", "A-5"]  # the farthest candidate from the fixed host
    three = placement.spread_sample(line, 3, random.Random(1), fixed=[line[0]])
    assert [a.asset_id for a in three] == ["A-1", "A-5", "A-4"]  # then the one farthest from both
    assert placement.spread_sample(line, 9, random.Random(1)) == line  # cap above the pool: everything
    assert placement.spread_sample(line, 0, random.Random(1)) == []


# --- positions ----------------------------------------------------------------------------------------------------
def test_every_sensor_lies_inside_the_study_area_and_on_its_asset(plan, town):
    geometry = {a.asset_id: a.geometry for a in town} | {m.asset_id: m.geometry for m in plan.water_mains}
    for sensor in plan.sensors:
        assert WEST <= sensor.lon <= EAST and SOUTH <= sensor.lat <= NORTH
        shape = geometry[sensor.asset_id]
        if shape["type"] == "LineString":
            assert geo.point_to_line_m(sensor.lon, sensor.lat, [tuple(p) for p in shape["coordinates"]]) < 0.5
        elif shape["type"] == "Polygon":
            assert geo.point_in_polygon(sensor.lon, sensor.lat, [tuple(p) for p in shape["coordinates"][0]])
        else:
            assert geo.haversine(sensor.lon, sensor.lat, *shape["coordinates"]) <= 8.5


def test_sensors_sharing_an_asset_are_a_few_metres_apart(town, town_settings):
    plan = placement.plan_placement(renamed(town, CIVIC), town_settings)
    shared = [sensors for sensors in (sensors_on(plan, asset_id) for asset_id in {s.asset_id for s in plan.sensors})
              if len(sensors) > 1]  # fmt: skip
    assert len(shared) >= 6  # three bridge structures and three showcase buildings
    for sensors in shared:
        for a, b in combinations(sensors, 2):
            distance = geo.haversine(a.lon, a.lat, b.lon, b.lat)
            assert distance >= 3.9, (a.sensor_id, b.sensor_id, distance)
    on_point = sensors_on(plan, "BRG-003")  # two sensors on a point asset stay within a few metres of it
    assert geo.haversine(on_point[0].lon, on_point[0].lat, on_point[1].lon, on_point[1].lat) <= 10.0


def test_deck_sensors_sit_at_the_third_points_and_the_abutment_probe_near_the_end(plan):
    bridge_line = [(-100.0150, 37.7560), (-100.0150, 37.7566), (-100.0150, 37.7572)]
    length = geo.line_length(bridge_line)
    along = lambda s: geo.haversine(*bridge_line[0], s.lon, s.lat)  # noqa: E731
    deck = sorted(along(s) for s in sensors_on(plan, "BRG-001") if s.placement == "bridge_deck" and s.sensor_type == "vibration")
    assert deck == pytest.approx([length / 3, 2 * length / 3], abs=0.5)
    abutment = next(s for s in sensors_on(plan, "BRG-001") if s.placement == "abutment_backfill")
    assert along(abutment) <= 8.0


def test_sensor_positions_on_a_small_footprint_stay_inside_it(town_settings):
    tiny = make_building(1, WEST + 0.001, SOUTH + 0.001, 6.0, "Engine House", amenity="fire_station")
    plan = placement.plan_placement([tiny], town_settings)
    ring = [tuple(p) for p in tiny.geometry["coordinates"][0]]
    assert len(plan.sensors) == 3  # showcase: one of each building class
    assert all(geo.point_in_polygon(s.lon, s.lat, ring) for s in plan.sensors)
    assert len({(s.lon, s.lat) for s in plan.sensors}) == 3


# --- simulated water mains ----------------------------------------------------------------------------------------
def test_water_mains_exist_only_where_a_pressure_sensor_is_hosted(plan, town):
    pressure = [s for s in plan.sensors if s.sensor_type == "pressure"]
    assert len(plan.water_mains) == 6 and len(pressure) == 6
    assert sorted(s.asset_id for s in pressure) == [m.asset_id for m in plan.water_mains]  # one sensor per main
    assert [m.asset_id for m in plan.water_mains] == [f"WM-{n:03d}" for n in range(1, 7)]
    assert {s.asset_type for s in pressure} == {"water_main"}
    assert not [s for s in plan.sensors if s.asset_type == "water_main" and s.sensor_type != "pressure"]


def test_water_mains_are_offsets_of_real_roads_with_no_invented_attribute(plan, town):
    roads = {a.asset_id: a for a in town if a.asset_type == "road"}
    hosts = [m.host_road_id for m in plan.water_mains]
    assert hosts == sorted(hosts) and len(set(hosts)) == len(hosts)  # numbered in host-road order, one main per road
    for main in plan.water_mains:
        host = roads[main.host_road_id]
        assert set(main.properties) == {"host_road_id", "length_m", "offset_m"}
        assert main.properties["offset_m"] == 4.5
        assert main.name == f"Simulated water main along {host.name}"
        host_line = [tuple(p) for p in host.geometry["coordinates"]]
        for lon, lat in main.coords:
            assert WEST <= lon <= EAST and SOUTH <= lat <= NORTH
            assert geo.point_to_line_m(lon, lat, host_line) == pytest.approx(4.5, abs=0.1)
        assert main.length_m == pytest.approx(geo.line_length(list(main.coords)), abs=0.2)
        assert main.geometry["type"] == "LineString"


@pytest.mark.parametrize("requested", [0, 1, 6, 28, 500])
def test_number_of_water_mains_is_a_cap(town, settings_factory, requested):
    settings = settings_factory(STUDY_AREA_BBOX=BBOX, SIM_WATER_MAINS=requested)
    plan = placement.plan_placement(town, settings)
    roads = sum(1 for a in town if a.asset_type == "road")
    assert len(plan.water_mains) == min(requested, roads)
    assert sum(1 for s in plan.sensors if s.sensor_type == "pressure") == min(requested, roads)


def test_a_road_on_the_edge_gets_its_main_on_the_inside(town_settings):
    on_west_edge = AssetRecord("RD-0001", "road", "Edge Road",
                               {"type": "LineString", "coordinates": [[WEST, 37.752], [WEST, 37.756]]},
                               (WEST, 37.754), {"highway_class": "residential", "length_m": 442.0})  # fmt: skip
    line = placement.build_water_main_line(on_west_edge, town_settings.bbox)
    assert line is not None
    coords, length = line
    assert all(lon > WEST for lon, _ in coords)  # 4.5 m east of the edge, not outside the area
    assert length == pytest.approx(442.0, rel=0.02)


# --- the default study area ---------------------------------------------------------------------------------------
def test_default_plan_respects_caps_and_class_table(default_plan, default_assets, default_settings):
    counts = by_class(default_plan)
    pool = Counter(a.asset_type for a in default_assets)
    for key, cap in BUILDING_CAPS.items():
        assert counts[key] == min(cap, pool["building"])
    for key, cap in ROAD_CAPS.items():
        assert counts[key] == min(cap, pool["road"])
    assert counts[("pressure", "water_main")] == len(default_plan.water_mains) == min(default_settings.SIM_WATER_MAINS, pool["road"])
    assert set(counts) <= set(THRESHOLDS)
    bbox = default_settings.bbox
    assert all(bbox.contains(s.lon, s.lat) for s in default_plan.sensors)
    for sensors in (sensors_on(default_plan, asset_id) for asset_id in default_plan.monitored_asset_ids()):
        for a, b in combinations(sensors, 2):
            assert geo.haversine(a.lon, a.lat, b.lon, b.lat) >= 1.0, (a.sensor_id, b.sensor_id)


def test_default_showcase_assets_carry_three_sensor_types(default_plan, default_assets):
    matched = [a.asset_id for a in default_assets if placement.is_nbi_matched_bridge(a)]
    assert matched and default_plan.showcase_asset_ids[: len(matched)] == matched
    assert len(default_plan.showcase_asset_ids) == len(matched) + 3
    for asset_id in default_plan.showcase_asset_ids:
        assert {s.sensor_type for s in sensors_on(default_plan, asset_id)} == {"vibration", "moisture", "temperature"}
