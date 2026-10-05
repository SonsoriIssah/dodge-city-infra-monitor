"""GIS data acquisition (OpenStreetMap, National Bridge Inventory, TIGERweb) and pure-Python processing.

Modules:

* ``sources`` - data-source catalogue, ``SOURCES.json`` provenance file, shared file / HTTP helpers;
* ``osm``, ``nbi``, ``tiger`` - one downloader (and parser) per source;
* ``process`` - raw files -> study-area layers and the asset registry.

The names below are re-exported from ``pipeline.gis.sources`` for convenience.
"""
from pipeline.gis.sources import (
    ENTRY_FIELDS,
    HEIGHTS_CSV,
    HEIGHTS_META,
    IMAGERY_TILE_URL,
    OSM_ATTRIBUTION_DOCS,
    OSM_ATTRIBUTION_UI,
    SOURCE_ORDER,
    SOURCE_REGISTRY,
    SOURCES_FILE,
    USER_AGENT,
    SourceInfo,
    file_mtime_iso,
    heights_entry,
    http_client,
    load_source_entries,
    make_entry,
    read_json,
    read_sources,
    sha256_file,
    utc_now_iso,
    write_bytes_atomic,
    write_json,
    write_sources,
)

__all__ = [
    "ENTRY_FIELDS",
    "file_mtime_iso",
    "HEIGHTS_CSV",
    "heights_entry",
    "HEIGHTS_META",
    "http_client",
    "IMAGERY_TILE_URL",
    "load_source_entries",
    "make_entry",
    "OSM_ATTRIBUTION_DOCS",
    "OSM_ATTRIBUTION_UI",
    "read_json",
    "read_sources",
    "sha256_file",
    "SOURCE_ORDER",
    "SOURCE_REGISTRY",
    "SourceInfo",
    "SOURCES_FILE",
    "USER_AGENT",
    "utc_now_iso",
    "write_bytes_atomic",
    "write_json",
    "write_sources",
]
