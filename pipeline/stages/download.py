"""Stage 1 - download the raw GIS data into ``data/raw`` and record its provenance in ``SOURCES.json``.

Cached files are used unless ``--refresh`` is given. OpenStreetMap is mandatory; the bridge inventory and the
city boundary are optional layers that are skipped (with a warning) when they cannot be downloaded and no
cache exists.
"""
from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pipeline.config import RAW_DIR, Settings, get_settings
from pipeline.gis import nbi, osm, tiger
from pipeline.gis.sources import heights_entry, read_sources, write_sources
from pipeline.logging_utils import setup_logging

logger = logging.getLogger(__name__)


def run(settings: Settings, raw_dir: Path = RAW_DIR, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Download (or reuse) every raw file and rewrite ``SOURCES.json``; returns its entries by source id.

    Raises ``pipeline.gis.osm.OverpassError`` when OpenStreetMap data is neither downloadable nor cached.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    previous = read_sources(raw_dir)
    bbox = settings.bbox
    entries: dict[str, dict[str, Any]] = {}

    entries[osm.SOURCE_ID] = osm.download(raw_dir, bbox, refresh=refresh, previous=previous.get(osm.SOURCE_ID))
    optional = {
        nbi.SOURCE_ID: nbi.download(raw_dir, bbox, refresh=refresh, previous=previous.get(nbi.SOURCE_ID)),
        tiger.SOURCE_ID: tiger.download(
            raw_dir, bbox, settings.TIGER_PLACE_GEOID, refresh=refresh, previous=previous.get(tiger.SOURCE_ID)
        ),
        "usgs_3dep": heights_entry(raw_dir),
    }
    for source_id, entry in optional.items():
        if entry is not None:
            entries[source_id] = entry
    if "usgs_3dep" in entries:
        logger.info("measured building heights present: %s buildings", entries["usgs_3dep"]["feature_count"])

    path = write_sources(raw_dir, entries)
    for source_id, entry in entries.items():
        logger.info(
            "source %-9s file=%s features=%s vintage=%s",
            source_id, entry["file"], entry["feature_count"], entry["vintage"],
        )  # fmt: skip
    logger.info("wrote %s (%d sources)", path.name, len(entries))
    return entries


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/download_data.py [--refresh]``."""
    parser = argparse.ArgumentParser(prog="download_data", description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="download again even when a cached file exists")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="directory of the raw files (default: data/raw)")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        run(get_settings(), args.raw_dir, refresh=args.refresh)
    except osm.OverpassError as exc:
        logger.error("OpenStreetMap data is required and could not be downloaded: %s", exc)
        return 1
    return 0
