"""PostgreSQL / PostGIS connections (synchronous psycopg 3 only).

Every connection sets ``timezone=UTC`` and ``search_path=infra,public`` so timestamps come back in UTC and
unqualified names resolve the same way for every database role.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from pipeline.config import Settings, describe_dsn

CONNECTION_OPTIONS = "-c timezone=UTC -c search_path=infra,public"
CONNECT_TIMEOUT_S = 5

__all__ = ["CONNECTION_OPTIONS", "connect", "connection_pool", "describe_dsn", "dsn"]


def dsn(settings: Settings) -> str:
    """Connection string for the settings: non-empty DATABASE_URL, else built from the POSTGRES_* parts."""
    return settings.dsn


def _resolve(target: Settings | str) -> str:
    return target if isinstance(target, str) else dsn(target)


def connect(target: Settings | str, *, autocommit: bool = False, **kwargs: Any) -> psycopg.Connection:
    """Open a connection from settings or from an explicit DSN string.

    The caller owns the connection (use it as a context manager or close it). Extra keyword arguments are
    passed to ``psycopg.connect`` (for example ``row_factory``).
    """
    kwargs.setdefault("connect_timeout", CONNECT_TIMEOUT_S)
    kwargs.setdefault("options", CONNECTION_OPTIONS)
    return psycopg.connect(_resolve(target), autocommit=autocommit, **kwargs)


def connection_pool(
    target: Settings | str, *, min_size: int = 1, max_size: int = 8, timeout: float = 3.0
) -> ConnectionPool:
    """A closed connection pool (``open=False``); the API opens it in its lifespan with ``wait=False``."""
    return ConnectionPool(
        conninfo=_resolve(target),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs={"options": CONNECTION_OPTIONS, "connect_timeout": CONNECT_TIMEOUT_S},
        name="dcim-api",
    )
