"""Queries of ``/health``, ``/meta`` and ``/statistics``."""

from __future__ import annotations

from typing import Any

import psycopg

from backend.app.queries.common import (
    COORD_DECIMALS,
    DataNotReadyError,
    fetch_all,
    fetch_one,
    rounded,
    text_or_none,
)
from backend.app.schemas import SEVERITY_LEVELS
from backend.app.timeutil import iso_z
from pipeline.analysis import status
from pipeline.analysis.health import ASSET_STATUSES, FORMULA_TEXT, STATUS_BANDS, STATUS_NOT_MONITORED
from pipeline.analysis.risk_zones import RISK_LEVELS
from pipeline.config import DATA_NOTICE, SERVICE_NAME, SERVICE_VERSION, Settings
from pipeline.detection.events import ANOMALY_LABELS
from pipeline.sensors.thresholds import SENSOR_TYPE_LABELS, SENSOR_TYPES

HEALTH_NOTE = "service health; asset health scores are at /assets and /assets/{id}/health"
EVALUATION_NOTE = "Scored against injected simulated events — a self-consistency check, not field validation."
HEIGHT_SOURCES: tuple[str, ...] = ("osm_height", "lidar_3dep", "osm_levels", "estimated")
LABELS: dict[str, str] = {
    "sensor_data": "Simulated Sensor Data",
    "detection": "Prototype Anomaly Detection",
    "health": "Derived Asset Health Score",
    "buildings": (
        "3D building extrusions derived from OSM footprints. Heights: measured from USGS 3DEP lidar "
        "(2013–14) where available, otherwise OSM tags, otherwise estimated. Not detailed 3D building models."
    ),
    "water_network": "Simulated water network (not a record of real utilities)",
    "playback": "Playback replays a retrospective analysis of simulated readings.",
}

LATEST_RUN_SQL = """
SELECT run_id, finished_at, params, metrics
FROM infra.detection_runs
WHERE finished_at IS NOT NULL AND window_start IS NOT NULL AND window_end IS NOT NULL
ORDER BY run_id DESC
LIMIT 1
"""

ASSET_TOTALS_SQL = """
SELECT (SELECT count(*) FROM infra.infrastructure_assets) AS assets,
       (SELECT count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated) AS real_assets,
       (SELECT count(*) FROM infra.infrastructure_assets WHERE is_simulated) AS simulated_assets,
       (SELECT count(DISTINCT asset_id) FROM infra.sensors) AS monitored_assets,
       (SELECT count(*) FROM infra.sensors) AS sensors,
       (SELECT count(*) FROM infra.sensor_readings) AS readings,
       (SELECT count(*) FROM infra.anomalies) AS anomalies
"""

STUDY_AREA_SQL = """
SELECT slug, name, timezone, utm_srid,
       ST_XMin(geom) AS west, ST_YMin(geom) AS south, ST_XMax(geom) AS east, ST_YMax(geom) AS north
FROM infra.study_areas
ORDER BY study_area_id
LIMIT 1
"""

DATA_SOURCES_SQL = """
SELECT source_id, name, kind, provider, url, license, attribution_text, vintage, retrieved_at, notes
FROM infra.data_sources
WHERE %(source_ids)s::text[] IS NULL OR source_id = ANY(%(source_ids)s::text[])
ORDER BY source_id
"""

ANOMALIES_TO_DATE_SQL = f"""
SELECT an.severity, an.sensor_type, count(*) AS n
FROM infra.anomalies an
WHERE {status.ANOMALY_VISIBLE_SQL}
GROUP BY an.severity, an.sensor_type
"""


def _counts(conn: psycopg.Connection, query: str, keys: tuple[str, ...] = ()) -> dict[str, int]:
    """A ``key -> count`` mapping from a two-column query; ``keys`` are always present (0 when absent)."""
    counts = dict.fromkeys(keys, 0)
    for key, count in conn.execute(query).fetchall():
        counts[key] = int(count)
    return counts


def assets_by_type(conn: psycopg.Connection) -> dict[str, int]:
    """Number of assets per asset type."""
    return _counts(conn, "SELECT asset_type, count(*) FROM infra.infrastructure_assets GROUP BY asset_type")


def data_source_item(row: dict[str, Any]) -> dict[str, Any]:
    """One row of ``infra.data_sources`` in the response shape."""
    return {
        "source_id": row["source_id"],
        "name": row["name"],
        "kind": row["kind"],
        "provider": text_or_none(row["provider"]),
        "url": text_or_none(row["url"]),
        "license": text_or_none(row["license"]),
        "attribution_text": text_or_none(row["attribution_text"]),
        "vintage": text_or_none(row["vintage"]),
        "retrieved_at": iso_z(row["retrieved_at"]),
        "notes": text_or_none(row["notes"]),
    }


def data_sources(conn: psycopg.Connection, source_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """The registered data sources (all, or the given ids), ordered by id."""
    return [data_source_item(row) for row in fetch_all(conn, DATA_SOURCES_SQL, {"source_ids": source_ids})]


def service_health(conn: psycopg.Connection, axis: status.TimeAxis) -> dict[str, Any]:
    """Body of ``GET /health`` when the database answers and holds an analysed run."""
    postgis = conn.execute("SELECT postgis_lib_version()").fetchone()[0]
    totals = fetch_one(conn, ASSET_TOTALS_SQL)
    by_status = dict.fromkeys(ASSET_STATUSES, 0)
    rows = conn.execute(
        "SELECT status, count(*) FROM infra.asset_health WHERE as_of = %s GROUP BY status", (axis.end,)
    ).fetchall()
    for name, count in rows:
        by_status[name] = int(count)
    if int(totals["monitored_assets"]) > 0 and not rows:
        raise DataNotReadyError(
            "asset health has not been computed for the latest detection run; run scripts/analyze_spatial.py"
        )
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "database": "ok",
        "postgis": postgis,
        "data_window": {"start": iso_z(axis.start), "end": iso_z(axis.end)},
        "asset_health": {
            "as_of": iso_z(axis.end),
            **by_status,
            STATUS_NOT_MONITORED: int(totals["assets"]) - int(totals["monitored_assets"]),
        },
        "note": HEALTH_NOTE,
    }


def _study_area(conn: psycopg.Connection) -> dict[str, Any]:
    row = fetch_one(conn, STUDY_AREA_SQL)
    if row is None:
        raise DataNotReadyError("the database holds no study area; run scripts/seed_database.py first")
    bbox = [rounded(row[key], COORD_DECIMALS) for key in ("west", "south", "east", "north")]
    return {
        "slug": row["slug"],
        "name": row["name"],
        "bbox": bbox,
        "center": [
            rounded((row["west"] + row["east"]) / 2.0, COORD_DECIMALS),
            rounded((row["south"] + row["north"]) / 2.0, COORD_DECIMALS),
        ],
        "timezone": row["timezone"],
        "utm_srid": int(row["utm_srid"]),
    }


def _sensor_types(conn: psycopg.Connection) -> dict[str, Any]:
    rows = fetch_all(
        conn,
        """
        SELECT sensor_type, placement, unit, warn_low, warn_high, crit_low, crit_high, description
        FROM infra.sensor_thresholds
        ORDER BY sensor_type, placement
        """,
    )
    types: dict[str, Any] = {}
    for row in rows:
        entry = types.setdefault(
            row["sensor_type"],
            {
                "unit": row["unit"],
                "label": SENSOR_TYPE_LABELS.get(row["sensor_type"], row["sensor_type"].replace("_", " ").title()),
                "placements": {},
            },
        )
        entry["placements"][row["placement"]] = {
            "warn_low": row["warn_low"],
            "warn_high": row["warn_high"],
            "crit_low": row["crit_low"],
            "crit_high": row["crit_high"],
            "description": text_or_none(row["description"]),
        }
    return types


def _row_counts(conn: psycopg.Connection) -> dict[str, Any]:
    totals = fetch_one(conn, ASSET_TOTALS_SQL)
    return {
        **{key: int(value) for key, value in totals.items()},
        "assets_by_type": assets_by_type(conn),
        "sensors_by_type": _counts(
            conn, "SELECT sensor_type, count(*) FROM infra.sensors GROUP BY sensor_type", SENSOR_TYPES
        ),
        "building_height_sources": _counts(
            conn, "SELECT height_source, count(*) FROM infra.buildings GROUP BY height_source", HEIGHT_SOURCES
        ),
    }


def meta(conn: psycopg.Connection, settings: Settings, axis: status.TimeAxis) -> dict[str, Any]:
    """Body of ``GET /meta`` (build contract 10.3)."""
    run = fetch_one(conn, LATEST_RUN_SQL)
    if run is None:
        raise DataNotReadyError("no finished detection run in the database; run scripts/detect_anomalies.py first")
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "data_notice": DATA_NOTICE,
        "study_area": _study_area(conn),
        "time": {
            "start": iso_z(axis.start),
            "end": iso_z(axis.end),
            "step_minutes": axis.step_seconds / 60.0,
            "count": axis.count,
        },
        "labels": dict(LABELS),
        "sensor_types": _sensor_types(conn),
        "anomaly_types": dict(ANOMALY_LABELS),
        "severity_levels": list(SEVERITY_LEVELS),
        "health": {
            "formula": FORMULA_TEXT,
            "window_days": settings.HEALTH_WINDOW_DAYS,
            "half_life_hours": settings.HEALTH_HALF_LIFE_HOURS,
            "at_risk_below": status.AT_RISK_BELOW,
            "bands": dict(STATUS_BANDS),
        },
        "risk": {
            "hex_edge_m": settings.RISK_HEX_EDGE_M,
            "bandwidth_m": settings.RISK_BANDWIDTH_M,
            "half_life_hours": settings.RISK_HALF_LIFE_HOURS,
            "reference": settings.RISK_REFERENCE,
            "levels": dict(reversed(RISK_LEVELS)),
        },
        "spatial": {
            "proximity_radius_m": settings.PROXIMITY_RADIUS_M,
            "cluster_eps_m": settings.CLUSTER_EPS_M,
            "cluster_eps_hours": settings.CLUSTER_EPS_HOURS,
            "cluster_min_points": settings.CLUSTER_MIN_POINTS,
            "cluster_min_sensors": settings.CLUSTER_MIN_SENSORS,
        },
        "counts": _row_counts(conn),
        "data_sources": data_sources(conn),
        "detection_run": {
            "run_id": int(run["run_id"]),
            "finished_at": iso_z(run["finished_at"]),
            "params": run["params"] or {},
            "metrics": run["metrics"] or {},
            "evaluation_note": EVALUATION_NOTE,
        },
    }


def statistics(conn: psycopg.Connection, as_of: Any) -> dict[str, Any]:
    """Body of ``GET /statistics``: the KPIs of ``status.kpis_at`` plus breakdowns of the anomalies to date."""
    kpis = status.kpis_at(conn, as_of)
    by_severity = dict.fromkeys(SEVERITY_LEVELS, 0)
    by_sensor_type = dict.fromkeys(SENSOR_TYPES, 0)
    for severity, sensor_type, count in conn.execute(ANOMALIES_TO_DATE_SQL, {"as_of": as_of}).fetchall():
        by_severity[severity] = by_severity.get(severity, 0) + int(count)
        by_sensor_type[sensor_type] = by_sensor_type.get(sensor_type, 0) + int(count)
    return {
        "as_of": iso_z(as_of),
        "data_notice": DATA_NOTICE,
        **kpis,
        "anomalies_to_date": sum(by_severity.values()),
        "anomalies_by_severity": by_severity,
        "anomalies_by_sensor_type": by_sensor_type,
        "assets_by_type": assets_by_type(conn),
    }
