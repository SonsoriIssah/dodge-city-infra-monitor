"""Stage 2 - process the raw GIS data into study-area layers and the asset registry (no database access).

Reads ``data/raw`` and writes ``data/processed/{study_area, buildings, roads, assets, city_boundary}.geojson``
and ``processing_report.json``.
"""
from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from pipeline.config import PROCESSED_DIR, RAW_DIR, Settings, get_settings
from pipeline.gis.process import ProcessedData, process, write_outputs
from pipeline.logging_utils import setup_logging

logger = logging.getLogger(__name__)


def run(settings: Settings, raw_dir: Path = RAW_DIR, out_dir: Path = PROCESSED_DIR) -> ProcessedData:
    """Process the raw files and write the outputs; returns the processed data."""
    data = process(raw_dir, settings)
    written = write_outputs(data, out_dir)
    for key, path in written.items():
        logger.info("wrote %s (%s, %d bytes)", path.name, key, path.stat().st_size)
    for record in data.report["nbi"]["mismatched"]:
        logger.info(
            "listed in processing_report.json: NBI record %s (%s) creates no asset",
            record["structure_number"], record["facility_carried"],
        )  # fmt: skip
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/process_data.py``."""
    parser = argparse.ArgumentParser(prog="process_data", description=__doc__.splitlines()[0])
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="directory of the raw files (default: data/raw)")
    parser.add_argument(
        "--out-dir", type=Path, default=PROCESSED_DIR, help="output directory (default: data/processed)"
    )
    args = parser.parse_args(argv)
    setup_logging()
    try:
        run(get_settings(), args.raw_dir, args.out_dir)
    except (FileNotFoundError, ValueError) as exc:  # missing or malformed input file
        logger.error("%s", exc)
        return 1
    return 0
