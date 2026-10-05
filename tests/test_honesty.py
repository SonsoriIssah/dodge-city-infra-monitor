"""Honesty rules of the data layer (build contract sections 2, 8.6 and 10.2; requirement R22).

Nothing calls the data "live" or "real-time"; the three mandatory labels are literal; the data notice is on
the responses the contract names; every sensor and every anomaly says ``is_simulated: true``; simulated
assets are flagged and real ones are not; explanations describe and never name a cause or a safety verdict.

The scans of the OpenAPI document need no database; the others read every endpoint of the API on
``infra_test``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests import support
from tests.conftest import make_client, make_settings
from tests.support import BANNED_WORDS, DATA_NOTICE, LABELS
from tests.test_explain import FORBIDDEN as CAUSAL_OR_SAFETY_WORDS

MANDATORY_LABELS = ("Simulated Sensor Data", "Prototype Anomaly Detection", "Derived Asset Health Score")  # R22
DETAIL_URLS = (
    "/sensors/PRS-001",
    "/sensors/TMP-001?as_of=2026-09-15T00:00:00Z",
    "/anomalies/ANM-0001?radius_m=300",
    "/assets/BRG-001/health?start=2026-09-25T00:00:00Z",
    "/sensor-readings?sensor_id=PRS-001&limit=50",
    "/simulation-events?is_anomaly=true",
    "/simulation-events?is_anomaly=false",
    "/spatial/anomaly-density",
    "/anomalies?status=active",
    "/sensors?status=offline",
    "/assets?asset_type=water_main",
)
ERROR_URLS = ("/assets/NOPE-999", "/sensors/NOPE-999", "/anomalies/NOPE-999", "/nope", "/assets?limit=0", "/statistics?as_of=nope")


def all_urls() -> list[str]:
    return [url for _, url in support.ENDPOINT_EXAMPLES] + list(DETAIL_URLS)


def dictionaries(node: Any):
    """Every JSON object inside a JSON value."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from dictionaries(value)
    elif isinstance(node, list):
        for value in node:
            yield from dictionaries(value)


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    """The OpenAPI document of the default application (no database is contacted for it)."""
    return make_client(make_settings(DATABASE_URL=support.unreachable_dsn())).get("/openapi.json").json()


@pytest.fixture(scope="module")
def responses(client) -> dict[str, Any]:
    """The JSON body of every example request of every endpoint."""
    bodies = {}
    for url in all_urls():
        response = client.get(url)
        assert response.status_code == 200, f"GET {url} -> {response.status_code}: {response.text[:200]}"
        bodies[url] = response.json()
    return bodies


# --- the words "live" and "real-time" -----------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["live", "Live data", "the LIVE feed", "real-time", "Real Time", "realtime", "near real-time view", "go live!"])
def test_banned_word_pattern_finds_the_words(text):
    assert BANNED_WORDS.search(text)


@pytest.mark.parametrize("text", ["delivered", "alive", "Oliver Street", "lives", "livestock", "retrospective batch analysis", "really, time passes", "Simulated time"])
def test_banned_word_pattern_works_at_word_level(text):
    assert not BANNED_WORDS.search(text)


def test_openapi_document_never_says_live_or_real_time(openapi):
    texts = list(support.strings(openapi))
    assert len(texts) > 1000
    assert [(where, text[:120]) for where, text in texts if BANNED_WORDS.search(text)] == []


def test_no_string_of_any_endpoint_response_says_live_or_real_time(responses):
    scanned = 0
    for url, body in responses.items():
        found = [(where, text[:120]) for where, text in support.strings(body) if BANNED_WORDS.search(text)]
        assert found == [], url
        scanned += sum(1 for _ in support.strings(body))
    assert scanned > 20_000  # keys and values of real responses were read


def test_error_answers_and_the_runtime_configuration_do_not_say_it_either(client):
    for url in ERROR_URLS:
        response = client.get(url)
        assert response.status_code in (404, 422) and not BANNED_WORDS.search(response.text), url
    served = make_client(make_settings(DATABASE_URL=support.unreachable_dsn()))
    assert not BANNED_WORDS.search(served.get("/config.js").text)


# --- mandatory labels and the data notice -------------------------------------------------------------------------
def test_meta_labels_are_the_literal_strings(responses):
    labels = responses["/meta"]["labels"]
    assert labels == LABELS
    assert (labels["sensor_data"], labels["detection"], labels["health"]) == MANDATORY_LABELS
    assert labels["water_network"] == "Simulated water network (not a record of real utilities)"
    assert labels["buildings"].endswith("Not detailed 3D building models.")
    assert labels["playback"] == "Playback replays a retrospective analysis of simulated readings."


def test_api_description_carries_the_labels_and_the_data_notice(openapi):
    description = " ".join(openapi["info"]["description"].split())
    assert DATA_NOTICE in description
    for label in MANDATORY_LABELS:
        assert label in description
    tags = {tag["name"]: tag["description"] for tag in openapi["tags"]}
    assert "Simulated Sensor Data" in tags["Sensors"]
    assert "Prototype Anomaly Detection" in tags["Anomalies"]
    assert "Derived Asset Health Score" in tags["Assets"]
    assert "not field validation" in description and "not an assessment" in description


DATA_NOTICE_URLS = (
    "/meta",
    "/statistics",
    f"/statistics?as_of={support.MID_WINDOW}",
    "/anomalies",
    "/anomalies?severity=critical&status=active&as_of=2026-09-01T05:00:00Z",  # an empty list still carries it
    "/sensor-readings?sensor_id=VIB-001&limit=5",
    "/sensor-readings?sensor_id=PRS-001&start=2030-01-01T00:00:00Z",
    "/playback",
)


@pytest.mark.parametrize("url", DATA_NOTICE_URLS)
def test_data_notice_is_at_the_top_level_where_the_contract_says(get_json, url):
    assert get_json(url)["data_notice"] == DATA_NOTICE


def test_the_data_notice_is_the_sentence_of_the_contract():
    assert DATA_NOTICE == "Simulated sensor data and prototype anomaly detection. Not a record of real infrastructure condition."


def test_health_is_service_health_and_the_evaluation_is_a_self_consistency_check(responses):
    assert responses["/health"]["note"] == support.HEALTH_NOTE
    run = responses["/meta"]["detection_run"]
    assert run["evaluation_note"] == support.EVALUATION_NOTE
    assert {"event_recall", "anomaly_precision"} <= set(run["metrics"])


# --- is_simulated --------------------------------------------------------------------------------------------------
def test_every_sensor_and_anomaly_item_of_every_response_is_flagged_simulated(responses, db_conn):
    sensor_items = anomaly_items = reading_envelopes = 0
    for url, body in responses.items():
        for node in dictionaries(body):
            if "anomaly_id" in node and "severity" in node:
                anomaly_items += 1
                assert node.get("is_simulated") is True, (url, node["anomaly_id"])
            elif "sensor_id" in node and "placement" in node and "readings" not in node and "value" not in node:
                sensor_items += 1
                assert node.get("is_simulated") is True, (url, node["sensor_id"])
            elif "sensor_id" in node and "placement" in node:
                reading_envelopes += 1
                assert node.get("is_simulated") is True, (url, node["sensor_id"])
    sensors, anomalies = db_conn.execute("SELECT (SELECT count(*) FROM infra.sensors), (SELECT count(*) FROM infra.anomalies)").fetchone()
    assert sensor_items > sensors and anomaly_items > anomalies and reading_envelopes >= 3


def test_every_sensor_and_every_anomaly_is_listed_as_simulated(get_json, db_conn):
    sensors = get_json("/sensors", limit=5000)["items"]
    anomalies = get_json("/anomalies", limit=1000)["items"]
    assert len(sensors) == db_conn.execute("SELECT count(*) FROM infra.sensors").fetchone()[0]
    assert len(anomalies) == db_conn.execute("SELECT count(*) FROM infra.anomalies").fetchone()[0]
    assert [item["sensor_id"] for item in sensors if item["is_simulated"] is not True] == []
    assert [item["anomaly_id"] for item in anomalies if item["is_simulated"] is not True] == []
    assert {item["source"] for item in sensors} == {"simulator"}
    assert all("simulated" in item["description"].lower() for item in sensors)
    assert db_conn.execute("SELECT count(*) FROM infra.sensors WHERE NOT is_simulated").fetchone()[0] == 0


# --- simulated assets ------------------------------------------------------------------------------------------------
def test_simulated_assets_are_flagged_and_real_assets_are_not(get_json, db_conn):
    features = [feature["properties"] for feature in get_json("/assets")["features"]]
    stored = dict(db_conn.execute("SELECT asset_id, is_simulated FROM infra.infrastructure_assets").fetchall())
    assert {p["asset_id"]: p["is_simulated"] for p in features} == stored
    kinds = {source["source_id"]: source["kind"] for source in get_json("/meta")["data_sources"]}
    simulated = [p for p in features if p["is_simulated"]]
    real = [p for p in features if not p["is_simulated"]]
    assert simulated and real
    for properties in simulated:
        assert properties["asset_type"] == "water_main" and properties["category"] == "Simulated network", properties["asset_id"]
        assert kinds[properties["source_id"]] == "simulated", properties["asset_id"]
    for properties in real:
        assert properties["asset_type"] != "water_main" and "simulated" not in properties["category"].lower(), properties["asset_id"]
        assert kinds[properties["source_id"]] == "real", properties["asset_id"]
    statistics = get_json("/statistics")
    assert (statistics["simulated_assets"], statistics["real_assets"]) == (len(simulated), len(real))
    counts = get_json("/meta")["counts"]
    assert (counts["simulated_assets"], counts["real_assets"]) == (len(simulated), len(real))


def test_detail_of_a_simulated_asset_says_so_and_invents_no_attributes(get_json):
    main = get_json("/assets", asset_type="water_main", limit=1)["features"][0]["properties"]
    detail = get_json(f"/assets/{main['asset_id']}")
    assert detail["provenance"]["is_simulated"] is True
    assert {source["kind"] for source in detail["provenance"]["sources"]} >= {"simulated"}
    # geometry bookkeeping only: no invented material, age, diameter or flow
    assert set(detail["provenance"]["attributes"]) <= {"host_road_id", "offset_m", "length_m"}
    bridge = get_json("/assets/BRG-001")
    assert bridge["provenance"]["is_simulated"] is False and bridge["properties"]["is_simulated"] is False


def test_data_sources_say_what_is_real_simulated_and_derived(get_json):
    sources = {source["source_id"]: source for source in get_json("/meta")["data_sources"]}
    assert {source["kind"] for source in sources.values()} == {"real", "simulated", "derived"}
    for source_id in ("osm", "nbi", "tiger"):
        assert sources[source_id]["kind"] == "real" and sources[source_id]["attribution_text"], source_id
    assert "OpenStreetMap contributors" in sources["osm"]["attribution_text"]
    for source_id in ("basemap", "imagery"):
        assert (sources[source_id]["kind"], sources[source_id]["notes"]) == ("real", "display only"), source_id
    for source in sources.values():
        if source["kind"] == "simulated":
            assert "simulated" in source["name"].lower(), source["source_id"]
            assert "Not a record of real infrastructure condition" in source["notes"], source["source_id"]
        if source["kind"] == "derived":
            assert "simulated" in source["notes"].lower(), source["source_id"]  # derived from simulated readings


def test_benign_simulated_events_are_not_presented_as_observed_weather(get_json):
    events = get_json("/simulation-events", is_anomaly="false")["items"]
    assert events
    for event in events:
        assert event["sensor_id"] is None and "simulated" in event["description"].lower(), event


# --- explanations --------------------------------------------------------------------------------------------------
def test_explanations_describe_and_never_name_a_cause_or_a_safety_verdict(get_json):
    anomalies = get_json("/anomalies", limit=1000)["items"]
    assert anomalies
    for item in anomalies:
        text = item["explanation"]
        lowered = text.lower()
        assert [word for word in CAUSAL_OR_SAFETY_WORDS if word in lowered] == [], (item["anomaly_id"], text)
        assert item["sensor_id"] in text and item["severity"] in lowered, item["anomaly_id"]
        assert any(character.isdigit() for character in text), item["anomaly_id"]  # "plain English with the numbers"
        assert get_json(f"/anomalies/{item['anomaly_id']}")["explanation"] == text


def test_anomaly_labels_are_the_descriptive_signatures_of_the_contract(get_json):
    labels = get_json("/meta")["anomaly_types"]
    assert support.ANOMALY_LABELS.items() <= labels.items()
    for label in labels.values():
        lowered = label.lower()
        assert [word for word in CAUSAL_OR_SAFETY_WORDS if word in lowered] == [], label
    for item in get_json("/anomalies", limit=1000)["items"]:
        assert item["anomaly_label"] == labels[item["anomaly_type"]], item["anomaly_id"]


def test_no_response_gives_a_verdict_on_a_real_structure(responses):
    verdicts = ("structurally deficient", "unsafe", "safe to", "condemned", "will fail", "root cause")
    for url, body in responses.items():
        found = [(where, text[:100]) for where, text in support.strings(body) if any(word in text.lower() for word in verdicts)]
        assert found == [], url
