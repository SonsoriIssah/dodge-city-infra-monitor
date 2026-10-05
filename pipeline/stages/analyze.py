"""Stage 6 - spatial analysis and the Derived Asset Health Score (one transaction, idempotent).

Works on the anomalies of the latest detection run: spatio-temporal co-occurrence clusters, the hexagonal
risk grid with an hourly risk score per cell, and the health score of every monitored asset at every hour
of the analysed window. Writes ``infra.anomaly_clusters`` (and ``anomalies.cluster_id``),
``infra.risk_zones``, ``infra.risk_zone_scores`` and ``infra.asset_health``.

Cleared first: those four tables (with DELETE; the anomalies keep their rows and lose their cluster id).
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from pipeline.analysis import clustering, health, risk_zones, status
from pipeline.config import Settings, get_settings
from pipeline.db.connection import connect
from pipeline.logging_utils import setup_logging
from pipeline.stages import EXIT_DATABASE_UNAVAILABLE, EXIT_FAILED, EXIT_OK

logger = logging.getLogger(__name__)

REQUIRED_TABLES = ("study_areas", "sensors", "sensor_readings", "detection_runs", "reading_scores", "anomalies",
                   "anomaly_clusters", "risk_zones", "risk_zone_scores", "asset_health")  # fmt: skip
# Order matters: scores before the cells they refer to.
CLEARED_TABLES = ("anomaly_clusters", "risk_zone_scores", "risk_zones", "asset_health")
ANALYZED_TABLES = ("anomalies", "anomaly_clusters", "risk_zones", "risk_zone_scores", "asset_health")


class StageOrderError(RuntimeError):
    """An earlier pipeline stage has not been run."""


def check_detected(conn: psycopg.Connection) -> status.TimeAxis:
    """Fail loudly when the schema or a finished detection run is missing; returns the time axis of the run."""
    missing = [
        table
        for table in REQUIRED_TABLES
        if conn.execute("SELECT to_regclass(%s)", (f"infra.{table}",)).fetchone()[0] is None
    ]
    if missing:
        raise StageOrderError(
            f"the database has no schema yet (missing infra.{missing[0]}); run scripts/seed_database.py first"
        )
    try:
        axis = status.load_time_axis(conn)
    except status.NoDetectionRunError as exc:
        raise StageOrderError(f"{exc} (stage 5)") from exc
    scored = conn.execute("SELECT EXISTS (SELECT 1 FROM infra.reading_scores WHERE run_id = %s)", (axis.run_id,))
    if scored.fetchone()[0] is not True:
        raise StageOrderError("the latest detection run has no reading scores; run scripts/detect_anomalies.py again")
    return axis


def clear_analysis(conn: psycopg.Connection) -> None:
    """Remove what this stage produced (build contract section 4.1, row 6)."""
    for table in CLEARED_TABLES:
        conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier("infra", table)))


def run(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """Run the stage through ``conn`` (the caller owns the transaction); returns a summary of what was written."""
    started = time.perf_counter()
    axis = check_detected(conn)
    clear_analysis(conn)
    run_id = int(axis.run_id)
    logger.info(
        "analysing run %d: %s .. %s (%d steps)", run_id, axis.start.isoformat(), axis.end.isoformat(), axis.count
    )

    clusters = clustering.run_clustering(conn, settings, run_id)
    anomalies = status.load_anomaly_intervals(conn)
    zones = risk_zones.run_risk_zones(conn, settings, axis, anomalies, run_id)
    matrix = status.load_sensor_matrix(conn, axis, anomalies)
    asset_health = health.run_health(conn, settings, matrix, anomalies, run_id)
    for table in ANALYZED_TABLES:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier("infra", table)))

    kpis = status.kpis_at(conn, axis.end)
    logger.info(
        "at %s: %d of %d sensors reporting (%d offline, %d warning), %d active anomalies (%d critical), "
        "%d assets at risk",
        axis.end.isoformat(), kpis["active_sensors"], kpis["total_sensors"], kpis["offline_sensors"],
        kpis["warning_sensors"], kpis["active_anomalies"], kpis["critical_alerts"], kpis["assets_at_risk"],
    )  # fmt: skip
    seconds = round(time.perf_counter() - started, 2)
    logger.info("stage 6 finished in %.1f s", seconds)
    return {
        "run_id": run_id,
        "anomalies": len(anomalies),
        "clusters": clusters,
        "risk_zones": zones,
        "asset_health": asset_health,
        "kpis_at_end": kpis,
        "seconds": seconds,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/analyze_spatial.py``."""
    parser = argparse.ArgumentParser(prog="analyze_spatial", description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    logger.info(
        "spatial analysis (%s; cluster eps %.0f m / %.0f h, hex edge %.0f m, health window %d d)",
        settings.dsn_summary(), settings.CLUSTER_EPS_M, settings.CLUSTER_EPS_HOURS, settings.RISK_HEX_EDGE_M,
        settings.HEALTH_WINDOW_DAYS,
    )  # fmt: skip
    try:
        with connect(settings) as conn, conn.transaction():
            run(conn, settings)
    except psycopg.OperationalError as exc:
        logger.error("database unavailable: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return EXIT_DATABASE_UNAVAILABLE
    except (StageOrderError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    return EXIT_OK
