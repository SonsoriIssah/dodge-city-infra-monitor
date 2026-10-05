"""Stage 7 - static snapshot of the API responses for the dashboard (build contract section 11).

Drives the real application in-process (``TestClient``, no network port) and writes every response the
dashboard needs under ``dashboard/data/snapshot/``, so GitHub Pages can serve the dashboard without a
backend. Each file is byte-for-byte the body of the API response it was fetched from (``SNAPSHOT_FILES``,
``reading_url``, ``health_url``); the API encodes canonical JSON (sorted keys, compact separators, no line
breaks), so unchanged data yields identical files.

    meta.json  assets.geojson  roads.geojson  study-area.geojson  city-boundary.geojson  sensors.json
    anomalies.json  clusters.geojson  risk-zones.geojson  simulation-events.json  playback.json
    readings/<sensor_id>.json   health/<asset_id>.json (monitored assets)   manifest.json

``manifest.json`` lists every file with its size and SHA-256; its ``generated_at`` is the finishing time of
the detection run, not the wall clock. Everything is fetched and checked (completeness, parity of the
playback bundle with the single-timestamp endpoints, size budgets) before the folder is cleared and written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import time
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.exceptions import StarletteDeprecationWarning

from backend.app.main import create_app
from backend.app.schemas import canonical_json
from pipeline.analysis.status import KPI_SERIES_KEYS, SENSOR_STATUS_CHARS, SENSOR_STATUSES
from pipeline.config import ROOT, SNAPSHOT_DIR, Settings, get_settings
from pipeline.logging_utils import setup_logging
from pipeline.stages import EXIT_DATABASE_UNAVAILABLE, EXIT_FAILED, EXIT_OK

with warnings.catch_warnings():
    # The test client still works with httpx; its notice about the httpx2 package is not relevant here.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
READINGS_DIR = "readings"
HEALTH_DIR = "health"
PLAYBACK_FILE = "playback.json"
TOTAL_BUDGET_BYTES = 8_000_000
PLAYBACK_BUDGET_BYTES = 2_000_000
PARITY_SAMPLES = 5
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Snapshot file -> API request it is the response body of (the per-sensor and per-asset files follow below).
SNAPSHOT_FILES: tuple[tuple[str, str], ...] = (
    ("meta.json", "/meta"),
    ("assets.geojson", "/assets"),
    ("roads.geojson", "/layers/roads"),
    ("study-area.geojson", "/layers/study-area"),
    ("city-boundary.geojson", "/layers/city-boundary"),
    ("sensors.json", "/sensors"),
    ("anomalies.json", "/anomalies?include=nearby_assets&limit=1000"),
    ("clusters.geojson", "/spatial/clusters"),
    ("risk-zones.geojson", "/spatial/risk-zones"),
    ("simulation-events.json", "/simulation-events"),
    (PLAYBACK_FILE, "/playback"),
)


class ExportError(RuntimeError):
    """The snapshot cannot be produced (the reason is the message)."""


class DatabaseUnavailableError(ExportError):
    """The API could not reach the database."""


def reading_url(sensor_id: str) -> str:
    """API request behind ``readings/<sensor_id>.json``: the full window, columnar."""
    return f"/sensor-readings?sensor_id={quote(sensor_id, safe='')}&shape=columns"


def health_url(asset_id: str) -> str:
    """API request behind ``health/<asset_id>.json``: the full window."""
    return f"/assets/{quote(asset_id, safe='')}/health"


def _file_name(directory: str, record_id: str) -> str:
    if not SAFE_ID.match(record_id):
        raise ExportError(f"id {record_id!r} cannot be used as a snapshot file name")
    return f"{directory}/{record_id}.json"


def _get(client: TestClient, url: str) -> Any:
    """GET an API path; the response, or ``ExportError`` when the API does not answer 200."""
    response = client.get(url)
    if response.status_code != 200:
        raise ExportError(f"GET {url} answered {response.status_code}: {response.text[:300]}")
    return response


def check_service(client: TestClient) -> None:
    """Make sure the API can answer from an analysed database before anything is fetched."""
    response = client.get("/health")
    if response.status_code == 200:
        return
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if body.get("database") == "unavailable":
        raise DatabaseUnavailableError("database unavailable")
    raise ExportError(body.get("detail") or f"GET /health answered {response.status_code}")


def check_parity(client: TestClient, playback: dict[str, Any]) -> None:
    """The playback bundle must agree with the single-timestamp endpoints at sampled time steps.

    Compares the KPI series with ``/statistics?as_of=`` and the sensor status characters and values with
    ``/sensors?as_of=``. A mismatch means the two forms of the status rules have diverged.
    """
    timestamps = playback["timestamps"]
    last = len(timestamps) - 1
    indices = sorted({round(last * step / (PARITY_SAMPLES - 1)) for step in range(PARITY_SAMPLES)})
    chars = dict(zip(SENSOR_STATUSES, SENSOR_STATUS_CHARS, strict=True))
    for index in indices:
        moment = timestamps[index]
        statistics = _get(client, f"/statistics?as_of={moment}").json()
        for key in KPI_SERIES_KEYS:
            if statistics[key] != playback["stats"][key][index]:
                raise ExportError(
                    f"playback.stats.{key}[{index}] = {playback['stats'][key][index]} but /statistics at "
                    f"{moment} says {statistics[key]}"
                )
        page = _get(client, f"/sensors?as_of={moment}&limit=5000").json()
        for item in page["items"]:
            series = playback["sensors"][item["sensor_id"]]
            value = None if item["latest"] is None else item["latest"]["value"]
            if series["status"][index] != chars[item["status"]] or series["values"][index] != value:
                raise ExportError(
                    f"playback and /sensors disagree on {item['sensor_id']} at {moment}: "
                    f"{series['status'][index]} {series['values'][index]} vs {item['status']} {value}"
                )
    logger.info("playback agrees with /statistics and /sensors at %d sampled time steps", len(indices))


def fetch_snapshot(client: TestClient) -> dict[str, bytes]:
    """Fetch every file of the snapshot (except the manifest): relative path -> response body."""
    files: dict[str, bytes] = {}
    parsed: dict[str, Any] = {}
    for name, url in SNAPSHOT_FILES:
        response = _get(client, url)
        files[name] = response.content
        parsed[name] = response.json()

    assets, sensors, anomalies = parsed["assets.geojson"], parsed["sensors.json"], parsed["anomalies.json"]
    if assets["numberReturned"] != assets["numberMatched"]:
        raise ExportError(f"/assets returned {assets['numberReturned']} of {assets['numberMatched']} assets")
    if len(sensors["items"]) != sensors["total"]:
        raise ExportError(f"/sensors returned {len(sensors['items'])} of {sensors['total']} sensors")
    if len(anomalies["items"]) != anomalies["total"]:
        raise ExportError(f"/anomalies returned {len(anomalies['items'])} of {anomalies['total']} anomalies")
    check_parity(client, parsed[PLAYBACK_FILE])

    steps = parsed["meta.json"]["time"]["count"]
    for item in sensors["items"]:
        sensor_id = item["sensor_id"]
        response = _get(client, reading_url(sensor_id))
        if response.json()["count"] != steps:
            raise ExportError(f"readings of {sensor_id} cover {response.json()['count']} of {steps} time steps")
        files[_file_name(READINGS_DIR, sensor_id)] = response.content
    for feature in assets["features"]:
        if feature["properties"]["monitored"]:
            asset_id = feature["properties"]["asset_id"]
            files[_file_name(HEALTH_DIR, asset_id)] = _get(client, health_url(asset_id)).content
    return files


def build_manifest(files: dict[str, bytes], meta: dict[str, Any]) -> bytes:
    """``manifest.json``: when the data was produced and the size and SHA-256 of every other file."""
    return canonical_json(
        {
            "generated_at": meta["detection_run"]["finished_at"],
            "as_of": meta["time"]["end"],
            "api_version": meta["version"],
            "files": [
                {"path": path, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                for path, body in sorted(files.items())
            ],
        }
    )


def check_budgets(files: dict[str, bytes]) -> None:
    """Fail when the snapshot is too large for a static site."""
    playback, total = len(files[PLAYBACK_FILE]), sum(len(body) for body in files.values())
    if playback > PLAYBACK_BUDGET_BYTES:
        raise ExportError(f"{PLAYBACK_FILE} is {playback} bytes; the budget is {PLAYBACK_BUDGET_BYTES}")
    if total > TOTAL_BUDGET_BYTES:
        raise ExportError(f"the snapshot is {total} bytes; the budget is {TOTAL_BUDGET_BYTES}")


def clear_directory(directory: Path) -> None:
    """Empty the snapshot folder (create it when missing); refuses a folder that is not a snapshot."""
    if directory.exists() and not directory.is_dir():
        raise ExportError(f"{directory} is not a directory")
    directory.mkdir(parents=True, exist_ok=True)
    entries = list(directory.iterdir())
    is_snapshot = directory.resolve() == SNAPSHOT_DIR.resolve() or (directory / MANIFEST_NAME).is_file()
    if entries and not is_snapshot:
        raise ExportError(f"refusing to clear {directory}: it is not empty and holds no {MANIFEST_NAME}")
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def write_snapshot(directory: Path, files: dict[str, bytes]) -> None:
    """Clear the folder and write the files exactly as given (binary, no newline translation)."""
    clear_directory(directory)
    for path, body in files.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


def _log_sizes(files: dict[str, bytes], directory: Path) -> None:
    groups: dict[str, list[int]] = {}
    for path, body in sorted(files.items()):
        if "/" in path:
            groups.setdefault(path.split("/")[0], []).append(len(body))
        else:
            logger.info("  %-26s %9d bytes", path, len(body))
    for name, sizes in groups.items():
        logger.info("  %-26s %9d bytes in %d files", name + "/", sum(sizes), len(sizes))
    total = sum(len(body) for body in files.values())
    try:
        shown = directory.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        shown = str(directory)
    logger.info(
        "snapshot written to %s: %d files, %d bytes (budget %d); %s %d bytes (budget %d)",
        shown, len(files), total, TOTAL_BUDGET_BYTES, PLAYBACK_FILE, len(files[PLAYBACK_FILE]), PLAYBACK_BUDGET_BYTES,
    )  # fmt: skip


def export_snapshot(settings: Settings, directory: Path = SNAPSHOT_DIR) -> dict[str, Any]:
    """Fetch, check and write the snapshot for the database of ``settings``; returns a summary."""
    started = time.perf_counter()
    app = create_app(settings.model_copy(update={"SERVE_DASHBOARD": False}))
    with TestClient(app) as client:
        check_service(client)
        files = fetch_snapshot(client)
    meta = json.loads(files["meta.json"])
    files[MANIFEST_NAME] = build_manifest(files, meta)
    check_budgets(files)
    write_snapshot(directory, files)
    _log_sizes(files, directory)
    seconds = round(time.perf_counter() - started, 2)
    logger.info("stage 7 finished in %.1f s", seconds)
    return {
        "directory": str(directory),
        "files": len(files),
        "bytes": sum(len(body) for body in files.values()),
        "playback_bytes": len(files[PLAYBACK_FILE]),
        "generated_at": meta["detection_run"]["finished_at"],
        "seconds": seconds,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``python scripts/export_static.py``."""
    parser = argparse.ArgumentParser(prog="export_static", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path, default=SNAPSHOT_DIR, help="snapshot folder (default: dashboard/data/snapshot)"
    )
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    logger.info("exporting the static snapshot from the API (%s)", settings.dsn_summary())
    try:
        export_snapshot(settings, args.output)
    except DatabaseUnavailableError as exc:
        logger.error("%s", exc)
        return EXIT_DATABASE_UNAVAILABLE
    except (ExportError, OSError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
