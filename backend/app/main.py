"""Application factory of the API (build contract section 10).

    uvicorn backend.app.main:create_app --factory --host $API_HOST --port $API_PORT

The application starts when the database is down: the connection pool is opened without waiting, requests
that need the database answer 503 until it is reachable, and ``/health`` reports ``degraded``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from backend.app.deps import Database
from backend.app.queries.common import DataNotReadyError, NotFoundError
from backend.app.queries.playback import PlaybackCache
from backend.app.routers import API_ROUTERS
from backend.app.routers.service import NOT_INITIALISED
from backend.app.schemas import CanonicalJSONResponse
from pipeline.analysis.status import NoDetectionRunError
from pipeline.config import DASHBOARD_DIR, DATA_NOTICE, SERVICE_VERSION, Settings, get_settings
from pipeline.logging_utils import setup_logging

logger = logging.getLogger(__name__)

API_TITLE = "Dodge City 3D Urban Infrastructure Monitoring API"
API_DESCRIPTION = f"""
Data layer of a research prototype that monitors urban infrastructure assets in a study area of Dodge City,
Kansas. Real geographic data (OpenStreetMap, FHWA National Bridge Inventory, U.S. Census Bureau TIGERweb,
USGS 3DEP) is stored in PostGIS; a simulated sensor network, a prototype anomaly detection and a derived
spatial analysis run on top of it.

**Data notice: {DATA_NOTICE}**

* **Simulated Sensor Data** - every sensor, reading and sensor status comes from a deterministic simulator
  (`is_simulated: true`). The simulated water mains are not a record of real utilities.
* **Prototype Anomaly Detection** - a retrospective batch analysis: baselines and scales are estimated from the
  whole data window. It is scored against the simulator's injected events, which is a self-consistency check and
  not field validation. It is not a certified infrastructure safety system.
* **Derived Asset Health Score** - computed from simulated sensors and their anomalies; it is not an assessment of
  the condition of any structure. Recorded attributes of real features (for example National Bridge Inventory
  ratings) never feed the score.

**Conventions**

* Timestamps are `YYYY-MM-DDTHH:MM:SSZ` (UTC). Datetime parameters are ISO 8601; a value without an offset is UTC.
* `as_of` selects a point of the analysed window: it is floored to the time step, clamped to the window and echoed
  in the response. Without it an endpoint answers for the end of the window. An anomaly is active while
  `started_at <= as_of <= ended_at`.
* `bbox` is `west,south,east,north` in WGS84 degrees. Geometries are GeoJSON in WGS84.
* Values are rounded to 3 decimals, robust z-scores to 2, scores to 3, coordinates to 6, distances to 0.1 m.
* Errors are `{{"detail": "..."}}` with status 404 (unknown id) or 503 (database unavailable); invalid parameters
  answer 422 with the list of problems.
"""
OPENAPI_TAGS = [
    {"name": "Service", "description": "Service health, dataset description and key figures."},
    {"name": "Assets", "description": "Infrastructure asset registry with the Derived Asset Health Score."},
    {"name": "Sensors", "description": "Sensors and their historical readings (Simulated Sensor Data)."},
    {"name": "Anomalies", "description": "Results of the Prototype Anomaly Detection; the simulator's ground truth."},
    {"name": "Spatial analysis", "description": "PostGIS proximity queries, anomaly density, risk zones, clusters."},
    {"name": "Map layers", "description": "Base-map layers of real geographic data as GeoJSON."},
    {"name": "Playback", "description": "Time-playback bundle of the analysed window."},
    {"name": "Ingestion", "description": "Entry point for readings from a source other than the simulator."},
]
GZIP_MINIMUM_BYTES = 1024
GZIP_LEVEL = 6
CORS_MAX_AGE_S = 600
CONFIG_JS_PATH = "/config.js"
DATABASE_UNAVAILABLE = "database unavailable"


def register_mimetypes() -> None:
    """Content types of the dashboard files (the Windows registry may map ``.js`` to ``text/plain``)."""
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("application/geo+json", ".geojson")


def config_js(settings: Settings) -> str:
    """Runtime configuration of the dashboard when the API serves it: always the API on the same origin."""
    config = {"mode": "api", "apiBaseUrl": "", "basemapStyleUrl": settings.BASEMAP_STYLE_URL}
    return f"window.DCIM_CONFIG = {json.dumps(config, ensure_ascii=False)};\n"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the connection pool without waiting for the database; close it on shutdown."""
    database: Database = app.state.database
    database.open()
    try:
        yield
    finally:
        database.close()


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _install_error_handlers(app: FastAPI) -> None:
    """Map database and lookup errors to JSON answers: 503, 404 and a JSON 500."""

    def database_unavailable(request: Request, exc: Exception) -> Response:
        logger.warning("%s %s: database unavailable: %s", request.method, request.url.path, _first_line(exc))
        return CanonicalJSONResponse({"detail": DATABASE_UNAVAILABLE}, status_code=503)

    def not_ready(request: Request, exc: Exception) -> Response:
        return CanonicalJSONResponse({"detail": str(exc)}, status_code=503)

    def not_initialised(request: Request, exc: Exception) -> Response:
        return CanonicalJSONResponse({"detail": NOT_INITIALISED}, status_code=503)

    def not_found(request: Request, exc: Exception) -> Response:
        detail = exc.detail if isinstance(exc, NotFoundError) else str(exc)
        return CanonicalJSONResponse({"detail": detail}, status_code=404)

    def internal_error(request: Request, exc: Exception) -> Response:
        logger.error("%s %s failed: %s: %s", request.method, request.url.path, type(exc).__name__, _first_line(exc))
        return CanonicalJSONResponse({"detail": "internal server error"}, status_code=500)

    # psycopg_pool.PoolTimeout and PoolClosed are OperationalError subclasses.
    app.add_exception_handler(psycopg.OperationalError, database_unavailable)
    app.add_exception_handler(NoDetectionRunError, not_ready)
    app.add_exception_handler(DataNotReadyError, not_ready)
    app.add_exception_handler(psycopg.errors.UndefinedTable, not_initialised)
    app.add_exception_handler(psycopg.errors.InvalidSchemaName, not_initialised)
    app.add_exception_handler(NotFoundError, not_found)
    app.add_exception_handler(Exception, internal_error)


def _api_root_segments(app: FastAPI) -> set[str]:
    """First path segment of every registered route (``assets``, ``docs``, ``config.js``, ...)."""
    return {segment for route in app.routes if (segment := getattr(route, "path", "").strip("/").split("/")[0])}


def _mount_dashboard(app: FastAPI, settings: Settings, directory: Path) -> None:
    """Serve the dashboard at ``/`` after every API route, with its runtime configuration at ``/config.js``."""
    if not directory.is_dir():
        logger.warning("SERVE_DASHBOARD is set but %s does not exist: the dashboard is not served", directory)
        return
    script = config_js(settings)

    @app.get(CONFIG_JS_PATH, include_in_schema=False)
    def get_config_js() -> Response:
        """Dashboard runtime configuration (shadows the static ``dashboard/config.js``)."""
        return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    shadowed = sorted((_api_root_segments(app) - {CONFIG_JS_PATH.strip("/")}) & {p.name for p in directory.iterdir()})
    if shadowed:
        logger.warning("dashboard entries hidden by API routes of the same name: %s", ", ".join(shadowed))
    app.mount("/", StaticFiles(directory=str(directory), html=True), name="dashboard")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application for the given settings (default: the process-wide settings from the environment)."""
    settings = settings if settings is not None else get_settings()
    setup_logging()
    register_mimetypes()

    app = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version=SERVICE_VERSION,
        openapi_tags=OPENAPI_TAGS,
        default_response_class=CanonicalJSONResponse,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.database = Database(settings)
    app.state.playback_cache = PlaybackCache()

    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_BYTES, compresslevel=GZIP_LEVEL)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "If-None-Match", "X-API-Key"],
        expose_headers=["ETag"],
        max_age=CORS_MAX_AGE_S,
    )
    _install_error_handlers(app)
    for router in API_ROUTERS:
        app.include_router(router)
    if settings.SERVE_DASHBOARD:
        _mount_dashboard(app, settings, DASHBOARD_DIR)

    logger.info(
        "API ready: database %s; dashboard %s; ingest endpoint %s; CORS origins %s",
        settings.dsn_summary(),
        "served at /" if settings.SERVE_DASHBOARD else "not served",
        "enabled" if settings.ingest_enabled else "disabled",
        ", ".join(settings.cors_origin_list),
    )
    return app
