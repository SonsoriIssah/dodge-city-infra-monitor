"""``POST /ingest/readings`` (build contract 7 and 10.2): the door for a source other than the simulator.

Disabled (404) while ``INGEST_API_KEY`` is empty; 401 without the right ``X-API-Key``; with it the readings
go through ``IngestionService`` into ``infra.sensor_readings`` (upsert), the answer counts accepted and
rejected readings per reason, an oversized body is refused, and the detection is NOT run.

Everything is ingested at timestamps long before the analysed window of ``infra_test``, so no reading of the
dataset is replaced; every test removes what it stored and the module checks the database fingerprint at
the end.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from backend.app.routers.ingest import MAX_BODY_BYTES
from pipeline.db.connection import connect
from tests import support
from tests.conftest import assert_test_database, make_client
from tests.support import INGEST_DISABLED_DETAIL, iso_z

pytestmark = pytest.mark.db

KEY = "ingest-key-of-the-test-suite-41c7"
PATH = "/ingest/readings"
SOURCE = "api"
BASE = datetime(2024, 1, 1, tzinfo=UTC)  # long before the analysed window (10000 hours end in February 2025)
GUARD = datetime(2026, 1, 1, tzinfo=UTC)  # everything this module stores is older than this
STORED_SQL = """
SELECT sensor_id, ts, value, unit, status, source
FROM infra.sensor_readings WHERE ts < %s ORDER BY sensor_id, ts
"""


def reading(sensor_id: str = "VIB-001", hour: int = 0, value: float = 1.25, unit: str = "mm/s") -> dict[str, Any]:
    return {"sensor_id": sensor_id, "ts": iso_z(BASE + timedelta(hours=hour)), "value": value, "unit": unit}


def post(client, readings: list[dict[str, Any]], key: str | None = KEY):
    headers = {} if key is None else {"X-API-Key": key}
    return client.post(PATH, json={"readings": readings}, headers=headers)


def stored(conn: psycopg.Connection) -> list[tuple]:
    rows = conn.execute(STORED_SQL, (GUARD,)).fetchall()
    conn.rollback()  # no snapshot is kept between two looks at the table
    return rows


def analysis_state(conn: psycopg.Connection) -> dict[str, Any]:
    """Everything a detection or analysis run would touch."""
    state = {
        "runs": conn.execute("SELECT * FROM infra.detection_runs ORDER BY run_id").fetchall(),
        "anomalies": support.table_md5(conn, "anomalies"),
        "scores": conn.execute("SELECT count(*), sum(robust_z::float8), count(*) FILTER (WHERE flagged) FROM infra.reading_scores").fetchone(),
        "health": support.table_count(conn, "asset_health"),
        "risk": support.table_count(conn, "risk_zone_scores"),
        "clusters": support.table_count(conn, "anomaly_clusters"),
    }
    conn.rollback()
    return state


@pytest.fixture(scope="module")
def ingest_client(api_settings):
    """The API on the test database with the ingestion endpoint enabled."""
    with make_client(api_settings.model_copy(update={"INGEST_API_KEY": KEY})) as test_client:
        yield test_client


@pytest.fixture(scope="module", autouse=True)
def dataset_is_left_as_it_was(test_db) -> Iterator[None]:
    with connect(test_db.dsn) as conn:
        before = support.database_fingerprint(conn)
        conn.rollback()
        yield
        assert support.database_fingerprint(conn) == before


@pytest.fixture
def table(test_db) -> Iterator[psycopg.Connection]:
    """A connection for looking at the committed readings; removes what the test ingested."""
    with connect(test_db.dsn) as conn:
        assert_test_database(conn)
        assert stored(conn) == []
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute("DELETE FROM infra.sensor_readings WHERE ts < %s", (GUARD,))
            conn.commit()


# --- disabled ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", [None, "", KEY, "anything"])
def test_disabled_endpoint_is_a_404_with_the_documented_detail(client, table, key):
    response = post(client, [reading()], key)
    assert response.status_code == 404
    assert response.json() == {"detail": INGEST_DISABLED_DETAIL}
    assert stored(table) == []


def test_disabled_endpoint_does_not_look_at_the_body(client):
    for content in (b"{not json", b"", json.dumps({"readings": "nope"}).encode(), b" " * (MAX_BODY_BYTES + 1)):
        response = client.post(PATH, content=content, headers={"Content-Type": "application/json", "X-API-Key": KEY})
        assert response.status_code == 404 and response.json() == {"detail": INGEST_DISABLED_DETAIL}


def test_a_blank_key_setting_keeps_the_endpoint_disabled(api_settings):
    settings = api_settings.model_copy(update={"INGEST_API_KEY": "   "})
    assert settings.ingest_enabled is False
    with make_client(settings) as blank:
        for key in (None, "", "   ", KEY):
            response = post(blank, [reading()], key)
            assert response.status_code == 404 and response.json() == {"detail": INGEST_DISABLED_DETAIL}


# --- authentication ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", [None, "", "wrong", KEY.upper(), KEY[:-1], KEY + "x", f"Bearer {KEY}"])
def test_missing_or_wrong_key_is_a_401(ingest_client, table, key):
    response = post(ingest_client, [reading()], key)
    assert response.status_code == 401
    body = response.json()
    assert set(body) == {"detail"} and "X-API-Key" in body["detail"]
    assert KEY not in response.text
    assert stored(table) == []


def test_the_key_is_only_read_from_the_x_api_key_header(ingest_client, table):
    payload = {"readings": [reading()]}
    assert ingest_client.post(PATH, json=payload, headers={"Authorization": f"Bearer {KEY}"}).status_code == 401
    assert ingest_client.post(f"{PATH}?api_key={KEY}", json=payload).status_code == 401
    assert ingest_client.post(PATH, json={**payload, "api_key": KEY}).status_code == 401
    assert stored(table) == []


def test_the_key_is_checked_before_the_body(ingest_client, table):
    for content in (b"{not json", json.dumps({"readings": "nope"}).encode(), b" " * (MAX_BODY_BYTES + 1)):
        response = ingest_client.post(PATH, content=content, headers={"Content-Type": "application/json", "X-API-Key": "wrong"})
        assert response.status_code == 401
    assert stored(table) == []


# --- storing -------------------------------------------------------------------------------------------------------
def test_valid_readings_are_stored_through_the_ingestion_service(ingest_client, table):
    batch = [reading("VIB-001", hour, round(1.0 + hour / 10, 3)) for hour in range(5)]
    batch += [reading("PRS-001", hour, 60.0 + hour, "psi") for hour in range(3)]
    batch += [reading("TMP-001", 0, -3.5, "°C")]
    response = post(ingest_client, batch)
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    assert response.json() == {"accepted": 9, "rejected": 0, "reasons": {}}
    expected = sorted(
        (item["sensor_id"], support.parse_z(item["ts"]), item["value"], item["unit"], "ok", SOURCE) for item in batch
    )
    assert stored(table) == expected


def test_timestamps_with_an_offset_are_stored_as_the_same_moment(ingest_client, table):
    item = {"sensor_id": "VIB-001", "ts": "2024-01-01T06:00:00+05:00", "value": 1.5, "unit": "mm/s"}
    assert post(ingest_client, [item]).json() == {"accepted": 1, "rejected": 0, "reasons": {}}
    assert [(row[0], row[1]) for row in stored(table)] == [("VIB-001", BASE + timedelta(hours=1))]


def test_rejected_readings_are_counted_per_reason_and_not_stored(ingest_client, table):
    body = {
        "readings": [
            reading("VIB-001", 0, 1.1),  # stored
            reading("NOPE-001", 0, 1.0),  # unknown sensor
            reading("NOPE-002", 1, 1.0),  # unknown sensor
            reading("VIB-001", 1, 1.0, "psi"),  # wrong unit for a vibration sensor
            {"sensor_id": "VIB-001", "ts": "2024-01-01T03:00:00", "value": 1.0, "unit": "mm/s"},  # no UTC offset
            reading("VIB-001", 5, 1.0),  # the same sensor and hour twice:
            reading("VIB-001", 5, 2.0),  # ... the later one is kept
            reading("PRS-001", 0, 9999.0, "psi"),  # physically implausible: stored as suspect
            reading("VIB-001", 7, 0.0),  # placeholder for NaN, replaced below
        ]
    }
    raw = json.dumps(body).replace('"value": 0.0', '"value": NaN').encode()
    assert raw.count(b"NaN") == 1
    response = ingest_client.post(PATH, content=raw, headers={"Content-Type": "application/json", "X-API-Key": KEY})
    assert response.status_code == 200
    assert response.json() == {
        "accepted": 3,
        "rejected": 6,
        "reasons": {"duplicate_reading": 1, "naive_timestamp": 1, "non_finite_value": 1, "unit_mismatch": 1, "unknown_sensor": 2},
    }
    assert stored(table) == [
        ("PRS-001", BASE, 9999.0, "psi", "suspect", SOURCE),
        ("VIB-001", BASE, 1.1, "mm/s", "ok", SOURCE),
        ("VIB-001", BASE + timedelta(hours=5), 2.0, "mm/s", "ok", SOURCE),
    ]


def test_an_empty_batch_is_accepted_and_stores_nothing(ingest_client, table):
    assert post(ingest_client, []).json() == {"accepted": 0, "rejected": 0, "reasons": {}}
    assert stored(table) == []


def test_ingesting_is_an_idempotent_upsert(ingest_client, table):
    batch = [reading("VIB-001", hour, 1.0 + hour) for hour in range(4)] + [reading("PRS-001", 0, 61.0, "psi")]
    first = post(ingest_client, batch)
    rows = stored(table)
    second = post(ingest_client, batch)
    assert first.json() == second.json() == {"accepted": 5, "rejected": 0, "reasons": {}}
    assert stored(table) == rows and len(rows) == 5  # the same rows, not ten

    changed = [{**item, "value": item["value"] + 0.5} for item in batch[:2]]
    assert post(ingest_client, changed).json() == {"accepted": 2, "rejected": 0, "reasons": {}}
    after = stored(table)
    assert len(after) == 5
    values = {(row[0], row[1]): row[2] for row in after}
    for item in changed:
        assert values[(item["sensor_id"], support.parse_z(item["ts"]))] == item["value"]  # replaced
    for item in batch[2:]:
        assert values[(item["sensor_id"], support.parse_z(item["ts"]))] == item["value"]  # untouched


def test_a_suspect_reading_becomes_ok_when_it_is_sent_again_with_a_plausible_value(ingest_client, table):
    assert post(ingest_client, [reading("PRS-001", 0, -50.0, "psi")]).json()["accepted"] == 1
    assert [row[4] for row in stored(table)] == ["suspect"]
    assert post(ingest_client, [reading("PRS-001", 0, 62.0, "psi")]).json()["accepted"] == 1
    assert [(row[2], row[4]) for row in stored(table)] == [(62.0, "ok")]


# --- the detection is not run --------------------------------------------------------------------------------------
def test_ingesting_does_not_run_the_detection(ingest_client, table):
    before = analysis_state(table)
    statistics = ingest_client.get("/statistics").json()
    anomalies = ingest_client.get("/anomalies?limit=1000").json()
    etag = ingest_client.get("/playback").headers["etag"]

    batch = [reading("PRS-001", hour, 5.0, "psi") for hour in range(48)]  # two days far below the critical limit
    assert post(ingest_client, batch).json() == {"accepted": 48, "rejected": 0, "reasons": {}}
    assert len(stored(table)) == 48

    assert analysis_state(table) == before
    assert before["runs"] and len(before["runs"]) == 1
    assert ingest_client.get("/statistics").json() == statistics
    assert ingest_client.get("/anomalies?limit=1000").json() == anomalies
    assert ingest_client.get("/playback").headers["etag"] == etag  # the analysed window is the one of the last run
    scored = table.execute("SELECT count(*) FROM infra.reading_scores WHERE ts < %s", (GUARD,)).fetchone()[0]
    table.rollback()
    assert scored == 0


# --- body validation ------------------------------------------------------------------------------------------------
INVALID_BODIES: tuple[tuple[str, Any], ...] = (
    ("no readings key", {}),
    ("readings is not a list", {"readings": "nope"}),
    ("readings is an object", {"readings": {"sensor_id": "VIB-001"}}),
    ("a reading without ts, value and unit", {"readings": [{"sensor_id": "VIB-001"}]}),
    ("a reading without sensor_id", {"readings": [{"ts": "2024-01-01T00:00:00Z", "value": 1.0, "unit": "mm/s"}]}),
    ("value is text", {"readings": [{**reading(), "value": "high"}]}),
    ("value is null", {"readings": [{**reading(), "value": None}]}),
    ("ts is not a date", {"readings": [{**reading(), "ts": "yesterday"}]}),
    ("empty sensor id", {"readings": [{**reading(), "sensor_id": ""}]}),
    ("empty unit", {"readings": [{**reading(), "unit": ""}]}),
    ("overlong sensor id", {"readings": [{**reading(), "sensor_id": "V" * 65}]}),
    ("unknown key in a reading", {"readings": [{**reading(), "quality": "good"}]}),
    ("unknown key at the top", {"readings": [], "source": "simulator"}),
    ("a list instead of an object", [reading()]),
)


@pytest.mark.parametrize("body", [body for _, body in INVALID_BODIES], ids=[name for name, _ in INVALID_BODIES])
def test_a_body_of_the_wrong_shape_is_a_422(ingest_client, table, body):
    response = ingest_client.post(PATH, json=body, headers={"X-API-Key": KEY})
    assert response.status_code == 422
    problems = response.json()["detail"]
    assert isinstance(problems, list) and problems and all(problem["loc"][0] == "body" for problem in problems)
    assert stored(table) == []


@pytest.mark.parametrize("content", [b"{not json", b"", b"readings=1", b'{"readings": [}'], ids=["broken", "empty", "form", "truncated"])
def test_a_body_that_is_not_json_is_a_422(ingest_client, table, content):
    response = ingest_client.post(PATH, content=content, headers={"Content-Type": "application/json", "X-API-Key": KEY})
    assert response.status_code == 422 and isinstance(response.json()["detail"], list)
    assert stored(table) == []


def test_more_readings_than_the_documented_maximum_are_a_422(ingest_client, table, get_json):
    documented = get_json("/openapi.json")["paths"][PATH]["post"]
    assert "at most 10000" in documented["description"]
    too_many = [reading("VIB-001", hour) for hour in range(10_001)]
    response = post(ingest_client, too_many)
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["body", "readings"]
    assert stored(table) == []
    assert post(ingest_client, too_many[:10_000]).json() == {"accepted": 10_000, "rejected": 0, "reasons": {}}
    assert len(stored(table)) == 10_000


# --- body size -------------------------------------------------------------------------------------------------------
def padded_body(size: int) -> bytes:
    """A valid, empty batch padded with white space to exactly ``size`` bytes."""
    head, tail = b'{"readings":[', b"]}"
    return head + b" " * (size - len(head) - len(tail)) + tail


def test_an_oversized_body_is_refused(ingest_client, table):
    response = ingest_client.post(PATH, content=padded_body(MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json", "X-API-Key": KEY})
    assert response.status_code == 413
    body = response.json()
    assert set(body) == {"detail"} and str(MAX_BODY_BYTES) in body["detail"]
    assert stored(table) == []


def test_an_oversized_body_without_a_declared_length_is_refused_too(ingest_client, table):
    whole = padded_body(MAX_BODY_BYTES + 1)

    def chunks() -> Iterator[bytes]:
        for offset in range(0, len(whole), 1 << 20):
            yield whole[offset : offset + (1 << 20)]

    response = ingest_client.post(PATH, content=chunks(), headers={"Content-Type": "application/json", "X-API-Key": KEY})
    assert "content-length" not in response.request.headers
    assert response.status_code == 413 and set(response.json()) == {"detail"}
    assert stored(table) == []


def test_a_body_of_exactly_the_maximum_size_is_read(ingest_client, table):
    response = ingest_client.post(PATH, content=padded_body(MAX_BODY_BYTES), headers={"Content-Type": "application/json", "X-API-Key": KEY})
    assert response.status_code == 200 and response.json() == {"accepted": 0, "rejected": 0, "reasons": {}}


# --- documentation ---------------------------------------------------------------------------------------------------
def test_the_endpoint_is_documented_with_its_key_and_its_answers(get_json):
    document = get_json("/openapi.json")
    operation = document["paths"][PATH]["post"]
    assert {"200", "401", "404", "413", "422", "503"} <= set(operation["responses"])
    schemes = document["components"]["securitySchemes"]
    assert [(scheme["type"], scheme["in"], scheme["name"]) for scheme in schemes.values()] == [("apiKey", "header", "X-API-Key")]
    assert operation["security"] == [{name: []} for name in schemes]
    assert "does not run the anomaly detection" in operation["description"].replace("\n", " ")
