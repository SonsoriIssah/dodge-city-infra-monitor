"""Playback endpoint: ``/playback`` (cached bundle, ETag)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from backend.app import schemas
from backend.app.deps import Cache, Conn
from backend.app.queries.playback import etag_matches

router = APIRouter(tags=["Playback"])

JSON_MEDIA_TYPE = "application/json"


@router.get(
    "/playback",
    response_model=schemas.Playback,
    summary="Time-playback bundle of the whole analysed window",
    description=(
        "Everything that changes with time, for every time step: per sensor the value (null while offline) and "
        "a status character (n normal, w warning, a anomaly, o offline); per monitored asset the Derived Asset "
        "Health Score and a status character (n normal, w watch, r at_risk, c critical); per hexagonal cell "
        "that is ever above zero the risk score as an integer; and the key figures of `/statistics`. Element "
        "`i` of every series belongs to `timestamps[i]`. Playback replays a retrospective analysis of "
        "simulated readings: it does not reproduce what a streaming detector would have known at that hour. "
        "The bundle is built once per detection run and served with an ETag; send `If-None-Match` to get 304."
    ),
    responses={
        304: {"description": "The bundle has not changed since the ETag sent in If-None-Match."},
        **schemas.UNAVAILABLE_RESPONSE,
    },
)
def get_playback(request: Request, conn: Conn, cache: Cache) -> Response:
    """Serve the cached playback bundle."""
    bundle = cache.get(conn)
    headers = {"ETag": bundle.etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if etag_matches(request.headers.get("if-none-match"), bundle.etag):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(bundle.gzip_body, media_type=JSON_MEDIA_TYPE, headers=headers)
    return Response(bundle.body, media_type=JSON_MEDIA_TYPE, headers=headers)
