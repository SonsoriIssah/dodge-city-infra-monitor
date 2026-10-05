"""Service endpoints: ``/health``, ``/meta``, ``/statistics``."""

from __future__ import annotations

import logging

import psycopg
from fastapi import APIRouter, Request
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import HEALTH_CHECK_TIMEOUT_S, AppSettings, AsOf, Axis, Conn, get_database
from backend.app.queries import meta as meta_queries
from backend.app.queries.common import DataNotReadyError
from backend.app.schemas import CanonicalJSONResponse
from pipeline.analysis import status
from pipeline.config import SERVICE_NAME

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Service"])

NOT_INITIALISED = "the database is not initialised; run the pipeline (python run_pipeline.py) first"


@router.get(
    "/health",
    response_model=schemas.ServiceHealth,
    summary="Service health",
    description=(
        "Health of the service, not of the assets: database connectivity, PostGIS version, the analysed data "
        "window and how many assets are in each status of the Derived Asset Health Score at the end of that "
        "window. Answers 503 with `status: degraded` when the database is unreachable or holds no analysed data."
    ),
    responses={503: {"model": schemas.ServiceDegraded, "description": "The service cannot answer from the database."}},
)
def get_health(request: Request) -> Response:
    """Report service health; never raises for a database problem."""
    degraded = {"status": "degraded", "service": SERVICE_NAME}
    try:
        with get_database(request).connection(read_only=True, timeout=HEALTH_CHECK_TIMEOUT_S) as conn:
            body = meta_queries.service_health(conn, status.load_time_axis(conn))
    except psycopg.OperationalError as exc:  # includes PoolTimeout: no connection within the time limit
        logger.warning("health check: database unavailable (%s)", type(exc).__name__)
        return CanonicalJSONResponse({**degraded, "database": "unavailable"}, status_code=503)
    except (status.NoDetectionRunError, DataNotReadyError) as exc:
        return CanonicalJSONResponse({**degraded, "database": "ok", "detail": str(exc)}, status_code=503)
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        return CanonicalJSONResponse({**degraded, "database": "ok", "detail": NOT_INITIALISED}, status_code=503)
    return CanonicalJSONResponse(body)


@router.get(
    "/meta",
    response_model=schemas.Meta,
    summary="Study area, time axis, labels, thresholds, formulas, counts and data provenance",
    description=(
        "Everything a client needs to label and interpret the other responses: the study area, the time axis "
        "of the analysed window, the mandatory labels of simulated and derived components, sensor types with "
        "their warning and critical limits, anomaly types, the health-score and risk-zone parameters, row "
        "counts, the provenance of every data source and the latest detection run with its self-consistency "
        "metrics."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_meta(conn: Conn, axis: Axis, settings: AppSettings) -> Response:
    """Describe the dataset."""
    return CanonicalJSONResponse(meta_queries.meta(conn, settings, axis))


@router.get(
    "/statistics",
    response_model=schemas.Statistics,
    summary="Key figures at a point in time",
    description=(
        "The dashboard's key figures evaluated at `as_of`: assets (total, recorded, simulated, monitored), "
        "sensors (total, reporting, offline, warning), active anomalies, critical alerts (active anomalies of "
        "severity critical) and assets at risk (monitored assets with a Derived Asset Health Score below 70). "
        "`anomalies_to_date` and its two breakdowns count the anomalies that had started by `as_of`."
    ),
    responses=schemas.UNAVAILABLE_RESPONSE,
)
def get_statistics(conn: Conn, axis: Axis, as_of: AsOf = None) -> Response:
    """Evaluate the key figures at ``as_of``."""
    return CanonicalJSONResponse(meta_queries.statistics(conn, axis.resolve(as_of)))
