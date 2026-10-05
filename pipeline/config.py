"""Project settings (environment driven) and repository paths.

Every tunable of the pipeline, the detector and the API is an environment variable documented in
``.env.example``. Values are read from ``ROOT/.env`` (by absolute path) and real environment variables
override that file.

Field names are the environment variable names (``settings.SIM_SEED``); a lower-case spelling
(``settings.sim_seed``) resolves to the same field. Parsed helpers have their own names (``bbox``, ``tz``,
``dsn``, ``sim_start_utc``, ``time_axis()``, ...).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT: Path = Path(__file__).resolve().parent.parent
ENV_FILE: Path = ROOT / ".env"
DATA_DIR: Path = ROOT / "data"
RAW_DIR: Path = DATA_DIR / "raw"
PROCESSED_DIR: Path = DATA_DIR / "processed"
SQL_DIR: Path = ROOT / "sql"
MIGRATIONS_DIR: Path = SQL_DIR / "migrations"
QUERIES_DIR: Path = SQL_DIR / "queries"
DASHBOARD_DIR: Path = ROOT / "dashboard"
SNAPSHOT_DIR: Path = DASHBOARD_DIR / "data" / "snapshot"

SERVICE_NAME = "dodge-city-infra-monitor"
SERVICE_VERSION = "1.0.0"
DATA_NOTICE = (
    "Simulated sensor data and prototype anomaly detection. Not a record of real infrastructure condition."
)

TEST_DATABASE_NAME = "infra_test"


class BBox(NamedTuple):
    """Bounding box in API order: west, south, east, north (WGS84 degrees)."""

    west: float
    south: float
    east: float
    north: float

    @property
    def center(self) -> tuple[float, float]:
        """Centre as (lon, lat)."""
        return ((self.west + self.east) / 2.0, (self.south + self.north) / 2.0)

    @property
    def overpass(self) -> tuple[float, float, float, float]:
        """The same box in Overpass order: south, west, north, east."""
        return (self.south, self.west, self.north, self.east)

    def ring(self) -> list[list[float]]:
        """Closed counter-clockwise polygon ring of the rectangle ([lon, lat] pairs)."""
        return [
            [self.west, self.south],
            [self.east, self.south],
            [self.east, self.north],
            [self.west, self.north],
            [self.west, self.south],
        ]

    def contains(self, lon: float, lat: float) -> bool:
        """True when the point lies inside or on the edge of the box."""
        return self.west <= lon <= self.east and self.south <= lat <= self.north


class Settings(BaseSettings):
    """All configuration of the pipeline and the API. See ``.env.example`` for the documentation of each value."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database -------------------------------------------------------------------------------------------
    DATABASE_URL: str = Field(default="", repr=False)
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5433
    POSTGRES_HOST_PORT: int = 5433  # used only by docker-compose port publishing
    POSTGRES_DB: str = "infra"
    POSTGRES_USER: str = "infra"
    POSTGRES_PASSWORD: str = Field(default="", repr=False)
    TEST_DATABASE_URL: str = Field(default="", repr=False)

    # --- Study area -----------------------------------------------------------------------------------------
    STUDY_AREA_SLUG: str = "dodge-city-downtown"
    STUDY_AREA_NAME: str = "Downtown Dodge City, Kansas"
    STUDY_AREA_BBOX: str = "37.745,-100.030,37.762,-100.005"  # south,west,north,east (Overpass order)
    STUDY_AREA_UTM_SRID: int = 32614
    TIMEZONE: str = "America/Chicago"
    # Census place whose boundary is drawn as context ("city limits"): a GEOID, or "auto" for the incorporated
    # place that contains the centre of the study area. (An empty value in .env means "use the default".)
    TIGER_PLACE_GEOID: str = "2018250"

    # --- Sensor source and simulation -----------------------------------------------------------------------
    SENSOR_SOURCE: str = "simulated"
    SIM_SEED: int = 42
    SIM_START: datetime = datetime.fromisoformat("2026-09-01T00:00:00-05:00")
    SIM_DAYS: int = Field(default=30, ge=1, le=366)
    SIM_STEP_MINUTES: int = Field(default=60, ge=1, le=1440)
    SIM_ANOMALY_EVENTS: int = Field(default=40, ge=0)
    SIM_DROPOUT_RATE: float = Field(default=0.12, ge=0.0, le=1.0)
    SIM_WATER_MAINS: int = Field(default=28, ge=0)
    HTTP_SOURCE_URL: str = ""

    # --- Detection ------------------------------------------------------------------------------------------
    DETECT_Z_STRONG: float = 6.0
    DETECT_Z_MIN: float = 3.0
    DETECT_ROLLING_HOURS: int = Field(default=6, ge=1)
    DETECT_IFOREST_THRESHOLD: float = 0.62
    DETECT_MERGE_GAP_HOURS: int = Field(default=2, ge=0)

    # --- Spatial analysis -----------------------------------------------------------------------------------
    PROXIMITY_RADIUS_M: float = 100.0
    CLUSTER_EPS_M: float = 200.0
    CLUSTER_EPS_HOURS: float = 48.0
    CLUSTER_MIN_POINTS: int = Field(default=3, ge=1)
    CLUSTER_MIN_SENSORS: int = Field(default=3, ge=1)
    RISK_HEX_EDGE_M: float = Field(default=150.0, gt=0)
    RISK_BANDWIDTH_M: float = Field(default=250.0, gt=0)
    RISK_HALF_LIFE_HOURS: float = Field(default=72.0, gt=0)
    RISK_REFERENCE: float = Field(default=14.0, gt=0)
    HEALTH_WINDOW_DAYS: int = Field(default=7, ge=1)
    HEALTH_HALF_LIFE_HOURS: float = Field(default=48.0, gt=0)

    # --- API ------------------------------------------------------------------------------------------------
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "*"
    INGEST_API_KEY: str = Field(default="", repr=False)  # empty = ingest endpoint disabled
    SERVE_DASHBOARD: bool = True
    AUTO_SEED: bool = True
    BASEMAP_STYLE_URL: str = "https://tiles.openfreemap.org/styles/dark"

    def __init__(self, **values: Any) -> None:
        """Accept field names in any letter case (``Settings(sim_days=10)`` equals ``Settings(SIM_DAYS=10)``)."""
        fields = type(self).model_fields
        normalised = {(key.upper() if key.upper() in fields else key): value for key, value in values.items()}
        super().__init__(**normalised)

    def __getattr__(self, name: str) -> Any:
        """Resolve ``settings.sim_seed`` to the ``SIM_SEED`` field."""
        upper = name.upper()
        if upper != name and upper in type(self).model_fields:
            return getattr(self, upper)
        return super().__getattr__(name)

    # --- validation -----------------------------------------------------------------------------------------
    @field_validator("STUDY_AREA_BBOX")
    @classmethod
    def _check_bbox(cls, value: str) -> str:
        parse_bbox(value)
        return value

    @field_validator("TIMEZONE")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _make_sim_start_aware(self) -> Settings:
        """A SIM_START without an offset is interpreted in TIMEZONE."""
        if self.SIM_START.tzinfo is None:
            object.__setattr__(self, "SIM_START", self.SIM_START.replace(tzinfo=ZoneInfo(self.TIMEZONE)))
        return self

    # --- parsed helpers: study area -------------------------------------------------------------------------
    @property
    def bbox(self) -> BBox:
        """Study-area bounding box as west, south, east, north floats."""
        return parse_bbox(self.STUDY_AREA_BBOX)

    @property
    def tz(self) -> ZoneInfo:
        """Local time zone of the study area."""
        return ZoneInfo(self.TIMEZONE)

    def study_area_geometry(self) -> dict[str, Any]:
        """GeoJSON Polygon of the study area (the bbox rectangle)."""
        return {"type": "Polygon", "coordinates": [self.bbox.ring()]}

    # --- parsed helpers: time axis --------------------------------------------------------------------------
    @property
    def sim_start_utc(self) -> datetime:
        """First timestamp of the simulation window (aware, UTC)."""
        return self.SIM_START.astimezone(UTC)

    @property
    def sim_step(self) -> timedelta:
        """Sampling step of the simulation."""
        return timedelta(minutes=self.SIM_STEP_MINUTES)

    @property
    def sim_steps(self) -> int:
        """Number of timestamps in the simulation window (720 for 30 days of hourly readings)."""
        return (self.SIM_DAYS * 24 * 60) // self.SIM_STEP_MINUTES

    @property
    def sim_end_utc(self) -> datetime:
        """Last timestamp of the simulation window (aware, UTC, inclusive)."""
        return self.sim_start_utc + (self.sim_steps - 1) * self.sim_step

    def time_axis(self) -> list[datetime]:
        """Every timestamp of the simulation window (aware, UTC), first to last."""
        start, step = self.sim_start_utc, self.sim_step
        return [start + i * step for i in range(self.sim_steps)]

    # --- parsed helpers: database ---------------------------------------------------------------------------
    @property
    def dsn(self) -> str:
        """Connection string: a non-empty DATABASE_URL wins, otherwise it is built from the POSTGRES_* parts."""
        if self.DATABASE_URL.strip():
            return self.DATABASE_URL.strip()
        return make_conninfo(
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            dbname=self.POSTGRES_DB,
            user=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
        )

    @property
    def test_dsn(self) -> str:
        """Connection string of the test database: TEST_DATABASE_URL, else the main DSN with db ``infra_test``."""
        if self.TEST_DATABASE_URL.strip():
            return self.TEST_DATABASE_URL.strip()
        return make_conninfo(self.dsn, dbname=TEST_DATABASE_NAME)

    def dsn_summary(self) -> str:
        """Host, port, database and user of the DSN for log messages (never the password)."""
        return describe_dsn(self.dsn)

    # --- parsed helpers: API --------------------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        """CORS_ORIGINS split on commas ("*" allows every origin)."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def ingest_enabled(self) -> bool:
        """True when INGEST_API_KEY is set (the ingest endpoint is disabled otherwise)."""
        return bool(self.INGEST_API_KEY.strip())


def parse_bbox(value: str) -> BBox:
    """Parse the STUDY_AREA_BBOX string (south,west,north,east) into a BBox (west, south, east, north)."""
    try:
        south, west, north, east = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise ValueError("STUDY_AREA_BBOX must be four numbers: south,west,north,east") from exc
    if not (-90.0 <= south < north <= 90.0):
        raise ValueError("STUDY_AREA_BBOX: need -90 <= south < north <= 90")
    if not (-180.0 <= west < east <= 180.0):
        raise ValueError("STUDY_AREA_BBOX: need -180 <= west < east <= 180")
    return BBox(west=west, south=south, east=east, north=north)


def describe_dsn(dsn: str) -> str:
    """Describe a connection string without its password (safe for logs)."""
    try:
        parts = conninfo_to_dict(dsn)
    except psycopg.Error:  # a malformed DSN must not leak into a log line either
        return "<unparseable DSN>"
    shown = {key: parts.get(key) for key in ("host", "port", "dbname", "user") if parts.get(key) is not None}
    return " ".join(f"{key}={value}" for key, value in shown.items())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings (cached). Call ``get_settings.cache_clear()`` after changing the environment."""
    return Settings()
