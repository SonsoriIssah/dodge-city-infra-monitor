"""Shared fixtures of the test suite.

* Unit tests need no database: ``default_settings`` and the ``default_*`` fixtures run the pure parts of the
  pipeline (processing, placement, simulation, detection) on the committed ``data/raw`` files in memory.
* Integration tests are marked ``db`` and use ONLY the test database (``TEST_DATABASE_URL``, by default the
  configured server with database ``infra_test``). The ``test_db`` session fixture drops and recreates that
  database, applies the migrations, and runs the real pipeline stages 2-6 in-process with the default
  settings (30 days, seed 42) once per session. Before any write it asserts that it is connected to a
  database whose name ends with ``_test``.
* The main database is never written: ``main_database_guard`` records its fingerprint (row counts, md5 of
  the readings and of the anomalies) before the test database is created and compares it again when the
  session ends; ``tests/test_zz_main_database_guard.py`` asserts the same as the last test of the session.

PostGIS unreachable: the ``db`` tests are skipped, unless ``REQUIRE_DB=1``, which makes that an error.
``KEEP_TEST_DB=1`` leaves ``infra_test`` in place after the session (it is dropped otherwise).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# The pipeline logs every stage at INFO level; the suite keeps warnings and errors only.
os.environ.setdefault("LOG_LEVEL", "WARNING")

from pipeline.config import RAW_DIR, Settings, describe_dsn, get_settings  # noqa: E402
from pipeline.db.connection import connect  # noqa: E402
from tests.support import SPEC_DEFAULTS, database_fingerprint  # noqa: E402

TEST_SUFFIX = "_test"
MAINTENANCE_DATABASE = "postgres"
CONNECT_TIMEOUT_S = 5
TRUE_VALUES = ("1", "true", "yes", "on")
GUARD_MODULE = "test_zz_main_database_guard"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


# --- collection ---------------------------------------------------------------------------------------------------
DATABASE_FIXTURES = frozenset({"database_targets", "main_database_guard", "test_db", "db_conn", "client", "get_json"})


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark database tests, deselect ``slow`` tests unless asked for, run the main-database guard last.

    Every test that (directly or through another fixture) uses the test database gets the ``db`` marker, so
    ``-m "not db"`` can never open a database connection.
    """
    for item in items:
        if DATABASE_FIXTURES & set(getattr(item, "fixturenames", ())) and not item.get_closest_marker("db"):
            item.add_marker(pytest.mark.db)
    if "slow" not in (config.option.markexpr or ""):
        slow = [item for item in items if item.get_closest_marker("slow")]
        if slow:
            config.hook.pytest_deselected(items=slow)
            items[:] = [item for item in items if not item.get_closest_marker("slow")]
    items.sort(key=lambda item: GUARD_MODULE in item.nodeid)  # stable: only the guard module moves to the end


# --- settings without a database ----------------------------------------------------------------------------------
def make_settings(**overrides: Any) -> Settings:
    """Settings with the contract defaults, independent of ``.env`` and of the process environment."""
    return Settings(_env_file=None, **{**SPEC_DEFAULTS, **overrides})


@pytest.fixture(scope="session")
def settings_factory() -> Callable[..., Settings]:
    """Build settings from the contract defaults plus overrides."""
    return make_settings


@pytest.fixture(scope="session")
def default_settings() -> Settings:
    """The default configuration of the contract (30 days, seed 42, 40 events); no database values."""
    return make_settings()


# --- the default dataset in memory (no database) ------------------------------------------------------------------
@pytest.fixture(scope="session")
def default_processed(default_settings: Settings):
    """Stage 2 on the committed raw files (``pipeline.gis.process.process``)."""
    from pipeline.gis.process import process

    return process(RAW_DIR, default_settings)


def asset_records(processed) -> list:
    """Processed asset features as the records the placement rules read from the database."""
    from pipeline.sensors.placement import AssetRecord

    return [
        AssetRecord(
            asset_id=feature["properties"]["asset_id"],
            asset_type=feature["properties"]["asset_type"],
            name=feature["properties"]["name"],
            geometry=feature["geometry"],
            centroid=tuple(feature["properties"]["centroid"]),
            properties=feature["properties"]["attributes"],
            source_id=feature["properties"]["source_id"],
        )
        for feature in processed.assets["features"]
    ]


@pytest.fixture(scope="session")
def default_assets(default_processed) -> list:
    """The real assets of the default study area as placement records."""
    return asset_records(default_processed)


@pytest.fixture(scope="session")
def default_plan(default_assets, default_settings: Settings):
    """Sensor placement on the default assets (seed 42)."""
    from pipeline.sensors.placement import plan_placement

    return plan_placement(default_assets, default_settings)


@pytest.fixture(scope="session")
def default_simulation(default_plan, default_settings: Settings):
    """The default simulation run (30 days, 40 events, seed 42)."""
    from pipeline.sensors.simulator import simulate

    return simulate(default_plan.sensors, default_settings)


def detect_in_memory(simulation, settings: Settings):
    """Run the detection on a simulation result without a database: ``(grid, DetectionResult)``."""
    from pipeline.detection import runner
    from pipeline.sensors.thresholds import THRESHOLDS

    sensors = [runner.SensorRow.from_spec(spec) for spec in sorted(simulation.sensors, key=lambda s: s.sensor_id)]
    grid = runner.grid_from_readings(sensors, simulation.readings(), settings.sim_step)
    thresholds = {(t.sensor_type, t.placement): t for t in THRESHOLDS}
    return grid, runner.detect(grid, thresholds, settings)


@pytest.fixture(scope="session")
def default_detection(default_simulation, default_settings: Settings):
    """Detection over the default simulation, in memory: ``(grid, DetectionResult)``."""
    return detect_in_memory(default_simulation, default_settings)


# --- the test database --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DatabaseTargets:
    """Connection strings the suite works with. They are kept out of every repr: a failing test must not
    print a password."""

    test_dsn: str = field(repr=False)
    test_name: str
    maintenance_dsn: str = field(repr=False)
    main_dsn: str = field(repr=False)
    main_name: str


@dataclass(slots=True)
class PreparedDatabase:
    """The prepared test database: its settings and what the pipeline stages reported."""

    dsn: str = field(repr=False)
    name: str
    settings: Settings = field(repr=False)
    processed_dir: Path
    stage_results: dict[str, Any] = field(default_factory=dict, repr=False)


def _unavailable(reason: str) -> None:
    """Skip the db tests, or fail them when a database is required."""
    if _flag("REQUIRE_DB"):
        pytest.fail(f"REQUIRE_DB is set but the test database cannot be used: {reason}", pytrace=False)
    pytest.skip(f"PostGIS not reachable ({reason}); set REQUIRE_DB=1 to make this an error")


@pytest.fixture(scope="session")
def database_targets() -> DatabaseTargets:
    """Resolve the test and main connection strings from ``pipeline.config`` and check the server answers."""
    base = get_settings()
    test_dsn, main_dsn = base.test_dsn, base.dsn
    test_name = conninfo_to_dict(test_dsn).get("dbname") or ""
    main_name = conninfo_to_dict(main_dsn).get("dbname") or ""
    if not test_name.endswith(TEST_SUFFIX):
        pytest.fail(
            f"TEST_DATABASE_URL must name a database ending with '{TEST_SUFFIX}', got '{test_name}'", pytrace=False
        )
    if test_name == main_name:
        pytest.fail("the test database and the main database are the same database", pytrace=False)
    maintenance_dsn = make_conninfo(test_dsn, dbname=MAINTENANCE_DATABASE)
    try:
        with psycopg.connect(maintenance_dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S) as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError as exc:
        _unavailable(f"{type(exc).__name__} for {describe_dsn(maintenance_dsn)}")
    return DatabaseTargets(test_dsn, test_name, maintenance_dsn, main_dsn, main_name)


def main_fingerprint(targets: DatabaseTargets) -> dict[str, Any]:
    """Read-only fingerprint of the main database; ``{"database": None}`` when it does not exist."""
    try:
        with psycopg.connect(targets.main_dsn, connect_timeout=CONNECT_TIMEOUT_S) as conn:
            conn.read_only = True
            return database_fingerprint(conn)
    except psycopg.OperationalError:
        return {"database": None}


@pytest.fixture(scope="session")
def main_database_guard(database_targets: DatabaseTargets) -> Iterator[dict[str, Any]]:
    """Fingerprint of the main database taken before the test database exists; checked again at the end."""
    before = main_fingerprint(database_targets)
    yield before
    after = main_fingerprint(database_targets)
    assert after == before, "the main database changed during the test session"


def _recreate_database(targets: DatabaseTargets) -> None:
    name = sql.Identifier(targets.test_name)
    with psycopg.connect(targets.maintenance_dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))


def _drop_database(targets: DatabaseTargets) -> None:
    with psycopg.connect(targets.maintenance_dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(targets.test_name)))


def assert_test_database(conn: psycopg.Connection) -> str:
    """The name of the connected database; fails unless it ends with ``_test``."""
    name = conn.execute("SELECT current_database()").fetchone()[0]
    assert name.endswith(TEST_SUFFIX), f"refusing to write to database '{name}': its name does not end with _test"
    return name


def run_stages(database: PreparedDatabase, names: tuple[str, ...]) -> dict[str, Any]:
    """Run pipeline stages in-process against the test database, each in its own transaction."""
    from pipeline.stages import analyze, detect, generate, seed
    from pipeline.stages import process as process_stage

    results: dict[str, Any] = {}
    for name in names:
        if name == "process":
            results[name] = process_stage.run(database.settings, RAW_DIR, database.processed_dir)
            continue
        with connect(database.dsn) as conn:
            assert_test_database(conn)
            with conn.transaction():
                if name == "seed":
                    results[name] = seed.run(conn, database.settings, database.processed_dir, RAW_DIR)
                elif name == "generate":
                    results[name] = generate.run(conn, database.settings)
                elif name == "detect":
                    results[name] = detect.run(conn, database.settings)
                elif name == "analyze":
                    results[name] = analyze.run(conn, database.settings)
                else:
                    raise ValueError(f"unknown stage {name!r}")
    database.stage_results.update(results)
    return results


@pytest.fixture(scope="session")
def test_db(
    database_targets: DatabaseTargets, main_database_guard: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[PreparedDatabase]:
    """Fresh ``infra_test`` with the migrations applied and the full default pipeline (stages 2-6) run once."""
    from pipeline.db.migrate import apply_migrations

    targets = database_targets
    _recreate_database(targets)
    settings = make_settings(DATABASE_URL=targets.test_dsn, TEST_DATABASE_URL=targets.test_dsn)
    with connect(targets.test_dsn) as conn:
        name = assert_test_database(conn)  # before any write
        apply_migrations(conn)
        conn.commit()
    database = PreparedDatabase(
        dsn=targets.test_dsn, name=name, settings=settings, processed_dir=tmp_path_factory.mktemp("processed")
    )
    run_stages(database, ("process", "seed", "generate", "detect", "analyze"))
    yield database
    if not _flag("KEEP_TEST_DB"):
        _drop_database(targets)


@pytest.fixture
def db_conn(test_db: PreparedDatabase) -> Iterator[psycopg.Connection]:
    """A connection to the test database; whatever a test does through it is rolled back."""
    with connect(test_db.dsn) as conn:
        assert_test_database(conn)
        try:
            yield conn
        finally:
            conn.rollback()


# --- the API ------------------------------------------------------------------------------------------------------
def make_client(settings: Settings):
    """A ``TestClient`` (context manager) of the application built for ``settings``."""
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    return TestClient(create_app(settings=settings))


@pytest.fixture(scope="session")
def api_settings(test_db: PreparedDatabase) -> Settings:
    """Settings of the API under test: the test database, no dashboard mount, ingestion disabled."""
    return test_db.settings.model_copy(update={"SERVE_DASHBOARD": False, "INGEST_API_KEY": ""})


@pytest.fixture(scope="session")
def client(api_settings: Settings):
    """The API on the test database (one application for the session: one pool, one playback cache)."""
    with make_client(api_settings) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def get_json(client) -> Callable[..., Any]:
    """GET a path and return its JSON body, asserting status 200."""

    def fetch(url: str, **params: Any) -> Any:
        response = client.get(url, params=params or None)
        assert response.status_code == 200, f"GET {url} {params or ''} -> {response.status_code}: {response.text[:300]}"
        return response.json()

    return fetch
