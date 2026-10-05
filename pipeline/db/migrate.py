"""Schema migrations: ``sql/migrations/*.sql`` applied in file-name order, tracked in ``infra.schema_migrations``.

The functions take an explicit connection and never commit: the caller decides the transaction (the seed stage
applies migrations and loads data in one transaction).
"""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from pipeline.config import MIGRATIONS_DIR

logger = logging.getLogger(__name__)

SCHEMA = "infra"
_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS infra;
CREATE TABLE IF NOT EXISTS infra.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migration files in the order they are applied (sorted by file name)."""
    return sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name)


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """Versions recorded in ``infra.schema_migrations`` (empty when the table does not exist yet)."""
    exists = conn.execute("SELECT to_regclass('infra.schema_migrations') IS NOT NULL").fetchone()[0]
    if not exists:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM infra.schema_migrations")}


def apply_migrations(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration that is not recorded yet; return the versions applied by this call.

    Idempotent: a second call applies nothing. Nothing is committed here.
    """
    files = migration_files(migrations_dir)
    if not files:
        raise FileNotFoundError(f"no migration files found in {migrations_dir}")
    conn.execute(_BOOTSTRAP)
    done = applied_versions(conn)
    applied: list[str] = []
    for path in files:
        version = path.stem
        if version in done:
            continue
        logger.info("applying migration %s", path.name)
        conn.execute(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO infra.schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (version,),
        )
        applied.append(version)
    if not applied:
        logger.info("database schema is up to date (%d migrations recorded)", len(done))
    return applied


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop the whole ``infra`` schema (development convenience; every table, view and function goes)."""
    logger.warning("dropping schema %s CASCADE", SCHEMA)
    conn.execute("DROP SCHEMA IF EXISTS infra CASCADE")
