"""Every endpoint of build contract section 10.2: status, exact key sets, envelopes, rounding, units, filters.

Integration tests: the real application (``create_app(settings)`` + ``TestClient``) on ``infra_test``. The key
sets come from ``tests/support.py`` (typed from the contract); values are compared with the database through
SQL written for the test. Every 200 response is also validated against the strict response models of the
OpenAPI schema, which forbid unknown keys.
"""

from __future__ import annotations

import math
import re
from collections import Counter

import pytest

from backend.app import schemas
from tests import support
from tests.support import (
    ANOMALY_ITEM_KEYS,
    ASSET_COMMON_KEYS,
    ASSET_TYPE_KEYS,
    DATA_NOTICE,
    SENSOR_ITEM_KEYS,
    decimals,
    parse_z,
)

pytestmark = pytest.mark.db

T_END = "2026-10-01T04:00:00Z"
HALF_COORD = 5.1e-7  # half a unit of the sixth decimal
T_START = "2026-09-01T05:00:00Z"
STEPS = 720
BBOX = (-100.03, 37.745, -100.005, 37.762)  # west, south, east, north


def one(conn, query: str, params=None):
    return conn.execute(query, params).fetchone()[0]


def coordinates(geometry: dict):
    """Every [lon, lat] pair of a GeoJSON geometry."""
    def walk(node):
        if isinstance(node[0], (int, float)):
            yield node
        else:
            for child in node:
                yield from walk(child)

    yield from walk(geometry["coordinates"])


def assert_coordinates(geometry: dict) -> None:
    pairs = list(coordinates(geometry))
    assert pairs
    for lon, lat in pairs:
        assert -180 <= lon <= 180 and -90 <= lat <= 90
        assert decimals(lon) <= 6 and decimals(lat) <= 6


def assert_sensor_item(item: dict, extra: frozenset = frozenset()) -> None:
    assert set(item) == SENSOR_ITEM_KEYS | extra
    assert item["unit"] == support.UNITS[item["sensor_type"]]
    assert item["sensor_id"].startswith(support.SENSOR_ID_PREFIX[item["sensor_type"]] + "-")
    assert item["is_simulated"] is True and item["source"] == "simulator"
    assert item["status"] in ("normal", "warning", "anomaly", "offline")
    assert decimals(item["lon"]) <= 6 and decimals(item["lat"]) <= 6
    assert (item["sensor_type"], item["placement"]) in support.THRESHOLDS
    assert item["asset_name"] != "" and item["description"] != ""
    if item["status"] == "offline":
        assert item["latest"] is None
    else:
        assert set(item["latest"]) == support.SENSOR_LATEST_KEYS
        assert support.ISO_Z.match(item["latest"]["ts"])
        assert decimals(item["latest"]["value"]) <= 3 and decimals(item["latest"]["expected"]) <= 3
        assert decimals(item["latest"]["robust_z"]) <= 2
        assert item["latest"]["status"] in ("ok", "suspect")


def assert_anomaly_item(item: dict, extra: frozenset = frozenset()) -> None:
    assert set(item) == ANOMALY_ITEM_KEYS | extra
    assert re.fullmatch(r"ANM-\d{4}", item["anomaly_id"])
    assert item["unit"] == support.UNITS[item["sensor_type"]]
    assert item["is_simulated"] is True
    assert item["severity"] in support.SEVERITIES and item["status"] in ("active", "resolved")
    for key in ("started_at", "ended_at", "peak_at"):
        assert support.ISO_Z.match(item[key])
    assert item["started_at"] <= item["peak_at"] <= item["ended_at"]
    assert item["duration_hours"] == (parse_z(item["ended_at"]) - parse_z(item["started_at"])).total_seconds() / 3600 + 1
    assert decimals(item["observed_value"]) <= 3 and decimals(item["expected_value"]) <= 3
    assert decimals(item["robust_z"]) <= 2
    assert decimals(item["anomaly_score"]) <= 3 and 0 <= item["anomaly_score"] <= 1
    assert set(item["score_components"]) == {"magnitude", "duration", "threshold"}
    assert all(decimals(value) <= 3 and 0 <= value <= 1 for value in item["score_components"].values())
    assert decimals(item["lon"]) <= 6 and decimals(item["lat"]) <= 6
    assert item["detection_method"] and item["explanation"] and item["anomaly_label"]
    assert isinstance(item["nearby_asset_count"], int) and item["nearby_asset_count"] >= 0
    assert item["cluster_id"] is None or isinstance(item["cluster_id"], int)
    for near in item.get("nearby_assets") or []:
        assert set(near) == support.NEARBY_ASSET_KEYS
        assert decimals(near["distance_m"]) <= 1 and near["name"] != ""


def assert_asset_properties(properties: dict, extra: frozenset = frozenset()) -> None:
    kind = properties["asset_type"]
    plain = {key for key in properties if not key.startswith("addr:")}
    assert plain == ASSET_COMMON_KEYS | ASSET_TYPE_KEYS[kind] | extra, (properties["asset_id"], plain)
    assert re.fullmatch(support.ASSET_ID_PATTERN[kind], properties["asset_id"])
    assert properties["category"] == support.ASSET_CATEGORY[kind]
    assert properties["is_simulated"] is (kind == "water_main")
    assert properties["name"] != ""
    assert properties["monitored"] is (properties["sensor_count"] > 0)
    assert len(properties["centroid"]) == 2 and all(decimals(value) <= 6 for value in properties["centroid"])
    assert properties["sensor_types"] == sorted(properties["sensor_types"])
    if properties["monitored"]:
        assert properties["status"] in ("normal", "watch", "at_risk", "critical")
        assert isinstance(properties["health_score"], int) and 0 <= properties["health_score"] <= 100
    else:
        assert properties["status"] == "not_monitored" and properties["health_score"] is None
        assert properties["sensor_types"] == [] and properties["anomaly_count"] == 0


# --- /health ------------------------------------------------------------------------------------------------------
def test_health_reports_service_health_not_asset_health(get_json, db_conn):
    body = get_json("/health")
    assert set(body) == support.HEALTH_SERVICE_KEYS
    assert (body["status"], body["service"], body["database"]) == ("ok", support.SERVICE_NAME, "ok")
    assert body["version"] == "1.0.0"
    assert body["postgis"] == one(db_conn, "SELECT postgis_lib_version()")
    assert body["data_window"] == {"start": T_START, "end": T_END}
    assert body["note"] == support.HEALTH_NOTE
    assert set(body["asset_health"]) == {"as_of", "normal", "watch", "at_risk", "critical", "not_monitored"}
    assert body["asset_health"]["as_of"] == T_END
    stored = dict(db_conn.execute("SELECT status, count(*) FROM infra.asset_health WHERE as_of = %s GROUP BY 1", (parse_z(T_END),)).fetchall())
    for status in ("normal", "watch", "at_risk", "critical"):
        assert body["asset_health"][status] == stored.get(status, 0)
    assert body["asset_health"]["not_monitored"] == one(
        db_conn, "SELECT count(*) FROM infra.infrastructure_assets a WHERE NOT EXISTS (SELECT 1 FROM infra.sensors s WHERE s.asset_id = a.asset_id)"
    )
    schemas.ServiceHealth.model_validate(body)


# --- /statistics --------------------------------------------------------------------------------------------------
def test_statistics_keys_and_kpi_definitions_at_t_end(get_json, db_conn):
    body = get_json("/statistics")
    assert set(body) == support.STATISTICS_KEYS
    assert body["as_of"] == T_END and body["data_notice"] == DATA_NOTICE
    t_end = parse_z(T_END)
    assert body["total_assets"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets")
    assert body["real_assets"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated")
    assert body["simulated_assets"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE is_simulated")
    assert body["real_assets"] + body["simulated_assets"] == body["total_assets"]
    assert body["monitored_assets"] == one(db_conn, "SELECT count(DISTINCT asset_id) FROM infra.sensors")
    assert body["total_sensors"] == one(db_conn, "SELECT count(*) FROM infra.sensors")
    offline = one(db_conn, "SELECT count(*) FROM infra.sensors s WHERE NOT EXISTS (SELECT 1 FROM infra.sensor_readings r WHERE r.sensor_id = s.sensor_id AND r.ts = %s)", (t_end,))
    assert body["offline_sensors"] == offline and body["active_sensors"] == body["total_sensors"] - offline
    assert body["active_anomalies"] == one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE started_at <= %(t)s AND ended_at >= %(t)s", {"t": t_end})
    assert body["critical_alerts"] == one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE started_at <= %(t)s AND ended_at >= %(t)s AND severity = 'critical'", {"t": t_end})
    assert body["assets_at_risk"] == one(db_conn, "SELECT count(*) FROM infra.asset_health WHERE as_of = %s AND health_score < 70", (t_end,))
    assert body["anomalies_to_date"] == one(db_conn, "SELECT count(*) FROM infra.anomalies")
    assert body["anomalies_by_severity"] == {
        severity: one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE severity = %s", (severity,)) for severity in support.SEVERITIES
    }
    assert set(body["anomalies_by_sensor_type"]) == set(support.SENSOR_TYPES)
    assert sum(body["anomalies_by_sensor_type"].values()) == body["anomalies_to_date"]
    assert body["assets_by_type"] == dict(db_conn.execute("SELECT asset_type, count(*) FROM infra.infrastructure_assets GROUP BY 1").fetchall())
    assert body["critical_alerts"] >= 1 and body["active_anomalies"] >= 2  # the scenario ends with ongoing events
    schemas.Statistics.model_validate(body)


def test_statistics_warning_sensors_follow_the_rule(get_json, db_conn):
    """warning = reporting, no active anomaly, and the reading is flagged or outside the warning limits."""
    moment = one(db_conn, "SELECT ts FROM infra.reading_scores WHERE flagged GROUP BY ts ORDER BY count(*) DESC, ts LIMIT 1")
    expected = one(
        db_conn,
        """
        SELECT count(*) FROM infra.sensors s
        JOIN infra.sensor_thresholds th USING (sensor_type, placement)
        JOIN infra.sensor_readings r ON r.sensor_id = s.sensor_id AND r.ts = %(t)s
        JOIN infra.reading_scores sc ON sc.sensor_id = r.sensor_id AND sc.ts = r.ts
        WHERE (sc.flagged OR r.value < th.warn_low OR r.value > th.warn_high)
          AND NOT EXISTS (SELECT 1 FROM infra.anomalies an WHERE an.sensor_id = s.sensor_id AND an.started_at <= %(t)s AND an.ended_at >= %(t)s)
        """,
        {"t": moment},
    )
    body = get_json("/statistics", as_of=support.iso_z(moment))
    assert body["warning_sensors"] == expected


# --- /assets ------------------------------------------------------------------------------------------------------
def test_assets_is_a_geojson_collection_of_every_asset(get_json, db_conn):
    body = get_json("/assets")
    assert set(body) == {"type", "features", "numberMatched", "numberReturned"}
    assert body["type"] == "FeatureCollection"
    total = one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets")
    assert body["numberMatched"] == body["numberReturned"] == len(body["features"]) == total  # all rows when limit is omitted
    ids = [feature["properties"]["asset_id"] for feature in body["features"]]
    assert ids == sorted(ids) and len(set(ids)) == total
    seen = Counter()
    for feature in body["features"]:
        assert set(feature) == {"type", "geometry", "properties"} and feature["type"] == "Feature"
        assert_asset_properties(feature["properties"])
        seen[feature["properties"]["asset_type"]] += 1
    assert set(seen) == set(support.ASSET_TYPES)
    for feature in body["features"][:60] + body["features"][-60:]:
        assert_coordinates(feature["geometry"])
    schemas.AssetCollection.model_validate(body)


def test_asset_properties_equal_the_database_at_t_end(get_json, db_conn):
    features = {f["properties"]["asset_id"]: f["properties"] for f in get_json("/assets")["features"]}
    rows = db_conn.execute(
        """
        SELECT a.asset_id, a.name, a.source_id, a.properties, ST_X(a.centroid), ST_Y(a.centroid),
               (SELECT count(*) FROM infra.sensors s WHERE s.asset_id = a.asset_id),
               (SELECT array_agg(DISTINCT s.sensor_type ORDER BY s.sensor_type) FROM infra.sensors s WHERE s.asset_id = a.asset_id),
               (SELECT count(*) FROM infra.anomalies an WHERE an.asset_id = a.asset_id),
               (SELECT h.health_score FROM infra.asset_health h WHERE h.asset_id = a.asset_id AND h.as_of = %s),
               (SELECT h.status FROM infra.asset_health h WHERE h.asset_id = a.asset_id AND h.as_of = %s)
        FROM infra.infrastructure_assets a
        """,
        (parse_z(T_END), parse_z(T_END)),
    ).fetchall()
    assert len(rows) == len(features)
    for asset_id, name, source_id, stored, lon, lat, sensors, sensor_types, anomalies, score, status in rows:
        properties = features[asset_id]
        assert (properties["name"], properties["source_id"]) == (name, source_id)
        assert properties["centroid"] == pytest.approx([lon, lat], abs=HALF_COORD)  # rounded to 6 decimals
        assert (properties["sensor_count"], properties["sensor_types"], properties["anomaly_count"]) == (sensors, sensor_types or [], anomalies)
        assert (properties["health_score"], properties["status"]) == (score, status or "not_monitored")
        for key in ASSET_TYPE_KEYS[properties["asset_type"]]:
            assert properties[key] == stored.get(key), (asset_id, key)  # recorded attribute or null, never invented
        assert {k: v for k, v in properties.items() if k.startswith("addr:")} == {k: v for k, v in stored.items() if k.startswith("addr:")}


def test_asset_heights_carry_their_source(get_json):
    buildings = [f["properties"] for f in get_json("/assets", asset_type="building")["features"]]
    assert buildings and all(b["height_m"] > 0 for b in buildings)
    assert {b["height_source"] for b in buildings} <= {"osm_height", "lidar_3dep", "osm_levels", "estimated"}
    bridges = [f["properties"] for f in get_json("/assets", asset_type="bridge")["features"]]
    assert all(b["structure_kind"] in ("bridge", "culvert") for b in bridges)
    matched = [b for b in bridges if b["nbi"] and b["source_id"] == "osm"]
    assert matched and all("bridge_condition_label" in b["nbi"] for b in matched)
    mains = [f["properties"] for f in get_json("/assets", asset_type="water_main")["features"]]
    assert mains and all(re.fullmatch(r"RD-\d{4}", m["host_road_id"]) and m["is_simulated"] for m in mains)


@pytest.mark.parametrize("asset_type", support.ASSET_TYPES)
def test_assets_filter_by_type(get_json, asset_type):
    body = get_json("/assets", asset_type=asset_type)
    assert body["numberMatched"] == body["numberReturned"] == len(body["features"]) > 0
    assert {f["properties"]["asset_type"] for f in body["features"]} == {asset_type}


def test_assets_filter_by_category_monitored_and_status(get_json):
    everything = get_json("/assets")["features"]
    by = lambda predicate: sorted(f["properties"]["asset_id"] for f in everything if predicate(f["properties"]))  # noqa: E731
    ids = lambda body: [f["properties"]["asset_id"] for f in body["features"]]  # noqa: E731
    assert ids(get_json("/assets", category="Transportation")) == by(lambda p: p["category"] == "Transportation")
    assert ids(get_json("/assets", category="Simulated network")) == by(lambda p: p["asset_type"] == "water_main")
    assert ids(get_json("/assets", monitored="true")) == by(lambda p: p["monitored"])
    assert ids(get_json("/assets", monitored="false")) == by(lambda p: not p["monitored"])
    for status in ("normal", "watch", "at_risk", "critical", "not_monitored"):
        assert ids(get_json("/assets", status=status)) == by(lambda p, s=status: p["status"] == s)
    combined = get_json("/assets", asset_type="building", monitored="true", status="normal")
    assert ids(combined) == by(lambda p: p["asset_type"] == "building" and p["monitored"] and p["status"] == "normal")
    assert get_json("/assets", category="No such category") == {"type": "FeatureCollection", "features": [], "numberMatched": 0, "numberReturned": 0}


def test_assets_bbox_is_west_south_east_north(get_json, db_conn):
    west, south, east, north = -100.020, 37.750, -100.015, 37.754
    body = get_json("/assets", bbox=f"{west},{south},{east},{north}")
    expected = [row[0] for row in db_conn.execute(
        "SELECT asset_id FROM infra.infrastructure_assets WHERE ST_Intersects(geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326)) ORDER BY asset_id",
        (west, south, east, north),
    )]  # fmt: skip
    assert [f["properties"]["asset_id"] for f in body["features"]] == expected
    assert 0 < body["numberMatched"] < one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets")
    everything = get_json("/assets", bbox=",".join(str(value) for value in BBOX))
    assert everything["numberMatched"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets")


def test_assets_limit_and_offset_page_through_the_same_order(get_json):
    everything = [f["properties"]["asset_id"] for f in get_json("/assets")["features"]]
    first = get_json("/assets", limit=25)
    second = get_json("/assets", limit=25, offset=25)
    assert (first["numberMatched"], first["numberReturned"]) == (len(everything), 25)
    assert [f["properties"]["asset_id"] for f in first["features"]] == everything[:25]
    assert [f["properties"]["asset_id"] for f in second["features"]] == everything[25:50]
    beyond = get_json("/assets", limit=10, offset=len(everything) + 5)
    assert (beyond["numberMatched"], beyond["numberReturned"], beyond["features"]) == (len(everything), 0, [])
    last = get_json("/assets", asset_type="bridge", limit=20000)
    assert last["numberReturned"] == last["numberMatched"]


# --- /assets/{id} -------------------------------------------------------------------------------------------------
def monitored_asset(get_json, asset_type: str) -> str:
    return get_json("/assets", asset_type=asset_type, monitored="true", limit=1)["features"][0]["properties"]["asset_id"]


@pytest.mark.parametrize("asset_type", ["bridge", "building", "road", "water_main", "power"])
def test_asset_detail_of_a_monitored_asset(get_json, db_conn, asset_type):
    asset_id = monitored_asset(get_json, asset_type)
    body = get_json(f"/assets/{asset_id}")
    assert set(body) == {"type", "geometry", "properties", "as_of", "health", "sensors", "recent_anomalies", "provenance"}
    assert body["type"] == "Feature" and body["as_of"] == T_END
    assert_asset_properties(body["properties"])
    assert_coordinates(body["geometry"])
    assert body["properties"] == next(f["properties"] for f in get_json("/assets", asset_type=asset_type)["features"] if f["properties"]["asset_id"] == asset_id)
    assert set(body["health"]) == {"score", "status", "components"}
    assert set(body["health"]["components"]) == {"frequency_penalty", "severity_penalty", "reading_penalty", "sensor_penalty"}
    assert (body["health"]["score"], body["health"]["status"]) == (body["properties"]["health_score"], body["properties"]["status"])  # at T_end
    penalties = sum(body["health"]["components"].values())
    assert body["health"]["score"] == pytest.approx(100 - penalties, abs=0.51)  # the components explain the score
    assert len(body["sensors"]) == body["properties"]["sensor_count"]
    for sensor in body["sensors"]:
        assert_sensor_item(sensor)
        assert sensor["asset_id"] == asset_id
    assert len(body["recent_anomalies"]) == min(body["properties"]["anomaly_count"], 10)
    for anomaly in body["recent_anomalies"]:
        assert_anomaly_item(anomaly)
        assert anomaly["asset_id"] == asset_id
    assert [a["started_at"] for a in body["recent_anomalies"]] == sorted((a["started_at"] for a in body["recent_anomalies"]), reverse=True)
    assert set(body["provenance"]) == {"is_simulated", "sources", "attributes"}
    assert body["provenance"]["is_simulated"] is (asset_type == "water_main")
    assert body["provenance"]["attributes"] == one(db_conn, "SELECT properties FROM infra.infrastructure_assets WHERE asset_id = %s", (asset_id,))
    for source in body["provenance"]["sources"]:
        assert set(source) == support.DATA_SOURCE_KEYS
    assert body["provenance"]["sources"][0]["source_id"] == body["properties"]["source_id"]
    schemas.AssetDetail.model_validate(body)


def test_asset_detail_of_an_unmonitored_asset_has_no_score(get_json):
    asset_id = get_json("/assets", monitored="false", asset_type="building", limit=1)["features"][0]["properties"]["asset_id"]
    body = get_json(f"/assets/{asset_id}")
    assert body["health"] == {"score": None, "status": "not_monitored", "components": None}
    assert body["sensors"] == [] and body["recent_anomalies"] == []
    assert body["properties"]["status"] == "not_monitored" and body["properties"]["health_score"] is None
    schemas.AssetDetail.model_validate(body)


def test_asset_provenance_names_every_source_of_its_recorded_attributes(get_json):
    bridge = next(f["properties"] for f in get_json("/assets", asset_type="bridge")["features"] if f["properties"]["nbi"] and f["properties"]["source_id"] == "osm")
    sources = [s["source_id"] for s in get_json(f"/assets/{bridge['asset_id']}")["provenance"]["sources"]]
    assert sources == ["osm", "nbi"]
    lidar = next(f["properties"] for f in get_json("/assets", asset_type="building")["features"] if f["properties"]["height_source"] == "lidar_3dep")
    detail = get_json(f"/assets/{lidar['asset_id']}")["provenance"]
    assert [s["source_id"] for s in detail["sources"]] == ["osm", "usgs_3dep"]
    assert {s["kind"] for s in detail["sources"]} == {"real"}
    main = get_json("/assets", asset_type="water_main", limit=1)["features"][0]["properties"]["asset_id"]
    simulated = get_json(f"/assets/{main}")["provenance"]
    assert simulated["is_simulated"] is True and [(s["source_id"], s["kind"]) for s in simulated["sources"]] == [("simulator", "simulated")]


# --- /assets/{id}/health ------------------------------------------------------------------------------------------
def test_asset_health_series_is_columnar_over_the_whole_window(get_json, db_conn):
    asset_id = monitored_asset(get_json, "bridge")
    body = get_json(f"/assets/{asset_id}/health")
    assert set(body) == support.ASSET_HEALTH_SERIES_KEYS
    assert (body["asset_id"], body["start"], body["step_minutes"], body["count"]) == (asset_id, T_START, 60, STEPS)
    arrays = ("health_score", "frequency_penalty", "severity_penalty", "reading_penalty", "sensor_penalty", "active_anomalies", "sensors_reporting")
    assert all(len(body[name]) == STEPS for name in arrays) and len(body["status"]) == STEPS
    assert set(body["status"]) <= set("nwrc")
    assert isinstance(body["sensors_total"], int) and body["sensors_total"] == one(db_conn, "SELECT count(*) FROM infra.sensors WHERE asset_id = %s", (asset_id,))
    stored = db_conn.execute(
        "SELECT health_score, status, frequency_penalty, severity_penalty, reading_penalty, sensor_penalty, active_anomalies, sensors_reporting "
        "FROM infra.asset_health WHERE asset_id = %s ORDER BY as_of",
        (asset_id,),
    ).fetchall()
    assert body["health_score"] == [row[0] for row in stored]
    assert body["status"] == "".join({"normal": "n", "watch": "w", "at_risk": "r", "critical": "c"}[row[1]] for row in stored)
    for position, name in enumerate(("frequency_penalty", "severity_penalty", "reading_penalty", "sensor_penalty"), start=2):
        assert body[name] == pytest.approx([row[position] for row in stored], abs=6e-4)
        assert all(decimals(value) <= 3 for value in body[name])
    assert body["active_anomalies"] == [row[6] for row in stored] and body["sensors_reporting"] == [row[7] for row in stored]
    for score, status in zip(body["health_score"], body["status"]):
        assert status == ("n" if score >= 90 else "w" if score >= 70 else "r" if score >= 45 else "c")
    schemas.AssetHealthSeries.model_validate(body)


def test_asset_health_series_start_and_end_select_a_range(get_json):
    asset_id = monitored_asset(get_json, "bridge")
    whole = get_json(f"/assets/{asset_id}/health")
    part = get_json(f"/assets/{asset_id}/health", start="2026-09-10T00:20:00Z", end="2026-09-10T05:59:00Z")
    assert (part["start"], part["count"]) == ("2026-09-10T00:00:00Z", 6)  # floored to the hour, both ends inclusive
    offset = int((parse_z(part["start"]) - parse_z(T_START)).total_seconds() // 3600)
    assert part["health_score"] == whole["health_score"][offset : offset + 6]
    assert part["status"] == whole["status"][offset : offset + 6]
    clamped = get_json(f"/assets/{asset_id}/health", start="2020-01-01T00:00:00Z", end="2030-01-01T00:00:00Z")
    assert (clamped["start"], clamped["count"]) == (T_START, STEPS)


# --- /sensors -----------------------------------------------------------------------------------------------------
def test_sensors_envelope_and_items(get_json, db_conn):
    body = get_json("/sensors")
    total = one(db_conn, "SELECT count(*) FROM infra.sensors")
    assert set(body) == {"total", "limit", "offset", "as_of", "items"}
    assert (body["total"], body["offset"], body["as_of"]) == (total, 0, T_END) and len(body["items"]) == total
    assert body["limit"] >= total
    ids = [item["sensor_id"] for item in body["items"]]
    assert ids == sorted(ids)
    for item in body["items"]:
        assert_sensor_item(item)
    rows = {
        row[0]: row[1:]
        for row in db_conn.execute(
            """
            SELECT s.sensor_id, s.asset_id, a.name, a.asset_type, s.sensor_type, s.placement, s.unit, s.description,
                   ST_X(s.geom), ST_Y(s.geom),
                   (SELECT count(*) FROM infra.anomalies an WHERE an.sensor_id = s.sensor_id),
                   r.value, sc.expected, sc.robust_z
            FROM infra.sensors s JOIN infra.infrastructure_assets a USING (asset_id)
            LEFT JOIN infra.sensor_readings r ON r.sensor_id = s.sensor_id AND r.ts = %s
            LEFT JOIN infra.reading_scores sc ON sc.sensor_id = r.sensor_id AND sc.ts = r.ts
            """,
            (parse_z(T_END),),
        )
    }
    for item in body["items"]:
        asset_id, asset_name, asset_type, sensor_type, placement, unit, description, lon, lat, anomalies, value, expected, z = rows[item["sensor_id"]]
        assert (item["asset_id"], item["asset_name"], item["asset_type"]) == (asset_id, asset_name, asset_type)
        assert (item["sensor_type"], item["placement"], item["unit"], item["description"]) == (sensor_type, placement, unit, description)
        assert (item["lon"], item["lat"]) == pytest.approx((lon, lat), abs=HALF_COORD) and item["anomaly_count"] == anomalies
        if value is None:
            assert item["status"] == "offline" and item["latest"] is None
        else:
            assert item["latest"]["ts"] == T_END
            assert item["latest"]["value"] == pytest.approx(value, abs=5.1e-4)
            assert item["latest"]["expected"] == pytest.approx(expected, abs=5.1e-4)
            assert item["latest"]["robust_z"] == pytest.approx(z, abs=5.1e-3)
    assert Counter(item["status"] for item in body["items"])["offline"] == 2
    schemas.SensorList.model_validate(body)


def test_sensors_filters(get_json):
    everything = get_json("/sensors")["items"]
    ids = lambda body: [item["sensor_id"] for item in body["items"]]  # noqa: E731
    for sensor_type in support.SENSOR_TYPES:
        body = get_json("/sensors", sensor_type=sensor_type)
        assert ids(body) == [i["sensor_id"] for i in everything if i["sensor_type"] == sensor_type] and body["total"] == len(body["items"]) > 0
    two = get_json("/sensors", sensor_type="pressure,moisture")
    assert {item["sensor_type"] for item in two["items"]} == {"pressure", "moisture"}
    asset_id = Counter(item["asset_id"] for item in everything).most_common(1)[0][0]
    assert ids(get_json("/sensors", asset_id=asset_id)) == [i["sensor_id"] for i in everything if i["asset_id"] == asset_id]
    for status in ("normal", "warning", "anomaly", "offline"):
        assert ids(get_json("/sensors", status=status)) == [i["sensor_id"] for i in everything if i["status"] == status]
    assert len(get_json("/sensors", status="anomaly")["items"]) >= 2 and len(get_json("/sensors", status="offline")["items"]) == 2
    page = get_json("/sensors", limit=10, offset=20)
    assert (page["total"], page["limit"], page["offset"]) == (len(everything), 10, 20)
    assert ids(page) == [i["sensor_id"] for i in everything][20:30]


def test_sensor_detail(get_json, db_conn):
    sensor_id = one(db_conn, "SELECT sensor_id FROM infra.anomalies GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1")
    body = get_json(f"/sensors/{sensor_id}")
    assert set(body) == SENSOR_ITEM_KEYS | {"as_of", "thresholds", "baseline", "anomalies"}
    assert_sensor_item(body, frozenset({"as_of", "thresholds", "baseline", "anomalies"}))
    listed = next(item for item in get_json("/sensors")["items"] if item["sensor_id"] == sensor_id)
    assert {key: body[key] for key in SENSOR_ITEM_KEYS} == listed
    warn_low, warn_high, crit_low, crit_high = support.THRESHOLDS[(body["sensor_type"], body["placement"])]
    assert body["thresholds"] == {"warn_low": warn_low, "warn_high": warn_high, "crit_low": crit_low, "crit_high": crit_high}
    assert set(body["baseline"]) == {"scale", "floor"}
    floors = {"temperature": (0.5, 1.5), "vibration": (0.10,), "moisture": (0.5,), "pressure": (0.5,)}
    assert body["baseline"]["floor"] in floors[body["sensor_type"]] and body["baseline"]["scale"] >= body["baseline"]["floor"]
    assert body["anomaly_count"] == len(body["anomalies"]) == one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE sensor_id = %s", (sensor_id,))
    for anomaly in body["anomalies"]:
        assert_anomaly_item(anomaly)
        assert anomaly["sensor_id"] == sensor_id
    assert [a["started_at"] for a in body["anomalies"]] == sorted((a["started_at"] for a in body["anomalies"]), reverse=True)
    schemas.SensorDetail.model_validate(body)


# --- /sensor-readings ---------------------------------------------------------------------------------------------
def test_sensor_readings_records(get_json, db_conn):
    sensor_id = one(db_conn, "SELECT sensor_id FROM infra.sensors WHERE sensor_type = 'vibration' ORDER BY 1 LIMIT 1")
    body = get_json("/sensor-readings", sensor_id=sensor_id)
    assert set(body) == support.READINGS_RECORDS_KEYS
    assert (body["sensor_id"], body["sensor_type"], body["unit"], body["is_simulated"], body["source"]) == (sensor_id, "vibration", "mm/s", True, "simulator")
    assert body["data_notice"] == DATA_NOTICE and (body["start"], body["end"]) == (T_START, T_END)
    stored = db_conn.execute(
        "SELECT r.ts, r.value, r.status, sc.expected, sc.expected_low, sc.expected_high, sc.robust_z, sc.flagged "
        "FROM infra.sensor_readings r JOIN infra.reading_scores sc USING (sensor_id, ts) WHERE r.sensor_id = %s ORDER BY r.ts",
        (sensor_id,),
    ).fetchall()
    assert body["count"] == len(body["readings"]) == len(stored)
    for record, (ts, value, status, expected, low, high, z, flagged) in zip(body["readings"], stored):
        assert set(record) == support.READING_RECORD_KEYS
        assert record["ts"] == support.iso_z(ts) and record["status"] == status and record["flagged"] is flagged
        assert record["value"] == pytest.approx(value, abs=5.1e-4) and decimals(record["value"]) <= 3
        assert record["expected"] == pytest.approx(expected, abs=5.1e-4) and decimals(record["expected"]) <= 3
        assert record["expected_low"] == pytest.approx(low, abs=5.1e-4) and record["expected_high"] == pytest.approx(high, abs=5.1e-4)
        assert record["robust_z"] == pytest.approx(z, abs=5.1e-3) and decimals(record["robust_z"]) <= 2
    assert [r["ts"] for r in body["readings"]] == sorted(r["ts"] for r in body["readings"])
    schemas.ReadingsRecords.model_validate(body)


def test_sensor_readings_records_range_and_limit(get_json, db_conn):
    sensor_id = one(db_conn, "SELECT sensor_id FROM infra.sensors WHERE sensor_type = 'pressure' ORDER BY 1 LIMIT 1")
    part = get_json("/sensor-readings", sensor_id=sensor_id, start="2026-09-10T00:00:00Z", end="2026-09-10T05:00:00Z")
    assert [r["ts"] for r in part["readings"]] == [f"2026-09-10T0{h}:00:00Z" for h in range(6)]  # both ends inclusive
    assert (part["start"], part["end"], part["count"]) == ("2026-09-10T00:00:00Z", "2026-09-10T05:00:00Z", 6)
    limited = get_json("/sensor-readings", sensor_id=sensor_id, limit=5)
    assert limited["count"] == 5 and limited["readings"][0]["ts"] == T_START


def test_sensor_readings_columns_are_aligned_to_the_hourly_grid(get_json, db_conn):
    sensor_id = one(db_conn, "SELECT sensor_id FROM infra.sensors s ORDER BY (SELECT count(*) FROM infra.sensor_readings r WHERE r.sensor_id = s.sensor_id), 1 LIMIT 1")
    body = get_json("/sensor-readings", sensor_id=sensor_id, shape="columns")
    assert set(body) == support.READINGS_COLUMNS_KEYS | {"data_notice"}
    assert (body["start"], body["step_minutes"], body["count"]) == (T_START, 60, STEPS)
    assert body["is_simulated"] is True and body["data_notice"] == DATA_NOTICE
    assert set(body["thresholds"]) == {"warn_low", "warn_high", "crit_low", "crit_high"}
    for name in ("value", "expected", "expected_low", "expected_high", "robust_z"):
        assert len(body[name]) == STEPS
    records = {r["ts"]: r for r in get_json("/sensor-readings", sensor_id=sensor_id)["readings"]}
    assert len(records) < STEPS  # this sensor has a gap
    start = parse_z(T_START)
    flagged = []
    for index in range(STEPS):
        stamp = support.iso_z(start + index * (parse_z("2026-09-01T06:00:00Z") - start))
        record = records.get(stamp)
        if record is None:
            assert all(body[name][index] is None for name in ("value", "expected", "expected_low", "expected_high", "robust_z"))  # null where no reading
        else:
            assert [body[name][index] for name in ("value", "expected", "expected_low", "expected_high", "robust_z")] == [
                record["value"], record["expected"], record["expected_low"], record["expected_high"], record["robust_z"]
            ]  # fmt: skip
            if record["flagged"]:
                flagged.append(index)
    assert body["flagged"] == flagged  # indices of the flagged readings
    schemas.ReadingsColumns.model_validate(body)


def test_sensor_readings_columns_range_and_maximum_points(get_json, db_conn):
    sensor_id = one(db_conn, "SELECT sensor_id FROM infra.sensors ORDER BY 1 LIMIT 1")
    part = get_json("/sensor-readings", sensor_id=sensor_id, shape="columns", start="2026-09-30T22:10:00Z")
    assert (part["start"], part["count"]) == ("2026-09-30T22:00:00Z", 7)
    limited = get_json("/sensor-readings", sensor_id=sensor_id, shape="columns", limit=24)
    assert (limited["start"], limited["count"], len(limited["value"])) == (T_START, 24, 24)
    whole = get_json("/sensor-readings", sensor_id=sensor_id, shape="columns")
    assert limited["value"] == whole["value"][:24] and part["value"] == whole["value"][-7:]


# --- /anomalies ---------------------------------------------------------------------------------------------------
def test_anomalies_envelope_items_and_database_values(get_json, db_conn):
    body = get_json("/anomalies")
    total = one(db_conn, "SELECT count(*) FROM infra.anomalies")
    assert set(body) == {"total", "limit", "offset", "as_of", "data_notice", "items"}
    assert (body["total"], body["limit"], body["offset"], body["as_of"], body["data_notice"]) == (total, 500, 0, T_END, DATA_NOTICE)
    assert len(body["items"]) == total
    for item in body["items"]:
        assert_anomaly_item(item)
    stored = {
        row[0]: row[1:]
        for row in db_conn.execute(
            """
            SELECT an.anomaly_id, an.sensor_id, an.asset_id, a.name, a.asset_type, an.sensor_type, s.placement, an.anomaly_type,
                   an.started_at, an.ended_at, an.peak_at, an.duration_hours, an.observed_value, an.expected_value, an.unit,
                   an.robust_z, an.anomaly_score, an.score_components, an.severity, an.detection_method, an.explanation, an.status,
                   an.cluster_id, ST_X(an.geom), ST_Y(an.geom)
            FROM infra.anomalies an JOIN infra.sensors s USING (sensor_id) JOIN infra.infrastructure_assets a ON a.asset_id = an.asset_id
            """
        )
    }
    for item in body["items"]:
        (sensor_id, asset_id, asset_name, asset_type, sensor_type, placement, anomaly_type, started, ended, peak, duration, observed,
         expected, unit, z, score, components, severity, method, explanation, status, cluster_id, lon, lat) = stored[item["anomaly_id"]]  # fmt: skip
        assert (item["sensor_id"], item["asset_id"], item["asset_name"], item["asset_type"]) == (sensor_id, asset_id, asset_name, asset_type)
        assert (item["sensor_type"], item["placement"], item["anomaly_type"], item["unit"]) == (sensor_type, placement, anomaly_type, unit)
        assert item["anomaly_label"] == support.ANOMALY_LABELS[anomaly_type]
        assert (item["started_at"], item["ended_at"], item["peak_at"]) == (support.iso_z(started), support.iso_z(ended), support.iso_z(peak))
        assert item["duration_hours"] == duration
        assert item["observed_value"] == pytest.approx(observed, abs=5.1e-4) and item["expected_value"] == pytest.approx(expected, abs=5.1e-4)
        assert item["robust_z"] == pytest.approx(z, abs=5.1e-3) and item["anomaly_score"] == pytest.approx(score, abs=5.1e-4)
        assert item["score_components"] == pytest.approx(components, abs=5.1e-4)
        assert (item["severity"], item["detection_method"], item["explanation"]) == (severity, method, explanation)
        assert item["status"] == status  # at T_end the API status is the stored status (amendment A1)
        assert item["cluster_id"] == cluster_id and (item["lon"], item["lat"]) == pytest.approx((lon, lat), abs=HALF_COORD)
    schemas.AnomalyList.model_validate(body)


def test_anomalies_default_order_is_newest_first(get_json):
    items = get_json("/anomalies")["items"]
    keys = [(item["started_at"], item["anomaly_id"]) for item in items]
    assert keys == sorted(keys, reverse=True)


@pytest.mark.parametrize("sort", ["-started_at", "started_at", "-anomaly_score", "severity"])
def test_anomalies_sort_orders(get_json, sort):
    items = get_json("/anomalies", sort=sort)["items"]
    if sort == "started_at":
        assert [i["started_at"] for i in items] == sorted(i["started_at"] for i in items)
    elif sort == "-started_at":
        assert [i["started_at"] for i in items] == sorted((i["started_at"] for i in items), reverse=True)
    elif sort == "-anomaly_score":
        assert [i["anomaly_score"] for i in items] == sorted((i["anomaly_score"] for i in items), reverse=True)
    else:
        rank = [("critical", "high", "medium", "low").index(i["severity"]) for i in items]
        assert rank == sorted(rank)  # critical first
    assert len(items) == get_json("/anomalies")["total"]


def test_anomalies_filters(get_json):
    everything = get_json("/anomalies")["items"]
    ids = lambda body: sorted(item["anomaly_id"] for item in body["items"])  # noqa: E731
    pick = lambda predicate: sorted(item["anomaly_id"] for item in everything if predicate(item))  # noqa: E731
    for severity in support.SEVERITIES:
        body = get_json("/anomalies", severity=severity)
        assert ids(body) == pick(lambda i, s=severity: i["severity"] == s) and body["total"] == len(body["items"])
    assert ids(get_json("/anomalies", severity="critical,high")) == pick(lambda i: i["severity"] in ("critical", "high"))
    assert ids(get_json("/anomalies", severity=["critical", "low"])) == pick(lambda i: i["severity"] in ("critical", "low"))
    for sensor_type in support.SENSOR_TYPES:
        assert ids(get_json("/anomalies", sensor_type=sensor_type)) == pick(lambda i, t=sensor_type: i["sensor_type"] == t)
    assert ids(get_json("/anomalies", sensor_type="vibration,pressure", severity="high")) == pick(
        lambda i: i["sensor_type"] in ("vibration", "pressure") and i["severity"] == "high"
    )
    sample = everything[len(everything) // 2]
    assert ids(get_json("/anomalies", asset_id=sample["asset_id"])) == pick(lambda i: i["asset_id"] == sample["asset_id"])
    assert ids(get_json("/anomalies", sensor_id=sample["sensor_id"])) == pick(lambda i: i["sensor_id"] == sample["sensor_id"])
    assert ids(get_json("/anomalies", status="active")) == pick(lambda i: i["ended_at"] >= T_END)
    assert ids(get_json("/anomalies", status="resolved")) == pick(lambda i: i["ended_at"] < T_END)
    # start / end filter on started_at
    assert ids(get_json("/anomalies", start="2026-09-15T00:00:00Z", end="2026-09-22T00:00:00Z")) == pick(
        lambda i: "2026-09-15T00:00:00Z" <= i["started_at"] <= "2026-09-22T00:00:00Z"
    )
    assert ids(get_json("/anomalies", start=sample["started_at"], end=sample["started_at"])) == pick(lambda i: i["started_at"] == sample["started_at"])
    west, south, east, north = -100.021, 37.746, -100.018, 37.750
    assert ids(get_json("/anomalies", bbox=f"{west},{south},{east},{north}")) == pick(lambda i: west <= i["lon"] <= east and south <= i["lat"] <= north)


def test_anomalies_limit_offset_and_maximum(get_json):
    everything = get_json("/anomalies")["items"]
    page = get_json("/anomalies", limit=7, offset=14)
    assert (page["total"], page["limit"], page["offset"]) == (len(everything), 7, 14)
    assert [i["anomaly_id"] for i in page["items"]] == [i["anomaly_id"] for i in everything[14:21]]
    assert get_json("/anomalies", limit=1000)["limit"] == 1000
    empty = get_json("/anomalies", offset=len(everything))
    assert (empty["total"], empty["items"]) == (len(everything), [])


def test_anomalies_include_nearby_assets(get_json, db_conn, api_settings):
    plain = {item["anomaly_id"]: item for item in get_json("/anomalies")["items"]}
    body = get_json("/anomalies", include="nearby_assets", limit=1000)
    assert len(body["items"]) == len(plain)
    radius = api_settings.PROXIMITY_RADIUS_M
    for item in body["items"]:
        assert_anomaly_item(item, frozenset({"nearby_assets"}))
        assert item["nearby_asset_count"] == len(item["nearby_assets"]) == plain[item["anomaly_id"]]["nearby_asset_count"]
        assert {key: value for key, value in item.items() if key != "nearby_assets"} == plain[item["anomaly_id"]]
        distances = [near["distance_m"] for near in item["nearby_assets"]]
        assert distances == sorted(distances) and all(distance <= radius for distance in distances)
        assert item["asset_id"] not in {near["asset_id"] for near in item["nearby_assets"]}  # other assets only
    sample = body["items"][0]
    expected = db_conn.execute(
        """
        SELECT a.asset_id FROM infra.anomalies an CROSS JOIN infra.infrastructure_assets a
        WHERE an.anomaly_id = %s AND a.asset_id <> an.asset_id AND ST_Distance(a.geom::geography, an.geom::geography) <= %s
        """,
        (sample["anomaly_id"], radius),
    ).fetchall()
    assert {near["asset_id"] for near in sample["nearby_assets"]} == {row[0] for row in expected}


def test_anomaly_detail(get_json, db_conn):
    clustered = one(db_conn, "SELECT anomaly_id FROM infra.anomalies WHERE cluster_id IS NOT NULL ORDER BY 1 LIMIT 1")
    body = get_json(f"/anomalies/{clustered}")
    assert_anomaly_item(body, frozenset({"nearby_assets", "cluster"}))
    listed = next(item for item in get_json("/anomalies")["items"] if item["anomaly_id"] == clustered)
    assert {key: body[key] for key in ANOMALY_ITEM_KEYS} == listed
    assert set(body["cluster"]) == support.CLUSTER_KEYS
    assert body["cluster"]["cluster_id"] == body["cluster_id"] and clustered in body["cluster"]["anomaly_ids"]
    assert len(body["nearby_assets"]) == body["nearby_asset_count"]
    schemas.AnomalyDetail.model_validate(body)
    lone = one(db_conn, "SELECT anomaly_id FROM infra.anomalies WHERE cluster_id IS NULL ORDER BY 1 LIMIT 1")
    assert get_json(f"/anomalies/{lone}")["cluster"] is None


def test_anomaly_detail_radius(get_json, db_conn):
    anomaly_id = one(db_conn, "SELECT anomaly_id FROM infra.anomalies ORDER BY 1 LIMIT 1")
    default = get_json(f"/anomalies/{anomaly_id}")
    wide = get_json(f"/anomalies/{anomaly_id}", radius_m=300)
    narrow = get_json(f"/anomalies/{anomaly_id}", radius_m=5)
    assert narrow["nearby_asset_count"] <= default["nearby_asset_count"] < wide["nearby_asset_count"]
    assert max(n["distance_m"] for n in wide["nearby_assets"]) <= 300 and all(n["distance_m"] <= 100 for n in default["nearby_assets"])
    assert wide["nearby_asset_count"] == one(
        db_conn,
        "SELECT count(*) FROM infra.anomalies an CROSS JOIN infra.infrastructure_assets a WHERE an.anomaly_id = %s AND a.asset_id <> an.asset_id "
        "AND ST_Distance(a.geom::geography, an.geom::geography) <= 300",
        (anomaly_id,),
    )


# --- /simulation-events -------------------------------------------------------------------------------------------
def test_simulation_events(get_json, db_conn):
    body = get_json("/simulation-events")
    assert set(body) == {"total", "items"}
    assert body["total"] == len(body["items"]) == one(db_conn, "SELECT count(*) FROM infra.simulation_events")
    for item in body["items"]:
        assert set(item) == support.SIMULATION_EVENT_KEYS
        assert support.ISO_Z.match(item["started_at"]) and support.ISO_Z.match(item["ended_at"])
        assert item["magnitude"] is None or decimals(item["magnitude"]) <= 3
        if item["is_anomaly"]:
            assert item["sensor_id"] and item["asset_id"] and item["event_type"] in support.EVENT_MIX_40
        else:
            assert item["sensor_id"] is None and item["asset_id"] is None
    assert [item["event_id"] for item in body["items"]] == sorted(item["event_id"] for item in body["items"])
    injected = get_json("/simulation-events", is_anomaly="true")
    benign = get_json("/simulation-events", is_anomaly="false")
    assert Counter(item["event_type"] for item in injected["items"]) == Counter(support.EVENT_MIX_40)
    assert Counter(item["event_type"] for item in benign["items"]) == {"regional_rain": 3, "regional_hot_spell": 1}
    assert injected["total"] + benign["total"] == body["total"]
    schemas.SimulationEventList.model_validate(body)


# --- /spatial -----------------------------------------------------------------------------------------------------
def test_spatial_assets_within(get_json, db_conn, api_settings):
    lon, lat = -100.0195, 37.7474
    body = get_json("/spatial/assets-within", lon=lon, lat=lat, radius_m=150)
    assert set(body) == {"type", "features", "numberMatched", "numberReturned"} and body["type"] == "FeatureCollection"
    assert body["numberMatched"] == body["numberReturned"] == len(body["features"]) > 3
    distances = [feature["properties"]["distance_m"] for feature in body["features"]]
    assert distances == sorted(distances) and max(distances) <= 150 and all(decimals(d) <= 1 for d in distances)
    for feature in body["features"]:
        assert_asset_properties(feature["properties"], frozenset({"distance_m"}))
    expected = dict(
        db_conn.execute(
            "SELECT asset_id, ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) FROM infra.infrastructure_assets",
            (lon, lat),
        ).fetchall()
    )
    assert {f["properties"]["asset_id"] for f in body["features"]} == {asset_id for asset_id, d in expected.items() if d <= 150}
    for feature in body["features"]:
        assert feature["properties"]["distance_m"] == pytest.approx(expected[feature["properties"]["asset_id"]], abs=0.051)
    default = get_json("/spatial/assets-within", lon=lon, lat=lat)
    assert max(f["properties"]["distance_m"] for f in default["features"]) <= api_settings.PROXIMITY_RADIUS_M == 100
    far = get_json("/spatial/assets-within", lon=0, lat=0, radius_m=500)
    assert far == {"type": "FeatureCollection", "features": [], "numberMatched": 0, "numberReturned": 0}
    schemas.AssetCollection.model_validate(body)


def test_spatial_nearest_asset(get_json, db_conn):
    lon, lat = -100.0120, 37.7570
    body = get_json("/spatial/nearest-asset", lon=lon, lat=lat)
    assert set(body) == {"type", "geometry", "properties"} and body["type"] == "Feature"
    assert_asset_properties(body["properties"], frozenset({"distance_m"}))
    nearest = db_conn.execute(
        "SELECT asset_id, ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS d FROM infra.infrastructure_assets ORDER BY d LIMIT 1",
        (lon, lat),
    ).fetchone()
    assert body["properties"]["distance_m"] == pytest.approx(nearest[1], abs=0.051)
    for asset_type in ("bridge", "water_main", "street_light"):
        typed = get_json("/spatial/nearest-asset", lon=lon, lat=lat, asset_type=asset_type)
        assert typed["properties"]["asset_type"] == asset_type
        best = one(db_conn, "SELECT min(ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)) FROM infra.infrastructure_assets WHERE asset_type = %s", (lon, lat, asset_type))
        assert typed["properties"]["distance_m"] == pytest.approx(best, abs=0.051)
    schemas.AssetFeature.model_validate(body)


def test_spatial_sensors_in_asset_area(get_json, db_conn):
    asset_id = one(db_conn, "SELECT asset_id FROM infra.sensors GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1")
    body = get_json("/spatial/sensors-in-asset-area", asset_id=asset_id, buffer_m=80)
    assert set(body) == {"asset_id", "buffer_m", "items"} and (body["asset_id"], body["buffer_m"]) == (asset_id, 80)
    for item in body["items"]:
        assert_sensor_item(item, frozenset({"distance_m"}))
        assert decimals(item["distance_m"]) <= 1 and item["distance_m"] <= 80
    assert [item["distance_m"] for item in body["items"]] == sorted(item["distance_m"] for item in body["items"])
    expected = db_conn.execute(
        "SELECT s.sensor_id FROM infra.sensors s CROSS JOIN infra.infrastructure_assets a WHERE a.asset_id = %s AND ST_Distance(s.geom::geography, a.geom::geography) <= 80",
        (asset_id,),
    ).fetchall()
    assert {item["sensor_id"] for item in body["items"]} == {row[0] for row in expected}
    assert {item["asset_id"] for item in body["items"]} - {asset_id}  # sensors of neighbouring assets are included
    assert get_json("/spatial/sensors-in-asset-area", asset_id=asset_id)["buffer_m"] == 25
    schemas.SensorsInArea.model_validate(body)


def test_spatial_anomaly_density(get_json, db_conn):
    body = get_json("/spatial/anomaly-density")
    assert set(body) == {"type", "features"} and body["type"] == "FeatureCollection"
    cells = one(db_conn, "SELECT count(*) FROM infra.risk_zones")
    assert len(body["features"]) == cells
    for feature in body["features"]:
        assert set(feature["properties"]) == support.DENSITY_KEYS
        assert feature["geometry"]["type"] == "Polygon"
    assert_coordinates(body["features"][0]["geometry"])
    total = one(db_conn, "SELECT count(*) FROM infra.anomalies")
    assert sum(f["properties"]["anomaly_count"] for f in body["features"]) == total
    weights = dict(db_conn.execute("SELECT severity, count(*) FROM infra.anomalies GROUP BY 1").fetchall())
    assert sum(f["properties"]["weighted_severity"] for f in body["features"]) == sum(support.SEVERITY_WEIGHT[s] * n for s, n in weights.items())
    window = get_json("/spatial/anomaly-density", start="2026-09-20T00:00:00Z", end="2026-09-25T00:00:00Z")
    overlapping = one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE started_at <= '2026-09-25T00:00:00Z' AND ended_at >= '2026-09-20T00:00:00Z'")
    assert sum(f["properties"]["anomaly_count"] for f in window["features"]) == overlapping
    schemas.DensityCollection.model_validate(body)


def test_spatial_risk_zones_lists_every_cell_with_zero_where_none(get_json, db_conn):
    body = get_json("/spatial/risk-zones")
    assert set(body) == {"type", "features", "as_of"} and body["as_of"] == T_END
    stored = {row[0]: row[1:] for row in db_conn.execute("SELECT cell_id, risk_score, risk_level, anomaly_count FROM infra.risk_zone_scores WHERE as_of = %s", (parse_z(T_END),))}
    cells = [row[0] for row in db_conn.execute("SELECT cell_id FROM infra.risk_zones ORDER BY cell_id")]
    assert [f["properties"]["cell_id"] for f in body["features"]] == cells
    for feature in body["features"]:
        properties = feature["properties"]
        assert set(properties) == support.RISK_ZONE_KEYS
        assert re.fullmatch(r"-?\d+_-?\d+", properties["cell_id"])  # 'i_j'
        score, level, count = stored.get(properties["cell_id"], (0.0, "low", 0))
        assert properties["risk_score"] == pytest.approx(score, abs=5.1e-4) and decimals(properties["risk_score"]) <= 3
        assert (properties["risk_level"], properties["anomaly_count"]) == (level, count)
        assert 0 <= properties["risk_score"] <= 100
        expected_level = "very_high" if score >= 75 else "high" if score >= 50 else "moderate" if score >= 25 else "low"
        assert properties["risk_level"] == expected_level
        assert feature["geometry"]["type"] == "Polygon"
    assert sum(1 for f in body["features"] if f["properties"]["risk_score"] == 0) > 0 and len(stored) > 0
    assert sum(f["properties"]["anomaly_count"] for f in body["features"]) == get_json("/statistics")["active_anomalies"]
    schemas.RiskCollection.model_validate(body)


def test_spatial_clusters(get_json, db_conn):
    body = get_json("/spatial/clusters")
    assert set(body) == {"type", "features"}
    stored = db_conn.execute("SELECT cluster_id, n_anomalies, n_sensors, n_assets, sensor_types, max_severity, first_started_at, last_ended_at FROM infra.anomaly_clusters ORDER BY 1").fetchall()
    assert len(body["features"]) == len(stored) >= 1
    for feature, row in zip(body["features"], stored):
        properties = feature["properties"]
        assert set(properties) == support.CLUSTER_KEYS
        assert (properties["cluster_id"], properties["n_anomalies"], properties["n_sensors"], properties["n_assets"]) == tuple(row[:4])
        assert (properties["sensor_types"], properties["max_severity"]) == (row[4], row[5])
        assert (properties["first_started_at"], properties["last_ended_at"]) == (support.iso_z(row[6]), support.iso_z(row[7]))
        members = [r[0] for r in db_conn.execute("SELECT anomaly_id FROM infra.anomalies WHERE cluster_id = %s ORDER BY 1", (row[0],))]
        assert properties["anomaly_ids"] == members and len(members) == properties["n_anomalies"]
        assert feature["geometry"]["type"] == "Polygon"
        assert_coordinates(feature["geometry"])
    schemas.ClusterCollection.model_validate(body)


# --- /layers ------------------------------------------------------------------------------------------------------
def test_layer_roads(get_json, db_conn):
    body = get_json("/layers/roads")
    assert set(body) == {"type", "features"} and len(body["features"]) == one(db_conn, "SELECT count(*) FROM infra.roads")
    keys = {"road_id", "osm_id", "name", "highway_class", "surface", "lanes", "maxspeed", "oneway", "is_bridge", "length_m", "source_id"}
    for feature in body["features"]:
        assert set(feature["properties"]) == keys and feature["geometry"]["type"] == "LineString"
        assert feature["properties"]["source_id"] == "osm" and feature["properties"]["name"] != ""
        assert decimals(feature["properties"]["length_m"]) <= 1
    for feature in body["features"][:40]:
        assert_coordinates(feature["geometry"])
        assert all(BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3] for lon, lat in coordinates(feature["geometry"]))
    classes = {feature["properties"]["highway_class"] for feature in body["features"]}
    assert {"service", "residential"} <= classes  # the base map holds more classes than the asset registry
    assert sum(feature["properties"]["is_bridge"] for feature in body["features"]) >= 1
    schemas.FeatureCollection.model_validate(body)


def test_layer_study_area_and_city_boundary(get_json):
    area = get_json("/layers/study-area")
    assert len(area["features"]) == 1
    feature = area["features"][0]
    assert set(feature["properties"]) == {"slug", "name", "description", "timezone", "utm_srid"}
    assert feature["properties"]["slug"] == "dodge-city-downtown" and feature["properties"]["utm_srid"] == 32614
    ring = feature["geometry"]["coordinates"][0]
    assert {tuple(point) for point in ring} == {(BBOX[0], BBOX[1]), (BBOX[2], BBOX[1]), (BBOX[2], BBOX[3]), (BBOX[0], BBOX[3])}
    boundary = get_json("/layers/city-boundary")
    assert len(boundary["features"]) == 1
    assert boundary["features"][0]["geometry"]["type"] == "MultiPolygon"
    properties = boundary["features"][0]["properties"]
    assert set(properties) == {"kind", "name", "source_id"}
    assert (properties["kind"], properties["source_id"]) == ("city_limits", "tiger")
    assert properties["name"].startswith("Dodge City")  # the name as recorded by the Census Bureau
    assert_coordinates(boundary["features"][0]["geometry"])
    schemas.FeatureCollection.model_validate(area)
    schemas.FeatureCollection.model_validate(boundary)


# --- /playback ----------------------------------------------------------------------------------------------------
def test_playback_bundle_shape(get_json, db_conn):
    body = get_json("/playback")
    assert set(body) == support.PLAYBACK_KEYS
    assert body["data_notice"] == DATA_NOTICE and body["run_id"] == one(db_conn, "SELECT max(run_id) FROM infra.detection_runs")
    stamps = body["timestamps"]
    assert len(stamps) == STEPS and stamps[0] == T_START and stamps[-1] == T_END
    assert all(support.ISO_Z.match(stamp) for stamp in stamps) and stamps == sorted(stamps)
    assert {(parse_z(b) - parse_z(a)).total_seconds() for a, b in zip(stamps, stamps[1:])} == {3600.0}
    assert set(body["sensors"]) == {row[0] for row in db_conn.execute("SELECT sensor_id FROM infra.sensors")}
    for series in body["sensors"].values():
        assert set(series) == {"values", "status"}
        assert len(series["values"]) == len(series["status"]) == STEPS and set(series["status"]) <= set("nwao")
        assert all((value is None) == (char == "o") for value, char in zip(series["values"], series["status"]))  # null while offline
        assert all(value is None or decimals(value) <= 3 for value in series["values"])
    assert set(body["assets"]) == {row[0] for row in db_conn.execute("SELECT DISTINCT asset_id FROM infra.sensors")}
    for series in body["assets"].values():
        assert set(series) == {"health", "status"}
        assert len(series["health"]) == len(series["status"]) == STEPS and set(series["status"]) <= set("nwrc")
        assert all(isinstance(score, int) and 0 <= score <= 100 for score in series["health"])
    cells = {row[0] for row in db_conn.execute("SELECT cell_id FROM infra.risk_zones")}
    assert set(body["zones"]) <= cells and body["zones"]
    for series in body["zones"].values():
        assert set(series) == {"risk"} and len(series["risk"]) == STEPS
        assert all(isinstance(score, int) and 0 <= score <= 100 for score in series["risk"]) and any(series["risk"])  # only cells that are ever > 0
    assert set(body["stats"]) == support.PLAYBACK_STAT_KEYS
    assert all(len(series) == STEPS and all(isinstance(value, int) for value in series) for series in body["stats"].values())
    schemas.Playback.model_validate(body)


def test_playback_fits_its_size_budget(client):
    response = client.get("/playback", headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    assert len(response.content) <= 2_000_000


# --- /meta --------------------------------------------------------------------------------------------------------
def test_meta_top_level_and_study_area(get_json):
    body = get_json("/meta")
    assert set(body) >= support.META_KEYS
    assert set(body) - support.META_KEYS <= {"spatial"}  # the proximity / clustering parameters: an addition to 10.3
    assert (body["service"], body["version"], body["data_notice"]) == (support.SERVICE_NAME, "1.0.0", DATA_NOTICE)
    assert body["study_area"] == {
        "slug": "dodge-city-downtown",
        "name": "Downtown Dodge City, Kansas",
        "bbox": [-100.03, 37.745, -100.005, 37.762],  # west, south, east, north
        "center": [-100.0175, 37.7535],
        "timezone": "America/Chicago",
        "utm_srid": 32614,
    }
    assert body["time"] == {"start": T_START, "end": T_END, "step_minutes": 60, "count": STEPS}
    assert body["severity_levels"] == ["low", "medium", "high", "critical"]
    schemas.Meta.model_validate(body)


def test_meta_labels_are_the_literal_strings_of_the_contract(get_json):
    assert get_json("/meta")["labels"] == support.LABELS


def test_meta_sensor_types_and_thresholds(get_json):
    sensor_types = get_json("/meta")["sensor_types"]
    assert set(sensor_types) == set(support.SENSOR_TYPES)
    labels = {"temperature": "Temperature", "vibration": "Vibration", "moisture": "Moisture", "pressure": "Pressure"}
    found = {}
    for name, entry in sensor_types.items():
        assert set(entry) == {"unit", "label", "placements"}
        assert (entry["unit"], entry["label"]) == (support.UNITS[name], labels[name])
        for placement, limits in entry["placements"].items():
            assert set(limits) == {"warn_low", "warn_high", "crit_low", "crit_high", "description"}
            assert limits["description"]
            found[(name, placement)] = (limits["warn_low"], limits["warn_high"], limits["crit_low"], limits["crit_high"])
    assert found == support.THRESHOLDS


def test_meta_anomaly_types_health_and_risk(get_json, api_settings):
    body = get_json("/meta")
    assert support.ANOMALY_LABELS.items() <= body["anomaly_types"].items()
    assert set(body["health"]) == {"formula", "window_days", "half_life_hours", "at_risk_below", "bands"}
    assert (body["health"]["window_days"], body["health"]["half_life_hours"], body["health"]["at_risk_below"]) == (7, 48, 70)
    assert body["health"]["bands"] == {"normal": 90, "watch": 70, "at_risk": 45, "critical": 0}
    assert "min(20" in body["health"]["formula"] and "min(45" in body["health"]["formula"]
    assert body["risk"] == {
        "hex_edge_m": 150, "bandwidth_m": 250, "half_life_hours": 72, "reference": 14.0,
        "levels": {"low": 0, "moderate": 25, "high": 50, "very_high": 75},
    }  # fmt: skip
    assert body["spatial"]["proximity_radius_m"] == api_settings.PROXIMITY_RADIUS_M


def test_meta_counts_equal_the_database(get_json, db_conn):
    counts = get_json("/meta")["counts"]
    assert set(counts) == {"assets", "real_assets", "simulated_assets", "monitored_assets", "sensors", "readings", "anomalies",
                           "assets_by_type", "sensors_by_type", "building_height_sources"}  # fmt: skip
    assert counts["assets"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets")
    assert counts["real_assets"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated")
    assert counts["simulated_assets"] == one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE is_simulated")
    assert counts["monitored_assets"] == one(db_conn, "SELECT count(DISTINCT asset_id) FROM infra.sensors")
    assert counts["sensors"] == one(db_conn, "SELECT count(*) FROM infra.sensors")
    assert counts["readings"] == one(db_conn, "SELECT count(*) FROM infra.sensor_readings")
    assert counts["anomalies"] == one(db_conn, "SELECT count(*) FROM infra.anomalies")
    assert counts["assets_by_type"] == dict(db_conn.execute("SELECT asset_type, count(*) FROM infra.infrastructure_assets GROUP BY 1").fetchall())
    assert counts["sensors_by_type"] == dict(db_conn.execute("SELECT sensor_type, count(*) FROM infra.sensors GROUP BY 1").fetchall())
    stored = dict(db_conn.execute("SELECT height_source, count(*) FROM infra.buildings GROUP BY 1").fetchall())
    assert {"lidar_3dep", "osm_levels", "estimated"} <= set(counts["building_height_sources"])
    assert {source: count for source, count in counts["building_height_sources"].items() if count} == stored
    assert sum(counts["building_height_sources"].values()) == one(db_conn, "SELECT count(*) FROM infra.buildings")


def test_meta_data_sources_and_detection_run(get_json, db_conn):
    body = get_json("/meta")
    sources = body["data_sources"]
    assert [s["source_id"] for s in sources] == sorted(row[0] for row in db_conn.execute("SELECT source_id FROM infra.data_sources"))
    for source in sources:
        assert set(source) == support.DATA_SOURCE_KEYS
        assert source["kind"] in ("real", "simulated", "derived")
        assert source["retrieved_at"] is None or support.ISO_Z.match(source["retrieved_at"])
        assert all(value != "" for value in source.values())  # missing text is null, never ""
    kinds = {s["source_id"]: s["kind"] for s in sources}
    assert kinds["osm"] == "real" and kinds["simulator"] == "simulated" and kinds["derived"] == "derived"
    run = body["detection_run"]
    assert set(run) == {"run_id", "finished_at", "params", "metrics", "evaluation_note"}
    assert run["evaluation_note"] == support.EVALUATION_NOTE
    assert support.ISO_Z.match(run["finished_at"]) and run["run_id"] == one(db_conn, "SELECT max(run_id) FROM infra.detection_runs")
    assert set(run["metrics"]) == support.METRIC_KEYS
    assert run["metrics"] == one(db_conn, "SELECT metrics FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1")
    assert run["metrics"]["event_recall"] >= 0.9 and run["metrics"]["anomaly_precision"] >= 0.85
    assert run["params"]["retrospective"] is True


# --- data notice, simulated flag ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "params"),
    [("/meta", {}), ("/statistics", {}), ("/anomalies", {"limit": 1}), ("/sensor-readings", {"sensor_id": "VIB-001", "limit": 1}),
     ("/sensor-readings", {"sensor_id": "VIB-001", "shape": "columns", "limit": 1}), ("/playback", {})],
)  # fmt: skip
def test_data_notice_is_at_the_top_level(get_json, url, params):
    assert get_json(url, **params)["data_notice"] == DATA_NOTICE


def test_everything_simulated_says_so(get_json):
    assert {item["is_simulated"] for item in get_json("/sensors")["items"]} == {True}
    assert {item["is_simulated"] for item in get_json("/anomalies")["items"]} == {True}
    assert get_json("/sensor-readings", sensor_id="PRS-001", limit=1)["is_simulated"] is True
    flags = Counter((f["properties"]["asset_type"] == "water_main", f["properties"]["is_simulated"]) for f in get_json("/assets")["features"])
    assert set(flags) == {(True, True), (False, False)}  # only the simulated water mains, and all of them


# --- numbers ------------------------------------------------------------------------------------------------------
def test_integral_numbers_are_written_as_integers_and_nan_never_appears(client):
    import json

    seen = 0
    for url in ("/meta", "/statistics", "/assets?limit=50", "/anomalies", "/spatial/risk-zones", "/sensor-readings?sensor_id=VIB-001&shape=columns",
                "/assets/BRG-001/health", "/playback"):  # fmt: skip
        literals: list[str] = []
        constants: list[str] = []
        body = json.loads(client.get(url).text, parse_float=lambda text, literals=literals: literals.append(text) or float(text), parse_constant=constants.append)
        assert body, url
        assert constants == [], url  # no NaN / Infinity
        assert not [text for text in literals if float(text).is_integer()], url  # 60, not 60.0
        assert not [text for text in literals if "e" in text.lower()], url  # plain decimals
        seen += len(literals)
    assert seen > 10_000  # the check looked at real numbers
    assert math.isfinite(client.get("/statistics").json()["total_assets"])


def test_responses_are_canonical_json_with_sorted_keys(client):
    import json

    for url in ("/statistics", "/sensors?limit=3", "/anomalies?limit=2", "/health"):
        response = client.get(url)
        assert response.headers["content-type"] == "application/json"
        body = response.json()
        assert response.content == json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"), url
