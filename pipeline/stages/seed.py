"""Stage 3 - migrate the database and load the processed GIS layers (one transaction, idempotent).

Applies the migrations, removes the previous study area (which cascades to everything that belongs to it),
then registers the data sources (dropping those whose file is no longer present), upserts the sensor thresholds
and loads the study area, the reference boundary, the buildings, the base-map roads and the real
infrastructure assets.
"""
from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg

from pipeline.config import PROCESSED_DIR, RAW_DIR, Settings, get_settings
from pipeline.db import loaders, migrate
from pipeline.db.connection import connect
from pipeline.gis.sources import SOURCE_REGISTRY, load_source_entries
from pipeline.logging_utils import setup_logging
from pipeline.sensors.thresholds import seed_thresholds
from pipeline.stages import EXIT_DATABASE_UNAVAILABLE, EXIT_FAILED, EXIT_OK

logger = logging.getLogger(__name__)

# Sources that are registered whenever the database is seeded, with or without a downloaded file.
ALWAYS_REGISTERED = ("osm", "basemap", "imagery", "simulator", "derived")


def data_source_rows(entries: dict[str, dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    """Rows for ``infra.data_sources``: the source catalogue combined with the facts in ``SOURCES.json``.

    Downloaded sources (nbi, tiger, usgs_3dep) are registered only when their file is present.
    """
    rows: list[dict[str, Any]] = []
    for source_id, info in SOURCE_REGISTRY.items():
        entry = entries.get(source_id)
        if entry is None and source_id not in ALWAYS_REGISTERED:
            continue
        entry = entry or {}
        row = {
            "source_id": source_id,
            "name": info.name,
            "kind": info.kind,
            "provider": info.provider,
            "url": entry.get("url") or info.url,
            "license": info.license,
            "attribution_text": info.attribution_text,
            "vintage": entry.get("vintage"),
            "retrieved_at": entry.get("retrieved_at"),
            "notes": info.notes,
        }
        if source_id in ("nbi", "usgs_3dep") and entry.get("attribution_text"):
            row["attribution_text"] = entry["attribution_text"]  # carries the data date / lidar project
        if source_id == "basemap":
            row["url"] = settings.BASEMAP_STYLE_URL
        if source_id == "simulator":
            row["vintage"] = f"seed {settings.SIM_SEED}"
        rows.append(row)
    return rows


def clear_study_area(conn: psycopg.Connection) -> None:
    """Remove the current study area and everything derived from it; restart ids for a reproducible rebuild.

    ``DELETE FROM infra.study_areas`` cascades to boundaries, buildings, roads, assets, sensors, readings,
    scores, anomalies, health and risk zones. Detection runs (with their clusters) and the regional
    simulation events have no path to the study area and are cleared explicitly.
    """
    conn.execute("TRUNCATE infra.detection_runs RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM infra.simulation_events")
    conn.execute("DELETE FROM infra.study_areas")
    loaders.restart_sequences(conn)


def run(
    conn: psycopg.Connection,
    settings: Settings,
    processed_dir: Path = PROCESSED_DIR,
    raw_dir: Path = RAW_DIR,
    reset_schema: bool = False,
) -> dict[str, int]:
    """Seed the database through ``conn`` (the caller owns the transaction); returns the row count per table."""
    if reset_schema:
        migrate.reset_schema(conn)
    migrate.apply_migrations(conn)
    clear_study_area(conn)

    sources = data_source_rows(load_source_entries(raw_dir), settings)
    stale = loaders.delete_stale_data_sources(conn, [row["source_id"] for row in sources])
    if stale:
        logger.info("data sources no longer present, removed: %s", ", ".join(stale))
    loaders.upsert_data_sources(conn, sources)
    logger.info("data sources registered: %s", ", ".join(row["source_id"] for row in sources))

    loaded = loaders.load_gis(conn, processed_dir, settings)
    logger.info("GIS layers loaded: %s", ", ".join(f"{table}={count}" for table, count in loaded.items()))

    missing = conn.execute(
        """
        SELECT DISTINCT b.height_source
        FROM infra.buildings b
        WHERE b.height_source = 'lidar_3dep'
          AND NOT EXISTS (SELECT 1 FROM infra.data_sources d WHERE d.source_id = 'usgs_3dep')
        """
    ).fetchall()
    if missing:
        raise RuntimeError(
            "buildings carry lidar heights but the usgs_3dep source is not registered; "
            "re-run scripts/process_data.py so the processed layers match data/raw"
        )

    seed_thresholds(conn)
    counts = loaders.table_counts(conn)
    logger.info("row counts: %s", ", ".join(f"{table}={count}" for table, count in counts.items()))
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/seed_database.py [--reset-schema]``."""
    parser = argparse.ArgumentParser(prog="seed_database", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset-schema",
        action="store_true",
        help="drop schema infra CASCADE before migrating (development convenience)",
    )
    parser.add_argument(
        "--processed-dir", type=Path, default=PROCESSED_DIR, help="processed layers (default: data/processed)"
    )
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="raw files with SOURCES.json (default: data/raw)")
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    logger.info("seeding database (%s)", settings.dsn_summary())
    try:
        with connect(settings) as conn, conn.transaction():
            run(conn, settings, args.processed_dir, args.raw_dir, reset_schema=args.reset_schema)
    except psycopg.OperationalError as exc:
        logger.error("database unavailable: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return EXIT_DATABASE_UNAVAILABLE
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    return EXIT_OK
