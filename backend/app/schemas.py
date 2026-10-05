"""Wire format of the API: canonical JSON encoding, enumerations, response and request models.

The query layer builds plain dictionaries and the endpoints send them through ``CanonicalJSONResponse``, so
the bytes of a response are deterministic (sorted keys, compact separators, UTF-8) and the static snapshot
written by ``backend.export`` is byte-for-byte the API response. The models below document those shapes in
the OpenAPI schema (``response_model=``); they are strict (no unknown keys), which lets a test validate
every real response against them. Only the ingest request is parsed through its model.

Conventions: timestamps are ``YYYY-MM-DDTHH:MM:SSZ`` strings (UTC); values are rounded to 3 decimals,
robust z-scores to 2, scores to 3, coordinates to 6, distances to 0.1 m; a number without a fractional part
is written as an integer (60, not 60.0); missing text is null, never "".
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from backend.app.timeutil import ISO_Z_PATTERN
from pipeline.analysis.health import ASSET_STATUSES, STATUS_NOT_MONITORED
from pipeline.analysis.status import SENSOR_STATUSES
from pipeline.sensors.thresholds import SENSOR_TYPES

MAX_INGEST_READINGS = 10_000

SEVERITY_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")
ASSET_TYPES: tuple[str, ...] = ("building", "road", "bridge", "rail", "power", "street_light", "water_main")
ANOMALY_STATUSES: tuple[str, ...] = ("active", "resolved")
ANOMALY_SORTS: tuple[str, ...] = ("-started_at", "started_at", "-anomaly_score", "severity")
RISK_LEVEL_NAMES: tuple[str, ...] = ("low", "moderate", "high", "very_high")

# Literal[<tuple>] is Literal[<its items>]: the enumerations are defined once, next to the code that owns them.
Severity = Literal[SEVERITY_LEVELS]  # type: ignore[valid-type]
SensorType = Literal[SENSOR_TYPES]  # type: ignore[valid-type]
SensorStatus = Literal[SENSOR_STATUSES]  # type: ignore[valid-type]
AnomalyStatus = Literal[ANOMALY_STATUSES]  # type: ignore[valid-type]
AssetType = Literal[ASSET_TYPES]  # type: ignore[valid-type]
AssetStatus = Literal[(*ASSET_STATUSES, STATUS_NOT_MONITORED)]  # type: ignore[valid-type]
AnomalySort = Literal[ANOMALY_SORTS]  # type: ignore[valid-type]
RiskLevel = Literal[RISK_LEVEL_NAMES]  # type: ignore[valid-type]
ReadingShape = Literal["records", "columns"]
AnomalyInclude = Literal["nearby_assets"]

Timestamp = Annotated[str, Field(pattern=ISO_Z_PATTERN, examples=["2026-10-01T04:00:00Z"])]


# --- canonical JSON ---------------------------------------------------------------------------------------------
def _shortest_numbers(node: Any) -> Any:
    """Copy of a JSON value in which integral floats are integers (60.0 -> 60), as JSON.stringify writes them."""
    if isinstance(node, float):
        return int(node) if node.is_integer() else node
    if isinstance(node, dict):
        return {key: _shortest_numbers(value) for key, value in node.items()}
    if isinstance(node, list | tuple):
        return [_shortest_numbers(value) for value in node]
    return node


def canonical_json(content: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, compact separators, shortest numbers, UTF-8, no NaN, no line breaks."""
    return json.dumps(
        _shortest_numbers(content), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class CanonicalJSONResponse(JSONResponse):
    """JSON response with deterministic bytes (see ``canonical_json``)."""

    def render(self, content: Any) -> bytes:
        """Encode the payload."""
        return canonical_json(content)


# --- base -------------------------------------------------------------------------------------------------------
class ApiModel(BaseModel):
    """Base of every model: unknown keys are an error."""

    model_config = ConfigDict(extra="forbid")


class ErrorDetail(ApiModel):
    """Body of a 401, 404, 413 or 503 answer."""

    detail: str


# Documentation of the error answers shared by the endpoints (`responses=` of a route).
UNAVAILABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {"model": ErrorDetail, "description": "The database is unavailable or holds no analysed data yet."}
}
NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "No record with that id."}
}
LOOKUP_RESPONSES: dict[int | str, dict[str, Any]] = {**NOT_FOUND_RESPONSE, **UNAVAILABLE_RESPONSE}


def inline_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema of a model with its nested definitions inlined (usable anywhere in an OpenAPI document)."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node

    return resolve(schema)


# --- service ----------------------------------------------------------------------------------------------------
class DataWindow(ApiModel):
    """First and last timestamp of the analysed window."""

    start: Timestamp
    end: Timestamp


class AssetHealthCounts(ApiModel):
    """Number of assets per status of the Derived Asset Health Score at `as_of`."""

    as_of: Timestamp
    normal: int
    watch: int
    at_risk: int
    critical: int
    not_monitored: int


class ServiceHealth(ApiModel):
    """Health of the service (not of the assets)."""

    status: Literal["ok"]
    service: str
    version: str
    database: Literal["ok"]
    postgis: str
    data_window: DataWindow
    asset_health: AssetHealthCounts
    note: str


class ServiceDegraded(ApiModel):
    """Answer of `/health` when the database is unreachable or holds no analysed data."""

    status: Literal["degraded"]
    service: str
    database: Literal["unavailable", "ok"]
    detail: str | None = None


class StudyArea(ApiModel):
    """The one study area of the database; `bbox` is west, south, east, north."""

    slug: str
    name: str
    bbox: list[float]
    center: list[float]
    timezone: str
    utm_srid: int


class TimeInfo(ApiModel):
    """The time axis of the latest detection run."""

    start: Timestamp
    end: Timestamp
    step_minutes: float
    count: int


class Labels(ApiModel):
    """Mandatory labels of the simulated and derived components."""

    sensor_data: str
    detection: str
    health: str
    buildings: str
    water_network: str
    playback: str


class Thresholds(ApiModel):
    """Warning and critical limits of a (sensor type, placement) class; null = no limit on that side."""

    warn_low: float | None
    warn_high: float | None
    crit_low: float | None
    crit_high: float | None


class PlacementThresholds(Thresholds):
    """Limits of a placement with its description."""

    description: str | None


class SensorTypeInfo(ApiModel):
    """Unit, label and placements of a sensor type."""

    unit: str
    label: str
    placements: dict[str, PlacementThresholds]


class HealthInfo(ApiModel):
    """How the Derived Asset Health Score is computed."""

    formula: str
    window_days: int
    half_life_hours: float
    at_risk_below: int
    bands: dict[str, int]


class RiskInfo(ApiModel):
    """Parameters of the risk-zone layer."""

    hex_edge_m: float
    bandwidth_m: float
    half_life_hours: float
    reference: float
    levels: dict[str, float]


class SpatialInfo(ApiModel):
    """Parameters of the proximity analysis and of the co-occurrence clustering."""

    proximity_radius_m: float
    cluster_eps_m: float
    cluster_eps_hours: float
    cluster_min_points: int
    cluster_min_sensors: int


class Counts(ApiModel):
    """Row counts of the database."""

    assets: int
    real_assets: int
    simulated_assets: int
    monitored_assets: int
    sensors: int
    readings: int
    anomalies: int
    assets_by_type: dict[str, int]
    sensors_by_type: dict[str, int]
    building_height_sources: dict[str, int]


class DataSource(ApiModel):
    """Provenance of one data source (kind: real, simulated or derived)."""

    source_id: str
    name: str
    kind: Literal["real", "simulated", "derived"]
    provider: str | None
    url: str | None
    license: str | None
    attribution_text: str | None
    vintage: str | None
    retrieved_at: Timestamp | None
    notes: str | None


class DetectionRun(ApiModel):
    """The latest detection run with its parameters and its self-consistency metrics."""

    run_id: int
    finished_at: Timestamp
    params: dict[str, Any]
    metrics: dict[str, Any]
    evaluation_note: str


class Meta(ApiModel):
    """Everything a client needs to label and interpret the other responses."""

    service: str
    version: str
    data_notice: str
    study_area: StudyArea
    time: TimeInfo
    labels: Labels
    sensor_types: dict[str, SensorTypeInfo]
    anomaly_types: dict[str, str]
    severity_levels: list[str]
    health: HealthInfo
    risk: RiskInfo
    spatial: SpatialInfo
    counts: Counts
    data_sources: list[DataSource]
    detection_run: DetectionRun


class Statistics(ApiModel):
    """Key figures at `as_of`; the anomaly breakdowns count the anomalies that had started by then."""

    as_of: Timestamp
    data_notice: str
    total_assets: int
    real_assets: int
    simulated_assets: int
    monitored_assets: int
    total_sensors: int
    active_sensors: int
    offline_sensors: int
    warning_sensors: int
    active_anomalies: int
    critical_alerts: int
    assets_at_risk: int
    anomalies_to_date: int
    anomalies_by_severity: dict[str, int]
    anomalies_by_sensor_type: dict[str, int]
    assets_by_type: dict[str, int]


# --- GeoJSON ----------------------------------------------------------------------------------------------------
class Geometry(ApiModel):
    """GeoJSON geometry (WGS84, coordinates rounded to 6 decimals)."""

    type: str
    coordinates: list[Any]


class Feature(ApiModel):
    """GeoJSON feature with free-form properties."""

    type: Literal["Feature"]
    geometry: Geometry
    properties: dict[str, Any]


class FeatureCollection(ApiModel):
    """GeoJSON feature collection with free-form properties."""

    type: Literal["FeatureCollection"]
    features: list[Feature]


class AssetProperties(ApiModel):
    """Properties of an asset feature.

    `health_score`, `status` and `anomaly_count` are the values at the end of the analysed window. The
    type-specific keys are present only on that asset type; OSM `addr:*` tags are added when the source
    feature has them. `distance_m` is present on the results of the proximity queries.
    """

    model_config = ConfigDict(extra="allow")

    asset_id: str
    asset_type: AssetType
    category: str
    name: str | None
    is_simulated: bool
    source_id: str
    monitored: bool
    sensor_count: int
    sensor_types: list[SensorType]
    anomaly_count: int
    health_score: int | None
    status: AssetStatus
    centroid: list[float]
    # building
    height_m: float | None = None
    height_source: str | None = None
    building_type: str | None = None
    levels: float | None = None
    footprint_m2: float | None = None
    # road
    highway_class: str | None = None
    surface: str | None = None
    lanes: int | None = None
    # road, bridge
    length_m: float | None = None
    # bridge
    structure_kind: str | None = None
    nbi: dict[str, Any] | None = None
    # water_main
    host_road_id: str | None = None
    # proximity queries
    distance_m: float | None = None


class AssetFeature(ApiModel):
    """One infrastructure asset as a GeoJSON feature."""

    type: Literal["Feature"]
    geometry: Geometry
    properties: AssetProperties


class AssetCollection(ApiModel):
    """Assets as a GeoJSON feature collection."""

    type: Literal["FeatureCollection"]
    features: list[AssetFeature]
    numberMatched: int
    numberReturned: int


class HealthComponents(ApiModel):
    """The four penalties that explain a health score."""

    frequency_penalty: float
    severity_penalty: float
    reading_penalty: float
    sensor_penalty: float


class AssetHealthNow(ApiModel):
    """Derived Asset Health Score of one asset at `as_of`; no score for an asset without sensors."""

    score: int | None
    status: AssetStatus
    components: HealthComponents | None


class Provenance(ApiModel):
    """Where the asset and its recorded attributes come from."""

    is_simulated: bool
    sources: list[DataSource]
    attributes: dict[str, Any]


class SensorLatest(ApiModel):
    """The reading that is current at `as_of`."""

    ts: Timestamp
    value: float
    status: Literal["ok", "suspect"]
    expected: float | None
    robust_z: float | None


class SensorItem(ApiModel):
    """One sensor with its status and current reading at `as_of`."""

    sensor_id: str
    asset_id: str
    asset_name: str | None
    asset_type: AssetType
    sensor_type: SensorType
    placement: str
    unit: str
    description: str | None
    is_simulated: bool
    source: str
    lon: float
    lat: float
    status: SensorStatus
    latest: SensorLatest | None
    anomaly_count: int


class SensorList(ApiModel):
    """A page of sensors."""

    total: int
    limit: int
    offset: int
    as_of: Timestamp
    items: list[SensorItem]


class ScoreComponents(ApiModel):
    """Components of the anomaly score (each 0..1)."""

    magnitude: float
    duration: float
    threshold: float


class NearbyAsset(ApiModel):
    """An asset near an anomaly."""

    asset_id: str
    name: str | None
    asset_type: AssetType
    distance_m: float


class AnomalyItem(ApiModel):
    """One detected anomaly; `status` is evaluated at `as_of`."""

    anomaly_id: str
    sensor_id: str
    asset_id: str
    asset_name: str | None
    asset_type: AssetType
    sensor_type: SensorType
    placement: str
    anomaly_type: str
    anomaly_label: str
    started_at: Timestamp
    ended_at: Timestamp
    peak_at: Timestamp
    duration_hours: int
    observed_value: float
    expected_value: float
    unit: str
    robust_z: float
    anomaly_score: float
    score_components: ScoreComponents
    severity: Severity
    detection_method: str
    explanation: str
    status: AnomalyStatus
    is_simulated: bool
    cluster_id: int | None
    lon: float
    lat: float
    nearby_asset_count: int
    nearby_assets: list[NearbyAsset] | None = Field(default=None, description="Only with include=nearby_assets.")


class AnomalyList(ApiModel):
    """A page of anomalies."""

    total: int
    limit: int
    offset: int
    as_of: Timestamp
    data_notice: str
    items: list[AnomalyItem]


class ClusterProperties(ApiModel):
    """A co-occurrence cluster of anomalies (descriptive, not a causal finding)."""

    cluster_id: int
    n_anomalies: int
    n_sensors: int
    n_assets: int
    sensor_types: list[SensorType]
    max_severity: Severity
    first_started_at: Timestamp
    last_ended_at: Timestamp
    anomaly_ids: list[str]


class AnomalyDetail(AnomalyItem):
    """An anomaly with the assets around it and its cluster."""

    nearby_assets: list[NearbyAsset]
    cluster: ClusterProperties | None


class AssetDetail(AssetFeature):
    """An asset feature with its health at `as_of`, its sensors, recent anomalies and provenance."""

    as_of: Timestamp
    health: AssetHealthNow
    sensors: list[SensorItem]
    recent_anomalies: list[AnomalyItem]
    provenance: Provenance


class AssetHealthSeries(ApiModel):
    """Health of one monitored asset over time, one element per time step (columnar)."""

    asset_id: str
    start: Timestamp
    step_minutes: float
    count: int
    health_score: list[int | None]
    status: str = Field(description="One character per time step: n normal, w watch, r at_risk, c critical.")
    frequency_penalty: list[float | None]
    severity_penalty: list[float | None]
    reading_penalty: list[float | None]
    sensor_penalty: list[float | None]
    active_anomalies: list[int | None]
    sensors_reporting: list[int | None]
    sensors_total: int


class Baseline(ApiModel):
    """Robust scale of the sensor's baseline in the detector's work domain, and the floor that applies."""

    scale: float | None
    floor: float | None


class SensorDetail(SensorItem):
    """A sensor with its limits, baseline and anomalies."""

    as_of: Timestamp
    thresholds: Thresholds
    baseline: Baseline | None
    anomalies: list[AnomalyItem]


class ReadingRecord(ApiModel):
    """One reading with the detector's output for it."""

    ts: Timestamp
    value: float
    status: Literal["ok", "suspect"]
    expected: float | None
    expected_low: float | None
    expected_high: float | None
    robust_z: float | None
    flagged: bool


class ReadingsRecords(ApiModel):
    """Readings of one sensor as a list of records (`shape=records`)."""

    sensor_id: str
    sensor_type: SensorType
    unit: str
    placement: str
    is_simulated: bool
    source: str
    data_notice: str
    start: Timestamp
    end: Timestamp
    count: int
    readings: list[ReadingRecord]


class ReadingsColumns(ApiModel):
    """Readings of one sensor as arrays aligned to the time grid (`shape=columns`); null = no reading."""

    sensor_id: str
    sensor_type: SensorType
    unit: str
    placement: str
    is_simulated: bool
    source: str
    data_notice: str
    thresholds: Thresholds
    start: Timestamp
    step_minutes: float
    count: int
    value: list[float | None]
    expected: list[float | None]
    expected_low: list[float | None]
    expected_high: list[float | None]
    robust_z: list[float | None]
    flagged: list[int] = Field(description="Indices of the flagged readings.")


class SimulationEvent(ApiModel):
    """One event of the simulator's ground truth."""

    event_id: int
    sensor_id: str | None
    asset_id: str | None
    sensor_type: str | None
    event_type: str
    is_anomaly: bool
    started_at: Timestamp
    ended_at: Timestamp
    magnitude: float | None
    description: str | None


class SimulationEventList(ApiModel):
    """The simulator's ground truth."""

    total: int
    items: list[SimulationEvent]


# --- spatial ----------------------------------------------------------------------------------------------------
class SensorInArea(SensorItem):
    """A sensor with its distance from the asset geometry."""

    distance_m: float


class SensorsInArea(ApiModel):
    """Sensors within a buffer around an asset."""

    asset_id: str
    buffer_m: float
    items: list[SensorInArea]


class DensityProperties(ApiModel):
    """Anomaly density of one hexagonal cell."""

    cell_id: str
    anomaly_count: int
    weighted_severity: int


class DensityFeature(ApiModel):
    """Hexagonal cell with its anomaly density."""

    type: Literal["Feature"]
    geometry: Geometry
    properties: DensityProperties


class DensityCollection(ApiModel):
    """Anomaly density per hexagonal cell."""

    type: Literal["FeatureCollection"]
    features: list[DensityFeature]


class RiskProperties(ApiModel):
    """Risk of one hexagonal cell at `as_of`."""

    cell_id: str
    risk_score: float
    risk_level: RiskLevel
    anomaly_count: int


class RiskFeature(ApiModel):
    """Hexagonal cell with its risk score."""

    type: Literal["Feature"]
    geometry: Geometry
    properties: RiskProperties


class RiskCollection(ApiModel):
    """Risk zones at `as_of`."""

    type: Literal["FeatureCollection"]
    features: list[RiskFeature]
    as_of: Timestamp


class ClusterFeature(ApiModel):
    """Hull of one co-occurrence cluster."""

    type: Literal["Feature"]
    geometry: Geometry
    properties: ClusterProperties


class ClusterCollection(ApiModel):
    """Co-occurrence clusters of anomalies."""

    type: Literal["FeatureCollection"]
    features: list[ClusterFeature]


# --- playback ---------------------------------------------------------------------------------------------------
class PlaybackSensor(ApiModel):
    """Value and status of one sensor at every time step."""

    values: list[float | None]
    status: str = Field(description="One character per time step: n normal, w warning, a anomaly, o offline.")


class PlaybackAsset(ApiModel):
    """Health of one monitored asset at every time step."""

    health: list[int]
    status: str = Field(description="One character per time step: n normal, w watch, r at_risk, c critical.")


class PlaybackZone(ApiModel):
    """Risk score (integer 0..100) of one hexagonal cell at every time step."""

    risk: list[int]


class PlaybackStats(ApiModel):
    """The time-dependent key figures at every time step."""

    active_sensors: list[int]
    offline_sensors: list[int]
    warning_sensors: list[int]
    active_anomalies: list[int]
    critical_alerts: list[int]
    assets_at_risk: list[int]


class Playback(ApiModel):
    """Everything that changes with time, for every time step of the analysed window."""

    data_notice: str
    run_id: int
    timestamps: list[Timestamp]
    sensors: dict[str, PlaybackSensor]
    assets: dict[str, PlaybackAsset]
    zones: dict[str, PlaybackZone]
    stats: PlaybackStats


# --- ingestion --------------------------------------------------------------------------------------------------
class IngestReading(ApiModel):
    """One reading sent by a sensor gateway; `ts` needs a UTC offset."""

    sensor_id: str = Field(min_length=1, max_length=64, examples=["VIB-001"])
    ts: datetime = Field(examples=["2026-10-01T05:00:00Z"])
    value: float = Field(examples=[1.42])
    unit: str = Field(min_length=1, max_length=16, examples=["mm/s"])


class IngestRequest(ApiModel):
    """A batch of readings."""

    readings: list[IngestReading] = Field(max_length=MAX_INGEST_READINGS)


class IngestResponse(ApiModel):
    """Outcome of an ingestion call: readings stored, readings refused and why."""

    accepted: int
    rejected: int
    reasons: dict[str, int]
