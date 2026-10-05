"""The playback bundle (build contract 10.4) and its in-process cache.

The bundle holds everything that changes with time, for every step of the analysed window: sensor values
and status characters, asset health, risk per hexagonal cell and the KPI series. It is assembled from the
vectorised form of the status rules in ``pipeline.analysis.status`` (``load_sensor_matrix``, ``kpi_series``)
and from the stored hourly health and risk scores, so element ``i`` of every series equals what the
single-``as_of`` endpoints answer for ``timestamps[i]``.

It is built once per detection run and analysis generation, kept in the process as encoded bytes (plain and
gzip) and served with an ETag.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import psycopg

from backend.app.queries.common import VALUE_DECIMALS, DataNotReadyError, rounded
from backend.app.schemas import canonical_json
from backend.app.timeutil import iso_z
from pipeline.analysis import health, risk_zones, status
from pipeline.config import DATA_NOTICE

logger = logging.getLogger(__name__)

GZIP_LEVEL = 9
ETAG_HEX_DIGITS = 32

# What the cached bundle depends on: the latest finished run and the transaction that wrote the spatial
# analysis (stage 6 rewrites its tables in one transaction, so any row carries its id). The run id alone is
# not enough: a re-run of the detection restarts the numbering at 1.
GENERATION_SQL = """
SELECT r.run_id,
       r.finished_at,
       (SELECT h.xmin::text FROM infra.asset_health h LIMIT 1) AS health_generation,
       (SELECT z.xmin::text FROM infra.risk_zone_scores z LIMIT 1) AS risk_generation
FROM infra.detection_runs r
WHERE r.finished_at IS NOT NULL AND r.window_start IS NOT NULL AND r.window_end IS NOT NULL
ORDER BY r.run_id DESC
LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class PlaybackBundle:
    """The encoded playback bundle of one generation of the data."""

    generation: tuple[Any, ...]
    body: bytes
    gzip_body: bytes
    etag: str


def generation(conn: psycopg.Connection) -> tuple[Any, ...]:
    """Identity of the data the bundle is built from; ``DataNotReadyError`` without a finished run."""
    row = conn.execute(GENERATION_SQL).fetchone()
    if row is None:
        raise DataNotReadyError("no finished detection run in the database; run scripts/detect_anomalies.py first")
    return tuple(row)


def build_playback(conn: psycopg.Connection, axis: status.TimeAxis) -> dict[str, Any]:
    """Assemble the bundle for the time axis of the latest run."""
    anomalies = status.load_anomaly_intervals(conn)
    matrix = status.load_sensor_matrix(conn, axis, anomalies)
    statuses = matrix.status_strings()
    sensors = {
        sensor_id: {
            "values": [rounded(value, VALUE_DECIMALS) for value in matrix.values[i].tolist()],
            "status": statuses[sensor_id],
        }
        for i, sensor_id in enumerate(matrix.sensor_ids)
    }
    zones = {
        cell_id: {"risk": series}
        for cell_id, series in risk_zones.load_risk_series(conn, axis).items()
        if any(series)  # only cells whose risk is ever above zero
    }
    return {
        "data_notice": DATA_NOTICE,
        "run_id": axis.run_id,
        "timestamps": [iso_z(moment) for moment in axis.timestamps()],
        "sensors": sensors,
        "assets": health.load_health_series(conn, axis),
        "zones": zones,
        "stats": status.kpi_series(conn, axis, matrix),
    }


def encode_bundle(key: tuple[Any, ...], content: dict[str, Any]) -> PlaybackBundle:
    """Encode the bundle once: canonical JSON, its gzip form and a validator derived from the content."""
    body = canonical_json(content)
    digest = hashlib.sha256(body).hexdigest()[:ETAG_HEX_DIGITS]
    # Weak validator: the same ETag names the plain and the gzip representation.
    return PlaybackBundle(
        generation=key,
        body=body,
        gzip_body=gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0),
        etag=f'W/"{digest}"',
    )


class PlaybackCache:
    """Keeps the encoded bundle of the current data generation (one per application)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bundle: PlaybackBundle | None = None

    def get(self, conn: psycopg.Connection) -> PlaybackBundle:
        """The bundle for the data ``conn`` sees; built on first use and whenever the data generation changes."""
        key = generation(conn)
        bundle = self._bundle
        if bundle is not None and bundle.generation == key:
            return bundle
        with self._lock:
            bundle = self._bundle
            if bundle is not None and bundle.generation == key:
                return bundle
            started = time.perf_counter()
            bundle = encode_bundle(key, build_playback(conn, status.load_time_axis(conn)))
            self._bundle = bundle
            logger.info(
                "playback bundle built for run %s in %.2f s: %d bytes (%d bytes gzip)",
                key[0], time.perf_counter() - started, len(bundle.body), len(bundle.gzip_body),
            )  # fmt: skip
            return bundle

    def clear(self) -> None:
        """Forget the cached bundle (after readings were ingested through this process)."""
        with self._lock:
            self._bundle = None


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Weak comparison of an ``If-None-Match`` header with the current ETag."""
    if not if_none_match:
        return False
    opaque = etag.removeprefix("W/")
    candidates = (candidate.strip() for candidate in if_none_match.split(","))
    return any(candidate == "*" or candidate.removeprefix("W/") == opaque for candidate in candidates)
