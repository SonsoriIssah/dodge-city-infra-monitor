"""Ingestion: validate readings from any sensor source and store them in ``infra.sensor_readings``.

The same service is used by the pipeline (simulated or polled readings) and by ``POST /ingest/readings``.
It only writes readings; it never runs the detection.

Validation of each reading
--------------------------
Rejected (not stored), counted per reason in ``IngestResult.reasons``:

``invalid_record``     the reading is not shaped like a reading (no sensor id, timestamp not a datetime,
                       value not a number)
``unknown_sensor``     the sensor id is not in ``infra.sensors``
``naive_timestamp``    the timestamp has no UTC offset
``non_finite_value``   the value is NaN or infinite
``unit_mismatch``      the unit is not the unit registered for the sensor
``duplicate_reading``  the same sensor and timestamp occurred again in this call (the last one is kept)

Accepted but marked ``status='suspect'``: values outside a wide physically plausible range for the sensor
type (``PLAUSIBLE_RANGES``). Everything else is stored with ``status='ok'``.

Storage: batches are sent with COPY into a temporary table and upserted with
``INSERT ... ON CONFLICT (sensor_id, ts) DO UPDATE``, so re-sending a range replaces it. The service works
inside the caller's transaction (it opens a savepoint, or its own transaction when none is active).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import psycopg

from pipeline.db.loaders import copy_rows, create_temp_table
from pipeline.models import IngestResult, Reading

logger = logging.getLogger(__name__)

BATCH_SIZE = 20_000
STAGING_TABLE = "_stg_sensor_readings"

# Wide per-type limits of what a sensor could physically report; values outside are stored as 'suspect'.
PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (-60.0, 150.0),  # deg C
    "vibration": (0.0, 500.0),  # mm/s
    "moisture": (0.0, 100.0),  # % volumetric water content
    "pressure": (0.0, 500.0),  # psi
}

REASON_INVALID = "invalid_record"
REASON_UNKNOWN_SENSOR = "unknown_sensor"
REASON_NAIVE_TIMESTAMP = "naive_timestamp"
REASON_NON_FINITE = "non_finite_value"
REASON_UNIT = "unit_mismatch"
REASON_DUPLICATE = "duplicate_reading"

STATUS_OK = "ok"
STATUS_SUSPECT = "suspect"


class IngestionService:
    """Validates readings and upserts them through one database connection."""

    def __init__(self, conn: psycopg.Connection, batch_size: int = BATCH_SIZE) -> None:
        self.conn = conn
        self.batch_size = max(int(batch_size), 1)

    def known_sensors(self) -> dict[str, tuple[str, str]]:
        """Sensor id -> (sensor_type, unit) of every registered sensor."""
        rows = self.conn.execute("SELECT sensor_id, sensor_type, unit FROM infra.sensors").fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    @staticmethod
    def check(reading: Any, sensors: dict[str, tuple[str, str]]) -> tuple[str | None, str]:
        """Validate one reading: (rejection reason or None, status to store)."""
        sensor_id = getattr(reading, "sensor_id", None)
        ts = getattr(reading, "ts", None)
        value = getattr(reading, "value", None)
        if not isinstance(sensor_id, str) or not sensor_id or not isinstance(ts, datetime):
            return REASON_INVALID, STATUS_OK
        if isinstance(value, bool) or not isinstance(value, int | float):
            return REASON_INVALID, STATUS_OK
        sensor = sensors.get(sensor_id)
        if sensor is None:
            return REASON_UNKNOWN_SENSOR, STATUS_OK
        if ts.tzinfo is None or ts.utcoffset() is None:
            return REASON_NAIVE_TIMESTAMP, STATUS_OK
        if not math.isfinite(value):
            return REASON_NON_FINITE, STATUS_OK
        sensor_type, unit = sensor
        if getattr(reading, "unit", None) != unit:
            return REASON_UNIT, STATUS_OK
        low, high = PLAUSIBLE_RANGES.get(sensor_type, (-math.inf, math.inf))
        return None, STATUS_OK if low <= value <= high else STATUS_SUSPECT

    def ingest(self, readings: Iterable[Reading], source: Any) -> IngestResult:
        """Validate and store readings; ``source`` is the source's name (or an object with a ``name``)."""
        source_name = source if isinstance(source, str) else getattr(source, "name", None)
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("ingest() needs the name of the source of the readings")
        source_name = source_name.strip()
        result = IngestResult()
        reasons: dict[str, int] = {}
        seen: set[tuple[str, datetime]] = set()
        suspect: set[tuple[str, datetime]] = set()  # keys whose stored (latest) value is implausible
        batch: dict[tuple[str, datetime], tuple[float, str, str]] = {}

        with self.conn.transaction():
            sensors = self.known_sensors()
            create_temp_table(
                self.conn,
                STAGING_TABLE,
                "sensor_id text, ts timestamptz, value double precision, unit text, status text",
            )
            for reading in readings:
                reason, status = self.check(reading, sensors)
                if reason is not None:
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                key = (reading.sensor_id, reading.ts.astimezone(UTC))
                if key in seen:
                    reasons[REASON_DUPLICATE] = reasons.get(REASON_DUPLICATE, 0) + 1  # the later one replaces it
                seen.add(key)
                if status == STATUS_SUSPECT:
                    suspect.add(key)
                else:
                    suspect.discard(key)
                batch[key] = (float(reading.value), reading.unit, status)
                if len(batch) >= self.batch_size:
                    self._write(batch, source_name)
                    batch = {}
            if batch:
                self._write(batch, source_name)

        result.accepted = len(seen)
        result.suspect = len(suspect)
        result.reasons = dict(sorted(reasons.items()))
        result.rejected = sum(reasons.values())
        if result.rejected or result.suspect:
            logger.info(
                "ingestion from %s: %d accepted (%d suspect), %d rejected %s",
                source_name, result.accepted, result.suspect, result.rejected, result.reasons,
            )  # fmt: skip
        return result

    def _write(self, batch: dict[tuple[str, datetime], tuple[float, str, str]], source_name: str) -> None:
        """COPY one batch into the staging table and upsert it."""
        copy_rows(
            self.conn,
            f"pg_temp.{STAGING_TABLE}",
            ("sensor_id", "ts", "value", "unit", "status"),
            ((sensor_id, ts, value, unit, status) for (sensor_id, ts), (value, unit, status) in batch.items()),
        )
        self.conn.execute(
            f"""
            INSERT INTO infra.sensor_readings (sensor_id, ts, value, unit, status, source)
            SELECT sensor_id, ts, value, unit, status, %s
            FROM pg_temp.{STAGING_TABLE}
            ORDER BY sensor_id, ts
            ON CONFLICT (sensor_id, ts) DO UPDATE SET
                value = EXCLUDED.value,
                unit = EXCLUDED.unit,
                status = EXCLUDED.status,
                source = EXCLUDED.source,
                ingested_at = now()
            """,
            (source_name,),
        )
        self.conn.execute(f"TRUNCATE pg_temp.{STAGING_TABLE}")
