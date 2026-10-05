"""Stage 5 - run the prototype anomaly detection over the stored readings (one transaction, idempotent).

Reads the sensors, their limits and their readings from the database, estimates a baseline per sensor,
runs the detectors, merges flagged hours into anomaly events and scores the run against the simulator's
injected events (a self-consistency check). Writes ``infra.detection_runs``, ``infra.reading_scores`` and
``infra.anomalies``.

Cleared first: ``infra.detection_runs`` with everything derived from a run (reading scores, anomalies,
clusters, asset health, risk-zone scores). Run ``scripts/analyze_spatial.py`` afterwards to rebuild the
spatial analysis.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from typing import Any

import psycopg

from pipeline.config import Settings, get_settings
from pipeline.db.connection import connect
from pipeline.detection import runner
from pipeline.logging_utils import setup_logging
from pipeline.stages import EXIT_DATABASE_UNAVAILABLE, EXIT_FAILED, EXIT_OK

logger = logging.getLogger(__name__)


def run(conn: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """Run the stage through ``conn`` (the caller owns the transaction); returns the run summary."""
    return runner.run_detection(conn, settings)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/detect_anomalies.py``."""
    parser = argparse.ArgumentParser(prog="detect_anomalies", description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    logger.info(
        "detecting anomalies (%s; z_strong=%.1f, z_min=%.1f, rolling=%d h, merge gap=%d h)",
        settings.dsn_summary(), settings.DETECT_Z_STRONG, settings.DETECT_Z_MIN, settings.DETECT_ROLLING_HOURS,
        settings.DETECT_MERGE_GAP_HOURS,
    )  # fmt: skip
    try:
        with connect(settings) as conn, conn.transaction():
            run(conn, settings)
    except psycopg.OperationalError as exc:
        logger.error("database unavailable: %s", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        return EXIT_DATABASE_UNAVAILABLE
    except (runner.DetectionInputError, runner.DetectionTargetError, ValueError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    return EXIT_OK
