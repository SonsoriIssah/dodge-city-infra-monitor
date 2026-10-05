"""Stage 4 - place the sensors and ingest their readings (one transaction, idempotent).

Reads the real assets from the database, applies the placement rules (which also create the simulated water
mains that host the pressure sensors), reads the configured sensor source over the simulation window through
the ingestion service, and stores the simulator's ground truth in ``infra.simulation_events``.

Cleared first: the detection runs (with everything derived from them), the sensors (with their readings),
the simulation events and the simulated water mains. With ``SENSOR_SOURCE`` other than ``simulated`` the
sensors are still placed, readings come from that source and no simulation events are written.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from pipeline.config import Settings, get_settings
from pipeline.db import loaders
from pipeline.db.connection import connect
from pipeline.logging_utils import setup_logging
from pipeline.models import InjectedEvent
from pipeline.sensors import placement
from pipeline.sensors.ingestion import IngestionService
from pipeline.sensors.simulator import SimulationResult
from pipeline.sensors.sources import SensorSourceError, SimulatedSensorSource, get_sensor_source
from pipeline.stages import EXIT_DATABASE_UNAVAILABLE, EXIT_FAILED, EXIT_OK

logger = logging.getLogger(__name__)

REQUIRED_TABLES = ("study_areas", "infrastructure_assets", "sensor_thresholds", "sensors", "sensor_readings",
                   "simulation_events", "detection_runs", "data_sources")  # fmt: skip
ANALYZED_TABLES = ("infrastructure_assets", "sensors", "sensor_readings", "simulation_events")
MAX_LOGGED_VIOLATIONS = 10


class StageOrderError(RuntimeError):
    """An earlier pipeline stage has not been run."""


def check_seeded(conn: psycopg.Connection) -> None:
    """Fail loudly when stage 3 (seed_database) has not run on this database."""
    hint = "run scripts/seed_database.py (stage 3) first"
    missing = [
        table
        for table in REQUIRED_TABLES
        if conn.execute("SELECT to_regclass(%s)", (f"infra.{table}",)).fetchone()[0] is None
    ]
    if missing:
        raise StageOrderError(f"the database has no schema yet (missing infra.{missing[0]}); {hint}")
    if conn.execute("SELECT count(*) FROM infra.study_areas").fetchone()[0] != 1:
        raise StageOrderError(f"no study area in the database; {hint}")
    if conn.execute("SELECT count(*) FROM infra.infrastructure_assets WHERE NOT is_simulated").fetchone()[0] == 0:
        raise StageOrderError(f"no infrastructure assets in the database; {hint}")
    if conn.execute("SELECT count(*) FROM infra.sensor_thresholds").fetchone()[0] == 0:
        raise StageOrderError(f"no sensor thresholds in the database; {hint}")
    registered = conn.execute(
        "SELECT 1 FROM infra.data_sources WHERE source_id = %s", (placement.SIMULATOR_SOURCE_ID,)
    ).fetchone()
    if registered is None:
        raise StageOrderError(f"data source '{placement.SIMULATOR_SOURCE_ID}' is not registered; {hint}")


def clear_generated(conn: psycopg.Connection) -> None:
    """Remove what this stage (and the later stages) produced: build contract section 4.1, row 4."""
    conn.execute("TRUNCATE infra.detection_runs RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM infra.sensors")
    conn.execute("DELETE FROM infra.simulation_events")
    conn.execute("DELETE FROM infra.infrastructure_assets WHERE asset_type = 'water_main'")
    loaders.restart_sequences(conn, (("simulation_events", "event_id"),))


def write_simulation_events(conn: psycopg.Connection, events: Sequence[InjectedEvent]) -> int:
    """Store the simulator's ground truth (injected abnormal events and benign regional events)."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO infra.simulation_events
                (sensor_id, asset_id, sensor_type, event_type, is_anomaly, started_at, ended_at, magnitude,
                 description)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    e.sensor_id, e.asset_id, e.sensor_type, e.event_type, e.is_anomaly, e.started_at, e.ended_at,
                    e.magnitude, e.description,
                )
                for e in events
            ],
        )  # fmt: skip
    return len(events)


def _log_placement(plan: placement.PlacementPlan) -> None:
    """Report what the placement rules produced, per sensor type and per placement class."""
    logger.info(
        "placement: %d sensors (%s) on %d assets; %d simulated water mains; showcase assets: %s",
        len(plan.sensors), ", ".join(f"{name} {count}" for name, count in plan.sensors_by_type().items()),
        len(plan.monitored_asset_ids()), len(plan.water_mains), ", ".join(plan.showcase_asset_ids) or "none",
    )  # fmt: skip
    for (sensor_type, where), count in plan.sensors_by_class().items():
        logger.info(
            "  %-11s %-20s %3d sensors on %3d assets", sensor_type, where, count, len(plan.hosts[(sensor_type, where)])
        )


def store_ground_truth(conn: psycopg.Connection, simulation: SimulationResult) -> dict[str, Any]:
    """Write the simulator's ground truth to ``infra.simulation_events`` and report it; returns summary fields."""
    events = simulation.ground_truth()
    written = write_simulation_events(conn, events)
    kinds = Counter(event.event_type for event in events if event.is_anomaly)
    benign = Counter(event.event_type for event in events if not event.is_anomaly)
    logger.info(
        "ground truth: %d injected abnormal events (%s); %d benign regional events (%s)",
        sum(kinds.values()), ", ".join(f"{name} {count}" for name, count in sorted(kinds.items())),
        sum(benign.values()), ", ".join(f"{name} {count}" for name, count in sorted(benign.items())),
    )  # fmt: skip
    ongoing = simulation.ongoing_at_end()
    for detail in ongoing:
        logger.info(
            "  ongoing at the last timestamp: %s on %s (%s)", detail.event.event_type, detail.event.sensor_id,
            detail.event.asset_id,
        )  # fmt: skip
    if simulation.colocated_group:
        logger.info("  co-located group: %s", ", ".join(simulation.colocated_group))
    offline = simulation.offline_at_end()
    logger.info("  no reading at the last timestamp: %s", ", ".join(offline) or "none")
    violations = simulation.sigma_rule_violations()
    for problem in violations[:MAX_LOGGED_VIOLATIONS]:
        logger.warning("  benign / abnormal sigma rule not met: %s", problem)
    return {
        "simulation_events": written,
        "ongoing_at_end": [detail.event.sensor_id for detail in ongoing],
        "offline_at_end": offline,
        "sigma_rule_violations": len(violations),
    }


def run(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """Run the stage through ``conn`` (the caller owns the transaction); returns a summary of what was written."""
    started = time.perf_counter()
    check_seeded(conn)
    clear_generated(conn)

    assets = placement.load_assets(conn)
    plan = placement.plan_placement(assets, settings)
    if not plan.sensors:
        raise RuntimeError("the placement rules produced no sensor - the asset registry has no suitable host")
    placement.write_placement(conn, plan)
    _log_placement(plan)

    source = get_sensor_source(settings, plan.sensors)
    window = (settings.sim_start_utc, settings.sim_end_utc)
    logger.info("reading source '%s' for %s .. %s", source.name, window[0].isoformat(), window[1].isoformat())
    result = IngestionService(conn).ingest(source.read(*window), source.name)
    if result.accepted == 0:
        raise RuntimeError(
            f"sensor source '{source.name}' delivered no usable reading for the window ({result.reasons})"
        )
    logger.info(
        "ingested %d readings from '%s' (%d suspect, %d rejected%s)",
        result.accepted, source.name, result.suspect, result.rejected,
        f": {result.reasons}" if result.reasons else "",
    )  # fmt: skip

    summary: dict[str, Any] = {
        "source": source.name,
        "sensors": len(plan.sensors),
        "sensors_by_type": plan.sensors_by_type(),
        "water_mains": len(plan.water_mains),
        "monitored_assets": len(plan.monitored_asset_ids()),
        "showcase_assets": plan.showcase_asset_ids,
        "readings": result.accepted,
        "rejected": result.rejected,
        "suspect": result.suspect,
        "simulation_events": 0,
    }
    if isinstance(source, SimulatedSensorSource):
        summary.update(store_ground_truth(conn, source.result))
    else:
        logger.info("source '%s' is not the simulator: no simulation events are written", source.name)

    for table in ANALYZED_TABLES:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier("infra", table)))
    summary["seconds"] = round(time.perf_counter() - started, 2)
    logger.info(
        "stage 4 wrote %d sensors and %d readings in %.1f s",
        summary["sensors"],
        summary["readings"],
        summary["seconds"],
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/generate_sensors.py``."""
    parser = argparse.ArgumentParser(prog="generate_sensors", description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    logger.info(
        "generating sensors (%s; source=%s, seed=%d, %d days from %s)",
        settings.dsn_summary(), settings.SENSOR_SOURCE, settings.SIM_SEED, settings.SIM_DAYS,
        settings.SIM_START.isoformat(),
    )  # fmt: skip
    try:
        with connect(settings) as conn, conn.transaction():
            run(conn, settings)
    except psycopg.OperationalError as exc:
        logger.error("database unavailable: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return EXIT_DATABASE_UNAVAILABLE
    except (StageOrderError, SensorSourceError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    return EXIT_OK
