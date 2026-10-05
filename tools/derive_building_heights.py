#!/usr/bin/env python
"""Derive MEASURED building heights from public USGS 3DEP lidar (optional, offline).

For every OSM building footprint in ``data/raw/osm.json`` the tool reads a 2 m
height-above-ground raster derived from USGS 3DEP lidar (Cloud Optimized GeoTIFFs
on the Microsoft Planetary Computer, anonymous access) and takes the 90th
percentile of the pixels whose centre lies inside the footprint.

Two height-above-ground surfaces are supported (``--surface``):

``ndsm`` (default)
    digital surface model minus digital terrain model built from the lidar
    vendor's ground classification (collections ``3dep-lidar-dsm`` and
    ``3dep-lidar-dtm-native``).
``hag``
    the ready-made ``3dep-lidar-hag`` product. Kept for comparison: it reports
    tree canopy above houses and zero height inside large flat roofs
    (see tools/README.md, "Why the default surface is DSM minus DTM").

Outputs (committed, so the pipeline never needs the network or GDAL):
    data/raw/building_heights_3dep.csv        osm_id,height_m,n_pixels,lidar_project,collected
    data/raw/building_heights_3dep.meta.json  provenance record merged into SOURCES.json

This script runs in its own environment (``.venv-tools``, see
``tools/requirements-heights.txt``) because rasterio bundles GDAL, which the
application environment must not depend on.

Nothing is estimated here: a footprint either gets a value measured from the lidar
rasters or it gets no row at all.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import numpy as np
    import rasterio
    from rasterio.errors import RasterioError
    from rasterio.features import geometry_mask
    from rasterio.fill import fillnodata
    from rasterio.transform import Affine
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import Window
except ImportError as exc:  # wrong interpreter: give the fix, not a traceback
    raise SystemExit(
        "derive_building_heights.py needs rasterio and numpy, which live in the "
        "separate tools environment, not in .venv.\n"
        "  uv venv --python 3.12 .venv-tools\n"
        "  uv pip install --python .venv-tools/Scripts/python.exe "
        "-r tools/requirements-heights.txt\n"
        "  .venv-tools/Scripts/python.exe tools/derive_building_heights.py\n"
        f"(import failed: {exc})"
    ) from exc

log = logging.getLogger("derive_building_heights")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OSM = ROOT / "data" / "raw" / "osm.json"
DEFAULT_OUT = ROOT / "data" / "raw" / "building_heights_3dep.csv"
DEFAULT_BBOX = "37.745,-100.030,37.762,-100.005"  # south,west,north,east

STAC_API = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
USER_AGENT = "dodge-city-infra-monitor/1.0 (tools/derive_building_heights.py)"
COLLECTION_DSM = "3dep-lidar-dsm"
COLLECTION_DTM = "3dep-lidar-dtm-native"
COLLECTION_HAG = "3dep-lidar-hag"

SOURCE_ID = "usgs_3dep"
LICENSE_TEXT = (
    "US public domain (U.S. Geological Survey 3D Elevation Program data); "
    "rasters hosted by Microsoft Planetary Computer"
)
ATTRIBUTION_TEXT = "U.S. Geological Survey, 3D Elevation Program"

HEIGHT_PERCENTILE = 90.0
MIN_VALID_PIXELS = 4
MIN_HEIGHT_M = 2.5
MAX_HEIGHT_M = 60.0
WINDOW_PAD_PX = 2
TERRAIN_FILL_MAX_PX = 50  # 100 m on the 2 m grid
CSV_HEADER = ("osm_id", "height_m", "n_pixels", "lidar_project", "collected")

STATUS_OK = "ok"
STATUS_NO_COVERAGE = "no_coverage"
STATUS_TOO_FEW = "too_few_pixels"
STATUS_BELOW = "below_min_height"
STATUS_ABOVE = "above_max_height"

HTTP_TIMEOUT_S = 60
HTTP_ATTEMPTS = 3
GDAL_HTTP_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_USERAGENT": USER_AGENT,
}


class HeightsError(RuntimeError):
    """A failure the user can act on (bad input, no coverage, network, decoding)."""


@dataclass(frozen=True)
class Surface:
    """A height-above-ground surface: one raster, or a top raster minus a terrain."""

    key: str
    top: str
    terrain: str | None
    description: str

    @property
    def collections(self) -> tuple[str, ...]:
        """STAC collection ids this surface is read from."""
        return (self.top,) if self.terrain is None else (self.top, self.terrain)


SURFACES = {
    "ndsm": Surface(
        key="ndsm",
        top=COLLECTION_DSM,
        terrain=COLLECTION_DTM,
        description=(
            "digital surface model minus digital terrain model from the lidar "
            f"vendor's ground classification (STAC collections {COLLECTION_DSM} "
            f"and {COLLECTION_DTM}; terrain gaps beneath roofs interpolated from "
            "the surrounding ground)"
        ),
    ),
    "hag": Surface(
        key="hag",
        top=COLLECTION_HAG,
        terrain=None,
        description=f"height-above-ground product (STAC collection {COLLECTION_HAG})",
    ),
}
DEFAULT_SURFACE = "ndsm"


@dataclass(frozen=True)
class Bbox:
    """Study-area rectangle in WGS84 degrees."""

    south: float
    west: float
    north: float
    east: float

    def intersects(self, lons: Sequence[float], lats: Sequence[float]) -> bool:
        """True when the bounding box of the given vertices overlaps this rectangle."""
        return not (max(lats) < self.south or min(lats) > self.north or max(lons) < self.west or min(lons) > self.east)

    def as_text(self) -> str:
        """south,west,north,east (the STUDY_AREA_BBOX order)."""
        return f"{self.south:g},{self.west:g},{self.north:g},{self.east:g}"


@dataclass(frozen=True)
class Footprint:
    """One OSM building way: outer ring in WGS84 (first vertex == last vertex)."""

    osm_id: int
    name: str | None
    building: str
    lons: tuple[float, ...]
    lats: tuple[float, ...]


@dataclass(frozen=True)
class RasterItem:
    """One STAC item (one Cloud Optimized GeoTIFF) of a 3DEP lidar collection."""

    collection: str
    item_id: str
    project: str
    href: str
    start: str
    end: str


@dataclass
class HeightLayer:
    """Raster values of one lidar project on one grid (NaN = no data)."""

    project: str
    start: str
    end: str
    item_ids: tuple[str, ...]
    crs: Any
    grid_key: tuple[Any, ...]
    transform: Affine
    data: np.ndarray

    @property
    def collected(self) -> str:
        """Collection period as 'YYYY-MM/YYYY-MM' (or one month, or '')."""
        return collected_label(self.start, self.end)


@dataclass(frozen=True)
class Measurement:
    """Result for one footprint; ``height_m`` is set only when ``status == 'ok'``."""

    footprint: Footprint
    status: str
    n_pixels: int
    footprint_m2: float
    p50: float | None = None
    p90: float | None = None
    vmax: float | None = None
    project: str = ""
    collected: str = ""

    @property
    def height_m(self) -> float | None:
        """The accepted height rounded to 0.1 m, else None."""
        if self.status != STATUS_OK or self.p90 is None:
            return None
        return round(self.p90, 1)


# --------------------------------------------------------------------------- input


def parse_bbox(text: str) -> Bbox:
    """Parse 'south,west,north,east' and validate it."""
    try:
        south, west, north, east = (float(part) for part in text.split(","))
    except ValueError as exc:
        raise HeightsError(f"bbox must be 'south,west,north,east' in degrees, got {text!r}") from exc
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise HeightsError(f"bbox is not a valid south,west,north,east box: {text!r}")
    return Bbox(south, west, north, east)


def default_bbox() -> tuple[str, str]:
    """(bbox text, where it came from): environment, then ROOT/.env, then built-in.

    Only the STUDY_AREA_BBOX line of the .env file is looked at; nothing else from
    that file is kept, logged or returned.
    """
    value = os.environ.get("STUDY_AREA_BBOX", "").strip()
    if value:
        return value, "STUDY_AREA_BBOX environment variable"
    try:
        lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, raw = line.strip().partition("=")
        if sep and key.strip() == "STUDY_AREA_BBOX":
            value = raw.strip().strip("'\"")
            if value:
                return value, "STUDY_AREA_BBOX in .env"
    return DEFAULT_BBOX, "built-in default"


def load_footprints(osm_path: Path, bbox: Bbox) -> list[Footprint]:
    """Closed OSM ways with a ``building`` tag whose extent overlaps the bbox."""
    try:
        with osm_path.open(encoding="utf-8") as handle:
            elements = json.load(handle)["elements"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HeightsError(f"cannot read OSM file {osm_path}: {exc}") from exc

    footprints: list[Footprint] = []
    skipped: Counter[str] = Counter()
    for element in elements:
        tags = element.get("tags") or {}
        if element.get("type") != "way" or "building" not in tags:
            continue
        geometry = element.get("geometry") or []
        if any(not isinstance(vertex, dict) for vertex in geometry):
            skipped["incomplete geometry"] += 1
            continue
        lons = tuple(float(vertex["lon"]) for vertex in geometry)
        lats = tuple(float(vertex["lat"]) for vertex in geometry)
        if len(lons) < 4 or (lons[0], lats[0]) != (lons[-1], lats[-1]):
            skipped["not a closed ring"] += 1
            continue
        if not bbox.intersects(lons, lats):
            skipped["outside the bbox"] += 1
            continue
        footprints.append(
            Footprint(
                osm_id=int(element["id"]),
                name=tags.get("name") or None,
                building=str(tags["building"]),
                lons=lons,
                lats=lats,
            )
        )
    for reason, count in sorted(skipped.items()):
        log.warning("skipped %d building way(s): %s", count, reason)
    footprints.sort(key=lambda fp: fp.osm_id)
    return footprints


# ------------------------------------------------------------------------- network


def redact(text: object) -> str:
    """Remove URL query strings (SAS tokens) from a message before logging it."""
    return re.sub(r"\?[^\s'\"]+", "?<token>", str(text))


def http_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET (or POST ``payload`` as JSON) and decode a JSON object, with retries."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (408, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc
        if attempt < HTTP_ATTEMPTS:
            delay = 5 * attempt
            log.warning(
                "request to %s failed (%s); retry in %d s",
                redact(url),
                redact(last_error),
                delay,
            )
            time.sleep(delay)
    raise HeightsError(f"request to {redact(url)} failed: {redact(last_error)}")


def search_items(collection: str, west: float, south: float, east: float, north: float) -> list[RasterItem]:
    """All STAC items of ``collection`` whose footprint intersects the extent."""
    url = f"{STAC_API}/search"
    payload: dict[str, Any] | None = {
        "collections": [collection],
        "bbox": [west, south, east, north],
        "limit": 100,
    }
    items: dict[str, RasterItem] = {}
    for _page in range(20):
        page = http_json(url, payload)
        for feature in page.get("features", []):
            href = (feature.get("assets", {}).get("data") or {}).get("href")
            if not href:
                log.warning("STAC item %s has no data asset", feature.get("id"))
                continue
            props = feature.get("properties", {})
            item_id = str(feature["id"])
            start = props.get("start_datetime") or props.get("datetime") or ""
            end = props.get("end_datetime") or props.get("datetime") or ""
            project = props.get("3dep:usgs_id") or re.split(r"-[a-z_]+-\d+m-", item_id)[0]
            items[item_id] = RasterItem(
                collection=collection,
                item_id=item_id,
                project=str(project),
                href=str(href),
                start=str(start)[:10],
                end=str(end)[:10],
            )
        next_link = next(
            (link for link in page.get("links", []) if link.get("rel") == "next"),
            None,
        )
        if not next_link:
            break
        url = next_link["href"]
        if str(next_link.get("method", "GET")).upper() == "POST":
            payload = next_link.get("body") or payload
        else:
            payload = None
    return sorted(items.values(), key=lambda item: item.item_id)


class SasTokens:
    """Anonymous short-lived read tokens for Planetary Computer blob containers."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], str] = {}

    def sign(self, href: str) -> str:
        """Return ``href`` with the container's SAS token appended as query string."""
        parts = urllib.parse.urlsplit(href)
        host = parts.netloc.lower()
        container = parts.path.lstrip("/").split("/", 1)[0]
        if not host.endswith(".blob.core.windows.net") or not container:
            return href
        key = (host.split(".", 1)[0], container)
        if key not in self._tokens:
            reply = http_json(f"{SAS_TOKEN_URL}/{key[0]}/{key[1]}")
            token = reply.get("token")
            if not token:
                raise HeightsError(f"no SAS token returned for storage container {key[0]}/{key[1]}")
            self._tokens[key] = str(token)
            log.info(
                "anonymous SAS token for %s/%s obtained (expires %s)",
                key[0],
                key[1],
                reply.get("msft:expiry", "unknown"),
            )
        return f"{href}?{self._tokens[key]}"


# -------------------------------------------------------------------------- raster


def collected_label(start: str, end: str) -> str:
    """'2013-12/2014-01' from ISO dates; a single month gives '2010-12'."""
    months = [value[:7] for value in (start, end) if value]
    if not months:
        return ""
    return months[0] if len(set(months)) == 1 else f"{months[0]}/{months[-1]}"


def order_projects(items: Iterable[RasterItem]) -> list[tuple[str, list[RasterItem]]]:
    """Group items by lidar project, most recent collection first.

    Older collections are only consulted for footprints that no newer collection
    covers (default area: KS_Area1_2014 before the sparse KS_DodgeCity_2010).
    """
    groups: dict[str, list[RasterItem]] = {}
    for item in items:
        groups.setdefault(item.project, []).append(item)
    return sorted(
        groups.items(),
        key=lambda group: (max(item.end for item in group[1]), group[0]),
        reverse=True,
    )


class RingProjector:
    """Reprojects all footprint rings to a raster CRS once and caches the result."""

    def __init__(self, footprints: Sequence[Footprint]) -> None:
        self._footprints = list(footprints)
        self._cache: dict[str, dict[int, tuple[np.ndarray, np.ndarray]]] = {}

    def rings(self, crs: Any) -> dict[int, tuple[np.ndarray, np.ndarray]]:
        """osm_id -> (x, y) vertex arrays in ``crs``."""
        key = crs.to_wkt()
        if key not in self._cache:
            lons = [lon for fp in self._footprints for lon in fp.lons]
            lats = [lat for fp in self._footprints for lat in fp.lats]
            xs, ys = warp_transform("EPSG:4326", crs, lons, lats)
            x_all = np.asarray(xs, dtype=np.float64)
            y_all = np.asarray(ys, dtype=np.float64)
            if not (np.isfinite(x_all).all() and np.isfinite(y_all).all()):
                raise HeightsError("reprojecting footprints to the raster CRS failed")
            rings: dict[int, tuple[np.ndarray, np.ndarray]] = {}
            offset = 0
            for fp in self._footprints:
                stop = offset + len(fp.lons)
                rings[fp.osm_id] = (x_all[offset:stop], y_all[offset:stop])
                offset = stop
            self._cache[key] = rings
        return self._cache[key]


def pixel_span(low: float, high: float, origin: float, step: float, size: int, pad: int) -> tuple[int, int]:
    """Half-open pixel index range covering [low, high] on one axis, clipped to size."""
    first = (low - origin) / step
    last = (high - origin) / step
    start = math.floor(min(first, last)) - pad
    stop = math.ceil(max(first, last)) + pad
    return max(start, 0), min(stop, size)


def ring_area_m2(xs: np.ndarray, ys: np.ndarray) -> float:
    """Planar area of a closed ring (shoelace formula) in CRS units squared."""
    return float(abs(np.dot(xs[:-1], ys[1:]) - np.dot(xs[1:], ys[:-1])) / 2.0)


def read_project_layers(
    project: str,
    items: Sequence[RasterItem],
    pending: Sequence[Footprint],
    projector: RingProjector,
    tokens: SasTokens,
) -> list[HeightLayer]:
    """Read, from each COG of one project, only the window the pending footprints need.

    Tiles that share CRS, pixel size and grid alignment are mosaicked into one layer
    so that a footprint straddling a tile edge is measured once, without counting
    the overlapping pixel column twice.
    """
    tiles: dict[tuple[Any, ...], list[tuple[RasterItem, Affine, np.ndarray, Any]]] = {}
    for item in items:
        with rasterio.open(tokens.sign(item.href)) as dataset:
            transform = dataset.transform
            if transform.b or transform.d or transform.a <= 0 or transform.e >= 0:
                raise HeightsError(f"{item.item_id}: raster is not north-up")
            rings = projector.rings(dataset.crs)
            x_low = min(float(rings[fp.osm_id][0].min()) for fp in pending)
            x_high = max(float(rings[fp.osm_id][0].max()) for fp in pending)
            y_low = min(float(rings[fp.osm_id][1].min()) for fp in pending)
            y_high = max(float(rings[fp.osm_id][1].max()) for fp in pending)
            col0, col1 = pixel_span(x_low, x_high, transform.c, transform.a, dataset.width, WINDOW_PAD_PX)
            row0, row1 = pixel_span(y_low, y_high, transform.f, transform.e, dataset.height, WINDOW_PAD_PX)
            if col1 <= col0 or row1 <= row0:
                log.info("%s: does not overlap the footprints, not read", item.item_id)
                continue
            window = Window(col0, row0, col1 - col0, row1 - row0)
            data = dataset.read(1, window=window).astype(np.float32)
            if dataset.nodata is not None:
                data[data == np.float32(dataset.nodata)] = np.nan
            data[~np.isfinite(data)] = np.nan
            log.info(
                "%s: read window %d x %d px (of %d x %d), %s compression, %.2f %% valid pixels",
                item.item_id,
                data.shape[1],
                data.shape[0],
                dataset.width,
                dataset.height,
                dataset.tags(ns="IMAGE_STRUCTURE").get("COMPRESSION", "no"),
                100.0 * float(np.isfinite(data).mean()),
            )
            grid_key = (
                dataset.crs.to_wkt(),
                round(transform.a, 6),
                round(transform.e, 6),
                round((transform.c / transform.a) % 1.0, 3) % 1.0,
                round((transform.f / transform.e) % 1.0, 3) % 1.0,
            )
            tile_transform = transform * Affine.translation(col0, row0)
            tiles.setdefault(grid_key, []).append((item, tile_transform, data, dataset.crs))

    layers: list[HeightLayer] = []
    for grid_key, group in tiles.items():
        step_x, step_y = group[0][1].a, group[0][1].e
        x_origin = min(tile[1].c for tile in group)
        y_origin = max(tile[1].f for tile in group)
        x_end = max(tile[1].c + tile[2].shape[1] * step_x for tile in group)
        y_end = min(tile[1].f + tile[2].shape[0] * step_y for tile in group)
        shape = (round((y_end - y_origin) / step_y), round((x_end - x_origin) / step_x))
        mosaic = np.full(shape, np.nan, dtype=np.float32)
        for _item, tile_transform, data, _crs in group:
            col = round((tile_transform.c - x_origin) / step_x)
            row = round((tile_transform.f - y_origin) / step_y)
            view = mosaic[row : row + data.shape[0], col : col + data.shape[1]]
            np.copyto(view, data, where=np.isnan(view))
        layers.append(
            HeightLayer(
                project=project,
                start=min((tile[0].start for tile in group if tile[0].start), default=""),
                end=max((tile[0].end for tile in group if tile[0].end), default=""),
                item_ids=tuple(tile[0].item_id for tile in group),
                crs=group[0][3],
                grid_key=grid_key,
                transform=Affine(step_x, 0.0, x_origin, 0.0, step_y, y_origin),
                data=mosaic,
            )
        )
    return layers


def fill_terrain_gaps(terrain: HeightLayer) -> None:
    """Interpolate the terrain where it has no ground returns (under buildings).

    A terrain model built from ground-classified returns is empty beneath large
    roofs. The gaps are filled by inverse-distance interpolation from the
    surrounding ground (GDAL FillNodata), at most TERRAIN_FILL_MAX_PX pixels away;
    anything farther stays empty.
    """
    valid = np.isfinite(terrain.data)
    n_gaps = int((~valid).sum())
    if n_gaps == 0 or not valid.any():
        return
    terrain.data = fillnodata(
        terrain.data,
        mask=valid.astype(np.uint8),
        max_search_distance=TERRAIN_FILL_MAX_PX,
        smoothing_iterations=0,
    )
    n_left = int((~np.isfinite(terrain.data)).sum())
    log.info(
        "%s terrain: %d pixel(s) without ground returns (%.2f %%), %d filled "
        "from the surrounding ground, %d left empty",
        terrain.project,
        n_gaps,
        100.0 * n_gaps / valid.size,
        n_gaps - n_left,
        n_left,
    )


def subtract_terrain(top: HeightLayer, terrain: HeightLayer) -> HeightLayer | None:
    """Height above ground = ``top`` minus ``terrain`` where the two grids overlap."""
    step_x, step_y = top.transform.a, top.transform.e
    col_shift = round((terrain.transform.c - top.transform.c) / step_x)
    row_shift = round((terrain.transform.f - top.transform.f) / step_y)
    col0, row0 = max(0, col_shift), max(0, row_shift)
    col1 = min(top.data.shape[1], col_shift + terrain.data.shape[1])
    row1 = min(top.data.shape[0], row_shift + terrain.data.shape[0])
    if col1 <= col0 or row1 <= row0:
        return None
    ground = terrain.data[row0 - row_shift : row1 - row_shift, col0 - col_shift : col1 - col_shift]
    return HeightLayer(
        project=top.project,
        start=top.start,
        end=top.end,
        item_ids=top.item_ids + terrain.item_ids,
        crs=top.crs,
        grid_key=top.grid_key,
        transform=top.transform * Affine.translation(col0, row0),
        data=top.data[row0:row1, col0:col1] - ground,
    )


def height_layers(
    surface: Surface,
    project: str,
    items: Sequence[RasterItem],
    pending: Sequence[Footprint],
    projector: RingProjector,
    tokens: SasTokens,
) -> list[HeightLayer]:
    """Height-above-ground layers of one lidar project for the pending footprints."""
    top_items = [item for item in items if item.collection == surface.top]
    tops = read_project_layers(project, top_items, pending, projector, tokens)
    if surface.terrain is None:
        return tops
    terrain_items = [item for item in items if item.collection == surface.terrain]
    terrains = {
        layer.grid_key: layer for layer in read_project_layers(project, terrain_items, pending, projector, tokens)
    }
    layers: list[HeightLayer] = []
    for top in tops:
        terrain = terrains.get(top.grid_key)
        layer = None
        if terrain is not None:
            fill_terrain_gaps(terrain)
            layer = subtract_terrain(top, terrain)
        if layer is None:
            log.warning(
                "%s: no %s raster on the grid of the %s raster, layer skipped",
                project,
                surface.terrain,
                surface.top,
            )
            continue
        layers.append(layer)
    return layers


def footprint_values(xs: np.ndarray, ys: np.ndarray, layer: HeightLayer) -> tuple[np.ndarray, bool]:
    """(valid values of pixels whose CENTRE is inside the ring, layer covers the ring).

    ``covered`` is True when at least one pixel touched by the footprint holds data;
    only then does this layer decide the footprint (no fall-back to an older one).
    """
    transform = layer.transform
    rows, cols = layer.data.shape
    col0, col1 = pixel_span(float(xs.min()), float(xs.max()), transform.c, transform.a, cols, 1)
    row0, row1 = pixel_span(float(ys.min()), float(ys.max()), transform.f, transform.e, rows, 1)
    if col1 <= col0 or row1 <= row0:
        return np.empty(0, dtype=np.float64), False
    block = layer.data[row0:row1, col0:col1]
    block_transform = transform * Affine.translation(col0, row0)
    polygon = {
        "type": "Polygon",
        "coordinates": [list(zip(xs.tolist(), ys.tolist(), strict=True))],
    }
    touched = geometry_mask(
        [polygon],
        out_shape=block.shape,
        transform=block_transform,
        all_touched=True,
        invert=True,
    )
    covered = bool(np.isfinite(block[touched]).any())
    inside = geometry_mask(
        [polygon],
        out_shape=block.shape,
        transform=block_transform,
        all_touched=False,  # GDAL rule: a pixel counts when its centre is inside
        invert=True,
    )
    values = block[inside]
    return values[np.isfinite(values)].astype(np.float64), covered


def classify(values: np.ndarray) -> str:
    """Acceptance status for the valid in-footprint values of one building.

    The range test is applied to the unrounded percentile; rounding comes last.
    """
    if values.size < MIN_VALID_PIXELS:
        return STATUS_TOO_FEW
    p90 = float(np.percentile(values, HEIGHT_PERCENTILE))
    if p90 < MIN_HEIGHT_M:
        return STATUS_BELOW
    if p90 > MAX_HEIGHT_M:
        return STATUS_ABOVE
    return STATUS_OK


def measure_footprints(
    footprints: Sequence[Footprint], surface: Surface, items: Sequence[RasterItem]
) -> list[Measurement]:
    """Measure every footprint against the newest lidar project that covers it."""
    projector = RingProjector(footprints)
    tokens = SasTokens()
    pending = list(footprints)
    done: dict[int, Measurement] = {}
    areas: dict[int, float] = {}
    top_items = [item for item in items if item.collection == surface.top]
    with rasterio.Env(**GDAL_HTTP_OPTIONS):
        for project, _group in order_projects(top_items):
            if not pending:
                log.info("%s: not needed, every footprint is already covered", project)
                continue
            project_items = [item for item in items if item.project == project]
            for layer in height_layers(surface, project, project_items, pending, projector, tokens):
                rings = projector.rings(layer.crs)
                still_pending: list[Footprint] = []
                for fp in pending:
                    xs, ys = rings[fp.osm_id]
                    areas.setdefault(fp.osm_id, ring_area_m2(xs, ys))
                    values, covered = footprint_values(xs, ys, layer)
                    if not covered:
                        still_pending.append(fp)
                        continue
                    has_values = values.size > 0
                    done[fp.osm_id] = Measurement(
                        footprint=fp,
                        status=classify(values),
                        n_pixels=int(values.size),
                        footprint_m2=areas[fp.osm_id],
                        p50=float(np.percentile(values, 50.0)) if has_values else None,
                        p90=(float(np.percentile(values, HEIGHT_PERCENTILE)) if has_values else None),
                        vmax=float(values.max()) if has_values else None,
                        project=layer.project,
                        collected=layer.collected,
                    )
                log.info(
                    "%s: %d footprint(s) evaluated, %d not covered",
                    project,
                    len(pending) - len(still_pending),
                    len(still_pending),
                )
                pending = still_pending
    for fp in pending:
        done[fp.osm_id] = Measurement(
            footprint=fp,
            status=STATUS_NO_COVERAGE,
            n_pixels=0,
            footprint_m2=areas.get(fp.osm_id, float("nan")),
        )
    return [done[fp.osm_id] for fp in footprints]


# -------------------------------------------------------------------------- output


def write_text_atomic(path: Path, text: str) -> None:
    """Write UTF-8 text with LF newlines via a temporary file, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def csv_text(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """CSV document with LF line endings."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def heights_csv(measurements: Sequence[Measurement]) -> str:
    """The committed CSV: accepted heights only, sorted by osm_id."""
    accepted = sorted(
        (m for m in measurements if m.status == STATUS_OK),
        key=lambda m: m.footprint.osm_id,
    )
    return csv_text(
        CSV_HEADER,
        (
            (
                m.footprint.osm_id,
                f"{m.height_m:.1f}",
                m.n_pixels,
                m.project,
                m.collected,
            )
            for m in accepted
        ),
    )


def report_csv(measurements: Sequence[Measurement]) -> str:
    """Per-footprint diagnostics (every footprint, accepted or not) for review."""

    def fmt(value: float | None, digits: int = 2) -> str:
        return "" if value is None or math.isnan(value) else f"{value:.{digits}f}"

    return csv_text(
        (
            "osm_id",
            "status",
            "height_m",
            "n_pixels",
            "p50",
            "p90",
            "max",
            "footprint_m2",
            "building",
            "name",
            "lidar_project",
            "collected",
        ),
        (
            (
                m.footprint.osm_id,
                m.status,
                fmt(m.height_m, 1),
                m.n_pixels,
                fmt(m.p50),
                fmt(m.p90),
                fmt(m.vmax),
                fmt(m.footprint_m2, 1),
                m.footprint.building,
                m.footprint.name or "",
                m.project,
                m.collected,
            )
            for m in measurements
        ),
    )


def build_meta(
    measurements: Sequence[Measurement],
    surface: Surface,
    items: Sequence[RasterItem],
    retrieved_at: str,
) -> dict[str, Any]:
    """Provenance record for SOURCES.json (SPEC 4.3)."""
    accepted = [m for m in measurements if m.status == STATUS_OK]
    periods: list[str] = []
    for project in sorted({m.project for m in accepted}):
        group = [item for item in items if item.project == project]
        start = min((item.start for item in group if item.start), default="")
        end = max((item.end for item in group if item.end), default="")
        if start and end and start != end:
            period = f"lidar collected {start} to {end}"
        else:
            period = f"lidar collected {start or end or 'date unknown'}"
        periods.append(f"{project}: {period}")
    method = (
        f"Per OSM building footprint (closed way with a building tag): "
        f"{HEIGHT_PERCENTILE:g}th percentile of height above ground over the 2 m "
        f"raster pixels whose centre lies inside the footprint; at least "
        f"{MIN_VALID_PIXELS} valid pixels required; values outside "
        f"{MIN_HEIGHT_M:g}-{MAX_HEIGHT_M:g} m discarded; rounded to 0.1 m. "
        f"Height above ground = {surface.description}, derived from USGS 3DEP "
        f"lidar and hosted by the Microsoft Planetary Computer. Footprints "
        f"without a usable measurement have no row. Produced by "
        f"tools/derive_building_heights.py --surface {surface.key} "
        f"(rasterio {rasterio.__version__}, GDAL {rasterio.__gdal_version__})."
    )
    return {
        "source_id": SOURCE_ID,
        "url": f"{STAC_API}/collections/{surface.top}",
        "retrieved_at": retrieved_at,
        "license": LICENSE_TEXT,
        "attribution_text": ATTRIBUTION_TEXT,
        "vintage": "; ".join(periods) + " (2 m rasters)",
        "n_buildings": len(accepted),
        "n_footprints_total": len(measurements),
        "method": method,
    }


def summary_text(measurements: Sequence[Measurement], bbox: Bbox, surface: Surface) -> str:
    """Short human-readable result summary."""
    total = len(measurements)
    counts = Counter(m.status for m in measurements)
    heights = sorted(m.height_m for m in measurements if m.height_m is not None)
    few = counts[STATUS_TOO_FEW] + counts[STATUS_NO_COVERAGE]
    out_of_range = counts[STATUS_BELOW] + counts[STATUS_ABOVE]
    projects = Counter(f"{m.project} (collected {m.collected})" for m in measurements if m.status == STATUS_OK)
    lines = [
        "USGS 3DEP lidar building heights",
        f"  bbox (south,west,north,east): {bbox.as_text()}",
        f"  surface:                     {surface.key} = {' minus '.join(surface.collections)}",
        f"  footprints total:            {total}",
        f"  with measured height:        {len(heights)} ({100.0 * len(heights) / total:.1f} %)",
        f"  rejected:                    {few + out_of_range}",
        f"    too few pixels (< {MIN_VALID_PIXELS}):      {few} "
        f"(of which {counts[STATUS_NO_COVERAGE]} without lidar coverage)",
        f"    out of range:              {out_of_range} "
        f"(below {MIN_HEIGHT_M:g} m: {counts[STATUS_BELOW]}, "
        f"above {MAX_HEIGHT_M:g} m: {counts[STATUS_ABOVE]})",
    ]
    if heights:
        median = float(np.median(np.asarray(heights)))
        lines.append(f"  height min / median / max:   {heights[0]:.1f} / {median:.1f} / {heights[-1]:.1f} m")
    for project, count in sorted(projects.items()):
        lines.append(f"  lidar project:               {project}: {count}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description=(
            "Measure building heights from public USGS 3DEP lidar rasters "
            "(Microsoft Planetary Computer) for the OSM building footprints of the "
            "study area. Optional and offline: run it with .venv-tools, commit the "
            "CSV it writes."
        )
    )
    parser.add_argument(
        "--bbox",
        help=(
            "south,west,north,east in degrees (default: STUDY_AREA_BBOX from the "
            f"environment or .env, else {DEFAULT_BBOX})"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="output CSV (default: data/raw/building_heights_3dep.csv); the "
        "provenance record is written next to it as <name>.meta.json",
    )
    parser.add_argument(
        "--osm",
        type=Path,
        default=DEFAULT_OSM,
        help="Overpass JSON with building ways (default: data/raw/osm.json)",
    )
    parser.add_argument(
        "--surface",
        choices=sorted(SURFACES),
        default=DEFAULT_SURFACE,
        help=(
            "height-above-ground surface: 'ndsm' = surface model minus terrain "
            "model (default), 'hag' = the ready-made 3dep-lidar-hag product "
            "(see tools/README.md for why it is not the default)"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional CSV with one diagnostic row per footprint, including the rejected ones and the reason",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the tool; returns the process exit code."""
    if args.bbox:
        bbox_text, bbox_origin = args.bbox, "--bbox"
    else:
        bbox_text, bbox_origin = default_bbox()
    bbox = parse_bbox(bbox_text)
    surface = SURFACES[args.surface]
    log.info("study area bbox %s (%s)", bbox.as_text(), bbox_origin)
    log.info("height surface: %s", surface.description)

    footprints = load_footprints(args.osm, bbox)
    if not footprints:
        raise HeightsError(f"no closed building ways overlapping bbox {bbox.as_text()} in {args.osm}")
    log.info("%d building footprint(s) from %s", len(footprints), args.osm)

    west = min(min(fp.lons) for fp in footprints)
    east = max(max(fp.lons) for fp in footprints)
    south = min(min(fp.lats) for fp in footprints)
    north = max(max(fp.lats) for fp in footprints)
    retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    items: list[RasterItem] = []
    for collection in surface.collections:
        found = search_items(collection, west, south, east, north)
        if not found:
            raise HeightsError(
                f"the STAC collection {collection} has no item for this area; "
                "no measured heights are available (nothing written)"
            )
        for project, group in order_projects(found):
            log.info(
                "STAC %s: %s, %d item(s), collected %s",
                collection,
                project,
                len(group),
                collected_label(min(item.start for item in group), max(item.end for item in group)) or "unknown",
            )
        items.extend(found)

    measurements = measure_footprints(footprints, surface, items)
    sys.stdout.write(summary_text(measurements, bbox, surface))
    if args.report:
        write_text_atomic(args.report, report_csv(measurements))
        sys.stdout.write(f"  diagnostics:                 {args.report}\n")

    n_accepted = sum(1 for m in measurements if m.status == STATUS_OK)
    if n_accepted == 0:
        raise HeightsError(
            "no footprint received a usable lidar height; nothing written "
            "(existing output files, if any, were left unchanged)"
        )
    meta_path = args.out.with_suffix(".meta.json")
    meta = build_meta(measurements, surface, items, retrieved_at)
    write_text_atomic(args.out, heights_csv(measurements))
    write_text_atomic(meta_path, json.dumps(meta, indent=2) + "\n")
    sys.stdout.write(f"  wrote:                       {args.out} ({n_accepted} rows)\n")
    sys.stdout.write(f"                               {meta_path}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("rasterio").setLevel(logging.WARNING)
    try:
        return run(args)
    except (HeightsError, RasterioError, OSError) as exc:
        log.error("%s", redact(exc))
    except Exception as exc:  # last resort: GDAL can raise errors outside RasterioError
        log.error("unexpected %s: %s", type(exc).__name__, redact(exc))
        log.debug("traceback", exc_info=True)
    log.error("no output written by this run; see tools/README.md (Troubleshooting)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
