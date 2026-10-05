"""Error behaviour of the API (build contract section 10).

* unknown id -> 404 ``{"detail": "..."}`` on every route and filter that takes an id;
* invalid parameter -> 422 in FastAPI's format, naming the parameter;
* ids made of SQL metacharacters or control characters -> 404 / 422, never an error of the database, and the
  tables are unchanged;
* POST on a GET-only path -> 405; unknown path -> 404 JSON;
* database unreachable: the application still starts, ``/health`` answers 503 ``degraded``, every data
  endpoint answers 503 ``{"detail": "database unavailable"}`` and neither a response nor a log line shows
  the connection string or its password.

The tests of the last group need no database and are not marked ``db``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import psycopg
import pytest

from tests import support
from tests.conftest import make_client, make_settings

JSON = "application/json"
UNAVAILABLE = {"detail": "database unavailable"}


def assert_error(response, status: int) -> Any:
    """A JSON error answer with exactly one key, ``detail``; returns the detail."""
    assert response.status_code == status, f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text[:200]}"
    assert response.headers["content-type"] == JSON
    body = response.json()
    assert set(body) == {"detail"}
    return body["detail"]


# --- 404: unknown ids ---------------------------------------------------------------------------------------------
# (route template, path with a placeholder for the id)
ID_ROUTES = (
    ("/assets/{asset_id}", "/assets/{}"),
    ("/assets/{asset_id}/health", "/assets/{}/health"),
    ("/sensors/{sensor_id}", "/sensors/{}"),
    ("/anomalies/{anomaly_id}", "/anomalies/{}"),
)
# (path, query parameter that names an entity)
ID_FILTERS = (
    ("/sensor-readings", "sensor_id"),
    ("/spatial/sensors-in-asset-area", "asset_id"),
    ("/sensors", "asset_id"),
    ("/anomalies", "asset_id"),
    ("/anomalies", "sensor_id"),
)
# Ids are exact and case-sensitive; SQL wildcards are not patterns.
UNKNOWN_IDS = ("NOPE-999", "BRG-999", "brg-001", "BRG-00_", "BRG-%", "%", "VIB-001 ", "0")


def test_every_route_with_an_id_is_listed(get_json):
    with_id = {template for template in get_json("/openapi.json")["paths"] if "{" in template}
    assert with_id == {template for template, _ in ID_ROUTES}


@pytest.mark.parametrize(("template", "path"), ID_ROUTES, ids=[route[0] for route in ID_ROUTES])
@pytest.mark.parametrize("unknown", UNKNOWN_IDS)
def test_unknown_id_in_the_path_is_a_404(client, template, path, unknown):
    detail = assert_error(client.get(path.format(quote(unknown, safe=""))), 404)
    assert isinstance(detail, str) and detail


@pytest.mark.parametrize(("path", "name"), ID_FILTERS, ids=[f"{path}?{name}" for path, name in ID_FILTERS])
@pytest.mark.parametrize("unknown", UNKNOWN_IDS)
def test_unknown_id_in_a_filter_is_a_404(client, path, name, unknown):
    detail = assert_error(client.get(path, params={name: unknown}), 404)
    assert isinstance(detail, str) and detail


def test_known_ids_are_found_where_unknown_ones_are_not(get_json):
    assert get_json("/assets/BRG-001")["properties"]["asset_id"] == "BRG-001"
    assert get_json("/sensors/VIB-001")["sensor_id"] == "VIB-001"
    assert get_json("/anomalies/ANM-0001")["anomaly_id"] == "ANM-0001"
    assert get_json("/sensors", asset_id="BRG-001")["total"] > 0


# --- hostile ids --------------------------------------------------------------------------------------------------
HOSTILE_IDS = (
    "' OR 1=1 --",
    "'; DROP TABLE infra.sensors; --",
    "BRG-001' OR '1'='1",
    "BRG-001'; DELETE FROM infra.anomalies WHERE 'a'='a",
    '" OR ""="',
    "1; SELECT pg_sleep(10)",
    "BRG-001 UNION SELECT version()",
    "$$; TRUNCATE infra.sensor_readings; $$",
    "%(as_of)s",
    "%s",
    "\\",
    "\\x27 OR 1=1",
    "_%",
    "BRG-001\x00",
    "\x00",
    "BRG-001\n",
    "\tVIB-001",
    "\x7f",
    "A" * 65,
)
HOSTILE_NAMES = [repr(value)[:30] for value in HOSTILE_IDS]


@pytest.fixture(scope="module")
def tables_before(test_db) -> dict[str, Any]:
    from pipeline.db.connection import connect

    with connect(test_db.dsn) as conn:
        return support.database_fingerprint(conn)


@pytest.mark.parametrize("hostile", HOSTILE_IDS, ids=HOSTILE_NAMES)
def test_sql_metacharacters_in_an_id_are_not_found_or_invalid(client, tables_before, hostile):
    for _, path in ID_ROUTES:
        response = client.get(path.format(quote(hostile, safe="")))
        assert response.status_code in (404, 422), f"{path} {hostile!r} -> {response.status_code}: {response.text[:200]}"
        assert response.headers["content-type"] == JSON and set(response.json()) == {"detail"}
    for path, name in ID_FILTERS:
        response = client.get(path, params={name: hostile})
        assert response.status_code in (404, 422), f"{path}?{name}={hostile!r} -> {response.status_code}: {response.text[:200]}"
        assert response.headers["content-type"] == JSON and set(response.json()) == {"detail"}


@pytest.mark.parametrize("control", ["BRG-001\x00", "\x00", "BRG-001\n", "\x1f", "\x7f"], ids=["nul-suffix", "nul", "newline", "unit-separator", "delete"])
def test_control_characters_are_refused_before_they_reach_the_database(client, control):
    for _, path in ID_ROUTES:
        problems = assert_error(client.get(path.format(quote(control, safe=""))), 422)
        assert problems[0]["loc"][0] == "path"
    for path, name in ID_FILTERS:
        problems = assert_error(client.get(path, params={name: control}), 422)
        assert problems[0]["loc"] == ["query", name]
    problems = assert_error(client.get("/assets", params={"category": control}), 422)
    assert problems[0]["loc"] == ["query", "category"]


@pytest.mark.parametrize("hostile", ["' OR 1=1 --", "Transportation' OR 'a'='a", "%", "_ransportation", "Transportation'; DROP TABLE infra.roads; --"])
def test_sql_metacharacters_in_a_text_filter_match_nothing(get_json, hostile):
    body = get_json("/assets", category=hostile)
    assert (body["numberMatched"], body["numberReturned"], body["features"]) == (0, 0, [])
    assert get_json("/assets", category="Transportation", limit=1)["numberMatched"] > 0


def test_the_tables_are_intact_after_the_hostile_requests(client, tables_before, db_conn):
    for hostile in HOSTILE_IDS:  # once more in one go, so this test stands on its own
        client.get(f"/assets/{quote(hostile, safe='')}")
        client.get("/sensor-readings", params={"sensor_id": hostile})
        client.get("/anomalies", params={"asset_id": hostile, "sensor_id": hostile})
    after = support.database_fingerprint(db_conn)
    assert after == tables_before
    assert after["counts"]["sensors"] > 0 and after["counts"]["anomalies"] > 0 and after["counts"]["sensor_readings"] > 0
    assert db_conn.execute("SELECT to_regclass('infra.sensors') IS NOT NULL AND to_regclass('infra.roads') IS NOT NULL").fetchone()[0]


def test_read_endpoints_run_in_read_only_transactions(client):
    with client.app.state.database.connection(read_only=True) as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM infra.anomalies")
        conn.rollback()


# --- 422: invalid parameters --------------------------------------------------------------------------------------
POINT = "lon=-100.0195&lat=37.7474"
# (URL, parameter the answer must point at)
INVALID_REQUESTS: tuple[tuple[str, str], ...] = (
    # enumerations
    ("/assets?asset_type=castle", "asset_type"),
    ("/assets?status=broken", "status"),
    ("/assets?monitored=maybe", "monitored"),
    ("/sensors?sensor_type=sonar", "sensor_type"),
    ("/sensors?sensor_type=vibration,sonar", "sensor_type"),
    ("/sensors?status=broken", "status"),
    ("/sensor-readings?sensor_id=VIB-001&shape=cube", "shape"),
    ("/anomalies?severity=apocalyptic", "severity"),
    ("/anomalies?severity=high,apocalyptic", "severity"),
    ("/anomalies?sensor_type=sonar", "sensor_type"),
    ("/anomalies?status=pending", "status"),
    ("/anomalies?sort=random", "sort"),
    ("/anomalies?include=everything", "include"),
    ("/simulation-events?is_anomaly=perhaps", "is_anomaly"),
    (f"/spatial/nearest-asset?{POINT}&asset_type=castle", "asset_type"),
    # limit and offset
    ("/assets?limit=0", "limit"),
    ("/assets?limit=-1", "limit"),
    ("/assets?limit=20001", "limit"),
    ("/assets?limit=ten", "limit"),
    ("/assets?offset=-1", "offset"),
    ("/sensors?limit=0", "limit"),
    ("/sensors?limit=-5", "limit"),
    ("/sensors?limit=1000000", "limit"),
    ("/sensors?offset=-1", "offset"),
    ("/sensor-readings?sensor_id=VIB-001&limit=0", "limit"),
    ("/sensor-readings?sensor_id=VIB-001&limit=-1", "limit"),
    ("/sensor-readings?sensor_id=VIB-001&limit=5001", "limit"),
    ("/anomalies?limit=0", "limit"),
    ("/anomalies?limit=-1", "limit"),
    ("/anomalies?limit=1001", "limit"),
    ("/anomalies?offset=-5", "offset"),
    ("/anomalies?offset=1.5", "offset"),
    # radius and buffer
    ("/anomalies/ANM-0001?radius_m=0", "radius_m"),
    ("/anomalies/ANM-0001?radius_m=-3", "radius_m"),
    ("/anomalies/ANM-0001?radius_m=5000.5", "radius_m"),
    ("/anomalies/ANM-0001?radius_m=1e12", "radius_m"),
    ("/anomalies/ANM-0001?radius_m=abc", "radius_m"),
    ("/anomalies/ANM-0001?radius_m=nan", "radius_m"),
    ("/anomalies/ANM-0001?radius_m=inf", "radius_m"),
    (f"/spatial/assets-within?{POINT}&radius_m=0", "radius_m"),
    (f"/spatial/assets-within?{POINT}&radius_m=-10", "radius_m"),
    (f"/spatial/assets-within?{POINT}&radius_m=1e9", "radius_m"),
    ("/spatial/sensors-in-asset-area?asset_id=BRG-001&buffer_m=-1", "buffer_m"),
    ("/spatial/sensors-in-asset-area?asset_id=BRG-001&buffer_m=5001", "buffer_m"),
    ("/spatial/sensors-in-asset-area?asset_id=BRG-001&buffer_m=wide", "buffer_m"),
    # coordinates
    ("/spatial/assets-within?lat=37.7474", "lon"),
    ("/spatial/assets-within?lon=-100.0195", "lat"),
    ("/spatial/assets-within?lon=-200&lat=37.7", "lon"),
    ("/spatial/assets-within?lon=-100&lat=95", "lat"),
    ("/spatial/nearest-asset?lon=abc&lat=37.7", "lon"),
    ("/spatial/nearest-asset?lon=-100.0195", "lat"),
    ("/spatial/nearest-asset?lon=nan&lat=37.7", "lon"),
    # bounding boxes
    ("/assets?bbox=1,2,3", "bbox"),
    ("/assets?bbox=1,2,3,4,5", "bbox"),
    ("/assets?bbox=a,b,c,d", "bbox"),
    ("/assets?bbox=10,10,5,5", "bbox"),
    ("/assets?bbox=-100.03,37.762,-100.005,37.745", "bbox"),  # south and north swapped
    ("/assets?bbox=37.745,-100.03,37.762,-100.005", "bbox"),  # south,west,north,east: latitude out of range
    ("/assets?bbox=nan,0,1,1", "bbox"),
    ("/assets?bbox=-inf,0,1,1", "bbox"),
    ("/assets?bbox=-181,0,1,1", "bbox"),
    ("/assets?bbox=0,0,1,91", "bbox"),
    ("/assets?bbox=1;2;3;4", "bbox"),
    ("/anomalies?bbox=1,2", "bbox"),
    ("/anomalies?bbox=west,south,east,north", "bbox"),
    # required parameters
    ("/sensor-readings", "sensor_id"),
    ("/sensor-readings?sensor_id=", "sensor_id"),
    ("/sensor-readings?shape=columns", "sensor_id"),
    ("/spatial/sensors-in-asset-area", "asset_id"),
    ("/spatial/sensors-in-asset-area?buffer_m=10", "asset_id"),
    # dates and ranges
    ("/statistics?as_of=nope", "as_of"),
    ("/assets/BRG-001?as_of=nope", "as_of"),
    ("/sensors/VIB-001?as_of=nope", "as_of"),
    ("/spatial/risk-zones?as_of=31-12-2026", "as_of"),
    ("/assets/BRG-001/health?start=bad", "start"),
    ("/assets/BRG-001/health?end=bad", "end"),
    ("/assets/BRG-001/health?start=2026-09-20T00:00:00Z&end=2026-09-10T00:00:00Z", "end"),
    ("/sensor-readings?sensor_id=VIB-001&start=bad", "start"),
    ("/sensor-readings?sensor_id=VIB-001&start=2026-09-20T00:00:00Z&end=2026-09-10T00:00:00Z", "end"),
    ("/sensor-readings?sensor_id=VIB-001&shape=columns&start=2026-09-20T00:00:00Z&end=2026-09-10T00:00:00Z", "end"),
    ("/anomalies?start=nope", "start"),
    ("/anomalies?end=nope", "end"),
    ("/spatial/anomaly-density?start=nope", "start"),
    ("/spatial/anomaly-density?start=2026-09-20T00:00:00Z&end=2026-09-10T00:00:00Z", "end"),
    # lengths
    (f"/sensors?asset_id={'A' * 65}", "asset_id"),
    (f"/sensor-readings?sensor_id={'V' * 65}", "sensor_id"),
    (f"/assets?category={'C' * 65}", "category"),
    (f"/assets?bbox={'1,' * 80}1", "bbox"),
)


@pytest.mark.parametrize(("url", "name"), INVALID_REQUESTS, ids=[url[:70] for url, _ in INVALID_REQUESTS])
def test_invalid_parameter_is_a_422_that_names_it(client, url, name):
    problems = assert_error(client.get(url), 422)
    assert isinstance(problems, list) and problems
    assert ["query", name] in [problem["loc"][:2] for problem in problems], problems
    for problem in problems:
        assert {"type", "loc", "msg"} <= set(problem) and isinstance(problem["msg"], str) and problem["msg"]


@pytest.mark.parametrize(
    ("path", "name"),
    [("/assets/{}", "asset_id"), ("/assets/{}/health", "asset_id"), ("/sensors/{}", "sensor_id"), ("/anomalies/{}", "anomaly_id")],
)
def test_an_overlong_id_in_the_path_is_a_422(client, path, name):
    problems = assert_error(client.get(path.format("X" * 65)), 422)
    assert problems[0]["loc"] == ["path", name]
    assert_error(client.get(path.format("X" * 64)), 404)  # the longest accepted id is simply unknown


def test_every_invalid_request_targets_a_documented_parameter(get_json):
    paths = get_json("/openapi.json")["paths"]
    templates = sorted(paths, key=len, reverse=True)

    def template_of(url: str) -> str:
        path = url.split("?")[0]
        if path in paths:
            return path
        return next(t for t in templates if "{" in t and len(t.split("/")) == len(path.split("/")) and path.startswith(t.split("{")[0]))

    for url, name in INVALID_REQUESTS:
        documented = {parameter["name"] for parameter in paths[template_of(url)]["get"]["parameters"]}
        assert name in documented, (url, name)
    covered = {template_of(url) for url, _ in INVALID_REQUESTS}
    with_parameters = {t for t, operations in paths.items() if operations.get("get", {}).get("parameters")}
    assert covered == with_parameters  # every endpoint that takes parameters has at least one invalid request here


@pytest.mark.parametrize(
    "url",
    [
        "/assets?limit=20000",
        "/sensors?limit=5000",
        "/anomalies?limit=1000",
        "/sensor-readings?sensor_id=VIB-001&limit=5000",
        "/anomalies/ANM-0001?radius_m=5000",
        "/anomalies/ANM-0001?radius_m=0.5",
        f"/spatial/assets-within?{POINT}&radius_m=5000",
        "/spatial/sensors-in-asset-area?asset_id=BRG-001&buffer_m=0",
        "/spatial/sensors-in-asset-area?asset_id=BRG-001&buffer_m=5000",
        "/assets?bbox=-180,-90,180,90",
        "/assets?offset=0&limit=1",
    ],
)
def test_the_limits_themselves_are_accepted(client, url):
    assert client.get(url).status_code == 200, url


# --- a database that cannot be reached (no database needed for these tests) ---------------------------------------
class _Records(logging.Handler):
    """Collects every log record of the process as formatted text."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(logging.Formatter("%(name)s %(levelname)s %(message)s").format(record))


@dataclass
class DeadService:
    """The application configured with a database that does not exist, and everything it said."""

    client: Any
    dsn: str = field(repr=False)
    port: int
    log: _Records
    bodies: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    _health: Any = None
    _first: Any = None

    def request(self, method: str, url: str, **kwargs: Any):
        response = self.client.request(method, url, **kwargs)
        self.bodies.append(response.text)
        self.headers.extend(f"{key}: {value}" for key, value in response.headers.items())
        return response

    def health(self):
        if self._health is None:
            self._health = self.request("GET", "/health")
        return self._health

    def first_data_response(self):
        """A data endpoint with the pool exactly as the application configured it; later requests wait less."""
        if self._first is None:
            self._first = self.request("GET", "/statistics")
            self.client.app.state.database.open().timeout = 0.05
        return self._first


@pytest.fixture(scope="module")
def dead() -> Iterator[DeadService]:
    from pipeline.logging_utils import setup_logging

    dsn = support.unreachable_dsn()
    port = int(dsn.rsplit(":", 1)[1].split("/")[0])
    settings = make_settings(DATABASE_URL=dsn, SERVE_DASHBOARD=False, INGEST_API_KEY="key-for-the-dead-service")
    records = _Records()
    root = logging.getLogger()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("LOG_LEVEL", "DEBUG")  # the application logs its start-up line at INFO
            root.addHandler(records)
            with make_client(settings) as client:
                yield DeadService(client=client, dsn=dsn, port=port, log=records)
    finally:
        root.removeHandler(records)
        setup_logging()  # back to the level of the test session


def test_the_application_starts_without_a_database(dead):
    assert dead.request("GET", "/openapi.json").status_code == 200
    page = dead.request("GET", "/docs")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert any("API ready" in line for line in dead.log.lines)


def test_health_is_degraded_without_a_database(dead):
    response = dead.health()
    assert response.status_code == 503 and response.headers["content-type"] == JSON
    assert response.json() == {"status": "degraded", "service": support.SERVICE_NAME, "database": "unavailable"}


def test_data_endpoints_answer_503_without_a_database(dead):
    first = dead.first_data_response()
    assert first.status_code == 503 and first.json() == UNAVAILABLE and first.headers["content-type"] == JSON
    asked = set()
    for template, url in support.ENDPOINT_EXAMPLES:
        if template == "/health":
            continue
        response = dead.request("GET", url)
        assert response.status_code == 503, f"GET {url} -> {response.status_code}: {response.text[:200]}"
        assert response.json() == UNAVAILABLE and response.headers["content-type"] == JSON, url
        asked.add(template)
    assert asked == support.CONTRACT_GET_PATHS - {"/health"}
    for url in ("/assets/NOPE-999", "/sensor-readings?sensor_id=NOPE-999", "/anomalies/ANM-0001?radius_m=10"):
        assert dead.request("GET", url).json() == UNAVAILABLE, url


def test_ingestion_answers_503_without_a_database(dead):
    dead.first_data_response()
    reading = {"sensor_id": "VIB-001", "ts": "2026-10-01T05:00:00Z", "value": 1.0, "unit": "mm/s"}
    response = dead.request("POST", "/ingest/readings", json={"readings": [reading]}, headers={"X-API-Key": "key-for-the-dead-service"})
    assert response.status_code == 503 and response.json() == UNAVAILABLE
    refused = dead.request("POST", "/ingest/readings", json={"readings": [reading]}, headers={"X-API-Key": "wrong"})
    assert refused.status_code == 401  # the key is checked before the database is needed


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_other_methods_on_get_only_paths_are_405(dead, method):
    seen = set()
    for template, url in support.ENDPOINT_EXAMPLES:
        if template in seen:
            continue
        seen.add(template)
        response = dead.request(method, url.split("?")[0], json={})
        assert assert_error(response, 405) == "Method Not Allowed", url
        assert "GET" in response.headers["allow"], url
    assert seen == support.CONTRACT_GET_PATHS


def test_get_on_the_ingest_path_is_405(dead):
    response = dead.request("GET", "/ingest/readings")
    assert assert_error(response, 405) == "Method Not Allowed" and response.headers["allow"] == "POST"


def test_the_documented_methods_are_get_everywhere_and_post_for_ingestion(dead):
    paths = dead.request("GET", "/openapi.json").json()["paths"]
    methods = {template: set(operations) for template, operations in paths.items()}
    assert methods == {
        **{template: {"get"} for template in support.CONTRACT_GET_PATHS},
        **{template: {"post"} for template in support.CONTRACT_POST_PATHS},
    }


@pytest.mark.parametrize(
    "path",
    ["/nope", "/api/assets", "/v1/health", "/assets/BRG-001/nope", "/assets/BRG-001/health/extra", "/spatial", "/spatial/nope",
     "/layers", "/layers/nope", "/playback/2026", "/index.html", "/config.js", "/favicon.ico", "/.env", "/data/snapshot/meta.json"],
)  # fmt: skip
def test_unknown_get_path_is_a_404_json(dead, path):
    assert assert_error(dead.request("GET", path), 404) == "Not Found"


def test_no_answer_and_no_log_line_shows_the_connection_string_or_the_password(dead):
    dead.health()
    dead.first_data_response()
    dead.request("GET", "/meta")
    dead.request("GET", "/assets/BRG-001")
    dead.request("POST", "/ingest/readings", json={"readings": []}, headers={"X-API-Key": "key-for-the-dead-service"})
    assert any("database unavailable" in line for line in dead.log.lines)  # the failures were logged ...
    assert len(dead.bodies) > 5 and dead.headers
    for text in dead.bodies + dead.headers + dead.log.lines:  # ... and nothing shows the secret
        assert support.DEAD_DATABASE_PASSWORD not in text, text[:200]
        assert dead.dsn not in text, text[:200]
        assert "postgresql://" not in text, text[:200]
    unavailable = [body for body in dead.bodies if "unavailable" in body]
    assert len(unavailable) >= 4
    for body in unavailable:  # an answer says nothing about where the database is
        assert support.DEAD_DATABASE_USER not in body and support.DEAD_DATABASE_NAME not in body
        assert str(dead.port) not in body and "127.0.0.1" not in body

def test_settings_do_not_show_the_password_when_described(dead):
    settings = dead.client.app.state.settings
    assert settings.dsn == dead.dsn
    summary = settings.dsn_summary()
    assert support.DEAD_DATABASE_PASSWORD not in summary and str(dead.port) in summary and support.DEAD_DATABASE_NAME in summary
