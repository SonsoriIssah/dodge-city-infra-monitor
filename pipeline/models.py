"""Plain data records passed between the sensor, ingestion and detection layers.

They mirror the database rows of ``infra.sensors``, ``infra.sensor_readings``, ``infra.simulation_events`` and
``infra.anomalies`` (see ``sql/migrations/001_schema.sql``) and carry no behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# Values of ``SensorSpec.asset_tags``: hints from the placement rules to the simulator (not stored).
TAG_SHOWCASE = "showcase"  # the host asset was chosen by the showcase rule
TAG_NBI_BRIDGE = "nbi_bridge"  # the host is a highway bridge matched to a National Bridge Inventory record


@dataclass(frozen=True, slots=True)
class SensorSpec:
    """One sensor of the monitoring network (a row of ``infra.sensors``)."""

    sensor_id: str
    asset_id: str
    sensor_type: str  # temperature | vibration | moisture | pressure
    placement: str  # key into infra.sensor_thresholds together with sensor_type
    unit: str
    lon: float
    lat: float
    description: str | None = None
    is_simulated: bool = True
    source: str = "simulator"
    installed_at: date | None = None
    sampling_interval_s: int = 3600
    asset_type: str | None = None  # convenience copy of the host asset's type (not stored on the sensor row)
    asset_tags: tuple[str, ...] = ()  # TAG_SHOWCASE / TAG_NBI_BRIDGE (not stored on the sensor row)


@dataclass(frozen=True, slots=True)
class Reading:
    """One sensor reading as produced by a sensor source. ``ts`` must be timezone-aware."""

    sensor_id: str
    ts: datetime
    value: float
    unit: str


@dataclass(slots=True)
class IngestResult:
    """Outcome of one ingestion call: rows written, rows refused and why.

    ``accepted`` counts the readings written (inserted or updated), including those stored with
    ``status='suspect'`` (counted again in ``suspect``). ``reasons`` maps a rejection reason to its count.
    """

    accepted: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    suspect: int = 0


@dataclass(frozen=True, slots=True)
class InjectedEvent:
    """Simulator ground truth (a row of ``infra.simulation_events``).

    Injected abnormal events have ``is_anomaly=True`` and a sensor and asset; benign regional events
    (rain, hot spell) have ``is_anomaly=False`` and no sensor.
    """

    event_type: str
    is_anomaly: bool
    started_at: datetime
    ended_at: datetime
    sensor_id: str | None = None
    asset_id: str | None = None
    sensor_type: str | None = None
    magnitude: float | None = None
    description: str | None = None


@dataclass(slots=True)
class AnomalyRecord:
    """One detected anomaly event (a row of ``infra.anomalies``)."""

    anomaly_id: str
    sensor_id: str
    asset_id: str
    sensor_type: str
    anomaly_type: str
    started_at: datetime
    ended_at: datetime
    peak_at: datetime
    duration_hours: int
    observed_value: float
    expected_value: float
    unit: str
    robust_z: float
    anomaly_score: float
    severity: str  # low | medium | high | critical
    detection_method: str
    explanation: str
    status: str  # active | resolved (value at the end of the analysed window)
    lon: float
    lat: float
    score_components: dict[str, float] = field(default_factory=dict)
    run_id: int | None = None
    cluster_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # working data that is not persisted
