"""Time semantics of the API (build contract 10.1, amendment A1).

``as_of`` defaults to T_end, is floored to the time step, clamped to the analysed window and echoed; a value
without an offset is UTC. Anomaly status is evaluated at ``as_of`` (active while started_at <= t <= ended_at,
resolved after, not visible before). Every timestamp of every response is ``YYYY-MM-DDTHH:MM:SSZ``.

The expected values are computed here from the contract defaults (``tests/support.py``) and from SQL written
for the test - never with the time helpers of the application.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from tests import support
from tests.support import iso_z

pytestmark = pytest.mark.db

# The analysed window of the default configuration, from the contract defaults (section 4.2 and 7).
START = support.SPEC_DEFAULTS["SIM_START"].astimezone(UTC)
STEP = timedelta(minutes=support.SPEC_DEFAULTS["SIM_STEP_MINUTES"])
STEPS = support.SPEC_DEFAULTS["SIM_DAYS"] * 24 * 60 // support.SPEC_DEFAULTS["SIM_STEP_MINUTES"]
END = START + (STEPS - 1) * STEP
T_START, T_END = iso_z(START), iso_z(END)

# Every endpoint that takes ``as_of`` (route template, example path, extra parameters).
AS_OF_ENDPOINTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("/statistics", "/statistics", {}),
    ("/sensors", "/sensors", {"limit": 2}),
    ("/sensors/{sensor_id}", "/sensors/VIB-001", {}),
    ("/anomalies", "/anomalies", {"limit": 2}),
    ("/assets/{asset_id}", "/assets/BRG-001", {}),
    ("/spatial/risk-zones", "/spatial/risk-zones", {}),
)
TIMESTAMP_KEYS = frozenset(
    {"as_of", "ts", "started_at", "ended_at", "peak_at", "first_started_at", "last_ended_at", "retrieved_at",
     "finished_at", "generated_at"}
)  # fmt: skip


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def effective(moment: datetime) -> str:
    """The effective ``as_of`` of the contract: floored to the step, clamped to [start, T_end]."""
    steps = math.floor((moment - START) / STEP)
    return iso_z(START + min(max(steps, 0), STEPS - 1) * STEP)


def as_of_echo(client, path: str, params: dict[str, Any], value: str | None) -> str:
    query = dict(params) if value is None else {**params, "as_of": value}
    response = client.get(path, params=query)
    assert response.status_code == 200, f"GET {path} {query} -> {response.status_code}: {response.text[:200]}"
    return response.json()["as_of"]


# --- the window ---------------------------------------------------------------------------------------------------
def test_the_time_axis_is_the_hourly_grid_of_the_latest_run(get_json, db_conn):
    run_start, run_end = db_conn.execute(
        "SELECT window_start, window_end FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    first, last = db_conn.execute("SELECT min(ts), max(ts) FROM infra.sensor_readings").fetchone()
    assert (run_start, run_end) == (first, last) == (START, END)
    assert get_json("/meta")["time"] == {"start": T_START, "end": T_END, "step_minutes": 60, "count": STEPS}
    assert get_json("/health")["data_window"] == {"start": T_START, "end": T_END}


def test_the_list_of_as_of_endpoints_is_complete(get_json):
    paths = get_json("/openapi.json")["paths"]
    documented = {
        template
        for template, operations in paths.items()
        if any(parameter["name"] == "as_of" for parameter in operations.get("get", {}).get("parameters", []))
    }
    assert documented == {template for template, _, _ in AS_OF_ENDPOINTS}


# --- default, flooring, clamping, offsets -------------------------------------------------------------------------
@pytest.mark.parametrize(("template", "path", "params"), AS_OF_ENDPOINTS, ids=[e[0] for e in AS_OF_ENDPOINTS])
def test_as_of_defaults_to_the_end_of_the_window(client, template, path, params):
    assert as_of_echo(client, path, params, None) == T_END


AS_OF_CASES: tuple[tuple[str, str, datetime], ...] = (
    ("on the hour", "2026-09-20T12:00:00Z", utc(2026, 9, 20, 12)),
    ("minutes and seconds are floored", "2026-09-20T12:34:56Z", utc(2026, 9, 20, 12, 34, 56)),
    ("the last second of an hour", "2026-09-20T12:59:59Z", utc(2026, 9, 20, 12, 59, 59)),
    ("fractional seconds", "2026-09-20T12:59:59.999Z", utc(2026, 9, 20, 12, 59, 59, 999000)),
    ("explicit zero offset", "2026-09-20T12:10:00+00:00", utc(2026, 9, 20, 12, 10)),
    ("naive is UTC", "2026-09-20T12:34:56", utc(2026, 9, 20, 12, 34, 56)),
    ("a date is midnight UTC", "2026-09-20", utc(2026, 9, 20)),
    ("negative offset", "2026-09-20T07:30:00-05:00", datetime(2026, 9, 20, 7, 30, tzinfo=timezone(timedelta(hours=-5)))),
    ("half-hour offset", "2026-09-20T17:45:00+05:30", datetime(2026, 9, 20, 17, 45, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
    ("offset across midnight", "2026-09-21T01:10:00+13:00", datetime(2026, 9, 21, 1, 10, tzinfo=timezone(timedelta(hours=13)))),
    ("the first hour", T_START, START),
    ("the last hour", T_END, END),
    ("one second before the start", iso_z(START - timedelta(seconds=1)), START - timedelta(seconds=1)),
    ("long before the start", "2020-01-01T00:00:00Z", utc(2020, 1, 1)),
    ("one second after the end", iso_z(END + timedelta(seconds=1)), END + timedelta(seconds=1)),
    ("inside the hour after the end", iso_z(END + timedelta(minutes=59)), END + timedelta(minutes=59)),
    ("long after the end", "2030-01-01T00:00:00Z", utc(2030, 1, 1)),
)  # fmt: skip


def test_the_expected_values_of_the_cases_cover_floor_and_both_clamps():
    answers = {name: effective(moment) for name, _, moment in AS_OF_CASES}
    assert answers["minutes and seconds are floored"] == answers["naive is UTC"] == "2026-09-20T12:00:00Z"
    assert answers["negative offset"] == answers["half-hour offset"] == answers["offset across midnight"] == "2026-09-20T12:00:00Z"
    assert answers["a date is midnight UTC"] == "2026-09-20T00:00:00Z"
    assert answers["one second before the start"] == answers["long before the start"] == T_START
    assert answers["one second after the end"] == answers["inside the hour after the end"] == answers["long after the end"] == T_END


@pytest.mark.parametrize(("text", "moment"), [case[1:] for case in AS_OF_CASES], ids=[case[0] for case in AS_OF_CASES])
def test_as_of_is_floored_to_the_hour_clamped_to_the_window_and_echoed(client, text, moment):
    expected = effective(moment)
    for _, path, params in AS_OF_ENDPOINTS:
        # ``params=`` percent-encodes the value: the plus sign of an offset travels as %2B.
        assert as_of_echo(client, path, params, text) == expected, path


@pytest.mark.parametrize("offset", ["%2B05:30", "%2B0530", "-05:00", "%2B00:00"])
def test_url_encoded_offsets_are_read_as_offsets(client, offset):
    local = {"%2B05:30": "17:45:00", "%2B0530": "17:45:00", "-05:00": "07:15:00", "%2B00:00": "12:15:00"}[offset]
    response = client.get(f"/statistics?as_of=2026-09-20T{local}{offset}")
    assert response.status_code == 200 and response.json()["as_of"] == "2026-09-20T12:00:00Z"


def test_a_plus_sign_that_was_not_percent_encoded_is_still_an_offset(client):
    # "+05:30" arrives as " 05:30" when a client forgets to encode it.
    response = client.get("/statistics?as_of=2026-09-20T17:45:00+05:30")
    assert response.status_code == 200 and response.json()["as_of"] == "2026-09-20T12:00:00Z"


def test_the_same_moment_gives_the_same_answer_however_it_is_written(client):
    bodies = [
        client.get("/statistics", params={"as_of": text}).json()
        for text in ("2026-09-20T12:00:00Z", "2026-09-20T12:59:00", "2026-09-20T07:20:00-05:00", "2026-09-20T21:05:00+09:00")
    ]
    assert all(body == bodies[0] for body in bodies) and bodies[0]["as_of"] == "2026-09-20T12:00:00Z"


INVALID_DATETIMES = ("garbage", "", "now", "2026-13-40T00:00:00Z", "2026-09-20T25:00:00Z", "20-09-2026", "1758369600")


@pytest.mark.parametrize("value", INVALID_DATETIMES)
def test_an_invalid_as_of_is_a_422(client, value):
    for _, path, params in AS_OF_ENDPOINTS:
        response = client.get(path, params={**params, "as_of": value})
        assert response.status_code == 422, f"{path} as_of={value!r} -> {response.status_code}"
        problems = response.json()["detail"]
        assert [problem["loc"] for problem in problems] == [["query", "as_of"]], path


# --- ranges: start / end ------------------------------------------------------------------------------------------
def test_range_bounds_are_floored_clamped_and_naive_values_are_utc(get_json):
    series = get_json("/assets/BRG-001/health", start="2026-09-10T00:30:00Z", end="2026-09-10T05:10:00Z")
    assert (series["start"], series["count"]) == ("2026-09-10T00:00:00Z", 6)
    assert get_json("/assets/BRG-001/health", start="2026-09-10T00:30:00", end="2026-09-10T05:10:00") == series
    assert get_json("/assets/BRG-001/health", start="2026-09-09T19:30:00-05:00", end="2026-09-10T00:10:00-05:00") == series
    whole = get_json("/assets/BRG-001/health", start="2020-01-01", end="2030-01-01")
    assert (whole["start"], whole["count"]) == (T_START, STEPS)

    columns = get_json("/sensor-readings", sensor_id="VIB-001", shape="columns", start="2026-09-10T00:30:00Z", end="2026-09-10T05:10:00Z")
    assert (columns["start"], columns["count"], len(columns["value"])) == ("2026-09-10T00:00:00Z", 6, 6)
    assert get_json("/sensor-readings", sensor_id="VIB-001", shape="columns", start="2026-09-10T00:30:00", end="2026-09-10T05:10:00") == columns


def test_reading_records_keep_their_own_timestamps_inside_the_requested_range(get_json, db_conn):
    body = get_json("/sensor-readings", sensor_id="VIB-001", start="2026-09-10T00:00:00Z", end="2026-09-10T05:00:00Z")
    expected = [
        iso_z(row[0])
        for row in db_conn.execute(
            "SELECT ts FROM infra.sensor_readings WHERE sensor_id = 'VIB-001' AND ts >= %s AND ts <= %s ORDER BY ts",
            (utc(2026, 9, 10, 0), utc(2026, 9, 10, 5)),
        )
    ]
    assert [reading["ts"] for reading in body["readings"]] == expected and expected
    assert (body["start"], body["end"]) == ("2026-09-10T00:00:00Z", "2026-09-10T05:00:00Z")


@pytest.mark.parametrize(("bound", "value"), [("start", "2030-01-01T00:00:00Z"), ("end", "2020-01-01T00:00:00Z")])
def test_a_single_bound_outside_the_window_is_not_an_error(client, bound, value):
    """Only a reversed range that the client asked for is invalid; one far-away bound just selects little or nothing."""
    records = client.get("/sensor-readings", params={"sensor_id": "VIB-001", bound: value})
    assert records.status_code == 200, records.text[:200]
    body = records.json()
    assert (body["count"], body["readings"]) == (0, []) and body["start"] <= body["end"]
    assert support.ISO_Z.match(body["start"]) and support.ISO_Z.match(body["end"])
    assert body["data_notice"] == support.DATA_NOTICE

    columns = client.get("/sensor-readings", params={"sensor_id": "VIB-001", "shape": "columns", bound: value})
    assert columns.status_code == 200 and columns.json()["count"] == 1  # clamped to the nearest end of the grid
    health = client.get("/assets/BRG-001/health", params={bound: value})
    assert health.status_code == 200 and health.json()["count"] == 1
    assert health.json()["start"] == (T_END if bound == "start" else T_START)
    density = client.get("/spatial/anomaly-density", params={bound: value})
    assert density.status_code == 200
    assert {feature["properties"]["anomaly_count"] for feature in density.json()["features"]} == {0}
    listed = client.get("/anomalies", params={bound: value})
    assert listed.status_code == 200 and listed.json()["total"] == 0


# --- anomaly status at as_of --------------------------------------------------------------------------------------
@pytest.fixture
def anomalies(db_conn) -> list[dict[str, Any]]:
    rows = db_conn.execute(
        "SELECT anomaly_id, sensor_id, asset_id, started_at, ended_at, status FROM infra.anomalies ORDER BY anomaly_id"
    ).fetchall()
    keys = ("anomaly_id", "sensor_id", "asset_id", "started_at", "ended_at", "status")
    return [dict(zip(keys, row, strict=True)) for row in rows]


@pytest.fixture
def subject(anomalies) -> dict[str, Any]:
    """The longest anomaly that is over before the end of the window (several hours long, with a before and an after)."""
    finished = [a for a in anomalies if a["ended_at"] < END - STEP and a["started_at"] > START + STEP]
    longest = max(finished, key=lambda a: (a["ended_at"] - a["started_at"], a["anomaly_id"]))
    assert longest["ended_at"] - longest["started_at"] >= 2 * STEP
    return longest


def rule(anomaly: dict[str, Any], moment: datetime) -> str | None:
    """Contract 10.1: None while not yet visible, else active / resolved."""
    if anomaly["started_at"] > moment:
        return None
    return "active" if anomaly["ended_at"] >= moment else "resolved"


def probe_times(subject: dict[str, Any]) -> dict[str, datetime]:
    return {
        "one step before it starts": subject["started_at"] - STEP,
        "the hour it starts": subject["started_at"],
        "in between": subject["started_at"] + STEP,
        "the hour it ends": subject["ended_at"],
        "one step after it ends": subject["ended_at"] + STEP,
        "the first hour": START,
        "the last hour": END,
    }


def test_stored_status_is_the_status_at_the_end_of_the_window(get_json, anomalies):
    """Amendment A1: one definition of active - the stored value is the time rule at T_end."""
    assert {a["status"] for a in anomalies} == {"active", "resolved"}
    for anomaly in anomalies:
        assert anomaly["status"] == ("active" if anomaly["ended_at"] >= END else "resolved"), anomaly["anomaly_id"]
    listed = {item["anomaly_id"]: item["status"] for item in get_json("/anomalies", limit=1000)["items"]}
    assert listed == {a["anomaly_id"]: a["status"] for a in anomalies}
    for anomaly in anomalies[:: max(1, len(anomalies) // 8)]:
        assert get_json(f"/anomalies/{anomaly['anomaly_id']}")["status"] == anomaly["status"]


def test_anomaly_list_follows_the_time_rule_at_every_probe_time(get_json, anomalies, subject):
    for name, moment in probe_times(subject).items():
        expected = {a["anomaly_id"]: status for a in anomalies if (status := rule(a, moment)) is not None}
        body = get_json("/anomalies", as_of=iso_z(moment), limit=1000)
        assert body["as_of"] == iso_z(moment), name
        assert {item["anomaly_id"]: item["status"] for item in body["items"]} == expected, name
        assert body["total"] == len(expected), name
        for wanted in ("active", "resolved"):
            filtered = get_json("/anomalies", as_of=iso_z(moment), status=wanted, limit=1000)
            assert {item["anomaly_id"] for item in filtered["items"]} == {k for k, v in expected.items() if v == wanted}, (name, wanted)
            assert filtered["total"] == len(filtered["items"]), (name, wanted)


def test_one_anomaly_is_hidden_then_active_then_resolved(get_json, subject):
    def status_at(moment: datetime) -> str | None:
        items = get_json("/anomalies", as_of=iso_z(moment), sensor_id=subject["sensor_id"], limit=1000)["items"]
        return next((item["status"] for item in items if item["anomaly_id"] == subject["anomaly_id"]), None)

    times = probe_times(subject)
    assert status_at(times["one step before it starts"]) is None  # not yet visible
    assert status_at(times["the hour it starts"]) == "active"
    assert status_at(times["in between"]) == "active"
    assert status_at(times["the hour it ends"]) == "active"
    assert status_at(times["one step after it ends"]) == "resolved"
    assert status_at(END) == "resolved" == subject["status"]


def test_statistics_count_the_anomalies_by_the_same_rule(get_json, anomalies, subject):
    for name, moment in probe_times(subject).items():
        statuses = [rule(a, moment) for a in anomalies]
        body = get_json("/statistics", as_of=iso_z(moment))
        assert body["active_anomalies"] == statuses.count("active"), name
        assert body["anomalies_to_date"] == len(statuses) - statuses.count(None), name


def test_sensor_detail_shows_only_what_had_started_by_as_of(get_json, anomalies, subject, db_conn):
    own = [a for a in anomalies if a["sensor_id"] == subject["sensor_id"]]
    for name, moment in probe_times(subject).items():
        expected = {a["anomaly_id"]: status for a in own if (status := rule(a, moment)) is not None}
        body = get_json(f"/sensors/{subject['sensor_id']}", as_of=iso_z(moment))
        assert {item["anomaly_id"]: item["status"] for item in body["anomalies"]} == expected, name
        assert body["anomaly_count"] == len(expected), name
        has_reading = db_conn.execute(
            "SELECT EXISTS (SELECT 1 FROM infra.sensor_readings WHERE sensor_id = %s AND ts = %s)",
            (subject["sensor_id"], moment),
        ).fetchone()[0]
        if not has_reading:
            assert body["status"] == "offline" and body["latest"] is None, name
        elif "active" in expected.values():
            assert body["status"] == "anomaly", name
        else:
            assert body["status"] in ("normal", "warning"), name
        if has_reading:
            assert body["latest"]["ts"] == iso_z(moment), name


def test_asset_detail_shows_only_what_had_started_by_as_of(get_json, anomalies, subject):
    own = [a for a in anomalies if a["asset_id"] == subject["asset_id"]]
    for name, moment in probe_times(subject).items():
        expected = {a["anomaly_id"]: status for a in own if (status := rule(a, moment)) is not None}
        body = get_json(f"/assets/{subject['asset_id']}", as_of=iso_z(moment))
        shown = {item["anomaly_id"]: item["status"] for item in body["recent_anomalies"]}
        assert shown == {key: expected[key] for key in shown}, name  # nothing hidden is shown; statuses follow the rule
        assert len(shown) == min(len(expected), 10), name
        assert all(item["started_at"] <= iso_z(moment) for item in body["recent_anomalies"]), name
    before = get_json(f"/assets/{subject['asset_id']}", as_of=T_START)
    assert before["recent_anomalies"] == [] and before["health"]["score"] == 100  # the quiet lead-in


# --- one timestamp format -----------------------------------------------------------------------------------------
EXTRA_TIMESTAMP_URLS = (
    f"/sensors/VIB-001?as_of={support.MID_WINDOW}",
    "/anomalies/ANM-0001?radius_m=250",
    "/assets/BRG-001/health?start=2026-09-10T00:30:00&end=2026-09-12T05:10:00%2B02:00",
    "/sensor-readings?sensor_id=PRS-001&shape=columns&start=2026-09-10T00:30:00-05:00",
    "/sensors?as_of=2026-09-20T07:30:00-05:00&status=offline",
    "/anomalies?as_of=2026-09-20T12:34:56&start=2026-09-05&end=2026-09-19T23:00:00%2B01:00",
    "/statistics?as_of=2020-01-01",
    "/simulation-events?is_anomaly=false",
)


def timestamp_problems(node: Any, path: str = "$") -> tuple[int, list[str]]:
    """Count the timestamps of a JSON value and list every one that is not ``YYYY-MM-DDTHH:MM:SSZ``."""
    seen, problems = 0, []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}"
            if key in TIMESTAMP_KEYS and not isinstance(value, (dict, list)):
                if value is not None:
                    seen += 1
                    if not (isinstance(value, str) and support.ISO_Z.match(value)):
                        problems.append(f"{where} = {value!r}")
                continue
            count, found = timestamp_problems(value, where)
            seen, problems = seen + count, problems + found
    elif isinstance(node, list):
        for index, value in enumerate(node):
            count, found = timestamp_problems(value, f"{path}[{index}]")
            seen, problems = seen + count, problems + found
    elif isinstance(node, str) and support.LOOKS_LIKE_TIMESTAMP.match(node):
        seen += 1
        if not support.ISO_Z.match(node):
            problems.append(f"{path} = {node!r}")
    return seen, problems


def test_timestamp_scanner_recognises_other_notations():
    sample = {"ts": "2026-09-01T05:00:00+00:00", "items": ["2026-09-01 05:00:00", "2026-09-01T05:00Z", "2026-09-01T05:00:00.000Z"],
              "ended_at": 1756702800, "peak_at": None, "name": "Fort Dodge 1865", "fine": "2026-09-01T05:00:00Z"}  # fmt: skip
    seen, problems = timestamp_problems(sample)
    assert len(problems) == 5 and seen == 6


def test_every_timestamp_of_every_endpoint_is_iso_z(client):
    total = 0
    urls = [url for _, url in support.ENDPOINT_EXAMPLES] + list(EXTRA_TIMESTAMP_URLS)
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, f"GET {url} -> {response.status_code}: {response.text[:200]}"
        seen, problems = timestamp_problems(response.json())
        assert problems == [], f"GET {url}: {problems[:5]}"
        total += seen
    assert total > 1000  # the scan met real timestamps (lists, readings, the playback axis)

