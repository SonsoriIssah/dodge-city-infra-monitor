"""Database schema (build contract section 6): migrations, tables, constraints, indexes, views, functions.

Integration tests on ``infra_test``. Everything a test changes is rolled back.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from pipeline.config import MIGRATIONS_DIR
from pipeline.db import migrate

pytestmark = pytest.mark.db

# table -> columns named by the contract
TABLES: dict[str, set[str]] = {
    "schema_migrations": {"version", "applied_at"},
    "data_sources": {"source_id", "name", "kind", "provider", "url", "license", "attribution_text", "vintage",
                     "retrieved_at", "notes"},
    "study_areas": {"study_area_id", "slug", "name", "description", "utm_srid", "timezone", "geom", "created_at"},
    "reference_boundaries": {"boundary_id", "study_area_id", "kind", "name", "source_id", "geom"},
    "buildings": {"building_id", "study_area_id", "osm_id", "name", "building_type", "levels", "height_m",
                  "height_source", "footprint_m2", "tags", "source_id", "geom"},
    "roads": {"road_id", "study_area_id", "osm_id", "name", "highway_class", "surface", "lanes", "maxspeed", "oneway",
              "is_bridge", "length_m", "tags", "source_id", "geom"},
    "infrastructure_assets": {"asset_id", "study_area_id", "asset_type", "category", "name", "building_id", "road_id",
                              "is_simulated", "source_id", "properties", "geom", "centroid", "created_at"},
    "sensor_thresholds": {"sensor_type", "placement", "unit", "warn_low", "warn_high", "crit_low", "crit_high",
                          "description"},
    "sensors": {"sensor_id", "asset_id", "sensor_type", "placement", "unit", "description", "is_simulated", "source",
                "installed_at", "sampling_interval_s", "geom"},
    "sensor_readings": {"sensor_id", "ts", "value", "unit", "status", "source", "ingested_at"},
    "detection_runs": {"run_id", "started_at", "finished_at", "window_start", "window_end", "params", "metrics",
                       "n_readings", "n_anomalies"},
    "reading_scores": {"sensor_id", "ts", "run_id", "expected", "expected_low", "expected_high", "robust_z",
                       "iforest_score", "flagged"},
    "anomaly_clusters": {"cluster_id", "run_id", "method", "params", "n_anomalies", "n_sensors", "n_assets",
                         "sensor_types", "max_severity", "first_started_at", "last_ended_at", "centroid", "geom"},
    "anomalies": {"anomaly_id", "run_id", "sensor_id", "asset_id", "sensor_type", "anomaly_type", "started_at",
                  "ended_at", "peak_at", "duration_hours", "observed_value", "expected_value", "unit", "robust_z",
                  "anomaly_score", "score_components", "severity", "detection_method", "explanation", "status",
                  "cluster_id", "geom"},
    "asset_health": {"asset_id", "as_of", "run_id", "health_score", "status", "frequency_penalty", "severity_penalty",
                     "reading_penalty", "sensor_penalty", "anomalies_in_window", "active_anomalies",
                     "sensors_reporting", "sensors_total"},
    "risk_zones": {"cell_id", "study_area_id", "geom", "centroid"},
    "risk_zone_scores": {"cell_id", "as_of", "run_id", "risk_score", "risk_level", "anomaly_count"},
    "simulation_events": {"event_id", "sensor_id", "asset_id", "sensor_type", "event_type", "is_anomaly", "started_at",
                          "ended_at", "magnitude", "description"},
}  # fmt: skip
VIEWS = {"v_sensor_latest", "v_asset_health_latest", "v_asset_summary", "v_active_anomalies"}
PRIMARY_KEYS = {
    "schema_migrations": ["version"],
    "data_sources": ["source_id"],
    "study_areas": ["study_area_id"],
    "reference_boundaries": ["boundary_id"],
    "buildings": ["building_id"],
    "roads": ["road_id"],
    "infrastructure_assets": ["asset_id"],
    "sensor_thresholds": ["sensor_type", "placement"],
    "sensors": ["sensor_id"],
    "sensor_readings": ["sensor_id", "ts"],
    "detection_runs": ["run_id"],
    "reading_scores": ["sensor_id", "ts"],
    "anomaly_clusters": ["cluster_id"],
    "anomalies": ["anomaly_id"],
    "asset_health": ["asset_id", "as_of"],
    "risk_zones": ["cell_id"],
    "risk_zone_scores": ["cell_id", "as_of"],
    "simulation_events": ["event_id"],
}
# (table, columns) -> (referenced table, referenced columns, ON DELETE action)
FOREIGN_KEYS = {
    ("reference_boundaries", ("study_area_id",)): ("study_areas", ("study_area_id",), "CASCADE"),
    ("reference_boundaries", ("source_id",)): ("data_sources", ("source_id",), "NO ACTION"),
    ("buildings", ("study_area_id",)): ("study_areas", ("study_area_id",), "CASCADE"),
    ("buildings", ("source_id",)): ("data_sources", ("source_id",), "NO ACTION"),
    ("roads", ("study_area_id",)): ("study_areas", ("study_area_id",), "CASCADE"),
    ("roads", ("source_id",)): ("data_sources", ("source_id",), "NO ACTION"),
    ("infrastructure_assets", ("study_area_id",)): ("study_areas", ("study_area_id",), "CASCADE"),
    ("infrastructure_assets", ("building_id",)): ("buildings", ("building_id",), "CASCADE"),
    ("infrastructure_assets", ("road_id",)): ("roads", ("road_id",), "CASCADE"),
    ("infrastructure_assets", ("source_id",)): ("data_sources", ("source_id",), "NO ACTION"),
    ("sensors", ("asset_id",)): ("infrastructure_assets", ("asset_id",), "CASCADE"),
    ("sensors", ("sensor_type", "placement")): ("sensor_thresholds", ("sensor_type", "placement"), "NO ACTION"),
    ("sensor_readings", ("sensor_id",)): ("sensors", ("sensor_id",), "CASCADE"),
    ("reading_scores", ("run_id",)): ("detection_runs", ("run_id",), "CASCADE"),
    ("reading_scores", ("sensor_id", "ts")): ("sensor_readings", ("sensor_id", "ts"), "CASCADE"),
    ("anomaly_clusters", ("run_id",)): ("detection_runs", ("run_id",), "CASCADE"),
    ("anomalies", ("run_id",)): ("detection_runs", ("run_id",), "CASCADE"),
    ("anomalies", ("sensor_id",)): ("sensors", ("sensor_id",), "CASCADE"),
    ("anomalies", ("asset_id",)): ("infrastructure_assets", ("asset_id",), "CASCADE"),
    ("anomalies", ("cluster_id",)): ("anomaly_clusters", ("cluster_id",), "SET NULL"),
    ("anomalies", ("sensor_id", "peak_at")): ("sensor_readings", ("sensor_id", "ts"), "CASCADE"),  # reading -> anomaly
    ("asset_health", ("asset_id",)): ("infrastructure_assets", ("asset_id",), "CASCADE"),
    ("asset_health", ("run_id",)): ("detection_runs", ("run_id",), "CASCADE"),
    ("risk_zones", ("study_area_id",)): ("study_areas", ("study_area_id",), "CASCADE"),
    ("risk_zone_scores", ("cell_id",)): ("risk_zones", ("cell_id",), "CASCADE"),
    ("risk_zone_scores", ("run_id",)): ("detection_runs", ("run_id",), "CASCADE"),
    ("simulation_events", ("sensor_id",)): ("sensors", ("sensor_id",), "CASCADE"),
    ("simulation_events", ("asset_id",)): ("infrastructure_assets", ("asset_id",), "CASCADE"),
}
GEOMETRY_COLUMNS = {
    ("study_areas", "geom"): "POLYGON",
    ("reference_boundaries", "geom"): "MULTIPOLYGON",
    ("buildings", "geom"): "POLYGON",
    ("roads", "geom"): "LINESTRING",
    ("infrastructure_assets", "geom"): "GEOMETRY",
    ("infrastructure_assets", "centroid"): "POINT",
    ("sensors", "geom"): "POINT",
    ("anomaly_clusters", "centroid"): "POINT",
    ("anomaly_clusters", "geom"): "POLYGON",
    ("anomalies", "geom"): "POINT",
    ("risk_zones", "geom"): "POLYGON",
    ("risk_zones", "centroid"): "POINT",
}
BTREE_INDEXES = [
    ("infrastructure_assets", "(asset_type)"),
    ("sensors", "(asset_id)"),
    ("sensors", "(sensor_type)"),
    ("sensor_readings", "(ts)"),
    ("anomalies", "(started_at, ended_at)"),
    ("anomalies", "(asset_id)"),
    ("anomalies", "(sensor_id)"),
    ("anomalies", "(severity)"),
    ("asset_health", "(as_of)"),
    ("risk_zone_scores", "(as_of)"),
]
NOT_NULL = {
    "study_areas": ["slug", "name", "utm_srid", "timezone", "geom"],
    "buildings": ["osm_id", "height_m", "height_source", "geom"],
    "roads": ["osm_id", "highway_class", "is_bridge", "geom"],
    "infrastructure_assets": ["study_area_id", "asset_type", "category", "is_simulated", "source_id", "properties",
                              "geom", "centroid"],
    "sensor_thresholds": ["unit"],
    "sensors": ["asset_id", "sensor_type", "placement", "unit", "is_simulated", "source", "sampling_interval_s", "geom"],
    "sensor_readings": ["value", "unit", "status", "source"],
    "reading_scores": ["run_id", "flagged"],
    "anomalies": ["run_id", "sensor_id", "asset_id", "sensor_type", "anomaly_type", "started_at", "ended_at", "peak_at",
                  "duration_hours", "observed_value", "expected_value", "unit", "robust_z", "anomaly_score",
                  "score_components", "severity", "detection_method", "explanation", "status", "geom"],
    "asset_health": ["run_id", "health_score", "status", "frequency_penalty", "severity_penalty", "reading_penalty",
                     "sensor_penalty"],
    "risk_zones": ["geom"],
    "risk_zone_scores": ["run_id", "risk_score", "risk_level", "anomaly_count"],
    "simulation_events": ["event_type", "is_anomaly", "started_at", "ended_at"],
}  # fmt: skip
DELETE_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


def column(conn, query: str, params=None) -> list:
    return [row[0] for row in conn.execute(query, params).fetchall()]


# --- migrations ---------------------------------------------------------------------------------------------------
def test_every_migration_file_is_recorded_once_in_file_name_order(db_conn):
    files = [path.stem for path in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    assert files[:2] == ["001_schema", "002_views_functions"]
    assert [path.stem for path in migrate.migration_files()] == files
    assert column(db_conn, "SELECT version FROM infra.schema_migrations ORDER BY version") == files
    assert migrate.applied_versions(db_conn) == set(files)


def test_applying_the_migrations_again_changes_nothing(db_conn):
    before = db_conn.execute("SELECT count(*), max(applied_at) FROM infra.schema_migrations").fetchone()
    assert migrate.apply_migrations(db_conn) == []
    assert db_conn.execute("SELECT count(*), max(applied_at) FROM infra.schema_migrations").fetchone() == before


def test_migration_files_can_be_run_by_hand_repeatedly_without_losing_data(db_conn):
    counts = db_conn.execute("SELECT (SELECT count(*) FROM infra.sensor_readings), (SELECT count(*) FROM infra.anomalies)").fetchone()
    for path in migrate.migration_files():
        db_conn.execute(path.read_text(encoding="utf-8"))
        db_conn.execute(path.read_text(encoding="utf-8"))
    after = db_conn.execute("SELECT (SELECT count(*) FROM infra.sensor_readings), (SELECT count(*) FROM infra.anomalies)").fetchone()
    assert after == counts and counts[0] > 0


def test_a_dropped_schema_is_rebuilt_from_the_migrations(db_conn):
    """DDL is transactional in PostgreSQL: the schema is dropped and rebuilt inside a transaction that is rolled back."""
    migrate.reset_schema(db_conn)
    assert db_conn.execute("SELECT to_regclass('infra.anomalies')").fetchone()[0] is None
    assert migrate.applied_versions(db_conn) == set()
    applied = migrate.apply_migrations(db_conn)
    assert applied == [path.stem for path in migrate.migration_files()]
    assert migrate.apply_migrations(db_conn) == []  # idempotent
    tables = set(column(db_conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'infra' AND table_type = 'BASE TABLE'"))
    assert tables == set(TABLES)
    assert db_conn.execute("SELECT count(*) FROM infra.sensors").fetchone()[0] == 0
    # PostGIS survives the drop of schema infra because it lives in schema public
    assert db_conn.execute("SELECT postgis_lib_version()").fetchone()[0]
    db_conn.rollback()
    assert db_conn.execute("SELECT count(*) FROM infra.sensors").fetchone()[0] > 0


def test_missing_migration_folder_is_an_error(db_conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        migrate.apply_migrations(db_conn, tmp_path)


# --- connection ---------------------------------------------------------------------------------------------------
def test_connections_run_in_utc_with_the_documented_search_path(db_conn):
    assert db_conn.execute("SHOW timezone").fetchone()[0] == "UTC"
    assert db_conn.execute("SHOW search_path").fetchone()[0].replace(" ", "") == "infra,public"
    stamp = db_conn.execute("SELECT min(ts) FROM infra.sensor_readings").fetchone()[0]
    assert stamp.utcoffset().total_seconds() == 0


def test_postgis_is_installed_in_schema_public(db_conn):
    schema = db_conn.execute(
        "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis'"
    ).fetchone()
    assert schema == ("public",)
    assert db_conn.execute("SELECT to_regclass('public.spatial_ref_sys') IS NOT NULL").fetchone()[0]
    assert db_conn.execute("SELECT to_regclass('infra.spatial_ref_sys')").fetchone()[0] is None


# --- tables -------------------------------------------------------------------------------------------------------
def test_schema_has_exactly_the_tables_and_views_of_the_contract(db_conn):
    rows = db_conn.execute("SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'infra'").fetchall()
    assert {name for name, kind in rows if kind == "BASE TABLE"} == set(TABLES)
    assert {name for name, kind in rows if kind == "VIEW"} == VIEWS


@pytest.mark.parametrize("table", sorted(TABLES))
def test_table_has_the_columns_of_the_contract(db_conn, table):
    columns = set(column(db_conn, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'infra' AND table_name = %s", (table,)))
    assert TABLES[table] <= columns, TABLES[table] - columns


def test_every_timestamp_column_is_timestamptz(db_conn):
    rows = db_conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'infra' AND data_type LIKE 'timestamp%'"
    ).fetchall()
    assert len(rows) >= 15
    assert {data_type for _, _, data_type in rows} == {"timestamp with time zone"}


@pytest.mark.parametrize("table", sorted(PRIMARY_KEYS))
def test_primary_keys(db_conn, table):
    key = db_conn.execute(
        """
        SELECT array_agg(a.attname ORDER BY array_position(c.conkey, a.attnum))
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.contype = 'p' AND c.conrelid = %s::regclass
        """,
        (f"infra.{table}",),
    ).fetchone()[0]
    assert key == PRIMARY_KEYS[table]


def test_foreign_keys_and_their_delete_actions_are_exactly_those_of_the_contract(db_conn):
    rows = db_conn.execute(
        """
        SELECT c.conrelid::regclass::text, c.confrelid::regclass::text, c.confdeltype,
               (SELECT array_agg(a.attname ORDER BY array_position(c.conkey, a.attnum))
                FROM pg_attribute a WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)),
               (SELECT array_agg(a.attname ORDER BY array_position(c.confkey, a.attnum))
                FROM pg_attribute a WHERE a.attrelid = c.confrelid AND a.attnum = ANY(c.confkey))
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE c.contype = 'f' AND n.nspname = 'infra'
        """
    ).fetchall()
    found = {
        (table.removeprefix("infra."), tuple(columns)): (target.removeprefix("infra."), tuple(target_columns), DELETE_ACTION[action])
        for table, target, action, columns, target_columns in rows
    }
    assert found == FOREIGN_KEYS


@pytest.mark.parametrize("table", sorted(NOT_NULL))
def test_not_null_columns(db_conn, table):
    nullable = set(column(db_conn, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'infra' AND table_name = %s AND is_nullable = 'YES'", (table,)))
    assert not nullable & set(NOT_NULL[table]), nullable & set(NOT_NULL[table])


def test_nullable_links_of_the_contract_are_nullable(db_conn):
    nullable = {
        (table, name)
        for table, name in db_conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'infra' AND is_nullable = 'YES'"
        )
    }
    for pair in [("infrastructure_assets", "building_id"), ("infrastructure_assets", "road_id"), ("infrastructure_assets", "name"),
                 ("anomalies", "cluster_id"), ("simulation_events", "sensor_id"), ("simulation_events", "asset_id"),
                 ("sensor_thresholds", "warn_low"), ("sensor_thresholds", "crit_high")]:  # fmt: skip
        assert pair in nullable, pair


def test_unique_constraints(db_conn):
    unique = {
        (table.removeprefix("infra."), tuple(columns))
        for table, columns in db_conn.execute(
            """
            SELECT c.conrelid::regclass::text,
                   (SELECT array_agg(a.attname ORDER BY a.attnum) FROM pg_attribute a
                    WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey))
            FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE c.contype = 'u' AND n.nspname = 'infra'
            """
        )
    }
    assert {("study_areas", ("slug",)), ("buildings", ("osm_id",)), ("roads", ("osm_id",)),
            ("infrastructure_assets", ("building_id",))} <= unique  # fmt: skip


def test_defaults(db_conn):
    defaults = {
        (table, name): default
        for table, name, default in db_conn.execute(
            "SELECT table_name, column_name, column_default FROM information_schema.columns WHERE table_schema = 'infra'"
        )
    }
    assert defaults[("roads", "is_bridge")] == "false"
    assert defaults[("infrastructure_assets", "is_simulated")] == "false"
    assert defaults[("infrastructure_assets", "properties")].startswith("'{}'")
    assert defaults[("sensors", "is_simulated")] == "true"
    assert defaults[("sensors", "source")].startswith("'simulator'")
    assert defaults[("sensors", "sampling_interval_s")] == "3600"
    assert defaults[("sensor_readings", "status")].startswith("'ok'")
    assert defaults[("sensor_readings", "ingested_at")] == "now()"


# --- CHECK constraints: every invalid value is refused -------------------------------------------------------------
INVALID_UPDATES = [
    ("data_sources", "kind = 'guessed'"),
    ("buildings", "height_m = 0"),
    ("buildings", "height_m = -3"),
    ("buildings", "height_source = 'invented'"),
    ("infrastructure_assets", "asset_type = 'tunnel'"),
    ("sensor_thresholds", "sensor_type = 'humidity'"),
    ("sensor_readings", "status = 'bad'"),
    ("anomalies", "anomaly_score = 1.01"),
    ("anomalies", "anomaly_score = -0.01"),
    ("anomalies", "severity = 'extreme'"),
    ("anomalies", "status = 'open'"),
    ("anomalies", "started_at = ended_at + interval '1 hour'"),
    ("anomalies", "ended_at = peak_at - interval '1 hour'"),
    ("asset_health", "health_score = 101"),
    ("asset_health", "health_score = -1"),
    ("asset_health", "status = 'not_monitored'"),
    ("risk_zone_scores", "risk_score = 100.5"),
    ("risk_zone_scores", "risk_score = -1"),
    ("risk_zone_scores", "risk_level = 'extreme'"),
    ("anomaly_clusters", "max_severity = 'extreme'"),
    ("simulation_events", "ended_at = started_at - interval '1 hour'"),
    ("sensors", "sampling_interval_s = 0"),
]


@pytest.mark.parametrize(("table", "assignment"), INVALID_UPDATES, ids=[f"{t}: {a}" for t, a in INVALID_UPDATES])
def test_check_constraints_refuse_invalid_values(db_conn, table, assignment):
    assert db_conn.execute(f"SELECT count(*) FROM infra.{table}").fetchone()[0] > 0  # there are rows to update
    with pytest.raises(errors.CheckViolation):
        db_conn.execute(f"UPDATE infra.{table} SET {assignment}")


def test_invalid_building_geometry_is_refused(db_conn):
    bowtie = "SRID=4326;POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))"
    with pytest.raises(errors.CheckViolation):
        db_conn.execute("UPDATE infra.buildings SET geom = %s::geometry WHERE building_id = (SELECT min(building_id) FROM infra.buildings)", (bowtie,))


def test_wrong_srid_and_geometry_type_are_refused(db_conn):
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        db_conn.execute("UPDATE infra.sensors SET geom = ST_SetSRID(ST_MakePoint(1, 2), 3857)")
    db_conn.rollback()
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        db_conn.execute("UPDATE infra.sensors SET geom = ST_SetSRID(ST_MakeLine(ST_MakePoint(1, 2), ST_MakePoint(3, 4)), 4326)")


def test_the_database_holds_exactly_one_study_area(db_conn):
    assert db_conn.execute("SELECT count(*) FROM infra.study_areas").fetchone()[0] == 1
    with pytest.raises(errors.UniqueViolation):
        db_conn.execute(
            "INSERT INTO infra.study_areas (slug, name, utm_srid, timezone, geom) "
            "SELECT 'second-area', 'Second', utm_srid, timezone, geom FROM infra.study_areas"
        )


def test_a_sensor_class_must_exist_in_the_threshold_table(db_conn):
    with pytest.raises(errors.ForeignKeyViolation):
        db_conn.execute("UPDATE infra.sensors SET placement = 'rooftop' WHERE sensor_id = (SELECT min(sensor_id) FROM infra.sensors)")


def test_an_anomaly_peak_must_be_an_existing_reading_of_its_sensor(db_conn):
    with pytest.raises(errors.ForeignKeyViolation):
        db_conn.execute(
            "UPDATE infra.anomalies SET peak_at = peak_at + interval '17 minutes' "
            "WHERE anomaly_id = (SELECT min(anomaly_id) FROM infra.anomalies WHERE peak_at < ended_at)"
        )


# --- geometry columns and indexes ---------------------------------------------------------------------------------
def test_geometry_columns_have_the_declared_types_in_srid_4326(db_conn):
    rows = db_conn.execute(
        "SELECT f_table_name, f_geometry_column, type, srid FROM public.geometry_columns "
        "WHERE f_table_schema = 'infra' AND f_table_name NOT LIKE 'v\\_%'"
    ).fetchall()
    assert {(table, name): kind for table, name, kind, _ in rows} == GEOMETRY_COLUMNS
    assert {srid for *_, srid in rows} == {4326}


def index_definitions(conn) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for table, definition in conn.execute("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'infra'"):
        found.setdefault(table, []).append(definition)
    return found


def test_every_geometry_column_has_a_gist_index(db_conn):
    indexes = index_definitions(db_conn)
    for table, name in GEOMETRY_COLUMNS:
        assert any(f"USING gist ({name})" in definition for definition in indexes[table]), (table, name)


@pytest.mark.parametrize("table", ["infrastructure_assets", "sensors", "anomalies"])
def test_expression_gist_index_on_geography(db_conn, table):
    definitions = index_definitions(db_conn)[table]
    assert any("USING gist" in d and "(geom)::geography" in d.replace('"', "") for d in definitions), definitions


@pytest.mark.parametrize(("table", "columns"), BTREE_INDEXES)
def test_btree_indexes(db_conn, table, columns):
    definitions = index_definitions(db_conn)[table]
    assert any(f"USING btree {columns}" in definition for definition in definitions), definitions


def test_radius_search_uses_the_geography_index(db_conn):
    db_conn.execute("SET LOCAL enable_seqscan = off")
    plan = "\n".join(
        row[0]
        for row in db_conn.execute(
            "EXPLAIN SELECT asset_id FROM infra.infrastructure_assets a "
            "WHERE ST_DWithin(a.geom::geography, ST_SetSRID(ST_MakePoint(-100.0175, 37.7535), 4326)::geography, 100)"
        )
    )
    assert "infrastructure_assets_geog_gix" in plan


# --- views and functions ------------------------------------------------------------------------------------------
def test_view_asset_summary_marks_unmonitored_assets(db_conn):
    rows = db_conn.execute(
        "SELECT monitored, count(*), count(health_score), array_agg(DISTINCT status) FROM infra.v_asset_summary GROUP BY monitored"
    ).fetchall()
    by_flag = {row[0]: row[1:] for row in rows}
    assert by_flag[False][1] == 0 and by_flag[False][2] == ["not_monitored"]  # no score for assets without sensors
    assert by_flag[True][0] == by_flag[True][1]  # every monitored asset has a score
    assert set(by_flag[True][2]) <= {"normal", "watch", "at_risk", "critical"}
    columns = set(column(db_conn, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'infra' AND table_name = 'v_asset_summary'"))
    assert {"asset_id", "asset_type", "sensor_count", "sensor_types", "anomaly_count", "health_score", "status"} <= columns
    totals = db_conn.execute(
        "SELECT (SELECT sum(sensor_count) FROM infra.v_asset_summary), (SELECT count(*) FROM infra.sensors), "
        "(SELECT sum(anomaly_count) FROM infra.v_asset_summary), (SELECT count(*) FROM infra.anomalies), "
        "(SELECT count(*) FROM infra.v_asset_summary), (SELECT count(*) FROM infra.infrastructure_assets)"
    ).fetchone()
    assert totals[0] == totals[1] and totals[2] == totals[3] and totals[4] == totals[5]


def test_view_asset_health_latest_is_the_score_at_the_end_of_the_window(db_conn):
    mismatches = db_conn.execute(
        """
        SELECT count(*) FROM infra.v_asset_health_latest v
        JOIN infra.asset_health h ON h.asset_id = v.asset_id
         AND h.as_of = (SELECT window_end FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1)
        WHERE v.as_of <> h.as_of OR v.health_score <> h.health_score OR v.status <> h.status
        """
    ).fetchone()[0]
    assert mismatches == 0
    assert db_conn.execute("SELECT count(*) FROM infra.v_asset_health_latest").fetchone()[0] == db_conn.execute(
        "SELECT count(DISTINCT asset_id) FROM infra.sensors"
    ).fetchone()[0]


def test_view_sensor_latest_has_one_row_per_sensor_with_its_last_reading(db_conn):
    mismatches = db_conn.execute(
        """
        SELECT count(*) FROM infra.v_sensor_latest v
        WHERE v.ts IS DISTINCT FROM (SELECT max(r.ts) FROM infra.sensor_readings r WHERE r.sensor_id = v.sensor_id)
        """
    ).fetchone()[0]
    assert mismatches == 0
    assert db_conn.execute("SELECT count(*) FROM infra.v_sensor_latest").fetchone()[0] == db_conn.execute("SELECT count(*) FROM infra.sensors").fetchone()[0]


def test_view_active_anomalies_follows_the_time_rule_at_the_end_of_the_window(db_conn):
    """Amendment A1: one definition of active - ended_at >= T_end."""
    view = column(db_conn, "SELECT anomaly_id FROM infra.v_active_anomalies ORDER BY anomaly_id")
    rule = column(
        db_conn,
        """
        SELECT an.anomaly_id FROM infra.anomalies an,
             (SELECT window_end AS t_end FROM infra.detection_runs ORDER BY run_id DESC LIMIT 1) r
        WHERE an.started_at <= r.t_end AND an.ended_at >= r.t_end
        ORDER BY an.anomaly_id
        """,
    )
    assert view == rule and len(view) >= 2


def test_spatial_functions_exist_with_a_pinned_search_path(db_conn):
    rows = db_conn.execute(
        """
        SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.proconfig
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'infra'
        """
    ).fetchall()
    signatures = {name: arguments for name, arguments, _ in rows}
    assert set(signatures) == {"assets_within_radius", "nearest_asset", "sensors_in_asset_area"}
    assert signatures["assets_within_radius"] == "p_lon double precision, p_lat double precision, p_radius_m double precision"
    assert signatures["nearest_asset"] == "p_lon double precision, p_lat double precision, p_asset_type text"
    assert signatures["sensors_in_asset_area"] == "p_asset_id text, p_buffer_m double precision"
    for _, _, config in rows:
        assert config and any(setting.replace(" ", "") == "search_path=infra,public" for setting in config)
    # the asset type of nearest_asset is optional
    assert db_conn.execute("SELECT count(*) FROM infra.nearest_asset(-100.0175, 37.7535)").fetchone()[0] == 1
