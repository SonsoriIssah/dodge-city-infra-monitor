"""What the GIS downloaders and the processing stage share.

* the catalogue of data sources (licence, attribution, terms) behind ``infra.data_sources``;
* reading and writing ``data/raw/SOURCES.json`` (provenance of every cached file);
* small file helpers (sha256, atomic write, JSON with LF line endings) and the HTTP client that carries the
  project User-Agent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SOURCES_FILE = "SOURCES.json"
HEIGHTS_CSV = "building_heights_3dep.csv"
HEIGHTS_META = "building_heights_3dep.meta.json"

# Identifies the project to the public data services (Overpass usage policy asks for a descriptive User-Agent).
USER_AGENT = (
    "dodge-city-infra-monitor/1.0 (urban infrastructure monitoring research prototype; "
    "+https://github.com/SonsoriIssah/dodge-city-infra-monitor)"
)

# Field order of one SOURCES.json entry.
ENTRY_FIELDS = (
    "source_id",
    "file",
    "url",
    "retrieved_at",
    "sha256",
    "feature_count",
    "license",
    "attribution_text",
    "vintage",
    "terms_url",
)
SOURCE_ORDER = ("osm", "nbi", "tiger", "usgs_3dep")

OSM_ATTRIBUTION_UI = "© OpenStreetMap contributors"
OSM_ATTRIBUTION_DOCS = (
    "Contains OpenStreetMap data © OpenStreetMap contributors, available under the Open Database License "
    "(https://opendatacommons.org/licenses/odbl/1-0/)"
)
IMAGERY_TILE_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Static description of a data source (one row of ``infra.data_sources``)."""

    source_id: str
    name: str
    kind: str  # real | simulated | derived
    provider: str
    license: str | None
    attribution_text: str | None
    url: str | None = None
    terms_url: str | None = None
    notes: str | None = None


SOURCE_REGISTRY: dict[str, SourceInfo] = {
    "osm": SourceInfo(
        source_id="osm",
        name="OpenStreetMap: building footprints, roads, rail, bridges, power, street lamps",
        kind="real",
        provider="OpenStreetMap contributors, retrieved through the Overpass API",
        license="Open Database License (ODbL) 1.0 - https://opendatacommons.org/licenses/odbl/1-0/",
        attribution_text=OSM_ATTRIBUTION_UI,
        url="https://overpass-api.de/api/interpreter",
        terms_url="https://www.openstreetmap.org/copyright",
        notes=OSM_ATTRIBUTION_DOCS,
    ),
    "nbi": SourceInfo(
        source_id="nbi",
        name="National Bridge Inventory: highway bridge and culvert records",
        kind="real",
        provider="Federal Highway Administration (FHWA); distributed by USDOT/BTS in the National "
        "Transportation Atlas Database (NTAD)",
        license="US Government work, unrestricted public use",
        attribution_text="FHWA National Bridge Inventory, distributed by USDOT/BTS NTAD",
        url="https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
        "NTAD_National_Bridge_Inventory/FeatureServer/0",
        terms_url="https://www.arcgis.com/home/item.html?id=bd1b6ee967134ea68fac351cef461b5e",
        notes="Recorded inventory attributes as published by FHWA. They are shown as recorded and never feed "
        "the derived health score, status colours or risk zones.",
    ),
    "tiger": SourceInfo(
        source_id="tiger",
        name="TIGERweb Incorporated Places: city boundary (context outline)",
        kind="real",
        provider="U.S. Census Bureau, Geography Division",
        license="US Government work, not subject to copyright; cite the U.S. Census Bureau",
        attribution_text="U.S. Census Bureau, TIGERweb",
        url="https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/"
        "MapServer/4",
        terms_url="https://www2.census.gov/geo/pdfs/maps-data/data/tiger/tgrshp2025/TGRSHP2025_TechDoc_Ch1.pdf",
        notes="Statistical boundary, not a legal land description.",
    ),
    "usgs_3dep": SourceInfo(
        source_id="usgs_3dep",
        name="USGS 3DEP lidar height above ground: measured building heights",
        kind="real",
        provider="U.S. Geological Survey, 3D Elevation Program (2 m raster hosted by Microsoft Planetary "
        "Computer)",
        license="US public domain",
        attribution_text="U.S. Geological Survey, 3D Elevation Program",
        url="https://planetarycomputer.microsoft.com/dataset/3dep-lidar-hag",
        terms_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
        notes="Per-footprint heights derived offline by tools/derive_building_heights.py.",
    ),
    "basemap": SourceInfo(
        source_id="basemap",
        name="OpenFreeMap dark basemap",
        kind="real",
        provider="OpenFreeMap (OpenMapTiles schema, OpenStreetMap data)",
        license="Free public tile service; map data under ODbL 1.0",
        attribution_text="OpenFreeMap © OpenMapTiles Data from OpenStreetMap",
        url="https://tiles.openfreemap.org/styles/dark",
        terms_url="https://openfreemap.org/tos/",
        notes="display only",
    ),
    "imagery": SourceInfo(
        source_id="imagery",
        name="USGS The National Map orthoimagery",
        kind="real",
        provider="U.S. Geological Survey / U.S. Department of Agriculture",
        license="US public domain",
        attribution_text="USDA, USGS The National Map: Orthoimagery",
        url=IMAGERY_TILE_URL,
        terms_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
        notes="display only",
    ),
    "simulator": SourceInfo(
        source_id="simulator",
        name="Simulated sensor network",
        kind="simulated",
        provider="This project (pipeline.sensors)",
        license=None,
        attribution_text="Simulated Sensor Data",
        notes="Sensors, readings, sensor status, the simulated weather that drives them and the simulated "
        "water network. Not a record of real infrastructure condition.",
    ),
    "derived": SourceInfo(
        source_id="derived",
        name="Derived analysis outputs",
        kind="derived",
        provider="This project (pipeline.detection, pipeline.analysis)",
        license=None,
        attribution_text="Prototype Anomaly Detection / Derived Asset Health Score",
        notes="Anomalies, co-occurrence clusters, risk zones and asset health scores computed from the "
        "simulated readings; estimated building heights.",
    ),
}


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_mtime_iso(path: Path) -> str:
    """Modification time of a file as a UTC ``...Z`` string."""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write a file through a temporary sibling so a failed download never truncates an existing cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    """Write JSON as UTF-8 with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    separators = (",", ":") if indent is None else None
    text = json.dumps(payload, ensure_ascii=False, indent=indent, separators=separators)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")


def read_json(path: Path) -> Any:
    """Read a UTF-8 JSON file."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def http_client(timeout_s: float = 60.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """HTTP client with the project User-Agent. ``transport`` lets tests inject ``httpx.MockTransport``."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/geo+json;q=0.9, */*;q=0.1"},
        timeout=httpx.Timeout(timeout_s, connect=20.0),
        follow_redirects=True,
        transport=transport,
    )


def make_entry(**values: Any) -> dict[str, Any]:
    """One SOURCES.json entry with exactly the documented fields, in the documented order."""
    unknown = set(values) - set(ENTRY_FIELDS)
    if unknown:
        raise ValueError(f"unknown SOURCES.json fields: {sorted(unknown)}")
    return {name: values.get(name) for name in ENTRY_FIELDS}


def read_sources(raw_dir: Path) -> dict[str, dict[str, Any]]:
    """Entries of ``SOURCES.json`` keyed by source_id (empty when the file does not exist)."""
    path = raw_dir / SOURCES_FILE
    if not path.exists():
        return {}
    payload = read_json(path)
    entries = payload.get("sources", []) if isinstance(payload, dict) else payload
    return {entry["source_id"]: make_entry(**{k: entry.get(k) for k in ENTRY_FIELDS}) for entry in entries}


def write_sources(raw_dir: Path, entries: dict[str, dict[str, Any]]) -> Path:
    """Write ``SOURCES.json`` (known sources first, in a fixed order, then any others by id)."""
    order = [sid for sid in SOURCE_ORDER if sid in entries] + sorted(set(entries) - set(SOURCE_ORDER))
    payload = {
        "description": "Provenance of the files in data/raw (written by scripts/download_data.py). "
        "The cached files are the reproducible artefact: the upstream services change over time.",
        "sources": [make_entry(**entries[sid]) for sid in order],
    }
    path = raw_dir / SOURCES_FILE
    write_json(path, payload, indent=2)
    return path


def heights_entry(raw_dir: Path) -> dict[str, Any] | None:
    """SOURCES.json entry for the optional measured building heights, or None when they are absent.

    The entry is built from ``building_heights_3dep.meta.json`` (written by tools/derive_building_heights.py).
    A CSV without the metadata file still yields an entry, with the registry defaults for the missing fields.
    """
    csv_path, meta_path = raw_dir / HEIGHTS_CSV, raw_dir / HEIGHTS_META
    if not csv_path.exists() and not meta_path.exists():
        return None
    info = SOURCE_REGISTRY["usgs_3dep"]
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            loaded = read_json(meta_path)
            meta = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning("could not read %s (%s); using defaults for the usgs_3dep source", HEIGHTS_META, exc)
    feature_count = meta.get("n_buildings")
    if feature_count is None and csv_path.exists():
        with csv_path.open("r", encoding="utf-8") as handle:
            feature_count = max(sum(1 for line in handle if line.strip()) - 1, 0)
    return make_entry(
        source_id="usgs_3dep",
        file=HEIGHTS_CSV if csv_path.exists() else None,
        url=meta.get("url") or info.url,
        retrieved_at=meta.get("retrieved_at") or (file_mtime_iso(csv_path) if csv_path.exists() else None),
        sha256=sha256_file(csv_path) if csv_path.exists() else None,
        feature_count=feature_count,
        license=meta.get("license") or info.license,
        attribution_text=meta.get("attribution_text") or info.attribution_text,
        vintage=meta.get("vintage"),
        terms_url=meta.get("terms_url") or info.terms_url,
    )


def load_source_entries(raw_dir: Path) -> dict[str, dict[str, Any]]:
    """``SOURCES.json`` entries with the optional measured-heights entry merged in (nothing is written)."""
    entries = read_sources(raw_dir)
    heights = heights_entry(raw_dir)
    if heights is not None:
        entries["usgs_3dep"] = heights
    else:
        entries.pop("usgs_3dep", None)
    return entries
