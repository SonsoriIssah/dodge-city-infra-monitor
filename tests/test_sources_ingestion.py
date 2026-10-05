"""Sensor sources and ingestion validation (build contract section 7, brief R14). No database, no network.

``HttpPollingSource`` is driven through ``httpx.MockTransport``; ``IngestionService.check`` is the pure
validation step of the ingestion service (storage is covered by the ``db`` tests).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from pipeline.models import Reading, SensorSpec
from pipeline.sensors import ingestion, sources
from pipeline.sensors.ingestion import IngestionService
from pipeline.sensors.sources import HttpPollingSource, SensorSourceError, SimulatedSensorSource, get_sensor_source

URL = "https://gateway.example/api/readings"
START = datetime(2026, 9, 1, 5, tzinfo=UTC)
END = datetime(2026, 9, 1, 8, tzinfo=UTC)
SENSORS = [
    SensorSpec("TMP-001", "BRG-001", "temperature", "bridge_deck", "°C", -100.015, 37.756),
    SensorSpec("VIB-003", "BRG-001", "vibration", "bridge_deck", "mm/s", -100.015, 37.757),
]


def source_with(handler, **kwargs) -> HttpPollingSource:
    return HttpPollingSource(URL, SENSORS, transport=httpx.MockTransport(handler), **kwargs)


def json_response(payload, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


# --- HTTP polling source ------------------------------------------------------------------------------------------
def test_http_source_requests_the_range_once_and_parses_the_readings():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"readings": [
            {"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": 21.4, "unit": "°C"},
            {"sensor_id": "VIB-003", "ts": "2026-09-01T06:00:00+00:00", "value": 1},
            {"sensor_id": " TMP-001 ", "ts": "2026-09-01T02:00:00-05:00", "value": 22.25, "unit": "°C"},
        ]})  # fmt: skip

    readings = list(source_with(handler).read(START, END))
    assert readings == [
        Reading("TMP-001", datetime(2026, 9, 1, 5, tzinfo=UTC), 21.4, "°C"),
        Reading("VIB-003", datetime(2026, 9, 1, 6, tzinfo=UTC), 1.0, "mm/s"),  # unit of the registered sensor
        Reading("TMP-001", datetime(2026, 9, 1, 7, tzinfo=UTC), 22.25, "°C"),
    ]
    assert all(reading.ts.tzinfo is not None for reading in readings)
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET" and str(request.url).startswith(URL)
    assert dict(request.url.params) == {"start": "2026-09-01T05:00:00Z", "end": "2026-09-01T08:00:00Z"}
    assert request.headers["accept"] == "application/json"
    assert "dodge-city-infra-monitor" in request.headers["user-agent"]


def test_http_source_accepts_a_bare_array_and_extra_headers():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[{"sensor_id": "VIB-003", "ts": "2026-09-01T05:00:00Z", "value": 0.8}])

    source = source_with(handler, headers={"X-API-Key": "gateway-key"})
    assert list(source.read(START, END)) == [Reading("VIB-003", START, 0.8, "mm/s")]
    assert seen[0].headers["x-api-key"] == "gateway-key"
    assert list(source_with(json_response({"readings": []})).read(START, END)) == []


def test_http_source_range_is_sent_in_utc_whatever_the_offset_of_the_arguments():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    central = timezone(timedelta(hours=-5))
    list(source_with(handler).read(datetime(2026, 9, 1, 0, tzinfo=central), datetime(2026, 9, 1, 3, tzinfo=central)))
    assert seen == [{"start": "2026-09-01T05:00:00Z", "end": "2026-09-01T08:00:00Z"}]


def test_records_that_are_well_formed_but_unacceptable_are_passed_on_for_ingestion_to_reject():
    payload = [
        {"sensor_id": "NOPE-1", "ts": "2026-09-01T05:00:00Z", "value": 1.0, "unit": "psi"},  # unknown sensor
        {"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00", "value": 20.0, "unit": "°C"},  # no offset
        {"sensor_id": "TMP-001", "ts": "2026-09-01T06:00:00Z", "value": 20.0, "unit": "K"},  # wrong unit
    ]
    readings = list(source_with(json_response(payload)).read(START, END))
    assert [r.sensor_id for r in readings] == ["NOPE-1", "TMP-001", "TMP-001"]
    assert readings[0].unit == "psi" and readings[1].ts.tzinfo is None and readings[2].unit == "K"
    known = {"TMP-001": ("temperature", "°C"), "VIB-003": ("vibration", "mm/s")}
    assert [IngestionService.check(r, known)[0] for r in readings] == ["unknown_sensor", "naive_timestamp", "unit_mismatch"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"data": []}, "expected"),
        ("just text", "expected"),
        (17, "expected"),
        ({"readings": "many"}, "expected"),
        ([["TMP-001", "2026-09-01T05:00:00Z", 1.0]], "record 0 is not an object"),
        ([{"ts": "2026-09-01T05:00:00Z", "value": 1.0}], "record 0 has no sensor_id"),
        ([{"sensor_id": "TMP-001", "value": 1.0}], "record 0 has no ts"),
        ([{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z"}], "record 0 has no value"),
        ([{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": None}], "record 0 has no value"),
        ([{"sensor_id": "", "ts": "2026-09-01T05:00:00Z", "value": 1.0}], "sensor_id must be a non-empty string"),
        ([{"sensor_id": 7, "ts": "2026-09-01T05:00:00Z", "value": 1.0}], "sensor_id must be a non-empty string"),
        ([{"sensor_id": "TMP-001", "ts": 1756702800, "value": 1.0}], "ts must be an ISO 8601 string"),
        ([{"sensor_id": "TMP-001", "ts": "yesterday", "value": 1.0}], "not an ISO 8601 timestamp"),
        ([{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": "21.4"}], "value must be a number"),
        ([{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": True}], "value must be a number"),
        ([{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": 1.0, "unit": 5}], "unit must be a string"),
        ([{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": 1.0},
          {"sensor_id": "TMP-001", "ts": "2026-09-01T06:00:00Z"}], "record 1 has no value"),
    ],
)  # fmt: skip
def test_http_source_rejects_payloads_that_do_not_follow_the_contract(payload, message):
    with pytest.raises(SensorSourceError, match=message):
        list(source_with(json_response(payload)).read(START, END))


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_http_source_reports_an_error_status(status):
    with pytest.raises(SensorSourceError, match=f"HTTP {status}"):
        source_with(lambda request: httpx.Response(status, json={"detail": "no"})).read(START, END)


def test_http_source_reports_a_body_that_is_not_json():
    with pytest.raises(SensorSourceError, match="did not return JSON"):
        source_with(lambda request: httpx.Response(200, content=b"<html>maintenance</html>")).read(START, END)


def test_http_source_reports_a_network_failure_without_raising_httpx_errors():
    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SensorSourceError, match="could not be reached"):
        source_with(unreachable).read(START, END)


def test_http_source_needs_a_url_and_aware_bounds():
    with pytest.raises(ValueError):
        HttpPollingSource("  ")
    source = source_with(json_response([]))
    with pytest.raises(ValueError, match="timezone-aware"):
        source.read(datetime(2026, 9, 1), END)
    with pytest.raises(ValueError, match="before start"):
        source.read(END, START)


# --- registry -----------------------------------------------------------------------------------------------------
def test_sources_follow_the_protocol_and_are_selected_by_sensor_source(settings_factory):
    simulated = get_sensor_source(settings_factory(), SENSORS)
    assert isinstance(simulated, SimulatedSensorSource) and simulated.name == "simulator"
    http = get_sensor_source(settings_factory(SENSOR_SOURCE="http", HTTP_SOURCE_URL=URL), SENSORS)
    assert isinstance(http, HttpPollingSource) and http.name == "http" and http.url == URL
    assert isinstance(simulated, sources.SensorSource) and isinstance(http, sources.SensorSource)
    assert get_sensor_source(settings_factory(SENSOR_SOURCE=" Simulated "), SENSORS).name == "simulator"


def test_unknown_or_incomplete_source_configuration_is_an_error(settings_factory):
    with pytest.raises(ValueError, match="HTTP_SOURCE_URL"):
        get_sensor_source(settings_factory(SENSOR_SOURCE="http"), SENSORS)
    with pytest.raises(ValueError, match="unknown SENSOR_SOURCE"):
        get_sensor_source(settings_factory(SENSOR_SOURCE="mqtt"), SENSORS)


def test_another_source_can_be_registered_without_touching_anything_downstream(settings_factory, monkeypatch):
    class GatewaySource:
        name = "gateway"

        def __init__(self, settings, sensors):
            self.sensors = sensors

        def read(self, start, end):
            return iter([Reading(self.sensors[0].sensor_id, start, 20.0, self.sensors[0].unit)])

    monkeypatch.setitem(sources.SENSOR_SOURCES, "gateway", GatewaySource)
    source = get_sensor_source(settings_factory(SENSOR_SOURCE="gateway"), SENSORS)
    assert isinstance(source, sources.SensorSource)
    assert list(source.read(START, END)) == [Reading("TMP-001", START, 20.0, "°C")]


def test_simulated_source_yields_the_simulator_readings_for_the_inclusive_range(default_plan, default_settings, default_simulation):
    source = SimulatedSensorSource(default_settings, default_plan.sensors)
    window = list(source.read(default_settings.sim_start_utc, default_settings.sim_start_utc + timedelta(hours=2)))
    assert {r.ts for r in window} == {default_settings.sim_start_utc + timedelta(hours=h) for h in range(3)}
    assert len(window) == 3 * len(default_plan.sensors)  # nobody is offline in the first hours
    everything = list(source.read(default_settings.sim_start_utc, default_settings.sim_end_utc))
    assert len(everything) == default_simulation.reading_count()
    assert source.ground_truth() == default_simulation.ground_truth()  # same seed, same sensors: same run
    with pytest.raises(ValueError):
        source.read(datetime(2026, 9, 1), default_settings.sim_end_utc)


# --- ingestion validation -----------------------------------------------------------------------------------------
KNOWN = {
    "TMP-001": ("temperature", "°C"),
    "VIB-003": ("vibration", "mm/s"),
    "MST-002": ("moisture", "%"),
    "PRS-004": ("pressure", "psi"),
}
TS = datetime(2026, 9, 1, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("reading", "reason"),
    [
        (Reading("TMP-001", TS, 21.4, "°C"), None),
        (Reading("TMP-001", TS, 21, "°C"), None),  # an integer value is a number
        (Reading("TMP-001", TS.astimezone(timezone(timedelta(hours=-5))), 21.4, "°C"), None),
        (Reading("TMP-999", TS, 21.4, "°C"), "unknown_sensor"),
        (Reading("TMP-001", datetime(2026, 9, 1, 5), 21.4, "°C"), "naive_timestamp"),
        (Reading("TMP-001", TS, math.nan, "°C"), "non_finite_value"),
        (Reading("TMP-001", TS, math.inf, "°C"), "non_finite_value"),
        (Reading("TMP-001", TS, -math.inf, "°C"), "non_finite_value"),
        (Reading("TMP-001", TS, 21.4, "C"), "unit_mismatch"),
        (Reading("TMP-001", TS, 21.4, "°F"), "unit_mismatch"),
        (Reading("VIB-003", TS, 1.0, "mm/s "), "unit_mismatch"),  # the exact unit string
        (Reading("TMP-001", TS, 21.4, None), "unit_mismatch"),
        (Reading("", TS, 21.4, "°C"), "invalid_record"),
        (Reading(None, TS, 21.4, "°C"), "invalid_record"),
        (Reading("TMP-001", "2026-09-01T05:00:00Z", 21.4, "°C"), "invalid_record"),
        (Reading("TMP-001", TS, "21.4", "°C"), "invalid_record"),
        (Reading("TMP-001", TS, True, "°C"), "invalid_record"),
        (Reading("TMP-001", TS, None, "°C"), "invalid_record"),
        ({"sensor_id": "TMP-001"}, "invalid_record"),
    ],
)
def test_ingestion_validation_reasons(reading, reason):
    assert IngestionService.check(reading, KNOWN)[0] == reason


@pytest.mark.parametrize(
    ("sensor_id", "unit", "value", "stored_as"),
    [
        ("TMP-001", "°C", 21.4, "ok"), ("TMP-001", "°C", -60.0, "ok"), ("TMP-001", "°C", 150.0, "ok"),
        ("TMP-001", "°C", 150.1, "suspect"), ("TMP-001", "°C", -273.0, "suspect"),
        ("VIB-003", "mm/s", 0.0, "ok"), ("VIB-003", "mm/s", -0.1, "suspect"), ("VIB-003", "mm/s", 501.0, "suspect"),
        ("MST-002", "%", 100.0, "ok"), ("MST-002", "%", 100.5, "suspect"), ("MST-002", "%", -1.0, "suspect"),
        ("PRS-004", "psi", 16.5, "ok"), ("PRS-004", "psi", -5.0, "suspect"), ("PRS-004", "psi", 900.0, "suspect"),
    ],
)  # fmt: skip
def test_physically_implausible_values_are_accepted_as_suspect(sensor_id, unit, value, stored_as):
    reason, status = IngestionService.check(Reading(sensor_id, TS, value, unit), KNOWN)
    assert reason is None and status == stored_as


def test_plausible_ranges_cover_every_sensor_type_and_all_configured_limits():
    from tests.support import THRESHOLDS

    assert set(ingestion.PLAUSIBLE_RANGES) == {"temperature", "vibration", "moisture", "pressure"}
    for (sensor_type, _placement), limits in THRESHOLDS.items():
        low, high = ingestion.PLAUSIBLE_RANGES[sensor_type]
        for limit in limits:
            assert limit is None or low < limit < high  # a critical reading is still a plausible reading


def test_ingest_needs_the_name_of_the_source():
    service = IngestionService(conn=None)  # the name is checked before the connection is used
    for nameless in ("", "   ", None, object()):
        with pytest.raises(ValueError, match="name of the source"):
            service.ingest([], nameless)


def test_the_documented_request_example_matches_the_parser():
    """The JSON contract in the module docstring is the one the adapter implements."""
    example = json.loads(
        '{"readings": [{"sensor_id": "TMP-001", "ts": "2026-09-01T05:00:00Z", "value": 21.4, "unit": "°C"},'
        ' {"sensor_id": "VIB-003", "ts": "2026-09-01T05:00:00+00:00", "value": 1.02}]}'
    )
    parsed = HttpPollingSource(URL, SENSORS).parse(example)
    assert [(r.sensor_id, r.value, r.unit) for r in parsed] == [("TMP-001", 21.4, "°C"), ("VIB-003", 1.02, "mm/s")]
    assert "GET <HTTP_SOURCE_URL>?start=" in sources.__doc__
