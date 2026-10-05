"""Facts typed from the build contract, and small helpers shared by the test modules.

Everything in the first half of this file is copied from the contract text (not imported from the code under
test), so a test that compares the code with it checks the contract, not the implementation against itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg import sql

# --- contract section 4.2: every setting and its default --------------------------------------------------------
SPEC_DEFAULTS: dict[str, Any] = {
    "DATABASE_URL": "",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": 5433,
    "POSTGRES_HOST_PORT": 5433,
    "POSTGRES_DB": "infra",
    "POSTGRES_USER": "infra",
    "POSTGRES_PASSWORD": "",
    "TEST_DATABASE_URL": "",
    "STUDY_AREA_SLUG": "dodge-city-downtown",
    "STUDY_AREA_NAME": "Downtown Dodge City, Kansas",
    "STUDY_AREA_BBOX": "37.745,-100.030,37.762,-100.005",
    "STUDY_AREA_UTM_SRID": 32614,
    "TIMEZONE": "America/Chicago",
    "SENSOR_SOURCE": "simulated",
    "SIM_SEED": 42,
    "SIM_START": datetime(2026, 9, 1, 0, 0, tzinfo=timezone(timedelta(hours=-5))),  # 2026-09-01T00:00:00-05:00
    "SIM_DAYS": 30,
    "SIM_STEP_MINUTES": 60,
    "SIM_ANOMALY_EVENTS": 40,
    "SIM_DROPOUT_RATE": 0.12,
    "SIM_WATER_MAINS": 28,
    "DETECT_Z_STRONG": 6.0,
    "DETECT_Z_MIN": 3.0,
    "DETECT_ROLLING_HOURS": 6,
    "DETECT_IFOREST_THRESHOLD": 0.62,
    "DETECT_MERGE_GAP_HOURS": 2,
    "PROXIMITY_RADIUS_M": 100,
    "CLUSTER_EPS_M": 200,
    "CLUSTER_EPS_HOURS": 48,
    "CLUSTER_MIN_POINTS": 3,
    "CLUSTER_MIN_SENSORS": 3,
    "RISK_HEX_EDGE_M": 150,
    "RISK_BANDWIDTH_M": 250,
    "RISK_HALF_LIFE_HOURS": 72,
    "RISK_REFERENCE": 14.0,
    "HEALTH_WINDOW_DAYS": 7,
    "HEALTH_HALF_LIFE_HOURS": 48,
    "API_HOST": "0.0.0.0",
    "API_PORT": 8000,
    "CORS_ORIGINS": "*",
    "INGEST_API_KEY": "",
    "SERVE_DASHBOARD": True,
    "AUTO_SEED": True,
    "BASEMAP_STYLE_URL": "https://tiles.openfreemap.org/styles/dark",
    "HTTP_SOURCE_URL": "",
}

# --- contract sections 2, 7, 10: literal strings ------------------------------------------------------------------
DATA_NOTICE = "Simulated sensor data and prototype anomaly detection. Not a record of real infrastructure condition."
SERVICE_NAME = "dodge-city-infra-monitor"
LABELS = {
    "sensor_data": "Simulated Sensor Data",
    "detection": "Prototype Anomaly Detection",
    "health": "Derived Asset Health Score",
    "buildings": (
        "3D building extrusions derived from OSM footprints. Heights: measured from USGS 3DEP lidar (2013–14) "
        "where available, otherwise OSM tags, otherwise estimated. Not detailed 3D building models."
    ),
    "water_network": "Simulated water network (not a record of real utilities)",
    "playback": "Playback replays a retrospective analysis of simulated readings.",
}
EVALUATION_NOTE = "Scored against injected simulated events — a self-consistency check, not field validation."
HEALTH_NOTE = "service health; asset health scores are at /assets and /assets/{id}/health"
INGEST_DISABLED_DETAIL = "ingestion endpoint is disabled (INGEST_API_KEY not set)"
UNITS = {"temperature": "°C", "vibration": "mm/s", "moisture": "%", "pressure": "psi"}
SENSOR_ID_PREFIX = {"temperature": "TMP", "vibration": "VIB", "moisture": "MST", "pressure": "PRS"}
SENSOR_TYPES = ("temperature", "vibration", "moisture", "pressure")
SEVERITIES = ("low", "medium", "high", "critical")
ASSET_TYPES = ("building", "road", "bridge", "rail", "power", "street_light", "water_main")
ASSET_ID_PATTERN = {
    "building": r"BLD-\d{4}",
    "road": r"RD-\d{4}",
    "bridge": r"BRG-\d{3}",
    "rail": r"RAIL-\d{3}",
    "power": r"PWR-\d{3}",
    "street_light": r"SL-\d{3}",
    "water_main": r"WM-\d{3}",
}
ASSET_CATEGORY = {
    "building": "Buildings",
    "road": "Transportation",
    "bridge": "Transportation",
    "rail": "Transportation",
    "power": "Utilities",
    "street_light": "Utilities",
    "water_main": "Simulated network",
}
# (sensor_type, placement) -> (warn_low, warn_high, crit_low, crit_high), contract section 7
THRESHOLDS: dict[tuple[str, str], tuple[float | None, float | None, float | None, float | None]] = {
    ("vibration", "bridge_deck"): (None, 5.0, None, 10.0),
    ("vibration", "building_structure"): (None, 1.0, None, 3.0),
    ("vibration", "road_pavement"): (None, 2.5, None, 5.0),
    ("moisture", "road_subgrade"): (None, 35.0, None, 42.0),
    ("moisture", "foundation_perimeter"): (None, 35.0, None, 42.0),
    ("moisture", "abutment_backfill"): (None, 35.0, None, 42.0),
    ("temperature", "bridge_deck"): (None, 50.0, None, 58.0),
    ("temperature", "road_surface"): (None, 58.0, None, 65.0),
    ("temperature", "building_envelope"): (None, 40.0, None, 45.0),
    ("temperature", "equipment"): (None, 65.0, None, 75.0),
    ("pressure", "water_main"): (40.0, 90.0, 20.0, 110.0),
}
# Event mix for SIM_ANOMALY_EVENTS = 40, contract section 7
EVENT_MIX_40 = {
    "vibration_spike": 6,
    "sustained_high_vibration": 6,
    "moisture_increase": 7,
    "pressure_drop": 5,
    "pressure_spike": 4,
    "pressure_decline": 3,
    "temperature_spike": 5,
    "temperature_drift": 4,
}
ANOMALY_LABELS = {
    "vibration_spike": "Short elevated vibration",
    "sustained_high_vibration": "Sustained high vibration",
    "moisture_increase": "Unusual moisture increase",
    "pressure_drop": "Pressure drop",
    "pressure_spike": "Short pressure excursion",
    "pressure_decline": "Gradual pressure decline",
    "temperature_spike": "Abnormal temperature rise",
    "temperature_drift": "Temperature drift",
}
SEVERITY_WEIGHT = {"low": 1.0, "medium": 2.0, "high": 4.0, "critical": 7.0}
DETECTOR_ORDER = ("threshold", "robust_zscore", "rolling_median", "isolation_forest")
METRIC_KEYS = {
    "injected_events",
    "detected_events",
    "event_recall",
    "anomalies",
    "true_anomalies",
    "anomaly_precision",
    "false_anomalies",
    "false_anomalies_during_benign_events",
    "split_events",
    "detection_delay_hours",
    "severity_counts",
}

# --- contract section 10.2: key sets of the response items --------------------------------------------------------
SENSOR_ITEM_KEYS = {
    "sensor_id", "asset_id", "asset_name", "asset_type", "sensor_type", "placement", "unit", "description",
    "is_simulated", "source", "lon", "lat", "status", "latest", "anomaly_count",
}  # fmt: skip
SENSOR_LATEST_KEYS = {"ts", "value", "status", "expected", "robust_z"}
ANOMALY_ITEM_KEYS = {
    "anomaly_id", "sensor_id", "asset_id", "asset_name", "asset_type", "sensor_type", "placement", "anomaly_type",
    "anomaly_label", "started_at", "ended_at", "peak_at", "duration_hours", "observed_value", "expected_value",
    "unit", "robust_z", "anomaly_score", "score_components", "severity", "detection_method", "explanation",
    "status", "is_simulated", "cluster_id", "lon", "lat", "nearby_asset_count",
}  # fmt: skip
NEARBY_ASSET_KEYS = {"asset_id", "name", "asset_type", "distance_m"}
ASSET_COMMON_KEYS = {
    "asset_id", "asset_type", "category", "name", "is_simulated", "source_id", "monitored", "sensor_count",
    "sensor_types", "anomaly_count", "health_score", "status", "centroid",
}  # fmt: skip
ASSET_TYPE_KEYS = {
    "building": {"height_m", "height_source", "building_type", "levels", "footprint_m2"},
    "road": {"highway_class", "surface", "lanes", "length_m"},
    "bridge": {"structure_kind", "length_m", "nbi"},
    "water_main": {"host_road_id"},
    "rail": set(),
    "power": set(),
    "street_light": set(),
}
STATISTICS_KEYS = {
    "as_of", "data_notice", "total_assets", "real_assets", "simulated_assets", "monitored_assets", "total_sensors",
    "active_sensors", "offline_sensors", "warning_sensors", "active_anomalies", "critical_alerts", "assets_at_risk",
    "anomalies_to_date", "anomalies_by_severity", "anomalies_by_sensor_type", "assets_by_type",
}  # fmt: skip
HEALTH_SERVICE_KEYS = {"status", "service", "version", "database", "postgis", "data_window", "asset_health", "note"}
ASSET_HEALTH_SERIES_KEYS = {
    "asset_id", "start", "step_minutes", "count", "health_score", "status", "frequency_penalty", "severity_penalty",
    "reading_penalty", "sensor_penalty", "active_anomalies", "sensors_reporting", "sensors_total",
}  # fmt: skip
READINGS_RECORDS_KEYS = {
    "sensor_id", "sensor_type", "unit", "placement", "is_simulated", "source", "data_notice", "start", "end",
    "count", "readings",
}  # fmt: skip
READING_RECORD_KEYS = {"ts", "value", "status", "expected", "expected_low", "expected_high", "robust_z", "flagged"}
READINGS_COLUMNS_KEYS = {
    "sensor_id", "sensor_type", "unit", "placement", "is_simulated", "source", "thresholds", "start",
    "step_minutes", "count", "value", "expected", "expected_low", "expected_high", "robust_z", "flagged",
}  # fmt: skip
SIMULATION_EVENT_KEYS = {
    "event_id", "sensor_id", "asset_id", "sensor_type", "event_type", "is_anomaly", "started_at", "ended_at",
    "magnitude", "description",
}  # fmt: skip
CLUSTER_KEYS = {
    "cluster_id", "n_anomalies", "n_sensors", "n_assets", "sensor_types", "max_severity", "first_started_at",
    "last_ended_at", "anomaly_ids",
}  # fmt: skip
RISK_ZONE_KEYS = {"cell_id", "risk_score", "risk_level", "anomaly_count"}
DENSITY_KEYS = {"cell_id", "anomaly_count", "weighted_severity"}
PLAYBACK_KEYS = {"data_notice", "run_id", "timestamps", "sensors", "assets", "zones", "stats"}
PLAYBACK_STAT_KEYS = {
    "active_sensors", "offline_sensors", "warning_sensors", "active_anomalies", "critical_alerts", "assets_at_risk",
}  # fmt: skip
META_KEYS = {
    "service", "version", "data_notice", "study_area", "time", "labels", "sensor_types", "anomaly_types",
    "severity_levels", "health", "risk", "counts", "data_sources", "detection_run",
}  # fmt: skip
DATA_SOURCE_KEYS = {
    "source_id", "name", "kind", "provider", "url", "license", "attribution_text", "vintage", "retrieved_at", "notes",
}  # fmt: skip
# contract section 11: files of the static snapshot (plus readings/<sensor>.json and health/<asset>.json)
SNAPSHOT_TOP_LEVEL = {
    "meta.json", "assets.geojson", "roads.geojson", "study-area.geojson", "city-boundary.geojson", "sensors.json",
    "anomalies.json", "clusters.geojson", "risk-zones.geojson", "simulation-events.json", "playback.json",
    "manifest.json",
}  # fmt: skip
# contract section 10.5: names the dashboard folder must never use at its top level
RESERVED_DASHBOARD_NAMES = {
    "assets", "sensors", "sensor-readings", "anomalies", "statistics", "health", "meta", "spatial", "layers",
    "playback", "simulation-events", "ingest", "docs", "redoc", "openapi.json",
}  # fmt: skip

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LOOKS_LIKE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
BANNED_WORDS = re.compile(r"\blive\b|real[\s-]?time", re.IGNORECASE)


# --- helpers ------------------------------------------------------------------------------------------------------
def parse_z(text: str) -> datetime:
    """Parse a ``YYYY-MM-DDTHH:MM:SSZ`` timestamp into an aware UTC datetime."""
    assert ISO_Z.match(text), f"not an ISO-Z timestamp: {text!r}"
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def iso_z(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (the test suite's own formatter)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def walk(node: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """Every leaf of a JSON value as ``(path, value)``; dictionary keys are yielded as leaves too."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}<key>", key
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")
    else:
        yield path, node


def strings(node: Any) -> Iterator[tuple[str, str]]:
    """Every string (values and keys) of a JSON value with its path."""
    for path, value in walk(node):
        if isinstance(value, str):
            yield path, value


def decimals(value: float) -> int:
    """Number of decimals a JSON number was written with (0 for integers)."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        return len(f"{float(value):.12f}".rstrip("0").split(".")[1])
    return 0 if text.endswith(".0") else len(text.split(".")[1])


# --- database fingerprints ----------------------------------------------------------------------------------------
# Columns that legitimately differ between two runs on identical inputs (wall-clock stamps).
VOLATILE_COLUMNS = {"ingested_at", "created_at", "applied_at", "finished_at"}
VOLATILE_BY_TABLE = {"detection_runs": {"started_at"}}
DATA_TABLES = (
    "data_sources", "study_areas", "reference_boundaries", "buildings", "roads", "infrastructure_assets",
    "sensor_thresholds", "sensors", "sensor_readings", "simulation_events", "detection_runs", "reading_scores",
    "anomaly_clusters", "anomalies", "asset_health", "risk_zones", "risk_zone_scores",
)  # fmt: skip
READINGS_MD5_SQL = """
SELECT md5(string_agg(
           sensor_id || '|' || to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') || '|' || value::text,
           ',' ORDER BY sensor_id, ts))
FROM infra.sensor_readings
"""


def table_md5(conn: psycopg.Connection, table: str) -> str | None:
    """md5 over every row of ``infra.<table>`` (all columns but the wall-clock ones), order independent."""
    skip = VOLATILE_COLUMNS | VOLATILE_BY_TABLE.get(table, set())
    columns = [
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'infra' AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
        if row[0] not in skip
    ]
    query = sql.SQL("SELECT md5(string_agg(x::text, ',' ORDER BY x::text)) FROM (SELECT {} FROM {}) x").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in columns), sql.Identifier("infra", table)
    )
    return conn.execute(query).fetchone()[0]


def table_count(conn: psycopg.Connection, table: str) -> int:
    """Row count of ``infra.<table>``."""
    return conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("infra", table))).fetchone()[0]


def database_fingerprint(conn: psycopg.Connection) -> dict[str, Any]:
    """Row count of every table, md5 of the readings and of the anomalies, and the id of the latest run.

    A database without the ``infra`` schema has the fingerprint ``{"schema": None}``.
    """
    if conn.execute("SELECT to_regclass('infra.sensor_readings')").fetchone()[0] is None:
        return {"schema": None}
    latest = conn.execute(
        "SELECT run_id, finished_at FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return {
        "counts": {table: table_count(conn, table) for table in DATA_TABLES},
        "readings_md5": conn.execute(READINGS_MD5_SQL).fetchone()[0],
        "anomalies_md5": table_md5(conn, "anomalies"),
        "latest_run": None if latest is None else (latest[0], latest[1].isoformat() if latest[1] else None),
    }


# --- one example request per GET endpoint of contract section 10.2 --------------------------------------------------
# (route template, example URL). The ids exist in the default dataset (seed 42); a test asserts that this list
# covers every GET route of the OpenAPI document, so a new endpoint cannot be added without being exercised.
MID_WINDOW = "2026-09-20T12:00:00Z"
ENDPOINT_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("/health", "/health"),
    ("/meta", "/meta"),
    ("/statistics", "/statistics"),
    ("/statistics", f"/statistics?as_of={MID_WINDOW}"),
    ("/assets", "/assets"),
    ("/assets", "/assets?asset_type=bridge&monitored=true&bbox=-100.03,37.745,-100.005,37.762&limit=5"),
    ("/assets/{asset_id}", "/assets/BRG-001"),
    ("/assets/{asset_id}", f"/assets/BRG-001?as_of={MID_WINDOW}"),
    ("/assets/{asset_id}/health", "/assets/BRG-001/health"),
    ("/sensors", "/sensors"),
    ("/sensors", f"/sensors?sensor_type=vibration,pressure&as_of={MID_WINDOW}"),
    ("/sensors/{sensor_id}", "/sensors/VIB-001"),
    ("/sensor-readings", "/sensor-readings?sensor_id=VIB-001&start=2026-09-29T00:00:00Z"),
    ("/sensor-readings", "/sensor-readings?sensor_id=VIB-001&shape=columns"),
    ("/anomalies", "/anomalies?include=nearby_assets&limit=1000"),
    ("/anomalies", f"/anomalies?severity=critical,high&status=resolved&as_of={MID_WINDOW}&sort=severity"),
    ("/anomalies/{anomaly_id}", "/anomalies/ANM-0001"),
    ("/simulation-events", "/simulation-events"),
    ("/spatial/assets-within", "/spatial/assets-within?lon=-100.0195&lat=37.7474&radius_m=120"),
    ("/spatial/nearest-asset", "/spatial/nearest-asset?lon=-100.0195&lat=37.7474&asset_type=building"),
    ("/spatial/sensors-in-asset-area", "/spatial/sensors-in-asset-area?asset_id=BRG-001&buffer_m=50"),
    ("/spatial/anomaly-density", "/spatial/anomaly-density?start=2026-09-10T00:00:00Z&end=2026-09-25T00:00:00Z"),
    ("/spatial/risk-zones", "/spatial/risk-zones"),
    ("/spatial/risk-zones", f"/spatial/risk-zones?as_of={MID_WINDOW}"),
    ("/spatial/clusters", "/spatial/clusters"),
    ("/layers/roads", "/layers/roads"),
    ("/layers/study-area", "/layers/study-area"),
    ("/layers/city-boundary", "/layers/city-boundary"),
    ("/playback", "/playback"),
)
# Paths of the brief (R13) and of the contract: every one of them is served at the root.
CONTRACT_GET_PATHS = frozenset(template for template, _ in ENDPOINT_EXAMPLES)
CONTRACT_POST_PATHS = frozenset({"/ingest/readings"})


# --- a database that cannot be reached ----------------------------------------------------------------------------
DEAD_DATABASE_USER = "dcim_unreachable_user"
DEAD_DATABASE_NAME = "dcim_unreachable_db"
DEAD_DATABASE_PASSWORD = "pw-7f3c9e41-must-never-be-shown"


def closed_port() -> int:
    """A TCP port on the loopback interface that nothing listens on (bound once, then released)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def unreachable_dsn() -> str:
    """Connection string of a server that does not exist: a closed local port, a recognisable password."""
    return f"postgresql://{DEAD_DATABASE_USER}:{DEAD_DATABASE_PASSWORD}@127.0.0.1:{closed_port()}/{DEAD_DATABASE_NAME}"
