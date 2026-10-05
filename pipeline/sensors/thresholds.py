"""Sensor types, placements and their warning / critical limits (the defaults seeded into the database).

The table ``infra.sensor_thresholds`` is the configuration the detector and the API read; this module holds
its default content (build contract section 7) and the helper that seeds it. ``None`` means "no limit on
that side".
"""
from __future__ import annotations

from dataclasses import astuple, dataclass

import psycopg

SENSOR_TYPES: tuple[str, ...] = ("temperature", "vibration", "moisture", "pressure")

# Exact unit strings stored with every sensor and reading.
UNITS: dict[str, str] = {"temperature": "°C", "vibration": "mm/s", "moisture": "%", "pressure": "psi"}
SENSOR_TYPE_LABELS: dict[str, str] = {
    "temperature": "Temperature",
    "vibration": "Vibration",
    "moisture": "Moisture",
    "pressure": "Pressure",
}
SENSOR_ID_PREFIXES: dict[str, str] = {"temperature": "TMP", "vibration": "VIB", "moisture": "MST", "pressure": "PRS"}


@dataclass(frozen=True, slots=True)
class Threshold:
    """Limits of one (sensor_type, placement) class; a row of ``infra.sensor_thresholds``."""

    sensor_type: str
    placement: str
    unit: str
    warn_low: float | None
    warn_high: float | None
    crit_low: float | None
    crit_high: float | None
    description: str


def _t(
    sensor_type: str,
    placement: str,
    warn_low: float | None,
    warn_high: float | None,
    crit_low: float | None,
    crit_high: float | None,
    description: str,
) -> Threshold:
    return Threshold(sensor_type, placement, UNITS[sensor_type], warn_low, warn_high, crit_low, crit_high, description)


# (sensor_type, placement, warn_low, warn_high, crit_low, crit_high, description)
THRESHOLDS: tuple[Threshold, ...] = (
    _t("vibration", "bridge_deck", None, 5.0, None, 10.0, "Hourly RMS vibration velocity, bridge deck or culvert"),
    _t("vibration", "building_structure", None, 1.0, None, 3.0, "Hourly RMS vibration velocity, building structure"),
    _t("vibration", "road_pavement", None, 2.5, None, 5.0, "Hourly RMS vibration velocity, road pavement"),
    _t("moisture", "road_subgrade", None, 35.0, None, 42.0, "Volumetric water content, road subgrade"),
    _t("moisture", "foundation_perimeter", None, 35.0, None, 42.0, "Volumetric water content, building foundation"),
    _t("moisture", "abutment_backfill", None, 35.0, None, 42.0, "Volumetric water content, abutment backfill"),
    _t("temperature", "bridge_deck", None, 50.0, None, 58.0, "Bridge deck surface temperature"),
    _t("temperature", "road_surface", None, 58.0, None, 65.0, "Road surface temperature"),
    _t("temperature", "building_envelope", None, 40.0, None, 45.0, "Building envelope temperature"),
    _t("temperature", "equipment", None, 65.0, None, 75.0, "Equipment temperature, power substation"),
    _t("pressure", "water_main", 40.0, 90.0, 20.0, 110.0, "Operating pressure, simulated water main"),
)  # fmt: skip

_BY_KEY: dict[tuple[str, str], Threshold] = {(t.sensor_type, t.placement): t for t in THRESHOLDS}


def threshold_for(sensor_type: str, placement: str) -> Threshold:
    """Default limits of a (sensor_type, placement) class; ``KeyError`` when the class is not defined."""
    return _BY_KEY[(sensor_type, placement)]


def placements_for(sensor_type: str) -> tuple[str, ...]:
    """Placements defined for a sensor type, in table order."""
    return tuple(t.placement for t in THRESHOLDS if t.sensor_type == sensor_type)


def seed_thresholds(conn: psycopg.Connection) -> int:
    """Upsert the default limits into ``infra.sensor_thresholds``; returns the number of rows written."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO infra.sensor_thresholds
                (sensor_type, placement, unit, warn_low, warn_high, crit_low, crit_high, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sensor_type, placement) DO UPDATE SET
                unit = EXCLUDED.unit,
                warn_low = EXCLUDED.warn_low,
                warn_high = EXCLUDED.warn_high,
                crit_low = EXCLUDED.crit_low,
                crit_high = EXCLUDED.crit_high,
                description = EXCLUDED.description
            """,
            [astuple(t) for t in THRESHOLDS],
        )
    return len(THRESHOLDS)
