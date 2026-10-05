"""OpenStreetMap download through the Overpass API (one combined query, cached in ``data/raw/osm.json``).

Usage policy of the public Overpass instances that this module follows: a descriptive User-Agent, one
combined query per refresh, never refetch unless asked (``--refresh``), and at least 30 s of back-off after
HTTP 429/504 before a single retry, then the next mirror.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from pipeline.config import BBox
from pipeline.gis.sources import (
    OSM_ATTRIBUTION_DOCS,
    SOURCE_REGISTRY,
    file_mtime_iso,
    http_client,
    make_entry,
    read_json,
    sha256_file,
    utc_now_iso,
    write_bytes_atomic,
)

logger = logging.getLogger(__name__)

SOURCE_ID = "osm"
FILE_NAME = "osm.json"
OVERPASS_ENDPOINTS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
BUSY_STATUS = frozenset({429, 504})
MIN_BACKOFF_S = 30.0
MAX_BACKOFF_S = 120.0
ATTEMPTS_PER_ENDPOINT = 2  # the first try plus one retry after a back-off
QUERY_TIMEOUT_S = 120


class OverpassError(RuntimeError):
    """No Overpass endpoint returned usable data."""


def build_query(bbox: BBox) -> str:
    """The single combined Overpass QL query for the study area.

    ``out geom`` returns, for every way, its tags, its node ids (needed to merge contiguous bridge ways) and
    its full geometry (ways are NOT clipped to the box by Overpass; clipping happens in processing).
    """
    south, west, north, east = bbox.overpass
    return (
        f"[out:json][timeout:{QUERY_TIMEOUT_S}][maxsize:67108864][bbox:{south},{west},{north},{east}];\n"
        "(\n"
        '  way["building"];\n'
        '  way["highway"];\n'
        '  way["railway"];\n'
        '  way["bridge"];\n'
        '  way["power"~"^(substation|line|minor_line)$"];\n'
        '  node["power"="substation"];\n'
        '  node["highway"="street_lamp"];\n'
        ");\n"
        "out geom;\n"
    )


def validate_payload(payload: Any) -> list[dict[str, Any]]:
    """Return the element list of an Overpass response or raise ``ValueError`` when it is not usable."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("response has no 'elements' list")
    remark = str(payload.get("remark", ""))
    if "error" in remark.lower() or "timed out" in remark.lower():
        raise ValueError(f"Overpass reported a runtime problem: {remark[:200]}")
    if not payload["elements"]:
        raise ValueError("response contains no elements")
    return payload["elements"]


def _backoff_seconds(response: httpx.Response) -> float:
    """Back-off before retrying a busy endpoint: Retry-After when given, never less than 30 s."""
    try:
        requested = float(response.headers.get("Retry-After", "0"))
    except ValueError:
        requested = 0.0
    return min(max(requested, MIN_BACKOFF_S), MAX_BACKOFF_S)


def fetch_overpass(
    query: str,
    *,
    endpoints: tuple[str, ...] = OVERPASS_ENDPOINTS,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bytes, dict[str, Any], str]:
    """Run the query against the first endpoint that answers.

    Returns ``(raw response bytes, parsed payload, endpoint url)``. Raises ``OverpassError`` when every
    endpoint failed. Busy answers (HTTP 429/504) are retried once per endpoint after a back-off of at least
    30 s; any other failure moves straight on to the next mirror.
    """
    own_client = client is None
    http = client or http_client(timeout_s=QUERY_TIMEOUT_S + 60)
    failures: list[str] = []
    try:
        for url in endpoints:
            for attempt in range(1, ATTEMPTS_PER_ENDPOINT + 1):
                logger.info("querying Overpass: %s (attempt %d)", url, attempt)
                try:
                    response = http.post(url, data={"data": query})
                except httpx.HTTPError as exc:
                    failures.append(f"{url}: {type(exc).__name__}: {exc}")
                    logger.warning("Overpass request failed: %s (%s)", url, type(exc).__name__)
                    break
                if response.status_code in BUSY_STATUS:
                    failures.append(f"{url}: HTTP {response.status_code}")
                    if attempt < ATTEMPTS_PER_ENDPOINT:
                        wait = _backoff_seconds(response)
                        logger.warning("Overpass busy (HTTP %d); waiting %.0f s", response.status_code, wait)
                        sleep(wait)
                        continue
                    break
                if response.status_code != 200:
                    failures.append(f"{url}: HTTP {response.status_code}")
                    logger.warning("Overpass answered HTTP %d: %s", response.status_code, url)
                    break
                try:
                    payload = json.loads(response.content)
                    validate_payload(payload)
                except ValueError as exc:
                    failures.append(f"{url}: {exc}")
                    logger.warning("Overpass response not usable (%s): %s", exc, url)
                    break
                return response.content, payload, url
    finally:
        if own_client:
            http.close()
    raise OverpassError("; ".join(failures) or "no endpoint configured")


def describe_cache(path: Path, previous: dict[str, Any] | None) -> dict[str, Any]:
    """SOURCES.json entry for an existing ``osm.json`` (keeps url/retrieved_at of the previous entry)."""
    payload = read_json(path)
    previous = previous or {}
    info = SOURCE_REGISTRY[SOURCE_ID]
    return make_entry(
        source_id=SOURCE_ID,
        file=FILE_NAME,
        url=previous.get("url") or info.url,
        retrieved_at=previous.get("retrieved_at") or file_mtime_iso(path),
        sha256=sha256_file(path),
        feature_count=len(payload.get("elements", [])),
        license=info.license,
        attribution_text=OSM_ATTRIBUTION_DOCS,
        vintage=(payload.get("osm3s") or {}).get("timestamp_osm_base"),
        terms_url=info.terms_url,
    )


def download(
    raw_dir: Path,
    bbox: BBox,
    *,
    refresh: bool = False,
    previous: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Make sure ``raw_dir/osm.json`` exists and return its SOURCES.json entry.

    The cache is used unless ``refresh`` is set. A failed refresh keeps the existing file (with a warning);
    a failed download without any cache raises ``OverpassError`` (OSM is the one mandatory source).
    """
    path = raw_dir / FILE_NAME
    if path.exists() and not refresh:
        logger.info("using cached OSM data: %s", path.name)
        return describe_cache(path, previous)

    try:
        raw, payload, url = fetch_overpass(build_query(bbox), client=client, sleep=sleep)
    except OverpassError as exc:
        if path.exists():
            logger.warning("OSM download failed, keeping the cached file: %s", exc)
            return describe_cache(path, previous)
        raise

    write_bytes_atomic(path, raw)
    info = SOURCE_REGISTRY[SOURCE_ID]
    entry = make_entry(
        source_id=SOURCE_ID,
        file=FILE_NAME,
        url=url,
        retrieved_at=utc_now_iso(),
        sha256=sha256_file(path),
        feature_count=len(payload["elements"]),
        license=info.license,
        attribution_text=OSM_ATTRIBUTION_DOCS,
        vintage=(payload.get("osm3s") or {}).get("timestamp_osm_base"),
        terms_url=info.terms_url,
    )
    logger.info("downloaded %d OSM elements (data timestamp %s)", entry["feature_count"], entry["vintage"])
    return entry
