"""Ingestion endpoint: ``POST /ingest/readings`` (the door for a sensor gateway other than the simulator).

The readings go through ``pipeline.sensors.ingestion.IngestionService``, the same service the pipeline uses.
Ingesting never runs the detection: re-run ``scripts/detect_anomalies.py`` and ``scripts/analyze_spatial.py``.

Order of the checks: endpoint enabled (404) -> API key (401) -> body size (413) -> body shape (422). The body
is read only after the key was accepted.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader
from pydantic import ValidationError
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import AppSettings, Cache, WriteConn
from backend.app.schemas import CanonicalJSONResponse
from pipeline.models import Reading
from pipeline.sensors.ingestion import IngestionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["Ingestion"])

API_KEY_HEADER = "X-API-Key"
INGEST_SOURCE = "api"
MAX_BODY_BYTES = 4 * 1024 * 1024
DISABLED_DETAIL = "ingestion endpoint is disabled (INGEST_API_KEY not set)"
UNAUTHORIZED_DETAIL = "missing or invalid API key (header X-API-Key)"
TOO_LARGE_DETAIL = (
    f"request body larger than {MAX_BODY_BYTES} bytes; send at most {schemas.MAX_INGEST_READINGS} readings per call"
)

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER, auto_error=False, description="Key configured as INGEST_API_KEY on the server."
)


def _key_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of the presented key with the configured one."""
    return provided is not None and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def _read_body(request: Request) -> bytes:
    """Read the request body, refusing more than ``MAX_BODY_BYTES``."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
        chunks.append(chunk)
    return b"".join(chunks)


async def ingest_payload(
    request: Request, settings: AppSettings, api_key: Annotated[str | None, Security(api_key_header)] = None
) -> schemas.IngestRequest:
    """Authorise the call, then read and validate its body."""
    expected = settings.INGEST_API_KEY.strip()
    if not expected:
        raise HTTPException(status_code=404, detail=DISABLED_DETAIL)
    if not _key_matches(api_key, expected):
        raise HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)
    body = await _read_body(request)
    try:
        return schemas.IngestRequest.model_validate_json(body)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors]) from None


@router.post(
    "/readings",
    response_model=schemas.IngestResponse,
    summary="Store sensor readings from an external source",
    description=(
        "Validates and stores a batch of readings (at most 10000 per call) for registered sensors: an "
        "existing reading of the same sensor and timestamp is replaced. Readings of an unknown sensor, with a "
        "timestamp without UTC offset, a non-finite value or the wrong unit are rejected and counted per "
        "reason; physically implausible values are stored as `suspect`. The endpoint is disabled (404) unless "
        "`INGEST_API_KEY` is set on the server, and it requires that key in the `X-API-Key` header. It does "
        "not run the anomaly detection: re-run `scripts/detect_anomalies.py` and `scripts/analyze_spatial.py` "
        "to analyse new readings."
    ),
    responses={
        401: {"model": schemas.ErrorDetail, "description": "Missing or invalid API key."},
        404: {"model": schemas.ErrorDetail, "description": "The ingestion endpoint is disabled."},
        413: {"model": schemas.ErrorDetail, "description": "The request body is too large."},
        422: {"description": "The body is not a list of readings in the documented shape."},
        **schemas.UNAVAILABLE_RESPONSE,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schemas.inline_json_schema(schemas.IngestRequest)}},
        }
    },
)
def ingest_readings(
    payload: Annotated[schemas.IngestRequest, Depends(ingest_payload)], conn: WriteConn, cache: Cache
) -> Response:
    """Store the readings through the ingestion service."""
    readings = [
        Reading(sensor_id=item.sensor_id, ts=item.ts, value=item.value, unit=item.unit) for item in payload.readings
    ]
    result = IngestionService(conn).ingest(readings, INGEST_SOURCE)
    cache.clear()  # the playback bundle of this process was built from the previous readings
    logger.info("ingest: %d accepted, %d rejected", result.accepted, result.rejected)
    return CanonicalJSONResponse({"accepted": result.accepted, "rejected": result.rejected, "reasons": result.reasons})
