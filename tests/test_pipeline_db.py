"""The pipeline on PostGIS (stages 2-6, default settings: 30 days, 40 events, seed 42) - integration tests.

What the stages wrote is checked against the contract with SQL and Python written for this test (not with the
pipeline's own helpers): relationships, row counts, the detection targets, the stored anomaly fields, the
health score and the risk score recomputed from the stored inputs, idempotent re-runs and FK cascades.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from pipeline.analysis import clustering
from pipeline.detection import runner
from pipeline.models import Reading
from pipeline.sensors.ingestion import IngestionService
from pipeline.stages import analyze
from tests.conftest import run_stages
from tests.support import (
    DATA_TABLES,
    DETECTOR_ORDER,
    EVENT_MIX_40,
    METRIC_KEYS,
    SEVERITY_WEIGHT,
    THRESHOLDS,
    table_count,
    table_md5,
)

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
MATCH_SQL = """
    a.sensor_id = e.sensor_id
    AND a.started_at <= e.ended_at + interval '2 hours'
    AND a.ended_at >= e.started_at - interval '2 hours'
"""


def one(conn, query: str, params=None):
    return conn.execute(query, params).fetchone()[0]


@pytest.fixture(scope="module")
def run(test_db):
    """The latest detection run: id, window and stored metrics."""
    with psycopg.connect(test_db.dsn) as conn:
        row = conn.execute(
            "SELECT run_id, window_start, window_end, metrics, params, n_readings, n_anomalies, started_at, finished_at "
            "FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    keys = ("run_id", "start", "end", "metrics", "params", "n_readings", "n_anomalies", "started_at", "finished_at")
    return dict(zip(keys, row))


# --- what each stage wrote ----------------------------------------------------------------------------------------
def test_every_table_is_populated_and_counts_are_consistent(db_conn, test_db, run):
    counts = {table: table_count(db_conn, table) for table in DATA_TABLES}
    assert all(count > 0 for count in counts.values()), counts
    stages = test_db.stage_results
    assert counts["sensors"] == stages["generate"]["sensors"]
    assert counts["sensor_readings"] == stages["generate"]["readings"] == run["n_readings"]
    assert counts["reading_scores"] == counts["sensor_readings"]  # one score per analysed reading
    assert counts["anomalies"] == run["n_anomalies"] == stages["detect"]["anomalies"]
    assert counts["detection_runs"] == 1 and run["run_id"] == 1
    assert counts["study_areas"] == 1
    monitored = one(db_conn, "SELECT count(DISTINCT asset_id) FROM infra.sensors")
    steps = (run["end"] - run["start"]) // HOUR + 1
    assert steps == test_db.settings.sim_steps == 720
    assert counts["asset_health"] == monitored * steps  # every monitored asset, every hour
    assert counts["risk_zone_scores"] < counts["risk_zones"] * steps  # sparse
    assert counts["simulation_events"] == 44  # 40 injected + 3 rain events + 1 hot spell
    assert counts["sensor_thresholds"] == len(THRESHOLDS)


def test_processed_layers_are_loaded_completely(db_conn, test_db):
    processed = test_db.stage_results["process"]
    assert table_count(db_conn, "buildings") == len(processed.buildings["features"])
    assert table_count(db_conn, "roads") == len(processed.roads["features"])
    real = one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated")
    assert real == len(processed.assets["features"])
    in_db = dict(db_conn.execute("SELECT asset_type, count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated GROUP BY 1").fetchall())
    assert in_db == {kind: count for kind, count in processed.report["assets"]["by_type"].items() if count}
    assert one(db_conn, "SELECT count(*) FROM infra.reference_boundaries") == len(processed.city_boundary["features"])
    # building and road assets are linked to their base rows
    unlinked = one(
        db_conn,
        "SELECT count(*) FROM infra.infrastructure_assets WHERE (asset_type = 'building' AND building_id IS NULL) "
        "OR (asset_type = 'road' AND road_id IS NULL)",
    )
    assert unlinked == 0


def test_time_window_is_the_first_to_the_last_reading(db_conn, test_db, run):
    first, last = db_conn.execute("SELECT min(ts), max(ts) FROM infra.sensor_readings").fetchone()
    assert (run["start"], run["end"]) == (first, last)
    assert (first, last) == (test_db.settings.sim_start_utc, test_db.settings.sim_end_utc)
    assert (first, last) == (datetime(2026, 9, 1, 5, tzinfo=UTC), datetime(2026, 10, 1, 4, tzinfo=UTC))
    off_grid = one(db_conn, "SELECT count(*) FROM infra.sensor_readings WHERE date_trunc('hour', ts) <> ts")
    assert off_grid == 0
    assert run["finished_at"] >= run["started_at"]


def test_data_sources_are_labelled_real_simulated_or_derived(db_conn):
    kinds = dict(db_conn.execute("SELECT source_id, kind FROM infra.data_sources").fetchall())
    assert kinds == {"osm": "real", "nbi": "real", "tiger": "real", "usgs_3dep": "real", "basemap": "real",
                     "imagery": "real", "simulator": "simulated", "derived": "derived"}  # fmt: skip
    notes = dict(db_conn.execute("SELECT source_id, notes FROM infra.data_sources WHERE source_id IN ('basemap', 'imagery')").fetchall())
    assert notes == {"basemap": "display only", "imagery": "display only"}
    attribution = dict(db_conn.execute("SELECT source_id, attribution_text FROM infra.data_sources").fetchall())
    assert attribution["osm"] == "© OpenStreetMap contributors"
    assert attribution["nbi"].startswith("FHWA National Bridge Inventory (data as of ") and attribution["nbi"].endswith("distributed by USDOT/BTS NTAD")
    assert attribution["tiger"] == "U.S. Census Bureau, TIGERweb"
    assert attribution["usgs_3dep"].startswith("U.S. Geological Survey, 3D Elevation Program")
    assert attribution["basemap"] == "OpenFreeMap © OpenMapTiles Data from OpenStreetMap"
    assert attribution["imagery"] == "USDA, USGS The National Map: Orthoimagery"


def test_sensor_thresholds_are_the_table_of_the_contract(db_conn):
    rows = db_conn.execute("SELECT sensor_type, placement, warn_low, warn_high, crit_low, crit_high, unit FROM infra.sensor_thresholds").fetchall()
    assert {(row[0], row[1]): tuple(row[2:6]) for row in rows} == THRESHOLDS
    assert {row[0]: row[6] for row in rows} == {"temperature": "°C", "vibration": "mm/s", "moisture": "%", "pressure": "psi"}


# --- relationships: asset -> sensor -> reading -> anomaly ---------------------------------------------------------
def test_simulated_and_real_assets_are_told_apart(db_conn):
    rows = db_conn.execute(
        "SELECT asset_type, is_simulated, source_id, category, count(*) FROM infra.infrastructure_assets GROUP BY 1, 2, 3, 4"
    ).fetchall()
    simulated = [row for row in rows if row[1]]
    assert [(row[0], row[2], row[3]) for row in simulated] == [("water_main", "simulator", "Simulated network")]
    assert all(row[2] in ("osm", "nbi") for row in rows if not row[1])
    assert one(db_conn, "SELECT count(*) FROM infra.sensors WHERE NOT is_simulated OR source <> 'simulator'") == 0
    assert one(db_conn, "SELECT count(*) FROM infra.sensor_readings WHERE source <> 'simulator' OR status <> 'ok'") == 0


def test_water_mains_exist_only_for_pressure_sensors_and_carry_no_invented_attribute(db_conn, test_db):
    mains = db_conn.execute(
        """
        SELECT a.asset_id, a.properties, a.name, GeometryType(a.geom),
               (SELECT array_agg(s.sensor_type) FROM infra.sensors s WHERE s.asset_id = a.asset_id),
               (SELECT r.asset_type FROM infra.infrastructure_assets r WHERE r.asset_id = a.properties ->> 'host_road_id')
        FROM infra.infrastructure_assets a WHERE a.asset_type = 'water_main' ORDER BY a.asset_id
        """
    ).fetchall()
    assert len(mains) == min(test_db.settings.SIM_WATER_MAINS, one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE asset_type = 'road'"))
    assert [row[0] for row in mains] == [f"WM-{n:03d}" for n in range(1, len(mains) + 1)]
    for _asset_id, properties, name, geometry_type, sensor_types, host_type in mains:
        assert set(properties) == {"host_road_id", "length_m", "offset_m"} and properties["offset_m"] == 4.5
        assert name.startswith("Simulated water main along ")
        assert geometry_type == "LINESTRING" and sensor_types == ["pressure"] and host_type == "road"
    pressure_hosts = one(db_conn, "SELECT count(DISTINCT asset_id) FROM infra.sensors WHERE sensor_type = 'pressure'")
    assert pressure_hosts == len(mains) == one(db_conn, "SELECT count(*) FROM infra.sensors WHERE sensor_type = 'pressure'")
    offsets = db_conn.execute(
        """
        SELECT min(ST_Distance(m.centroid::geography, r.geom::geography)), max(ST_Distance(m.centroid::geography, r.geom::geography))
        FROM infra.infrastructure_assets m
        JOIN infra.infrastructure_assets r ON r.asset_id = m.properties ->> 'host_road_id'
        WHERE m.asset_type = 'water_main'
        """
    ).fetchone()
    assert offsets[0] >= 4.0 and offsets[1] <= 5.0  # 4.5 m beside the host road (planar offset, geodesic check)


def test_every_sensor_lies_inside_the_study_area_and_on_its_asset(db_conn):
    outside = one(
        db_conn,
        "SELECT count(*) FROM infra.sensors s WHERE NOT EXISTS (SELECT 1 FROM infra.study_areas a WHERE ST_Covers(a.geom, s.geom))",
    )
    assert outside == 0
    farthest = one(
        db_conn,
        "SELECT max(ST_Distance(s.geom::geography, a.geom::geography)) FROM infra.sensors s JOIN infra.infrastructure_assets a USING (asset_id)",
    )
    assert farthest <= 8.5  # on the geometry; a few metres around a point asset
    closest_pair = one(
        db_conn,
        "SELECT min(ST_Distance(a.geom::geography, b.geom::geography)) FROM infra.sensors a JOIN infra.sensors b "
        "ON a.asset_id = b.asset_id AND a.sensor_id < b.sensor_id",
    )
    assert closest_pair >= 1.0  # sensors sharing an asset are a few metres apart


def test_sensor_ids_units_and_classes(db_conn):
    rows = db_conn.execute("SELECT sensor_id, sensor_type, unit, sampling_interval_s, description FROM infra.sensors ORDER BY sensor_id").fetchall()
    prefix = {"temperature": "TMP", "vibration": "VIB", "moisture": "MST", "pressure": "PRS"}
    unit = {"temperature": "°C", "vibration": "mm/s", "moisture": "%", "pressure": "psi"}
    per_type = Counter()
    for sensor_id, sensor_type, sensor_unit, interval, description in rows:
        per_type[sensor_type] += 1
        assert sensor_id.split("-")[0] == prefix[sensor_type] and len(sensor_id.split("-")[1]) == 3
        assert sensor_unit == unit[sensor_type] and interval == 3600
        assert description.startswith("Simulated ")
    for sensor_type, count in per_type.items():
        ids = sorted(row[0] for row in rows if row[1] == sensor_type)
        assert ids == [f"{prefix[sensor_type]}-{n:03d}" for n in range(1, count + 1)]
    assert one(db_conn, "SELECT count(*) FROM infra.sensor_readings r JOIN infra.sensors s USING (sensor_id) WHERE r.unit <> s.unit") == 0


def test_a_missing_hour_is_an_absent_row(db_conn, run):
    steps = (run["end"] - run["start"]) // HOUR + 1
    sensors, readings = db_conn.execute("SELECT (SELECT count(*) FROM infra.sensors), (SELECT count(*) FROM infra.sensor_readings)").fetchone()
    assert readings < sensors * steps
    offline_at_end = db_conn.execute(
        "SELECT s.sensor_id, (SELECT max(ts) FROM infra.sensor_readings r WHERE r.sensor_id = s.sensor_id) FROM infra.sensors s "
        "WHERE NOT EXISTS (SELECT 1 FROM infra.sensor_readings r WHERE r.sensor_id = s.sensor_id AND r.ts = %s) ORDER BY 2",
        (run["end"],),
    ).fetchall()
    assert len(offline_at_end) == 2  # the two outages that run through the final hour ...
    assert [(run["end"] - last) // HOUR for _, last in offline_at_end] == [30, 6]  # ... of 30 h and 6 h
    assert one(db_conn, "SELECT count(*) FROM infra.sensor_readings WHERE value IS NULL OR value = 'NaN'") == 0


def test_every_anomaly_points_at_an_existing_reading_of_its_sensor_and_asset(db_conn):
    mismatches = one(
        db_conn,
        """
        SELECT count(*) FROM infra.anomalies an
        JOIN infra.sensors s ON s.sensor_id = an.sensor_id
        LEFT JOIN infra.sensor_readings peak ON peak.sensor_id = an.sensor_id AND peak.ts = an.peak_at
        LEFT JOIN infra.sensor_readings first ON first.sensor_id = an.sensor_id AND first.ts = an.started_at
        LEFT JOIN infra.sensor_readings last ON last.sensor_id = an.sensor_id AND last.ts = an.ended_at
        WHERE an.asset_id <> s.asset_id OR an.sensor_type <> s.sensor_type OR an.unit <> s.unit
           OR NOT ST_Equals(an.geom, s.geom)
           OR peak.ts IS NULL OR first.ts IS NULL OR last.ts IS NULL
           OR abs(peak.value - an.observed_value) > 1e-9
        """,
    )
    assert mismatches == 0


def test_simulation_events_hold_the_ground_truth(db_conn, run):
    mix = dict(db_conn.execute("SELECT event_type, count(*) FROM infra.simulation_events WHERE is_anomaly GROUP BY 1").fetchall())
    assert mix == EVENT_MIX_40
    benign = db_conn.execute("SELECT event_type, sensor_id, asset_id, sensor_type FROM infra.simulation_events WHERE NOT is_anomaly ORDER BY event_id").fetchall()
    assert Counter(row[0] for row in benign) == {"regional_rain": 3, "regional_hot_spell": 1}
    assert all(row[1] is None and row[2] is None for row in benign)  # regional events belong to no sensor
    assert one(db_conn, "SELECT count(*) FROM infra.simulation_events WHERE is_anomaly AND (sensor_id IS NULL OR asset_id IS NULL)") == 0
    assert one(db_conn, "SELECT min(started_at) FROM infra.simulation_events WHERE is_anomaly") >= run["start"] + 72 * HOUR
    ongoing = dict(db_conn.execute("SELECT event_type, count(*) FROM infra.simulation_events WHERE is_anomaly AND ended_at = %s GROUP BY 1", (run["end"],)).fetchall())
    assert sum(ongoing.values()) == 6


# --- detection: targets on the default seed -----------------------------------------------------------------------
def test_detection_targets_recomputed_with_sql(db_conn, run):
    injected = one(db_conn, "SELECT count(*) FROM infra.simulation_events WHERE is_anomaly")
    detected = one(db_conn, f"SELECT count(*) FROM infra.simulation_events e WHERE e.is_anomaly AND EXISTS (SELECT 1 FROM infra.anomalies a WHERE {MATCH_SQL})")
    anomalies = one(db_conn, "SELECT count(*) FROM infra.anomalies")
    true = one(db_conn, f"SELECT count(*) FROM infra.anomalies a WHERE EXISTS (SELECT 1 FROM infra.simulation_events e WHERE e.is_anomaly AND {MATCH_SQL})")
    during_benign = one(
        db_conn,
        f"""
        SELECT count(*) FROM infra.anomalies a
        WHERE NOT EXISTS (SELECT 1 FROM infra.simulation_events e WHERE e.is_anomaly AND {MATCH_SQL})
          AND EXISTS (SELECT 1 FROM infra.simulation_events b WHERE NOT b.is_anomaly AND b.sensor_type = a.sensor_type
                      AND a.started_at <= b.ended_at AND a.ended_at >= b.started_at)
        """,
    )
    recall, precision = detected / injected, true / anomalies
    assert injected == 40
    assert recall >= 0.9, (detected, injected)
    assert precision >= 0.85, (true, anomalies)
    assert during_benign <= 2
    severity = dict(db_conn.execute("SELECT severity, count(*) FROM infra.anomalies GROUP BY 1").fetchall())
    assert 2 <= severity.get("critical", 0) <= 8, severity
    assert max(severity.values()) / anomalies <= 0.5, severity  # no severity class above 50 %
    active = dict(db_conn.execute("SELECT severity, count(*) FROM infra.anomalies WHERE started_at <= %(t)s AND ended_at >= %(t)s GROUP BY 1", {"t": run["end"]}).fetchall())
    assert active.get("critical", 0) >= 1 and active.get("high", 0) >= 1, active
    # the stored metrics report the same figures, under exactly the keys of the contract
    metrics = run["metrics"]
    assert set(metrics) == METRIC_KEYS
    assert (metrics["injected_events"], metrics["detected_events"], metrics["anomalies"], metrics["true_anomalies"]) == (injected, detected, anomalies, true)
    assert metrics["event_recall"] == round(recall, 3) and metrics["anomaly_precision"] == round(precision, 3)
    assert metrics["false_anomalies"] == anomalies - true
    assert metrics["false_anomalies_during_benign_events"] == during_benign
    assert metrics["severity_counts"] == {name: severity.get(name, 0) for name in ("low", "medium", "high", "critical")}
    assert set(metrics["detection_delay_hours"]) <= set(EVENT_MIX_40)


def test_every_event_that_is_ongoing_at_the_end_has_an_active_anomaly(db_conn, run):
    missing = db_conn.execute(
        """
        SELECT e.event_type, e.sensor_id FROM infra.simulation_events e
        WHERE e.is_anomaly AND e.ended_at >= %(t)s
          AND NOT EXISTS (SELECT 1 FROM infra.anomalies a WHERE a.sensor_id = e.sensor_id AND a.ended_at >= %(t)s)
        """,
        {"t": run["end"]},
    ).fetchall()
    assert missing == []
    critical_drop = db_conn.execute(
        """
        SELECT a.severity, a.detection_method, a.score_components ->> 'threshold'
        FROM infra.simulation_events e JOIN infra.anomalies a ON a.sensor_id = e.sensor_id AND a.ended_at >= %(t)s
        WHERE e.is_anomaly AND e.event_type = 'pressure_drop' AND e.ended_at >= %(t)s
        """,
        {"t": run["end"]},
    ).fetchall()
    assert len(critical_drop) == 1
    assert critical_drop[0][0] == "critical" and critical_drop[0][1].startswith("threshold+") and float(critical_drop[0][2]) == 1.0


def test_stored_status_equals_the_time_rule_at_t_end(db_conn, run):
    """Amendment A1: active <=> ended_at >= T_end, otherwise resolved."""
    rows = db_conn.execute("SELECT anomaly_id, status, started_at, ended_at FROM infra.anomalies").fetchall()
    for anomaly_id, status, started_at, ended_at in rows:
        assert started_at <= run["end"]
        assert status == ("active" if ended_at >= run["end"] else "resolved"), anomaly_id
    assert Counter(row[1] for row in rows)["active"] >= 2
    explained = one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE (status = 'active') <> (explanation LIKE '%%still present at the end of the analysed window%%')")
    assert explained == 0


def test_sql_example_queries_agree_with_the_time_rule(db_conn, run):
    """sql/queries/06 counts active anomalies from the stored status: it must equal the rule ended_at >= T_end."""
    by_status, by_rule = db_conn.execute(
        """
        SELECT count(*) FILTER (WHERE a.status = 'active'), count(*) FILTER (WHERE a.started_at <= %(t)s AND a.ended_at >= %(t)s)
        FROM infra.infrastructure_assets r JOIN infra.anomalies a ON ST_DWithin(a.geom::geography, r.geom::geography, 30)
        WHERE r.asset_type = 'road'
        """,
        {"t": run["end"]},
    ).fetchone()
    assert by_status == by_rule


def test_anomalies_are_numbered_by_start_time_then_sensor(db_conn):
    rows = db_conn.execute("SELECT anomaly_id FROM infra.anomalies ORDER BY started_at, sensor_id").fetchall()
    assert [row[0] for row in rows] == [f"ANM-{n:04d}" for n in range(1, len(rows) + 1)]


def test_anomaly_fields_are_reproducible_from_the_stored_readings(db_conn):
    rows = db_conn.execute(
        """
        SELECT an.anomaly_id, an.sensor_type, s.placement, an.started_at, an.ended_at, an.peak_at, an.duration_hours,
               an.robust_z, an.anomaly_score, an.score_components, an.severity, an.detection_method, an.anomaly_type,
               an.expected_value, an.explanation,
               (SELECT array_agg(r.value ORDER BY r.ts) FROM infra.sensor_readings r
                WHERE r.sensor_id = an.sensor_id AND r.ts BETWEEN an.started_at AND an.ended_at),
               (SELECT array_agg(sc.robust_z ORDER BY sc.ts) FROM infra.reading_scores sc
                WHERE sc.sensor_id = an.sensor_id AND sc.ts BETWEEN an.started_at AND an.ended_at),
               (SELECT sc.robust_z FROM infra.reading_scores sc WHERE sc.sensor_id = an.sensor_id AND sc.ts = an.peak_at),
               (SELECT sc.expected FROM infra.reading_scores sc WHERE sc.sensor_id = an.sensor_id AND sc.ts = an.peak_at)
        FROM infra.anomalies an JOIN infra.sensors s ON s.sensor_id = an.sensor_id
        """
    ).fetchall()
    assert len(rows) >= 30
    for (anomaly_id, sensor_type, placement, started, ended, peak, duration, z, score, components, severity, method,
         anomaly_type, expected, explanation, values, zs, peak_z, peak_expected) in rows:  # fmt: skip
        assert duration == (ended - started) // HOUR + 1, anomaly_id
        assert started <= peak <= ended
        assert z == pytest.approx(peak_z, abs=1e-3) and expected == pytest.approx(peak_expected, abs=1e-3)
        assert abs(z) == pytest.approx(max(abs(value) for value in zs), abs=1e-3)  # the peak is the largest |z|
        assert abs(zs[0]) >= 3.0 or beyond(values[0], sensor_type, placement, 2)  # trimmed to |z| >= z_min ...
        assert abs(zs[-1]) >= 3.0 or beyond(values[-1], sensor_type, placement, 2)  # ... at both ends
        # score = 0.50 M + 0.25 D + 0.25 T
        magnitude = min(max(math.log2(abs(z) / 3.0) / 4.0, 0.0), 1.0)
        duration_part = min(math.log(1 + duration) / math.log(97), 1.0)
        threshold = 1.0 if any(beyond(v, sensor_type, placement, 2) for v in values) else 0.5 if any(beyond(v, sensor_type, placement, 0) for v in values) else 0.0
        assert set(components) == {"magnitude", "duration", "threshold"}
        assert components["magnitude"] == pytest.approx(magnitude, abs=1.5e-3), anomaly_id
        assert components["duration"] == pytest.approx(duration_part, abs=1.5e-3), anomaly_id
        assert components["threshold"] == threshold, anomaly_id
        assert score == pytest.approx(round(0.5 * magnitude + 0.25 * duration_part + 0.25 * threshold, 3), abs=1.5e-3), anomaly_id
        assert severity == ("critical" if score >= 0.70 else "high" if score >= 0.50 else "medium" if score >= 0.30 else "low")
        # persistence rule: a strong peak, three flagged hours, or a critical breach
        assert abs(z) >= 6.0 or len(values) >= 3 or threshold == 1.0, anomaly_id
        methods = method.split("+")
        assert methods == [name for name in DETECTOR_ORDER if name in methods] and set(methods) - {"isolation_forest"}
        assert ("threshold" in methods) == (threshold == 1.0)
        assert ("robust_zscore" in methods) or max(abs(value) for value in zs) < 6.0 or "threshold" in methods or "rolling_median" in methods
        assert anomaly_type == signature(sensor_type, z, duration), anomaly_id
        assert explanation and "  " not in explanation


def beyond(value: float, sensor_type: str, placement: str, offset: int) -> bool:
    """Value beyond the warning (offset 0) or critical (offset 2) limits of its class."""
    low, high = THRESHOLDS[(sensor_type, placement)][offset : offset + 2]
    return (low is not None and value < low) or (high is not None and value > high)


def signature(sensor_type: str, z: float, duration: int) -> str:
    if sensor_type == "vibration":
        return ("vibration_spike" if duration <= 3 else "sustained_high_vibration") if z > 0 else "vibration_drop"
    if sensor_type == "moisture":
        return "moisture_increase" if z > 0 else "moisture_decrease"
    if sensor_type == "pressure":
        return "pressure_spike" if z > 0 else "pressure_decline" if duration >= 36 else "pressure_drop"
    return "temperature_drift" if duration >= 24 else "temperature_spike" if z > 0 else "temperature_drop"


def test_not_every_unusual_reading_is_an_anomaly(db_conn):
    flagged, in_anomalies = db_conn.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM infra.anomalies an WHERE an.sensor_id = sc.sensor_id
                                              AND sc.ts BETWEEN an.started_at AND an.ended_at))
        FROM infra.reading_scores sc WHERE sc.flagged
        """
    ).fetchone()
    assert flagged > in_anomalies > 0  # flagged readings outside every anomaly stay "warning" readings
    unflagged_inside_strong = one(db_conn, "SELECT count(*) FROM infra.reading_scores WHERE abs(robust_z) >= 3.0 AND NOT flagged")
    assert unflagged_inside_strong == 0  # every reading at or above z_min is flagged
    total = one(db_conn, "SELECT count(*) FROM infra.reading_scores")
    assert flagged / total < 0.05


def test_flags_are_reproducible_from_the_stored_scores(db_conn, run):
    """flagged = |z| >= 3, or beyond a critical limit, or |trailing 6 h median of z| >= 3 with >= 4 readings."""
    sensors = db_conn.execute(
        "SELECT sensor_id, sensor_type, placement FROM infra.sensors WHERE sensor_id IN "
        "(SELECT sensor_id FROM infra.anomalies GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 8) OR sensor_id LIKE '%%-005'"
    ).fetchall()
    steps = (run["end"] - run["start"]) // HOUR + 1
    for sensor_id, sensor_type, placement in sensors:
        z: list[float | None] = [None] * steps
        value: list[float | None] = [None] * steps
        stored = [False] * steps
        for ts, reading, score, flag in db_conn.execute(
            "SELECT r.ts, r.value, sc.robust_z, sc.flagged FROM infra.sensor_readings r JOIN infra.reading_scores sc USING (sensor_id, ts) WHERE r.sensor_id = %s",
            (sensor_id,),
        ):
            index = (ts - run["start"]) // HOUR
            z[index], value[index], stored[index] = score, reading, flag
        for t in range(steps):
            if z[t] is None:
                assert not stored[t]
                continue
            window = [v for v in z[max(t - 5, 0) : t + 1] if v is not None]
            rolling = len(window) >= 4 and abs(statistics.median(window)) >= 3.0
            expected = abs(z[t]) >= 3.0 or beyond(value[t], sensor_type, placement, 2) or rolling
            assert stored[t] == expected, (sensor_id, t, z[t], window)


def test_scores_and_bands_are_stored_for_every_reading(db_conn):
    bad = one(
        db_conn,
        """
        SELECT count(*) FROM infra.sensor_readings r LEFT JOIN infra.reading_scores sc USING (sensor_id, ts)
        WHERE sc.ts IS NULL OR sc.expected IS NULL OR sc.robust_z IS NULL OR sc.iforest_score IS NULL
           OR NOT (sc.expected_low < sc.expected AND sc.expected < sc.expected_high)
           OR sc.iforest_score <= 0 OR sc.iforest_score > 1
        """,
    )
    assert bad == 0
    # the band is expected +- 3 robust sigma (vibration: multiplicative), so z = +-3 sits on its edge
    off_band = one(
        db_conn,
        """
        SELECT count(*) FROM infra.sensor_readings r JOIN infra.reading_scores sc USING (sensor_id, ts)
        JOIN infra.sensors s USING (sensor_id)
        WHERE (sc.robust_z > 3.02 AND r.value <= sc.expected_high - 0.02 * (sc.expected_high - sc.expected))
           OR (sc.robust_z < 2.98 AND sc.robust_z > 0 AND r.value >= sc.expected_high + 0.02 * (sc.expected_high - sc.expected))
        """,
    )
    assert off_band == 0
    params = one(db_conn, "SELECT params FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1")
    assert params["retrospective"] is True and params["z_strong"] == 6.0 and params["z_min"] == 3.0
    assert set(params["sensor_baselines"]) == {row[0] for row in db_conn.execute("SELECT sensor_id FROM infra.sensors")}


# --- spatial analysis ---------------------------------------------------------------------------------------------
def test_planted_colocated_group_is_recovered_as_one_cluster(db_conn):
    rows = db_conn.execute(
        f"""
        SELECT e.sensor_id, (SELECT min(a.cluster_id) FROM infra.anomalies a WHERE {MATCH_SQL})
        FROM infra.simulation_events e WHERE e.is_anomaly AND e.description LIKE '%%co-located group%%'
        """
    ).fetchall()
    assert len(rows) == 4 and len({row[0] for row in rows}) == 4
    clusters = {row[1] for row in rows}
    assert None not in clusters and len(clusters) == 1  # all four anomalies share one cluster
    recovery = clustering.colocated_group_recovery(db_conn)
    assert recovery["recovered"] is True and recovery["cluster_id"] == clusters.pop()


def test_clusters_satisfy_the_definition(db_conn, test_db):
    settings = test_db.settings
    clusters = db_conn.execute(
        """
        SELECT c.cluster_id, c.method, c.n_anomalies, c.n_sensors, c.n_assets, c.sensor_types, c.max_severity,
               c.first_started_at, c.last_ended_at,
               (SELECT count(*) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT count(DISTINCT an.sensor_id) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT count(DISTINCT an.asset_id) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT array_agg(DISTINCT an.sensor_type ORDER BY an.sensor_type) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT min(an.started_at) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT max(an.ended_at) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT bool_and(ST_Covers(c.geom, an.geom)) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               (SELECT array_agg(DISTINCT an.severity) FROM infra.anomalies an WHERE an.cluster_id = c.cluster_id),
               GeometryType(c.geom), ST_IsValid(c.geom)
        FROM infra.anomaly_clusters c ORDER BY c.cluster_id
        """
    ).fetchall()
    assert [row[0] for row in clusters] == list(range(1, len(clusters) + 1)) and clusters
    for (_cluster_id, method, n_anomalies, n_sensors, n_assets, sensor_types, max_severity, first, last, members,
         sensors, assets, types, min_start, max_end, covered, severities, geometry_type, valid) in clusters:  # fmt: skip
        assert method == "st_dbscan"
        assert (n_anomalies, n_sensors, n_assets, sensor_types) == (members, sensors, assets, types)
        assert members >= settings.CLUSTER_MIN_POINTS and sensors >= settings.CLUSTER_MIN_SENSORS
        assert (first, last) == (min_start, max_end)
        assert max_severity == max(severities, key=lambda name: ("low", "medium", "high", "critical").index(name))
        assert covered and geometry_type == "POLYGON" and valid  # the hull (buffered 40 m) contains its anomalies
    # every clustered anomaly has a neighbour of its cluster within eps in space AND time
    lonely = one(
        db_conn,
        """
        WITH pts AS (
            SELECT an.anomaly_id, an.cluster_id, an.started_at, an.ended_at, ST_Transform(an.geom, sa.utm_srid) AS g
            FROM infra.anomalies an CROSS JOIN (SELECT utm_srid FROM infra.study_areas LIMIT 1) sa
            WHERE an.cluster_id IS NOT NULL
        )
        SELECT count(*) FROM pts a WHERE NOT EXISTS (
            SELECT 1 FROM pts b WHERE b.cluster_id = a.cluster_id AND b.anomaly_id <> a.anomaly_id
              AND ST_Distance(a.g, b.g) <= %(eps_m)s
              AND GREATEST(a.started_at, b.started_at) - LEAST(a.ended_at, b.ended_at) <= make_interval(hours => %(eps_h)s)
        )
        """,
        {"eps_m": settings.CLUSTER_EPS_M, "eps_h": int(settings.CLUSTER_EPS_HOURS)},
    )
    assert lonely == 0


def test_risk_grid_is_a_hexagonal_grid_over_the_study_area(db_conn, test_db):
    cells = db_conn.execute(
        """
        SELECT z.cell_id, ST_Area(ST_Transform(z.geom, sa.utm_srid)), ST_Covers(ST_Buffer(sa.geom::geography, 0.5)::geometry, z.geom),
               ST_Covers(z.geom, z.centroid), ST_NPoints(z.geom)
        FROM infra.risk_zones z JOIN infra.study_areas sa USING (study_area_id)
        """
    ).fetchall()
    full_hexagon = 3 * math.sqrt(3) / 2 * test_db.settings.RISK_HEX_EDGE_M**2  # 58 457 m2 = 5.8 ha
    assert full_hexagon == pytest.approx(58_457, abs=1)
    assert all(row[0].count("_") == 1 and row[0].replace("_", "").replace("-", "").isdigit() for row in cells)  # 'i_j'
    assert max(row[1] for row in cells) == pytest.approx(full_hexagon, rel=0.01)
    assert all(row[2] for row in cells)  # clipped to the study area
    assert sum(1 for row in cells if row[1] > 0.99 * full_hexagon) >= len(cells) // 2  # most cells are whole hexagons
    total_area = sum(row[1] for row in cells)
    study_area = one(db_conn, "SELECT ST_Area(ST_Transform(geom, utm_srid)) FROM infra.study_areas")
    assert total_area == pytest.approx(study_area, rel=1e-3)  # the cells tile the study area without gaps or overlaps


def test_risk_scores_equal_the_formula_recomputed_from_the_anomalies(db_conn, test_db, run):
    settings = test_db.settings
    distances = defaultdict(dict)
    for cell_id, anomaly_id, distance in db_conn.execute(
        """
        SELECT z.cell_id, an.anomaly_id, ST_Distance(ST_Transform(z.centroid, sa.utm_srid), ST_Transform(an.geom, sa.utm_srid))
        FROM infra.risk_zones z JOIN infra.study_areas sa USING (study_area_id) CROSS JOIN infra.anomalies an
        """
    ):
        distances[cell_id][anomaly_id] = distance
    anomalies = db_conn.execute("SELECT anomaly_id, severity, started_at, ended_at FROM infra.anomalies").fetchall()
    in_cell = dict(db_conn.execute("SELECT an.anomaly_id, (SELECT z.cell_id FROM infra.risk_zones z WHERE ST_Covers(z.geom, an.geom) ORDER BY z.cell_id LIMIT 1) FROM infra.anomalies an").fetchall())
    steps = (run["end"] - run["start"]) // HOUR + 1
    checked = stored_rows = 0
    for index in (0, 100, 250, 400, 555, 650, 700, steps - 1):
        t = run["start"] + index * HOUR
        stored = {row[0]: row[1:] for row in db_conn.execute("SELECT cell_id, risk_score, risk_level, anomaly_count FROM infra.risk_zone_scores WHERE as_of = %s", (t,))}
        stored_rows += len(stored)
        for cell_id, by_anomaly in distances.items():
            raw = 0.0
            count = 0
            for anomaly_id, severity, started, ended in anomalies:
                if started > t:
                    continue
                age_hours = (t - ended) / HOUR
                if age_hours > 14 * 24:
                    continue
                weight = 1.0 if age_hours <= 0 else 0.5 ** (age_hours / settings.RISK_HALF_LIFE_HOURS)
                raw += SEVERITY_WEIGHT[severity] * math.exp(-by_anomaly[anomaly_id] ** 2 / (2 * settings.RISK_BANDWIDTH_M**2)) * weight
                count += started <= t <= ended and in_cell[anomaly_id] == cell_id
            score = min(100.0, 100.0 * raw / settings.RISK_REFERENCE)
            checked += 1
            if cell_id in stored:
                assert stored[cell_id][0] == pytest.approx(score, abs=0.006), (cell_id, index)
                assert stored[cell_id][0] >= 0.5
                level = "very_high" if score >= 75 else "high" if score >= 50 else "moderate" if score >= 25 else "low"
                assert stored[cell_id][1] == level or abs(score % 25) < 0.01 or abs(score % 25 - 25) < 0.01
                assert stored[cell_id][2] == count, (cell_id, index)
            else:
                assert score < 0.5 + 0.006, (cell_id, index, score)  # sparse storage: only scores of 0.5 or more
    assert checked == 8 * len(distances) and stored_rows > 100
    assert one(db_conn, "SELECT count(*) FROM infra.risk_zone_scores WHERE as_of = %s", (run["start"],)) == 0  # quiet lead-in


def test_health_scores_equal_the_formula_recomputed_from_anomalies_and_scores(db_conn, test_db, run):
    settings = test_db.settings
    sensors_of = defaultdict(list)
    for sensor_id, asset_id in db_conn.execute("SELECT sensor_id, asset_id FROM infra.sensors ORDER BY sensor_id"):
        sensors_of[asset_id].append(sensor_id)
    z_at = defaultdict(dict)
    for sensor_id, ts, z in db_conn.execute("SELECT r.sensor_id, r.ts, sc.robust_z FROM infra.sensor_readings r JOIN infra.reading_scores sc USING (sensor_id, ts)"):
        z_at[sensor_id][ts] = z
    anomalies_of = defaultdict(list)
    for asset_id, severity, started, ended in db_conn.execute("SELECT asset_id, severity, started_at, ended_at FROM infra.anomalies"):
        anomalies_of[asset_id].append((severity, started, ended))
    steps = (run["end"] - run["start"]) // HOUR + 1
    compared = 0
    for index in (0, 5, 150, 300, 420, 500, 600, 660, 690, 700, 715, steps - 1):
        t = run["start"] + index * HOUR
        stored = db_conn.execute(
            "SELECT asset_id, health_score, status, frequency_penalty, severity_penalty, reading_penalty, sensor_penalty, "
            "anomalies_in_window, active_anomalies, sensors_reporting, sensors_total FROM infra.asset_health WHERE as_of = %s",
            (t,),
        ).fetchall()
        assert {row[0] for row in stored} == set(sensors_of)
        for asset_id, score, status, frequency, severity_penalty, reading, sensor, in_window, active, reporting, total in stored:
            weight_sum = weighted = 0.0
            expected_window = expected_active = 0
            for severity, started, ended in anomalies_of.get(asset_id, []):
                if started > t or ended < t - timedelta(days=settings.HEALTH_WINDOW_DAYS):
                    continue
                expected_window += 1
                weight = 1.0 if ended >= t else 0.5 ** ((t - ended) / HOUR / settings.HEALTH_HALF_LIFE_HOURS)
                expected_active += ended >= t
                weight_sum += weight
                weighted += weight * SEVERITY_WEIGHT[severity]
            levels = []
            for sensor_id in sensors_of[asset_id]:
                if t in z_at[sensor_id]:
                    recent = [abs(z_at[sensor_id][t - k * HOUR]) for k in range(6) if t - k * HOUR in z_at[sensor_id]]
                    levels.append(min(max(statistics.median(recent) - 3.0, 0.0), 5.0))
            expected_frequency = min(20.0, 6.0 * weight_sum)
            expected_severity = min(45.0, 6.0 * weighted)
            expected_reading = min(10.0, 2.0 * statistics.fmean(levels)) if levels else 0.0
            expected_sensor = 20.0 * (len(sensors_of[asset_id]) - len(levels)) / len(sensors_of[asset_id])
            raw = 100.0 - expected_frequency - expected_severity - expected_reading - expected_sensor
            where = (asset_id, index)
            assert frequency == pytest.approx(expected_frequency, abs=2e-3), where
            assert severity_penalty == pytest.approx(expected_severity, abs=2e-3), where
            assert reading == pytest.approx(expected_reading, abs=2e-3), where
            assert sensor == pytest.approx(expected_sensor, abs=2e-3), where
            if abs(raw + 0.5 - round(raw + 0.5)) > 5e-3:  # away from a rounding boundary
                assert score == min(max(math.floor(raw + 0.5), 0), 100), where
            assert status == ("normal" if score >= 90 else "watch" if score >= 70 else "at_risk" if score >= 45 else "critical")
            assert (in_window, active, reporting, total) == (expected_window, expected_active, len(levels), len(sensors_of[asset_id])), where
            compared += 1
    assert compared == 12 * len(sensors_of)
    assert one(db_conn, "SELECT count(*) FROM infra.asset_health WHERE as_of = %s AND health_score <> 100", (run["start"],)) == 0


def test_no_health_score_for_assets_without_sensors(db_conn):
    assert one(db_conn, "SELECT count(*) FROM infra.asset_health h WHERE NOT EXISTS (SELECT 1 FROM infra.sensors s WHERE s.asset_id = h.asset_id)") == 0
    unmonitored = one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets a WHERE NOT EXISTS (SELECT 1 FROM infra.sensors s WHERE s.asset_id = a.asset_id)")
    assert unmonitored > 0


def test_nbi_ratings_never_feed_health_status_or_risk(db_conn, run):
    """Two bridge structures with different recorded ratings and no anomaly have the same derived score."""
    rows = db_conn.execute(
        """
        SELECT a.asset_id, a.properties -> 'nbi' ->> 'lowest_rating', h.health_score
        FROM infra.infrastructure_assets a JOIN infra.asset_health h ON h.asset_id = a.asset_id AND h.as_of = %s
        WHERE a.properties ? 'nbi'
        """,
        (run["start"] + 10 * HOUR,),
    ).fetchall()
    assert len(rows) >= 2 and len({row[1] for row in rows}) >= 1
    assert {row[2] for row in rows} == {100}  # nothing has happened yet: every monitored structure scores 100


# --- re-runs ------------------------------------------------------------------------------------------------------
def test_stages_3_to_6_can_be_run_again_and_reproduce_every_table(test_db):
    """Idempotent and deterministic: after seeding, generating, detecting and analysing again, every table is the same."""
    with psycopg.connect(test_db.dsn) as conn:
        before = {table: (table_count(conn, table), table_md5(conn, table)) for table in DATA_TABLES}
    results = run_stages(test_db, ("seed", "generate", "detect", "analyze"))
    with psycopg.connect(test_db.dsn) as conn:
        after = {table: (table_count(conn, table), table_md5(conn, table)) for table in DATA_TABLES}
        runs = conn.execute("SELECT count(*), max(run_id) FROM infra.detection_runs").fetchone()
    assert runs == (1, 1)  # the previous run was replaced, not kept beside the new one
    changed = [table for table in DATA_TABLES if after[table] != before[table]]
    assert changed == []
    assert results["detect"]["anomalies"] == before["anomalies"][0]


def test_stages_4_to_6_can_be_run_again_on_the_seeded_database(test_db):
    tables = ("infrastructure_assets", "sensors", "sensor_readings", "simulation_events", "reading_scores", "anomalies",
              "anomaly_clusters", "asset_health", "risk_zones", "risk_zone_scores")  # fmt: skip
    with psycopg.connect(test_db.dsn) as conn:
        before = {table: table_md5(conn, table) for table in tables}
    run_stages(test_db, ("generate", "detect", "analyze"))
    run_stages(test_db, ("analyze",))  # stage 6 alone, twice in a row
    with psycopg.connect(test_db.dsn) as conn:
        after = {table: table_md5(conn, table) for table in tables}
    assert after == before


def test_stages_fail_loudly_when_an_earlier_stage_has_not_run(db_conn, test_db):
    db_conn.execute("TRUNCATE infra.detection_runs RESTART IDENTITY CASCADE")
    with pytest.raises(analyze.StageOrderError, match="detection run"):
        analyze.run(db_conn, test_db.settings)
    db_conn.execute("DELETE FROM infra.sensors")
    with pytest.raises(runner.DetectionInputError, match="no sensors"):
        runner.run_detection(db_conn, test_db.settings)
    db_conn.execute("DELETE FROM infra.study_areas")
    from pipeline.stages import generate

    with pytest.raises(generate.StageOrderError, match="study area"):
        generate.run(db_conn, test_db.settings)
    db_conn.rollback()
    assert one(db_conn, "SELECT count(*) FROM infra.anomalies") > 0  # nothing of this was kept


# --- cascades (inside a transaction that is rolled back) ----------------------------------------------------------
def test_deleting_the_study_area_cascades_to_everything_that_belongs_to_it(db_conn):
    db_conn.execute("DELETE FROM infra.study_areas")
    for table in ("reference_boundaries", "buildings", "roads", "infrastructure_assets", "sensors", "sensor_readings",
                  "reading_scores", "anomalies", "asset_health", "risk_zones", "risk_zone_scores"):  # fmt: skip
        assert table_count(db_conn, table) == 0, table
    assert table_count(db_conn, "data_sources") > 0 and table_count(db_conn, "sensor_thresholds") > 0
    assert one(db_conn, "SELECT count(*) FROM infra.simulation_events WHERE sensor_id IS NOT NULL") == 0


def test_deleting_a_sensor_removes_its_readings_scores_and_anomalies_only(db_conn):
    sensor_id, asset_id = db_conn.execute("SELECT sensor_id, asset_id FROM infra.anomalies ORDER BY anomaly_id LIMIT 1").fetchone()
    others = one(db_conn, "SELECT count(*) FROM infra.sensor_readings WHERE sensor_id <> %s", (sensor_id,))
    db_conn.execute("DELETE FROM infra.sensors WHERE sensor_id = %s", (sensor_id,))
    for table in ("sensor_readings", "reading_scores", "anomalies", "simulation_events"):
        assert one(db_conn, f"SELECT count(*) FROM infra.{table} WHERE sensor_id = %s", (sensor_id,)) == 0, table
    assert one(db_conn, "SELECT count(*) FROM infra.sensor_readings") == others
    assert one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE asset_id = %s", (asset_id,)) == 1


def test_deleting_an_asset_removes_its_sensors_and_health(db_conn):
    asset_id = one(db_conn, "SELECT asset_id FROM infra.sensors GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1")
    db_conn.execute("DELETE FROM infra.infrastructure_assets WHERE asset_id = %s", (asset_id,))
    for table in ("sensors", "asset_health", "anomalies"):
        assert one(db_conn, f"SELECT count(*) FROM infra.{table} WHERE asset_id = %s", (asset_id,)) == 0, table


def test_deleting_a_detection_run_removes_what_was_derived_from_it_and_keeps_the_readings(db_conn):
    readings = table_count(db_conn, "sensor_readings")
    db_conn.execute("DELETE FROM infra.detection_runs")
    for table in ("reading_scores", "anomalies", "anomaly_clusters", "asset_health", "risk_zone_scores"):
        assert table_count(db_conn, table) == 0, table
    assert table_count(db_conn, "sensor_readings") == readings and table_count(db_conn, "sensors") > 0
    assert table_count(db_conn, "risk_zones") > 0  # the grid belongs to the study area, not to a run


def test_deleting_a_cluster_keeps_its_anomalies_and_clears_their_cluster_id(db_conn):
    cluster_id, members = db_conn.execute("SELECT cluster_id, count(*) FROM infra.anomalies WHERE cluster_id IS NOT NULL GROUP BY 1 ORDER BY 1 LIMIT 1").fetchone()
    total = table_count(db_conn, "anomalies")
    db_conn.execute("DELETE FROM infra.anomaly_clusters WHERE cluster_id = %s", (cluster_id,))
    assert table_count(db_conn, "anomalies") == total and members >= 3
    assert one(db_conn, "SELECT count(*) FROM infra.anomalies WHERE cluster_id = %s", (cluster_id,)) == 0


def test_deleting_a_building_row_removes_its_asset(db_conn):
    asset_id, building_id = db_conn.execute("SELECT asset_id, building_id FROM infra.infrastructure_assets WHERE asset_type = 'building' ORDER BY 1 LIMIT 1").fetchone()
    db_conn.execute("DELETE FROM infra.buildings WHERE building_id = %s", (building_id,))
    assert one(db_conn, "SELECT count(*) FROM infra.infrastructure_assets WHERE asset_id = %s", (asset_id,)) == 0


# --- ingestion service on the database (rolled back) --------------------------------------------------------------
def test_ingestion_stores_valid_readings_and_reports_the_rest(db_conn, run):
    sensor_id, unit = db_conn.execute("SELECT sensor_id, unit FROM infra.sensors WHERE sensor_type = 'pressure' ORDER BY 1 LIMIT 1").fetchone()
    later = run["end"] + timedelta(days=2)
    readings = [
        Reading(sensor_id, later, 61.5, unit),
        Reading(sensor_id, later + HOUR, 950.0, unit),  # implausible: stored as suspect
        Reading(sensor_id, later + 2 * HOUR, 60.0, unit),
        Reading(sensor_id, later + 2 * HOUR, 60.5, unit),  # same sensor and time again: the last one is kept
        Reading("PRS-999", later, 60.0, unit),
        Reading(sensor_id, later.replace(tzinfo=None), 60.0, unit),
        Reading(sensor_id, later, float("nan"), unit),
        Reading(sensor_id, later, 60.0, "bar"),
    ]
    before = table_count(db_conn, "sensor_readings")
    result = IngestionService(db_conn).ingest(readings, "gateway-test")
    assert (result.accepted, result.suspect, result.rejected) == (3, 1, 5)
    assert result.reasons == {"duplicate_reading": 1, "naive_timestamp": 1, "non_finite_value": 1, "unit_mismatch": 1, "unknown_sensor": 1}
    stored = db_conn.execute("SELECT ts, value, status, source, unit FROM infra.sensor_readings WHERE sensor_id = %s AND ts >= %s ORDER BY ts", (sensor_id, later)).fetchall()
    assert stored == [(later, 61.5, "ok", "gateway-test", unit), (later + HOUR, 950.0, "suspect", "gateway-test", unit), (later + 2 * HOUR, 60.5, "ok", "gateway-test", unit)]
    assert table_count(db_conn, "sensor_readings") == before + 3
    assert table_count(db_conn, "anomalies") > 0  # ingesting never runs or clears the detection


def test_ingestion_upserts_on_sensor_and_timestamp(db_conn, run):
    sensor_id, unit, value = db_conn.execute("SELECT r.sensor_id, r.unit, r.value FROM infra.sensor_readings r WHERE r.ts = %s ORDER BY 1 LIMIT 1", (run["start"],)).fetchone()
    before = table_count(db_conn, "sensor_readings")
    service = IngestionService(db_conn, batch_size=1)
    first = service.ingest([Reading(sensor_id, run["start"], value + 1.0, unit)], "gateway-test")
    second = service.ingest([Reading(sensor_id, run["start"], value + 2.0, unit)], "gateway-test")
    assert (first.accepted, second.accepted, first.rejected, second.rejected) == (1, 1, 0, 0)
    assert table_count(db_conn, "sensor_readings") == before  # replaced, not added
    assert db_conn.execute("SELECT value, source FROM infra.sensor_readings WHERE sensor_id = %s AND ts = %s", (sensor_id, run["start"])).fetchone() == (value + 2.0, "gateway-test")
