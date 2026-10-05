"""GIS processing (build contract sections 2, 3, 5) on the hand-made fixture in ``tests/fixtures/raw_small``.

The fixture is described in ``tests/fixtures/README.md``. No database, no network.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import shutil
from pathlib import Path

import pytest

from pipeline import geo
from pipeline.gis import nbi
from pipeline.gis import process as gis
from tests.support import ASSET_CATEGORY, ASSET_ID_PATTERN

FIXTURE_RAW = Path(__file__).parent / "fixtures" / "raw_small"
FIXTURE_BBOX = "37.7500,-100.0200,37.7600,-100.0100"  # south,west,north,east
WEST, SOUTH, EAST, NORTH = -100.02, 37.75, -100.01, 37.76
# Attributes the old prototype invented for real features; none may ever appear on an asset.
FABRICATED_KEYS = {
    "year_built", "condition", "condition_score", "pci", "aadt", "traffic", "resurfaced", "resurfacing_year",
    "last_resurfaced", "criticality", "material", "materials", "break_history", "breaks", "flow", "age",
    "diameter", "pressure_class", "install_year",
}  # fmt: skip
ALLOWED_PROPERTY_KEYS = {
    "building": {"osm_id", "building_type", "levels", "height_m", "height_source", "footprint_m2", "amenity",
                 "office", "operator"},
    "road": {"osm_id", "highway_class", "lanes", "oneway", "surface", "maxspeed", "ref", "length_m"},
    "bridge": {"structure_kind", "highway_class", "railway", "osm_way_ids", "length_m", "nbi", "surface",
               "maxspeed", "ref", "operator", "usage", "service"},
    "rail": {"osm_id", "railway", "operator", "usage", "service", "length_m"},
    "power": {"osm_id", "osm_type", "power", "operator", "voltage", "substation", "cables", "length_m",
              "footprint_m2"},
    "street_light": {"osm_id", "osm_type", "lamp_type", "lamp_mount", "light:method", "light:count", "operator",
                     "ref"},
}  # fmt: skip


@pytest.fixture(scope="module")
def fixture_settings(settings_factory):
    return settings_factory(STUDY_AREA_BBOX=FIXTURE_BBOX, STUDY_AREA_SLUG="fixture-area", STUDY_AREA_NAME="Fixture area")


@pytest.fixture(scope="module")
def processed(fixture_settings):
    """The fixture processed without measured (lidar) heights."""
    return gis.process(FIXTURE_RAW, fixture_settings)


@pytest.fixture
def raw_copy(tmp_path) -> Path:
    """A writable copy of the fixture."""
    target = tmp_path / "raw"
    shutil.copytree(FIXTURE_RAW, target)
    return target


def write_lidar_csv(raw_dir: Path, rows: list[str]) -> None:
    text = "osm_id,height_m,n_pixels,lidar_project,collected\n" + "".join(row + "\n" for row in rows)
    (raw_dir / "building_heights_3dep.csv").write_text(text, encoding="utf-8", newline="\n")


def assets(processed, asset_type: str | None = None) -> list[dict]:
    return [
        feature["properties"] | {"geometry": feature["geometry"]}
        for feature in processed.assets["features"]
        if asset_type is None or feature["properties"]["asset_type"] == asset_type
    ]


def asset_of_way(processed, osm_id: int) -> dict | None:
    found = [
        item
        for item in assets(processed)
        if item["attributes"].get("osm_id") == osm_id or osm_id in (item["attributes"].get("osm_way_ids") or [])
    ]
    assert len(found) <= 1, f"OSM way {osm_id} maps to {len(found)} assets"
    return found[0] if found else None


def building(processed, osm_id: int) -> dict:
    return next(f["properties"] for f in processed.buildings["features"] if f["properties"]["osm_id"] == osm_id)


# --- asset registry -----------------------------------------------------------------------------------------------
def test_asset_ids_are_sequential_zero_padded_and_sorted_by_source_id(processed):
    ids = {kind: [a["asset_id"] for a in assets(processed, kind)] for kind in ASSET_ID_PATTERN if kind != "water_main"}
    assert ids["building"] == [f"BLD-{n:04d}" for n in range(1, 8)]
    assert ids["road"] == [f"RD-{n:04d}" for n in range(1, 5)]
    assert ids["bridge"] == [f"BRG-{n:03d}" for n in range(1, 6)]
    assert ids["rail"] == ["RAIL-001"]
    assert ids["power"] == ["PWR-001", "PWR-002", "PWR-003"]
    assert ids["street_light"] == ["SL-001"]
    # numbering follows the source id: OSM way id for mapped features, then NBI structure number
    assert [a["attributes"]["osm_id"] for a in assets(processed, "building")] == [1001, 1002, 1004, 1005, 1007, 1008, 1010]
    assert [a["attributes"]["osm_id"] for a in assets(processed, "road")] == [2003, 2004, 2005, 2008]
    bridges = assets(processed, "bridge")
    assert [b["attributes"].get("osm_way_ids") for b in bridges[:3]] == [[2001, 2002], [2012], [3002]]
    assert [b["attributes"]["nbi"]["structure_number"] for b in bridges[3:]] == ["000000000000033", "000000000000044"]


def test_ids_categories_and_flags_follow_the_contract(processed):
    for item in assets(processed):
        assert re.fullmatch(ASSET_ID_PATTERN[item["asset_type"]], item["asset_id"])
        assert item["category"] == ASSET_CATEGORY[item["asset_type"]]
        assert item["is_simulated"] is False
        assert item["source_id"] in ("osm", "nbi")
    assert not assets(processed, "water_main")  # simulated mains are created by the sensor stage


def test_each_osm_way_maps_to_exactly_one_asset(processed):
    owners: dict[int, list[str]] = {}
    for item in assets(processed):
        ways = item["attributes"].get("osm_way_ids") or (
            [item["attributes"]["osm_id"]] if item["attributes"].get("osm_type", "way") == "way"
            and "osm_id" in item["attributes"] else []
        )  # fmt: skip
        for way_id in ways:
            owners.setdefault(way_id, []).append(item["asset_id"])
    assert all(len(asset_ids) == 1 for asset_ids in owners.values()), owners
    assert set(owners) == {1001, 1002, 1004, 1005, 1007, 1008, 1010, 2001, 2002, 2003, 2004, 2005, 2008, 2012, 3001,
                           3002, 4001, 4002}  # fmt: skip


def test_processing_is_deterministic_and_independent_of_the_element_order(fixture_settings, raw_copy, processed):
    again = gis.process(FIXTURE_RAW, fixture_settings)
    assert again.assets == processed.assets and again.buildings == processed.buildings and again.roads == processed.roads
    osm_path = raw_copy / "osm.json"
    payload = json.loads(osm_path.read_text(encoding="utf-8"))
    random.Random(5).shuffle(payload["elements"])
    osm_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    shuffled = gis.process(raw_copy, fixture_settings)
    assert shuffled.assets == processed.assets
    assert shuffled.roads == processed.roads


def test_no_asset_carries_a_fabricated_attribute(processed):
    for item in assets(processed):
        keys = set(item["attributes"])
        assert not keys & FABRICATED_KEYS, (item["asset_id"], keys & FABRICATED_KEYS)
        plain = {key for key in keys if not key.startswith("addr:")}
        assert plain <= ALLOWED_PROPERTY_KEYS[item["asset_type"]], (item["asset_id"], plain)


def test_attributes_hold_only_what_the_source_recorded(processed):
    # tags of interest are copied when present and never defaulted
    elm = asset_of_way(processed, 2004)["attributes"]
    assert elm == {"osm_id": 2004, "highway_class": "residential", "lanes": 2, "oneway": True, "surface": "asphalt",
                   "maxspeed": "25 mph", "length_m": elm["length_m"]}  # fmt: skip
    plain_road = asset_of_way(processed, 2003)["attributes"]
    assert set(plain_road) == {"osm_id", "highway_class", "length_m"}  # no surface / lanes / maxspeed invented
    courthouse = asset_of_way(processed, 1001)["attributes"]
    assert courthouse["amenity"] == "courthouse"
    assert courthouse["addr:street"] == "Main Street" and courthouse["addr:housenumber"] == "100"
    house = asset_of_way(processed, 1005)["attributes"]
    assert set(house) == {"osm_id", "building_type", "height_m", "height_source", "footprint_m2"}


# --- buildings ----------------------------------------------------------------------------------------------------
def test_small_unclosed_and_outside_buildings_are_dropped(processed):
    kept = {f["properties"]["osm_id"] for f in processed.buildings["features"]}
    assert kept == {1001, 1002, 1004, 1005, 1007, 1008, 1010}
    report = processed.report["buildings"]
    assert report["input"] == 10 and report["kept"] == 7
    assert report["dropped_footprint_under_25_m2"] == 1  # way 1003: 5 m x 4 m
    assert report["dropped_centroid_outside"] == 1  # way 1006
    assert report["dropped_not_closed"] == 1  # way 1009
    for dropped in (1003, 1006, 1009):
        assert asset_of_way(processed, dropped) is None


def test_a_building_with_its_centroid_inside_is_kept_whole(processed):
    feature = next(f for f in processed.buildings["features"] if f["properties"]["osm_id"] == 1010)
    lons = [point[0] for point in feature["geometry"]["coordinates"][0]]
    assert min(lons) < WEST  # the footprint was not clipped at the study-area edge
    centroid = asset_of_way(processed, 1010)["centroid"]
    assert WEST <= centroid[0] <= EAST and SOUTH <= centroid[1] <= NORTH


def test_footprints_are_computed_from_the_geometry(processed):
    expected = {1001: 40 * 30, 1002: 20 * 15, 1004: 30 * 20, 1005: 12 * 10, 1007: 50 * 40, 1008: 45 * 40}
    for osm_id, area in expected.items():
        assert building(processed, osm_id)["footprint_m2"] == pytest.approx(area, rel=0.01)
        assert asset_of_way(processed, osm_id)["attributes"]["footprint_m2"] == building(processed, osm_id)["footprint_m2"]


def test_building_rings_are_closed_and_counter_clockwise(processed):
    for feature in processed.buildings["features"]:
        ring = feature["geometry"]["coordinates"][0]
        assert feature["geometry"]["type"] == "Polygon" and ring[0] == ring[-1]
        assert geo.ring_signed_area_deg(ring[:-1]) > 0


def test_purely_numeric_names_become_null(processed):
    assert building(processed, 1002)["name"] is None
    assert asset_of_way(processed, 1002)["name"] is None
    assert processed.report["buildings"]["numeric_names_cleared"] == 1
    assert asset_of_way(processed, 1001)["name"] == "County Courthouse"
    assert asset_of_way(processed, 1005)["name"] is None  # untagged stays null, never ""


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [("1", None), ("10", None), ("007", None), (" 42 ", None), ("", None), (None, None), ("   ", None),
     ("Building 7", "Building 7"), ("7 Eleven", "7 Eleven"), (" City Hall ", "City Hall"), ("1st", "1st")],
)  # fmt: skip
def test_clean_name(raw, cleaned):
    assert gis.clean_name(raw) == cleaned


# --- building heights ---------------------------------------------------------------------------------------------
def test_heights_without_the_lidar_file(processed):
    expected = {
        1001: (10.8, "osm_levels"),  # 3 levels x 3.6 m
        1002: (4.5, "estimated"),  # 300 m2: below 400 m2
        1004: (12.0, "osm_height"),  # height tag wins over its 2 levels
        1005: (5.0, "estimated"),  # house
        1007: (9.0, "estimated"),  # warehouse
        1008: (8.0, "estimated"),  # 1800 m2: 1500 m2 or more
        1010: (6.0, "estimated"),  # 620 m2: 400 to 1500 m2
    }
    for osm_id, (height, source) in expected.items():
        record = building(processed, osm_id)
        assert (record["height_m"], record["height_source"]) == (height, source), osm_id
        attributes = asset_of_way(processed, osm_id)["attributes"]
        assert (attributes["height_m"], attributes["height_source"]) == (height, source)
    assert processed.report["buildings"]["height_sources"] == {
        "osm_height": 1, "lidar_3dep": 0, "osm_levels": 1, "estimated": 5,
    }  # fmt: skip
    assert processed.report["inputs"]["lidar_heights_available"] == 0


def test_heights_with_the_lidar_file_follow_the_precedence(fixture_settings, raw_copy, caplog):
    write_lidar_csv(
        raw_copy,
        [
            "1001,14.2,60,KS_Area1_2014,2013-12",  # measured: beats building:levels
            "1004,9.9,40,KS_Area1_2014,2013-12",  # the OSM height tag still wins
            "1005,6.4,12,KS_Area1_2014,2013-12",  # measured: beats the estimate
            "1007,75.0,90,KS_Area1_2014,2013-12",  # above 60 m: not usable
            "1002,1.0,8,KS_Area1_2014,2013-12",  # below 2.5 m: not usable
            "1008,abc,8,KS_Area1_2014,2013-12",  # unreadable
            "1001,20.0,60,KS_Area1_2014,2013-12",  # repeated id: the first row counts
            "999999,8.0,10,KS_Area1_2014,2013-12",  # no such building
        ],
    )
    with caplog.at_level(logging.WARNING, logger="pipeline.gis.process"):
        data = gis.process(raw_copy, fixture_settings)
    expected = {
        1001: (14.2, "lidar_3dep"),
        1004: (12.0, "osm_height"),
        1005: (6.4, "lidar_3dep"),
        1007: (9.0, "estimated"),
        1002: (4.5, "estimated"),
        1008: (8.0, "estimated"),
    }
    for osm_id, (height, source) in expected.items():
        record = building(data, osm_id)
        assert (record["height_m"], record["height_source"]) == (height, source), osm_id
    assert data.report["buildings"]["height_sources"] == {"osm_height": 1, "lidar_3dep": 2, "osm_levels": 0, "estimated": 4}
    assert data.report["inputs"]["lidar_heights_available"] == 4  # 1001, 1004, 1005 and the unknown id
    assert any("4 row(s) ignored" in record.message for record in caplog.records)
    # everything but the heights is unchanged by the optional file
    strip = lambda features: [  # noqa: E731
        {k: v for k, v in f["properties"].items() if k not in ("height_m", "height_source")} for f in features
    ]
    assert strip(data.buildings["features"]) == strip(gis.process(FIXTURE_RAW, fixture_settings).buildings["features"])


def test_a_lidar_file_without_the_expected_columns_is_an_error(fixture_settings, raw_copy):
    (raw_copy / "building_heights_3dep.csv").write_text("id,metres\n1001,14.2\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="osm_id"):
        gis.process(raw_copy, fixture_settings)


@pytest.mark.parametrize(
    ("tags", "footprint", "lidar", "expected"),
    [
        ({"height": "12"}, 100.0, 30.0, (12.0, "osm_height")),
        ({"height": "40 ft"}, 100.0, None, (12.2, "osm_height")),
        ({"height": "tall", "building:levels": "2"}, 100.0, None, (7.2, "osm_levels")),
        ({"building:levels": "2"}, 100.0, 9.3, (9.3, "lidar_3dep")),
        ({"building:levels": "2"}, 100.0, 2.4, (7.2, "osm_levels")),  # lidar value below the usable range
        ({"building:levels": "2"}, 100.0, 60.1, (7.2, "osm_levels")),  # ... and above it
        ({"building:levels": "0"}, 100.0, None, (4.5, "estimated")),
        ({"building": "house"}, 5000.0, None, (5.0, "estimated")),
        ({"building": "residential"}, 100.0, None, (5.0, "estimated")),
        ({"building": "detached"}, 100.0, None, (5.0, "estimated")),
        ({"building": "cabin"}, 100.0, None, (5.0, "estimated")),
        ({"building": "garage"}, 100.0, None, (3.5, "estimated")),
        ({"building": "shed"}, 100.0, None, (3.5, "estimated")),
        ({"building": "roof"}, 100.0, None, (3.5, "estimated")),
        ({"building": "pavilion"}, 100.0, None, (3.5, "estimated")),
        ({"building": "industrial"}, 100.0, None, (9.0, "estimated")),
        ({"building": "warehouse"}, 100.0, None, (9.0, "estimated")),
        ({"building": "storage_tank"}, 100.0, None, (10.0, "estimated")),
        ({"building": "yes", "man_made": "storage_tank"}, 100.0, None, (10.0, "estimated")),
        ({"building": "yes"}, 399.9, None, (4.5, "estimated")),
        ({"building": "yes"}, 400.0, None, (6.0, "estimated")),
        ({"building": "yes"}, 1499.9, None, (6.0, "estimated")),
        ({"building": "yes"}, 1500.0, None, (8.0, "estimated")),
        ({"building": "retail"}, 250.0, None, (4.5, "estimated")),
    ],
)
def test_resolve_height_precedence_and_estimation_rule(tags, footprint, lidar, expected):
    height, source = gis.resolve_height(tags, footprint, lidar)
    assert (height, source) == (pytest.approx(expected[0], abs=0.05), expected[1])


def test_estimated_heights_use_no_randomness():
    first = [gis.estimate_height(kind, area) for kind in ("yes", "house", "garage", None) for area in (50, 500, 5000)]
    second = [gis.estimate_height(kind, area) for kind in ("yes", "house", "garage", None) for area in (50, 500, 5000)]
    assert first == second


# --- roads, clipping ----------------------------------------------------------------------------------------------
def test_road_assets_are_the_main_classes_only(processed):
    classes = {a["attributes"]["highway_class"] for a in assets(processed, "road")}
    assert classes == {"primary", "residential", "tertiary", "secondary"}
    base = {f["properties"]["osm_id"]: f["properties"] for f in processed.roads["features"]}
    assert set(base) == {2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2012}
    assert base[2006]["highway_class"] == "service" and asset_of_way(processed, 2006) is None
    assert base[2007]["highway_class"] == "footway" and asset_of_way(processed, 2007) is None
    assert 2009 not in base and 2010 not in base and 2011 not in base  # area, outside, proposed


@pytest.mark.parametrize(
    "highway",
    ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "motorway_link",
     "trunk_link", "primary_link", "secondary_link", "tertiary_link"],
)  # fmt: skip
def test_asset_road_classes(highway):
    assert highway in gis.ROAD_ASSET_CLASSES


@pytest.mark.parametrize("highway", ["service", "footway", "pedestrian", "track", "path", "cycleway", "steps"])
def test_base_map_only_road_classes(highway):
    assert highway not in gis.ROAD_ASSET_CLASSES


def test_a_road_crossing_the_edge_is_clipped_and_measured_inside(processed):
    border = asset_of_way(processed, 2005)
    coords = border["geometry"]["coordinates"]
    assert coords == [[-100.012, 37.7545], [EAST, 37.7545]]
    inside_length = geo.haversine(-100.012, 37.7545, EAST, 37.7545)
    assert border["attributes"]["length_m"] == pytest.approx(inside_length, abs=0.1)
    assert border["attributes"]["length_m"] == pytest.approx(176.0, abs=1.0)  # half of the 352 m way


def test_every_line_feature_lies_inside_the_study_area(processed):
    lines = [f for f in processed.assets["features"] + processed.roads["features"] if f["geometry"]["type"] == "LineString"]
    assert len(lines) >= 15
    for feature in lines:
        assert all(WEST <= lon <= EAST and SOUTH <= lat <= NORTH for lon, lat in feature["geometry"]["coordinates"])


def test_a_road_with_several_parts_keeps_the_longest_and_logs_it(fixture_settings, caplog):
    with caplog.at_level(logging.INFO, logger="pipeline.gis.process"):
        data = gis.process(FIXTURE_RAW, fixture_settings)
    loop = asset_of_way(data, 2008)
    assert loop["geometry"]["coordinates"] == [[-100.013, NORTH], [-100.013, 37.758]]  # the 221 m part, not the 111 m one
    assert loop["attributes"]["length_m"] == pytest.approx(0.002 * 111_195.08, abs=0.5)
    assert data.report["lines_kept_longest_part"] == [
        {"layer": "roads", "osm_id": 2008, "parts": 2, "kept_m": loop["attributes"]["length_m"],
         "dropped_m": pytest.approx(0.001 * 111_195.08, abs=0.5)}
    ]  # fmt: skip
    assert any("2008" in record.getMessage() and "longest" in record.getMessage() for record in caplog.records)


def test_line_centroids_lie_on_their_line(processed):
    for item in assets(processed):
        if item["geometry"]["type"] == "LineString":
            line = [tuple(point) for point in item["geometry"]["coordinates"]]
            assert geo.point_to_line_m(item["centroid"][0], item["centroid"][1], line) < 0.05


# --- bridges ------------------------------------------------------------------------------------------------------
def test_contiguous_bridge_ways_are_merged_into_one_asset(processed):
    bridge = asset_of_way(processed, 2001)
    assert bridge is asset_of_way(processed, 2002) or bridge == asset_of_way(processed, 2002)
    assert bridge["asset_type"] == "bridge" and bridge["asset_id"] == "BRG-001"
    assert bridge["attributes"]["osm_way_ids"] == [2001, 2002]
    assert bridge["attributes"]["structure_kind"] == "bridge"
    assert bridge["geometry"] == {
        "type": "LineString",
        "coordinates": [[-100.015, 37.756], [-100.015, 37.7563], [-100.015, 37.7566], [-100.015, 37.7572]],
    }
    assert bridge["attributes"]["length_m"] == pytest.approx(0.0012 * 111_195.08, abs=0.2)


def test_the_road_sharing_a_node_with_the_bridge_is_not_merged_into_it(processed):
    road = asset_of_way(processed, 2003)
    assert road["asset_type"] == "road"
    assert 2003 not in asset_of_way(processed, 2001)["attributes"]["osm_way_ids"]


def test_a_bridge_way_is_never_also_a_road_or_rail_asset(processed):
    for way_id in (2001, 2002, 2012, 3002):
        assert asset_of_way(processed, way_id)["asset_type"] == "bridge"
    base = {f["properties"]["osm_id"]: f["properties"]["is_bridge"] for f in processed.roads["features"]}
    assert base[2001] is True and base[2002] is True and base[2012] is True  # the base map keeps the flag
    assert base[2003] is False
    assert [a["attributes"]["osm_id"] for a in assets(processed, "rail")] == [3001]


def test_a_bridge_area_is_not_a_bridge_asset(processed):
    assert asset_of_way(processed, 2009) is None
    assert processed.report["roads"]["skipped_area"] == 1


def test_rail_bridge_is_a_bridge_asset_of_its_own_class(processed):
    rail_bridge = asset_of_way(processed, 3002)
    assert rail_bridge["attributes"]["railway"] == "rail"
    assert "highway_class" not in rail_bridge["attributes"] and "nbi" not in rail_bridge["attributes"]


def test_merge_bridge_ways_orders_the_chain_whatever_the_input_order():
    ways = [
        {"id": 3, "nodes": [30, 40], "geometry": [{"lon": 3.0, "lat": 0.0}, {"lon": 4.0, "lat": 0.0}],
         "tags": {"highway": "primary", "bridge": "yes"}},
        {"id": 1, "nodes": [20, 10], "geometry": [{"lon": 2.0, "lat": 0.0}, {"lon": 1.0, "lat": 0.0}],
         "tags": {"highway": "primary", "bridge": "yes", "name": "A"}},
        {"id": 2, "nodes": [20, 30], "geometry": [{"lon": 2.0, "lat": 0.0}, {"lon": 3.0, "lat": 0.0}],
         "tags": {"highway": "primary", "bridge": "viaduct", "name": "B"}},
        {"id": 9, "nodes": [40, 50], "geometry": [{"lon": 4.0, "lat": 0.0}, {"lon": 5.0, "lat": 0.0}],
         "tags": {"highway": "secondary", "bridge": "yes"}},  # other class: not merged although it touches
        {"id": 7, "nodes": [70, 71], "geometry": [{"lon": 9.0, "lat": 0.0}, {"lon": 9.5, "lat": 0.0}],
         "tags": {"highway": "primary", "bridge": "no"}},  # not a bridge
    ]  # fmt: skip
    chains = gis.merge_bridge_ways(ways)
    reversed_chains = gis.merge_bridge_ways(ways[::-1])
    assert [sorted(chain.way_ids) for chain in chains] == [[1, 2, 3], [9]]
    assert [chain.coords for chain in chains] == [chain.coords for chain in reversed_chains]
    merged = chains[0]
    xs = [point[0] for point in merged.coords]
    assert xs in ([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0])  # one continuous line, no repeated vertex
    assert merged.names == ["A", "B"] or merged.names == ["B", "A"]
    assert merged.kind == ("highway", "primary")


# --- National Bridge Inventory ------------------------------------------------------------------------------------
def test_nbi_record_is_attached_to_the_nearest_bridge_within_60_m(processed):
    bridge = asset_of_way(processed, 2001)
    record = bridge["attributes"]["nbi"]
    assert record["structure_number"] == "000000000000011"
    assert record["match_distance_m"] == pytest.approx(4.4, abs=0.3)
    assert record["location_check"] == "ok"
    assert bridge["source_id"] == "osm"  # the geometry is the mapped bridge
    assert processed.report["nbi"]["matched"] == [
        {"structure_number": "000000000000011", "osm_way_ids": [2001, 2002], "distance_m": record["match_distance_m"]}
    ]
    others = [b for b in assets(processed, "bridge") if b["asset_id"] != bridge["asset_id"]]
    assert all((b["attributes"].get("nbi") or {}).get("structure_number") != "000000000000011" for b in others)


def test_nbi_fields_are_parsed_as_recorded(processed):
    record = asset_of_way(processed, 2001)["attributes"]["nbi"]
    assert record["facility_carried"] == "2nd. AVENUE" and record["features_intersected"] == "TEST RIVER"
    assert (record["year_built"], record["year_reconstructed"]) == (1935, 2001)
    assert (record["adt"], record["adt_year"]) == (10500, 2022)  # ADT is always shown with its year
    assert (record["inspection_date"], record["inspection_label"]) == ("2023-02", "February 2023")  # '223' = 02/23
    assert (record["owner_code"], record["owner"]) == ("01", "State Highway Agency")
    assert (record["deck_condition"], record["superstructure_condition"], record["substructure_condition"]) == ("6", "6", "5")
    assert record["culvert_condition"] == "N"
    assert record["bridge_condition"] == "F"
    assert record["bridge_condition_label"] == "Fair (FHWA classification from the lowest component rating)"
    assert record["structure_length_m"] == 131.1
    assert "STATUS" not in record and "status" not in record and "DATE" not in record and "date" not in record


def test_nbi_record_with_mismatching_coordinates_creates_no_asset(processed, fixture_settings, caplog):
    numbers = {(b["attributes"].get("nbi") or {}).get("structure_number") for b in assets(processed, "bridge")}
    assert "000000000000022" not in numbers
    listed = processed.report["nbi"]["mismatched"]
    assert [item["structure_number"] for item in listed] == ["000000000000022"]
    assert listed[0]["distance_m"] > 500 and listed[0]["facility_carried"] == "US-99 HWY"
    assert "no asset created" in listed[0]["action"]
    # the mismatching record sits 15 m from the bridge: it must not win the match over the valid one either
    assert asset_of_way(processed, 2001)["attributes"]["nbi"]["structure_number"] == "000000000000011"
    with caplog.at_level(logging.WARNING, logger="pipeline.gis.process"):
        gis.process(FIXTURE_RAW, fixture_settings)
    assert any("000000000000022" in record.getMessage() for record in caplog.records)


def test_unmatched_valid_nbi_records_become_point_assets(processed):
    culvert, point_bridge = assets(processed, "bridge")[3:]
    assert culvert["geometry"] == {"type": "Point", "coordinates": [-100.0185, 37.755]}
    assert culvert["source_id"] == "nbi" and culvert["attributes"]["structure_kind"] == "culvert"
    assert culvert["attributes"]["nbi"]["culvert_condition"] == "6"
    assert culvert["attributes"]["nbi"]["year_reconstructed"] is None  # 0 = none recorded
    assert culvert["attributes"]["nbi"]["owner"] == "City or Municipal Highway Agency"
    assert culvert["attributes"]["nbi"]["inspection_label"] == "November 2022"
    assert culvert["attributes"]["nbi"]["bridge_condition_label"].startswith("Good (FHWA classification")
    assert culvert["name"] == "West Test St. over Drainage Ditch"
    assert point_bridge["source_id"] == "nbi" and point_bridge["attributes"]["structure_kind"] == "bridge"
    assert point_bridge["attributes"]["nbi"]["owner"] == "Owner code 02"  # other owners: the code is shown
    assert point_bridge["attributes"]["nbi"]["inspection_label"] == "September 1998"
    assert point_bridge["attributes"]["nbi"]["bridge_condition_label"].startswith("Poor (FHWA classification")
    assert set(culvert["attributes"]) == {"structure_kind", "nbi"}


def test_records_outside_the_area_or_without_coordinates_create_no_asset(processed):
    report = processed.report["nbi"]
    assert report["records"] == 6
    assert [item["structure_number"] for item in report["outside_study_area"]] == ["000000000000055"]
    assert [item["structure_number"] for item in report["unverified"]] == ["000000000000066"]
    assert [item["structure_number"] for item in report["point_assets"]] == ["000000000000033", "000000000000044"]


def test_no_nbi_label_is_a_safety_verdict(processed):
    text = json.dumps([b["attributes"].get("nbi") for b in assets(processed, "bridge")]).lower()
    assert "structurally deficient" not in text
    assert "unsafe" not in text and "safe" not in text


@pytest.mark.parametrize(
    ("raw", "pivot", "expected"),
    [("223", 2025, (2023, 2)), ("0223", 2025, (2023, 2)), ("1122", 2025, (2022, 11)), ("998", 2025, (1998, 9)),
     ("1225", 2025, (2025, 12)), ("126", 2025, (1926, 1)), (223, 2025, (2023, 2)), ("1323", 2025, None),
     ("0023", 2025, None), ("", 2025, None), (None, 2025, None), ("22023", 2025, None), ("ab", 2025, None)],
)  # fmt: skip
def test_inspection_date_is_mmyy_with_the_leading_zero_dropped(raw, pivot, expected):
    assert nbi.parse_inspection_date(raw, pivot) == expected


@pytest.mark.parametrize(
    ("lat_016", "long_017", "expected"),
    [
        ("37451800", "100010660", (-100.0185, 37.755)),
        ("37452376", "100005418", (-100.01505, 37.7566)),
        (37451800, 100010660, (-100.0185, 37.755)),
        ("0", "0", None),
        ("37451800", None, None),
        ("37611800", "100010660", None),  # 61 minutes
        ("374518000", "100010660", None),  # too many digits
        ("north", "west", None),
    ],
)
def test_dms_coordinates_decode_to_decimal_degrees_west_negative(lat_016, long_017, expected):
    decoded = nbi.dms_to_decimal(lat_016, long_017)
    if expected is None:
        assert decoded is None
    else:
        assert decoded == pytest.approx(expected, abs=1e-6)


def test_location_check_uses_a_500_m_tolerance():
    def record(lat_016: str) -> nbi.NbiRecord:
        feature = {"attributes": {"STRUCTURE_NUMBER_008": "X1", "LAT_016": lat_016, "LONG_017": "100010660"},
                   "geometry": {"x": -100.0185, "y": 37.755}}  # fmt: skip
        return nbi.parse_record(feature, 2025)

    near = record("37453400")  # 16 arc seconds north: about 494 m
    far = record("37453500")  # 17 arc seconds north: about 525 m
    assert (near.location_check, far.location_check) == ("ok", "mismatch")
    assert near.check_distance_m == pytest.approx(494.0, abs=3.0) and far.check_distance_m == pytest.approx(525.0, abs=3.0)
    assert nbi.LOCATION_MISMATCH_M == 500.0


def test_match_nbi_gives_each_record_and_each_bridge_at_most_one_partner():
    lines = {"a": [(-100.0, 37.0), (-100.0, 37.001)], "b": [(-100.0005, 37.0), (-100.0005, 37.001)]}

    def record(number: str, lon: float) -> nbi.NbiRecord:
        return nbi.NbiRecord(number, lon, 37.0005, "ok", lon, 37.0005, 0.0)

    east_m, _ = geo.metres_per_degree(37.0005)
    close_to_a = record("N1", -100.0 + 5.0 / east_m)
    also_a = record("N2", -100.0 + 20.0 / east_m)  # only "a" is within 60 m (44 m separate the two lines)
    matches = gis.match_nbi([also_a, close_to_a], lines)
    assert matches["N1"] == ("a", pytest.approx(5.0, abs=0.2))
    assert "N2" not in matches or matches["N2"][0] == "b"
    assert len({key for key, _ in matches.values()}) == len(matches)
    too_far = record("N3", -100.0 + 61.0 / east_m)
    assert gis.match_nbi([too_far], {"a": lines["a"]}) == {}
    failed_check = nbi.NbiRecord("N4", -100.0, 37.0005, "mismatch", None, None, 900.0)
    assert gis.match_nbi([failed_check], lines) == {}
    assert gis.NBI_MATCH_RADIUS_M == 60.0


# --- other layers -------------------------------------------------------------------------------------------------
def test_power_and_street_light_assets(processed):
    substation, line, node = assets(processed, "power")
    assert substation["geometry"]["type"] == "Point" and substation["name"] == "North Substation"
    assert substation["attributes"]["power"] == "substation" and substation["attributes"]["operator"] == "Test Power"
    assert line["geometry"]["type"] == "LineString"
    assert line["geometry"]["coordinates"][-1] == [-100.0124, NORTH]  # clipped at the northern edge
    assert node["attributes"] == {"osm_id": 5003, "osm_type": "node", "power": "substation"}
    lamps = assets(processed, "street_light")
    assert [lamp["attributes"]["osm_id"] for lamp in lamps] == [5001]  # 5002 lies outside
    assert lamps[0]["geometry"] == {"type": "Point", "coordinates": [-100.016, 37.7531]}


def test_study_area_feature_and_city_boundary(processed, fixture_settings):
    area = processed.study_area["features"][0]
    assert area["properties"]["bbox"] == [WEST, SOUTH, EAST, NORTH]  # west, south, east, north
    assert area["properties"]["slug"] == "fixture-area" and area["properties"]["utm_srid"] == 32614
    assert area["geometry"] == fixture_settings.study_area_geometry()
    boundary = processed.city_boundary["features"]
    assert len(boundary) == 1 and boundary[0]["geometry"]["type"] == "MultiPolygon"
    assert len(boundary[0]["geometry"]["coordinates"]) == 2  # the context outline is not clipped
    assert boundary[0]["properties"] == {"kind": "city_limits", "name": "Test City", "geoid": "9900001", "source_id": "tiger"}


def test_optional_layers_may_be_absent(fixture_settings, raw_copy):
    (raw_copy / "nbi_bridges.json").unlink()
    (raw_copy / "city_boundary.geojson").unlink()
    data = gis.process(raw_copy, fixture_settings)
    assert data.report["layers_absent"] == ["nbi", "tiger"]
    assert data.city_boundary is None
    bridges = assets(data, "bridge")
    assert [b["attributes"]["osm_way_ids"] for b in bridges] == [[2001, 2002], [2012], [3002]]
    assert all("nbi" not in b["attributes"] for b in bridges)
    assert bridges[0]["name"] == "Second Avenue"  # the mapped name when no inventory record names it


def test_missing_osm_file_is_an_error(fixture_settings, tmp_path):
    with pytest.raises(FileNotFoundError, match="osm.json"):
        gis.process(tmp_path, fixture_settings)


def test_write_outputs_writes_the_documented_files_and_removes_a_stale_boundary(processed, tmp_path):
    written = gis.write_outputs(processed, tmp_path)
    assert {path.name for path in written.values()} == {
        "study_area.geojson", "buildings.geojson", "roads.geojson", "assets.geojson", "city_boundary.geojson",
        "processing_report.json",
    }  # fmt: skip
    assert json.loads((tmp_path / "assets.geojson").read_text(encoding="utf-8")) == processed.assets
    assert b"\r\n" not in (tmp_path / "processing_report.json").read_bytes()
    without_boundary = copy.copy(processed)
    without_boundary.city_boundary = None
    gis.write_outputs(without_boundary, tmp_path)
    assert not (tmp_path / "city_boundary.geojson").exists()


# --- the committed default data -----------------------------------------------------------------------------------
def test_default_data_each_osm_way_is_one_asset_and_ids_are_well_formed(default_processed):
    owners: dict[int, int] = {}
    for feature in default_processed.assets["features"]:
        properties = feature["properties"]
        assert re.fullmatch(ASSET_ID_PATTERN[properties["asset_type"]], properties["asset_id"])
        assert properties["category"] == ASSET_CATEGORY[properties["asset_type"]]
        attributes = properties["attributes"]
        is_way = attributes.get("osm_type", "way") == "way"
        for way_id in attributes.get("osm_way_ids") or ([attributes["osm_id"]] if "osm_id" in attributes and is_way else []):
            owners[way_id] = owners.get(way_id, 0) + 1
    assert owners and set(owners.values()) == {1}
    ids = [f["properties"]["asset_id"] for f in default_processed.assets["features"]]
    assert len(ids) == len(set(ids))


def test_default_data_has_no_fabricated_attribute_and_no_numeric_names(default_processed):
    for feature in default_processed.assets["features"]:
        properties = feature["properties"]
        keys = {key for key in properties["attributes"] if not key.startswith("addr:")}
        assert not keys & FABRICATED_KEYS, (properties["asset_id"], keys & FABRICATED_KEYS)
        assert keys <= ALLOWED_PROPERTY_KEYS[properties["asset_type"]], (properties["asset_id"], keys)
        assert properties["name"] is None or not properties["name"].strip().isdigit()
        assert properties["name"] != ""


def test_default_data_buildings_respect_the_size_and_location_rules(default_processed, default_settings):
    bbox = default_settings.bbox
    for feature in default_processed.buildings["features"]:
        properties = feature["properties"]
        assert properties["footprint_m2"] >= 25.0
        assert properties["height_m"] > 0
        assert properties["height_source"] in ("osm_height", "lidar_3dep", "osm_levels", "estimated")
        centroid = geo.polygon_area_centroid([tuple(point) for point in feature["geometry"]["coordinates"][0]])
        assert bbox.contains(*centroid)
    for feature in default_processed.assets["features"] + default_processed.roads["features"]:
        if feature["geometry"]["type"] == "LineString":
            assert all(bbox.contains(lon, lat) for lon, lat in feature["geometry"]["coordinates"])
            assert feature["properties"].get("length_m", feature["properties"].get("attributes", {}).get("length_m")) >= 5.0


def test_default_data_mismatching_nbi_record_is_listed_and_creates_no_asset(default_processed):
    report = default_processed.report["nbi"]
    used = {
        (feature["properties"]["attributes"].get("nbi") or {}).get("structure_number")
        for feature in default_processed.assets["features"]
    } - {None}
    assert report["mismatched"], "the default data holds one record whose recorded coordinates disagree"
    for item in report["mismatched"]:
        assert item["distance_m"] > 500
        assert item["structure_number"] not in used
    assert len(used) + len(report["mismatched"]) + len(report["unverified"]) + len(report["outside_study_area"]) == report["records"]
