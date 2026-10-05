"""Bulk loaders: processed GeoJSON layers -> PostGIS, plus the COPY helpers other stages reuse.

Every function takes an explicit connection and never commits. Geometry is validated in the database
(``ST_IsValid`` and the expected geometry type); features that fail are skipped and counted in the log.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from pipeline.config import Settings
from pipeline.gis.process import OUTPUT_FILES
from pipeline.gis.sources import read_json

logger = logging.getLogger(__name__)

# Every table of the schema, parents first (used for row-count reports).
TABLES: tuple[str, ...] = (
    "data_sources",
    "study_areas",
    "reference_boundaries",
    "buildings",
    "roads",
    "infrastructure_assets",
    "sensor_thresholds",
    "sensors",
    "sensor_readings",
    "simulation_events",
    "detection_runs",
    "reading_scores",
    "anomaly_clusters",
    "anomalies",
    "asset_health",
    "risk_zones",
    "risk_zone_scores",
)
GIS_SERIAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("study_areas", "study_area_id"),
    ("reference_boundaries", "boundary_id"),
    ("buildings", "building_id"),
    ("roads", "road_id"),
    ("simulation_events", "event_id"),
)


# --- generic helpers ----------------------------------------------------------------------------------------
def _qualified(table: str) -> sql.Identifier:
    """``'infra.sensors'`` or ``'sensors'`` (schema infra implied) as a safely quoted identifier."""
    parts = table.split(".")
    return sql.Identifier(*parts) if len(parts) == 2 else sql.Identifier("infra", table)


def copy_rows(conn: psycopg.Connection, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """COPY rows into a table; returns the number of rows sent.

    ``table`` is ``schema.name`` (a bare name means schema ``infra``). dict / list values must be wrapped in
    ``psycopg.types.json.Jsonb`` by the caller (see ``jsonb``).
    """
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        _qualified(table), sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    )
    count = 0
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
            count += 1
    return count


def create_temp_table(conn: psycopg.Connection, name: str, columns_sql: str) -> None:
    """(Re)create a session-temporary staging table that disappears at commit."""
    ident = sql.Identifier("pg_temp", name)
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(ident))
    conn.execute(sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(sql.Identifier(name), sql.SQL(columns_sql)))


def jsonb(value: Any) -> Jsonb:
    """Wrap a Python object for a jsonb column (compact, non-ASCII kept as is)."""
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def table_counts(conn: psycopg.Connection, tables: Sequence[str] = TABLES) -> dict[str, int]:
    """Row count of every table of the schema."""
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(_qualified(table))).fetchone()[0]
    return counts


def restart_sequences(conn: psycopg.Connection, columns: Sequence[tuple[str, str]] = GIS_SERIAL_COLUMNS) -> None:
    """Restart serial sequences so a rebuilt database gets the same ids (transactional)."""
    for table, column in columns:
        sequence = conn.execute("SELECT pg_get_serial_sequence(%s, %s)", (f"infra.{table}", column)).fetchone()[0]
        if sequence:
            schema, _, name = sequence.partition(".")
            conn.execute(sql.SQL("ALTER SEQUENCE {} RESTART WITH 1").format(sql.Identifier(schema, name)))


def _geojson(geometry: dict[str, Any]) -> str:
    return json.dumps(geometry, separators=(",", ":"))


def _features(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    return payload.get("features", []) if isinstance(payload, dict) else []


def _log_skipped(layer: str, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    logger.warning("%s: skipped %d feature(s) with invalid geometry or a missing parent", layer, len(rows))
    for ident, reason in rows[:10]:
        logger.warning("  %s %s: %s", layer, ident, reason)


# --- data sources -------------------------------------------------------------------------------------------
def upsert_data_sources(conn: psycopg.Connection, rows: Sequence[dict[str, Any]]) -> int:
    """Insert or update rows of ``infra.data_sources`` (keyed by source_id)."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO infra.data_sources
                (source_id, name, kind, provider, url, license, attribution_text, vintage, retrieved_at, notes)
            VALUES (%(source_id)s, %(name)s, %(kind)s, %(provider)s, %(url)s, %(license)s, %(attribution_text)s,
                    %(vintage)s, %(retrieved_at)s::timestamptz, %(notes)s)
            ON CONFLICT (source_id) DO UPDATE SET
                name = EXCLUDED.name,
                kind = EXCLUDED.kind,
                provider = EXCLUDED.provider,
                url = EXCLUDED.url,
                license = EXCLUDED.license,
                attribution_text = EXCLUDED.attribution_text,
                vintage = EXCLUDED.vintage,
                retrieved_at = EXCLUDED.retrieved_at,
                notes = EXCLUDED.notes
            """,
            list(rows),
        )
    return len(rows)


def delete_stale_data_sources(conn: psycopg.Connection, keep: Sequence[str]) -> list[str]:
    """Delete the ``infra.data_sources`` rows whose id is not in ``keep``; returns the removed ids.

    A source that is no longer present (for example the measured building heights after their CSV was
    removed) must not stay listed as provenance. Call it after the study area was cleared: no layer row
    references a data source then.
    """
    removed = conn.execute(
        "DELETE FROM infra.data_sources WHERE NOT (source_id = ANY(%s)) RETURNING source_id", (list(keep),)
    ).fetchall()
    return sorted(row[0] for row in removed)


# --- GIS layers ---------------------------------------------------------------------------------------------
def load_study_area(conn: psycopg.Connection, path: Path, settings: Settings) -> int:
    """Insert the study area (one row) and return its id."""
    features = _features(path)
    if len(features) != 1:
        raise ValueError(f"{path.name} must contain exactly one feature, found {len(features)}")
    props = features[0]["properties"]
    if props.get("slug") != settings.STUDY_AREA_SLUG or list(props.get("bbox", [])) != list(settings.bbox):
        logger.warning(
            "processed study area (%s) differs from the current settings (%s); re-run scripts/process_data.py",
            props.get("slug"), settings.STUDY_AREA_SLUG,
        )  # fmt: skip
    row = conn.execute(
        """
        INSERT INTO infra.study_areas (slug, name, description, utm_srid, timezone, geom)
        SELECT %(slug)s, %(name)s, %(description)s, %(utm_srid)s, %(timezone)s, g.geom
        FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(geojson)s), 4326) AS geom) g
        WHERE GeometryType(g.geom) = 'POLYGON' AND ST_IsValid(g.geom)
        RETURNING study_area_id
        """,
        {
            "slug": props["slug"],
            "name": props["name"],
            "description": props.get("description"),
            "utm_srid": props["utm_srid"],
            "timezone": props["timezone"],
            "geojson": _geojson(features[0]["geometry"]),
        },
    ).fetchone()
    if row is None:
        raise ValueError("study area geometry is not a valid polygon")
    return int(row[0])


def load_reference_boundaries(conn: psycopg.Connection, path: Path, study_area_id: int) -> int:
    """Load the context boundary (city limits). A missing file means the layer is absent: nothing is loaded."""
    if not path.exists():
        logger.warning("%s not found: no reference boundary loaded (layer absent)", path.name)
        return 0
    loaded, skipped = 0, []
    for feature in _features(path):
        props = feature.get("properties") or {}
        row = conn.execute(
            """
            INSERT INTO infra.reference_boundaries (study_area_id, kind, name, source_id, geom)
            SELECT %(sa)s, %(kind)s, %(name)s, %(source_id)s, ST_Multi(g.geom)
            FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(geojson)s), 4326) AS geom) g
            WHERE GeometryType(g.geom) IN ('POLYGON', 'MULTIPOLYGON') AND ST_IsValid(g.geom)
            RETURNING boundary_id
            """,
            {
                "sa": study_area_id,
                "kind": props.get("kind") or "city_limits",
                "name": props.get("name"),
                "source_id": props.get("source_id"),
                "geojson": _geojson(feature["geometry"]),
            },
        ).fetchone()
        if row is None:
            skipped.append((props.get("name"), "not a valid (multi)polygon"))
        else:
            loaded += 1
    _log_skipped("reference_boundaries", skipped)
    return loaded


def load_buildings(conn: psycopg.Connection, path: Path, study_area_id: int) -> int:
    """Load building footprints; invalid polygons are skipped and logged."""
    create_temp_table(
        conn,
        "_stg_buildings",
        "osm_id bigint, name text, building_type text, levels real, height_m real, height_source text, "
        "footprint_m2 real, tags jsonb, geojson text, geom geometry",
    )
    columns = ("osm_id", "name", "building_type", "levels", "height_m", "height_source", "footprint_m2", "tags",
               "geojson")  # fmt: skip
    staged = copy_rows(
        conn,
        "pg_temp._stg_buildings",
        columns,
        (
            (
                p["osm_id"], p.get("name"), p.get("building_type"), p.get("levels"), p["height_m"], p["height_source"],
                p.get("footprint_m2"), jsonb(p.get("tags") or {}), _geojson(f["geometry"]),
            )
            for f in _features(path)
            for p in (f["properties"],)
        ),
    )  # fmt: skip
    conn.execute("UPDATE pg_temp._stg_buildings SET geom = ST_SetSRID(ST_GeomFromGeoJSON(geojson), 4326)")
    valid = "GeometryType(geom) = 'POLYGON' AND ST_IsValid(geom)"
    skipped = conn.execute(
        f"SELECT osm_id, ST_IsValidReason(geom) FROM pg_temp._stg_buildings WHERE NOT ({valid}) ORDER BY osm_id"
    ).fetchall()
    loaded = conn.execute(
        f"""
        INSERT INTO infra.buildings (study_area_id, osm_id, name, building_type, levels, height_m, height_source,
                                     footprint_m2, tags, source_id, geom)
        SELECT %s, osm_id, name, building_type, levels, height_m, height_source, footprint_m2, tags, 'osm', geom
        FROM pg_temp._stg_buildings
        WHERE {valid}
        ORDER BY osm_id
        """,
        (study_area_id,),
    ).rowcount
    _log_skipped("buildings", skipped)
    logger.info("buildings: %d staged, %d loaded, %d skipped", staged, loaded, len(skipped))
    return loaded


def load_roads(conn: psycopg.Connection, path: Path, study_area_id: int) -> int:
    """Load the base-map road layer; invalid lines are skipped and logged."""
    create_temp_table(
        conn,
        "_stg_roads",
        "osm_id bigint, name text, highway_class text, surface text, lanes smallint, maxspeed text, oneway boolean, "
        "is_bridge boolean, length_m real, tags jsonb, geojson text, geom geometry",
    )
    columns = ("osm_id", "name", "highway_class", "surface", "lanes", "maxspeed", "oneway", "is_bridge", "length_m",
               "tags", "geojson")  # fmt: skip
    staged = copy_rows(
        conn,
        "pg_temp._stg_roads",
        columns,
        (
            (
                p["osm_id"], p.get("name"), p["highway_class"], p.get("surface"), p.get("lanes"), p.get("maxspeed"),
                p.get("oneway"), bool(p.get("is_bridge")), p.get("length_m"), jsonb(p.get("tags") or {}),
                _geojson(f["geometry"]),
            )
            for f in _features(path)
            for p in (f["properties"],)
        ),
    )  # fmt: skip
    conn.execute("UPDATE pg_temp._stg_roads SET geom = ST_SetSRID(ST_GeomFromGeoJSON(geojson), 4326)")
    valid = "GeometryType(geom) = 'LINESTRING' AND ST_IsValid(geom)"
    skipped = conn.execute(
        f"SELECT osm_id, ST_IsValidReason(geom) FROM pg_temp._stg_roads WHERE NOT ({valid}) ORDER BY osm_id"
    ).fetchall()
    loaded = conn.execute(
        f"""
        INSERT INTO infra.roads (study_area_id, osm_id, name, highway_class, surface, lanes, maxspeed, oneway,
                                 is_bridge, length_m, tags, source_id, geom)
        SELECT %s, osm_id, name, highway_class, surface, lanes, maxspeed, oneway, is_bridge, length_m, tags,
               'osm', geom
        FROM pg_temp._stg_roads
        WHERE {valid}
        ORDER BY osm_id
        """,
        (study_area_id,),
    ).rowcount
    _log_skipped("roads", skipped)
    logger.info("roads: %d staged, %d loaded, %d skipped", staged, loaded, len(skipped))
    return loaded


def load_assets(conn: psycopg.Connection, path: Path, study_area_id: int) -> int:
    """Load the asset registry and link building / road assets to their rows.

    Skipped (and logged): invalid geometry, and building / road assets whose footprint or road row was not
    loaded.
    """
    create_temp_table(
        conn,
        "_stg_assets",
        "asset_id text, asset_type text, category text, name text, is_simulated boolean, source_id text, "
        "properties jsonb, link_osm_id bigint, lon double precision, lat double precision, geojson text, "
        "geom geometry",
    )
    columns = ("asset_id", "asset_type", "category", "name", "is_simulated", "source_id", "properties", "link_osm_id",
               "lon", "lat", "geojson")  # fmt: skip
    staged = copy_rows(
        conn,
        "pg_temp._stg_assets",
        columns,
        (
            (
                p["asset_id"], p["asset_type"], p["category"], p.get("name"), bool(p.get("is_simulated", False)),
                p["source_id"], jsonb(p.get("attributes") or {}), p.get("link_osm_id"), p["centroid"][0],
                p["centroid"][1], _geojson(f["geometry"]),
            )
            for f in _features(path)
            for p in (f["properties"],)
        ),
    )  # fmt: skip
    conn.execute("UPDATE pg_temp._stg_assets SET geom = ST_SetSRID(ST_GeomFromGeoJSON(geojson), 4326)")
    joined = """
        FROM pg_temp._stg_assets s
        LEFT JOIN infra.buildings b ON s.asset_type = 'building' AND b.osm_id = s.link_osm_id
        LEFT JOIN infra.roads r ON s.asset_type IN ('road', 'bridge') AND r.osm_id = s.link_osm_id
    """
    valid = """
        ST_IsValid(s.geom) AND NOT ST_IsEmpty(s.geom)
        AND (s.asset_type <> 'building' OR b.building_id IS NOT NULL)
        AND (s.asset_type <> 'road' OR r.road_id IS NOT NULL)
    """
    skipped = conn.execute(
        f"""
        SELECT s.asset_id,
               CASE WHEN NOT ST_IsValid(s.geom) THEN ST_IsValidReason(s.geom)
                    WHEN ST_IsEmpty(s.geom) THEN 'empty geometry'
                    ELSE 'linked ' || s.asset_type || ' row was not loaded' END
        {joined}
        WHERE NOT ({valid})
        ORDER BY s.asset_id
        """
    ).fetchall()
    loaded = conn.execute(
        f"""
        INSERT INTO infra.infrastructure_assets
            (asset_id, study_area_id, asset_type, category, name, building_id, road_id, is_simulated, source_id,
             properties, geom, centroid)
        SELECT s.asset_id, %s, s.asset_type, s.category, s.name, b.building_id, r.road_id, s.is_simulated,
               s.source_id, s.properties, s.geom, ST_SetSRID(ST_MakePoint(s.lon, s.lat), 4326)
        {joined}
        WHERE {valid}
        ORDER BY s.asset_id
        """,
        (study_area_id,),
    ).rowcount
    _log_skipped("infrastructure_assets", skipped)
    logger.info("infrastructure_assets: %d staged, %d loaded, %d skipped", staged, loaded, len(skipped))
    return loaded


def load_gis(conn: psycopg.Connection, processed_dir: Path, settings: Settings) -> dict[str, int]:
    """Load every processed layer into an empty set of GIS tables; returns the rows loaded per table.

    The caller is responsible for clearing the study area first (``DELETE FROM infra.study_areas``) and for
    the ``infra.data_sources`` rows the layers reference.
    """
    for key in ("study_area", "buildings", "roads", "assets"):
        if not (processed_dir / OUTPUT_FILES[key]).exists():
            raise FileNotFoundError(
                f"{processed_dir / OUTPUT_FILES[key]} is missing - run scripts/process_data.py first"
            )
    study_area_id = load_study_area(conn, processed_dir / OUTPUT_FILES["study_area"], settings)
    counts = {
        "study_areas": 1,
        "reference_boundaries": load_reference_boundaries(
            conn, processed_dir / OUTPUT_FILES["city_boundary"], study_area_id
        ),
        "buildings": load_buildings(conn, processed_dir / OUTPUT_FILES["buildings"], study_area_id),
        "roads": load_roads(conn, processed_dir / OUTPUT_FILES["roads"], study_area_id),
        "infrastructure_assets": load_assets(conn, processed_dir / OUTPUT_FILES["assets"], study_area_id),
    }
    for table in ("study_areas", "reference_boundaries", "buildings", "roads", "infrastructure_assets"):
        conn.execute(sql.SQL("ANALYZE {}").format(_qualified(table)))
    return counts
