"""The playback bundle and the single-timestamp endpoints tell the same story (build contract 10.4).

``playback.stats[*][i]`` equals ``/statistics?as_of=timestamps[i]``; the sensor status characters and values
equal ``/sensors?as_of=``; the asset health series equal ``/assets/{id}/health`` and ``/assets/{id}?as_of=``;
the zone series equal ``/spatial/risk-zones?as_of=``. ``/statistics`` itself is checked against SQL written
for this test (not the query of the application) at several hours of the window.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tests import support
from tests.support import parse_z

pytestmark = pytest.mark.db

# Contract 10.4: one character per hour.
SENSOR_CHAR = {"normal": "n", "warning": "w", "anomaly": "a", "offline": "o"}
ASSET_CHAR = {"normal": "n", "watch": "w", "at_risk": "r", "critical": "c"}
RISK_LEVEL_FROM = (("very_high", 75.0), ("high", 50.0), ("moderate", 25.0), ("low", 0.0))  # contract section 9
# Positions in the window that are always compared: first, last and five in between.
FRACTIONS = (0.0, 0.125, 0.25, 0.5, 0.75, 0.9, 1.0)
AT_RISK_BELOW = 70


@pytest.fixture(scope="module")
def playback(get_json) -> dict[str, Any]:
    return get_json("/playback")


def index_at(playback: dict[str, Any], fraction: float) -> int:
    return round((len(playback["timestamps"]) - 1) * fraction)


def test_sampled_positions_are_spread_over_the_window(playback):
    indices = [index_at(playback, fraction) for fraction in FRACTIONS]
    last = len(playback["timestamps"]) - 1
    assert indices[0] == 0 and indices[-1] == last and indices == sorted(set(indices)) and len(indices) >= 5
    assert max(b - a for a, b in zip(indices, indices[1:])) <= last // 3  # no third of the window is skipped


# --- statistics ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fraction", FRACTIONS)
def test_playback_stats_equal_statistics_at_that_hour(get_json, playback, fraction):
    index = index_at(playback, fraction)
    moment = playback["timestamps"][index]
    statistics = get_json("/statistics", as_of=moment)
    assert statistics["as_of"] == moment
    assert {key: series[index] for key, series in playback["stats"].items()} == {
        key: statistics[key] for key in support.PLAYBACK_STAT_KEYS
    }


def test_playback_stats_equal_statistics_where_each_series_peaks(get_json, playback):
    """The busiest hours of the window: where every KPI series has its maximum and its minimum."""
    indices = set()
    for series in playback["stats"].values():
        indices |= {series.index(max(series)), series.index(min(series)), len(series) - 1 - series[::-1].index(max(series))}
    assert len(indices) >= 5
    for index in sorted(indices):
        moment = playback["timestamps"][index]
        statistics = get_json("/statistics", as_of=moment)
        for key, series in playback["stats"].items():
            assert series[index] == statistics[key], f"{key} at {moment}"


def test_playback_stats_are_consistent_with_the_other_series_of_the_bundle(playback):
    stats, steps = playback["stats"], len(playback["timestamps"])
    sensors = list(playback["sensors"].values())
    for index in range(steps):
        chars = [series["status"][index] for series in sensors]
        assert stats["offline_sensors"][index] == chars.count("o")
        assert stats["active_sensors"][index] == len(chars) - chars.count("o")
        assert stats["warning_sensors"][index] == chars.count("w")
        assert stats["assets_at_risk"][index] == sum(series["health"][index] < AT_RISK_BELOW for series in playback["assets"].values())
        assert stats["critical_alerts"][index] <= stats["active_anomalies"][index]


STATISTICS_SQL = """
WITH t AS (SELECT %(t)s::timestamptz AS at),
reporting AS (                                   -- sensors with a reading in (t - sampling interval, t]
    SELECT s.sensor_id, s.sensor_type, s.placement, r.ts, r.value
    FROM t, infra.sensors s
    JOIN infra.sensor_readings r ON r.sensor_id = s.sensor_id
    WHERE r.ts <= t.at AND r.ts > t.at - s.sampling_interval_s * interval '1 second'
),
running AS (                                     -- anomalies active at t
    SELECT a.anomaly_id, a.sensor_id, a.severity FROM t, infra.anomalies a WHERE a.started_at <= t.at AND t.at <= a.ended_at
)
SELECT
    (SELECT count(*) FROM infra.infrastructure_assets)                                        AS total_assets,
    (SELECT count(*) FROM infra.infrastructure_assets WHERE is_simulated IS FALSE)            AS real_assets,
    (SELECT count(*) FROM infra.infrastructure_assets WHERE is_simulated IS TRUE)             AS simulated_assets,
    (SELECT count(*) FROM infra.infrastructure_assets a
      WHERE EXISTS (SELECT 1 FROM infra.sensors s WHERE s.asset_id = a.asset_id))             AS monitored_assets,
    (SELECT count(*) FROM infra.sensors)                                                      AS total_sensors,
    (SELECT count(DISTINCT sensor_id) FROM reporting)                                         AS active_sensors,
    (SELECT count(*) FROM infra.sensors s
      WHERE s.sensor_id NOT IN (SELECT sensor_id FROM reporting))                             AS offline_sensors,
    (SELECT count(*) FROM reporting p
      JOIN infra.sensor_thresholds th ON th.sensor_type = p.sensor_type AND th.placement = p.placement
      LEFT JOIN infra.reading_scores sc ON sc.sensor_id = p.sensor_id AND sc.ts = p.ts
      WHERE p.sensor_id NOT IN (SELECT sensor_id FROM running)
        AND (sc.flagged IS TRUE OR p.value < th.warn_low OR p.value > th.warn_high))          AS warning_sensors,
    (SELECT count(*) FROM running)                                                            AS active_anomalies,
    (SELECT count(*) FROM running WHERE severity = 'critical')                                AS critical_alerts,
    (SELECT count(*) FROM t, infra.asset_health h
      WHERE h.as_of = t.at AND h.health_score < 70)                                           AS assets_at_risk,
    (SELECT count(*) FROM t, infra.anomalies a WHERE a.started_at <= t.at)                    AS anomalies_to_date
"""


def statistics_from_sql(conn, moment: datetime) -> dict[str, Any]:
    cursor = conn.execute(STATISTICS_SQL, {"t": moment})
    expected: dict[str, Any] = dict(zip([column.name for column in cursor.description], cursor.fetchone(), strict=True))
    started = "SELECT {0}, count(*) FROM infra.anomalies WHERE started_at <= %s GROUP BY {0}"
    expected["anomalies_by_severity"] = {
        **dict.fromkeys(support.SEVERITIES, 0), **dict(conn.execute(started.format("severity"), (moment,)).fetchall())
    }  # fmt: skip
    expected["anomalies_by_sensor_type"] = {
        **dict.fromkeys(support.SENSOR_TYPES, 0), **dict(conn.execute(started.format("sensor_type"), (moment,)).fetchall())
    }  # fmt: skip
    expected["assets_by_type"] = dict(
        conn.execute("SELECT asset_type, count(*) FROM infra.infrastructure_assets GROUP BY asset_type").fetchall()
    )
    return expected


def test_statistics_equal_independent_sql_at_several_hours(get_json, playback, db_conn):
    stats = playback["stats"]
    indices = {
        0,
        len(playback["timestamps"]) - 1,
        index_at(playback, 0.5),
        stats["active_anomalies"].index(max(stats["active_anomalies"])),
        stats["offline_sensors"].index(max(stats["offline_sensors"])),
        stats["warning_sensors"].index(max(stats["warning_sensors"])),
        stats["assets_at_risk"].index(max(stats["assets_at_risk"])),
    }
    assert len(indices) >= 3
    non_trivial = 0
    for index in sorted(indices):
        moment = playback["timestamps"][index]
        body = get_json("/statistics", as_of=moment)
        expected = statistics_from_sql(db_conn, parse_z(moment))
        assert set(body) == support.STATISTICS_KEYS
        assert {key: body[key] for key in expected} == expected, moment
        assert body["active_sensors"] + body["offline_sensors"] == body["total_sensors"]
        assert sum(body["anomalies_by_severity"].values()) == sum(body["anomalies_by_sensor_type"].values()) == body["anomalies_to_date"]
        non_trivial += bool(body["active_anomalies"] and body["warning_sensors"])
    assert non_trivial >= 2  # the comparison saw hours with anomalies and warnings, not only quiet ones


# --- sensors ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fraction", FRACTIONS)
def test_playback_sensor_status_and_value_equal_sensors_at_that_hour(get_json, playback, fraction):
    index = index_at(playback, fraction)
    moment = playback["timestamps"][index]
    page = get_json("/sensors", as_of=moment, limit=5000)
    assert page["as_of"] == moment and page["total"] == len(page["items"]) == len(playback["sensors"])
    for item in page["items"]:
        series = playback["sensors"][item["sensor_id"]]
        assert series["status"][index] == SENSOR_CHAR[item["status"]], (item["sensor_id"], moment)
        value = None if item["latest"] is None else item["latest"]["value"]
        assert series["values"][index] == value, (item["sensor_id"], moment)
        if item["latest"] is not None:
            assert item["latest"]["ts"] == moment
    for wanted, char in SENSOR_CHAR.items():
        filtered = get_json("/sensors", as_of=moment, status=wanted, limit=5000)
        assert {item["sensor_id"] for item in filtered["items"]} == {
            sensor_id for sensor_id, series in playback["sensors"].items() if series["status"][index] == char
        }, (wanted, moment)


def test_playback_sensor_values_equal_the_reading_series_of_every_sensor(get_json, playback):
    for sensor_id, series in playback["sensors"].items():
        columns = get_json("/sensor-readings", sensor_id=sensor_id, shape="columns")
        assert columns["start"] == playback["timestamps"][0] and columns["count"] == len(playback["timestamps"])
        assert columns["value"] == series["values"], sensor_id
        assert [char == "o" for char in series["status"]] == [value is None for value in columns["value"]], sensor_id


def test_sensor_detail_agrees_with_the_bundle(get_json, playback):
    for fraction in FRACTIONS:
        index = index_at(playback, fraction)
        moment = playback["timestamps"][index]
        for sensor_id in list(playback["sensors"])[:: max(1, len(playback["sensors"]) // 12)]:
            detail = get_json(f"/sensors/{sensor_id}", as_of=moment)
            assert SENSOR_CHAR[detail["status"]] == playback["sensors"][sensor_id]["status"][index], (sensor_id, moment)


# --- assets -------------------------------------------------------------------------------------------------------
def test_playback_asset_health_equals_the_health_series_of_every_monitored_asset(get_json, playback):
    assert playback["assets"]
    for asset_id, series in playback["assets"].items():
        health = get_json(f"/assets/{asset_id}/health")
        assert (health["start"], health["count"]) == (playback["timestamps"][0], len(playback["timestamps"])), asset_id
        assert health["health_score"] == series["health"], asset_id
        assert health["status"] == series["status"], asset_id


@pytest.mark.parametrize("fraction", FRACTIONS)
def test_playback_asset_health_equals_asset_detail_at_that_hour(get_json, playback, fraction):
    index = index_at(playback, fraction)
    moment = playback["timestamps"][index]
    for asset_id, series in playback["assets"].items():
        detail = get_json(f"/assets/{asset_id}", as_of=moment)
        assert detail["as_of"] == moment
        assert detail["health"]["score"] == series["health"][index], (asset_id, moment)
        assert ASSET_CHAR[detail["health"]["status"]] == series["status"][index], (asset_id, moment)
        statuses = {sensor["sensor_id"]: SENSOR_CHAR[sensor["status"]] for sensor in detail["sensors"]}
        assert statuses == {sensor_id: playback["sensors"][sensor_id]["status"][index] for sensor_id in statuses}, (asset_id, moment)


def test_asset_list_carries_the_values_of_the_last_hour(get_json, playback):
    features = get_json("/assets")["features"]
    monitored = {f["properties"]["asset_id"]: f["properties"] for f in features if f["properties"]["monitored"]}
    assert set(monitored) == set(playback["assets"])
    for asset_id, properties in monitored.items():
        series = playback["assets"][asset_id]
        assert properties["health_score"] == series["health"][-1], asset_id
        assert ASSET_CHAR[properties["status"]] == series["status"][-1], asset_id
    others = [f["properties"] for f in features if not f["properties"]["monitored"]]
    assert others and all(p["health_score"] is None and p["status"] == "not_monitored" for p in others)


def test_assets_at_risk_are_the_monitored_assets_below_70(get_json, playback):
    for fraction in FRACTIONS:
        index = index_at(playback, fraction)
        below = sum(series["health"][index] < AT_RISK_BELOW for series in playback["assets"].values())
        assert get_json("/statistics", as_of=playback["timestamps"][index])["assets_at_risk"] == below
    in_bad_status = sum(series["status"][-1] in "rc" for series in playback["assets"].values())
    assert playback["stats"]["assets_at_risk"][-1] == in_bad_status  # at_risk and critical are exactly "below 70"


# --- risk zones ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fraction", FRACTIONS)
def test_playback_zones_equal_risk_zones_at_that_hour(get_json, playback, fraction):
    index = index_at(playback, fraction)
    moment = playback["timestamps"][index]
    collection = get_json("/spatial/risk-zones", as_of=moment)
    assert collection["as_of"] == moment
    cells = {feature["properties"]["cell_id"]: feature["properties"] for feature in collection["features"]}
    assert set(playback["zones"]) <= set(cells)
    for cell_id, properties in cells.items():
        score = properties["risk_score"]
        if cell_id in playback["zones"]:
            # the bundle holds the stored score rounded half up to an integer; the endpoint rounds it to 3 decimals
            assert abs(playback["zones"][cell_id]["risk"][index] - score) <= 0.5 + 5e-4, (cell_id, moment)
        else:
            assert score == 0, (cell_id, moment)  # never above zero in the whole window
        assert properties["risk_level"] == next(level for level, lowest in RISK_LEVEL_FROM if score >= lowest), (cell_id, moment)


def test_playback_zones_equal_the_stored_scores(playback, db_conn):
    stored: dict[str, dict[str, float]] = {}
    for cell_id, as_of, score in db_conn.execute("SELECT cell_id, as_of, risk_score FROM infra.risk_zone_scores"):
        stored.setdefault(cell_id, {})[support.iso_z(as_of)] = float(score)
    assert set(playback["zones"]) == set(stored)  # sparse storage: a cell is in the bundle when it ever has a score
    for cell_id, series in playback["zones"].items():
        for moment, value in zip(playback["timestamps"], series["risk"], strict=True):
            score = stored[cell_id].get(moment, 0.0)
            assert isinstance(value, int) and abs(value - score) <= 0.5, (cell_id, moment)  # rounded to an integer


def test_anomaly_count_of_a_zone_counts_the_anomalies_active_in_the_cell(get_json, playback, db_conn):
    index = playback["stats"]["active_anomalies"].index(max(playback["stats"]["active_anomalies"]))
    moment = playback["timestamps"][index]
    expected = dict(
        db_conn.execute(
            """
            SELECT z.cell_id, count(*)
            FROM infra.risk_zones z
            JOIN infra.anomalies a ON ST_Covers(z.geom, a.geom)
            WHERE a.started_at <= %(t)s AND a.ended_at >= %(t)s
            GROUP BY z.cell_id
            """,
            {"t": parse_z(moment)},
        ).fetchall()
    )
    counts = {f["properties"]["cell_id"]: f["properties"]["anomaly_count"] for f in get_json("/spatial/risk-zones", as_of=moment)["features"]}
    active = playback["stats"]["active_anomalies"][index]
    assert active > 0 and sum(expected.values()) == active  # every active anomaly lies in exactly one cell
    assert {cell: count for cell, count in counts.items() if count} == expected
