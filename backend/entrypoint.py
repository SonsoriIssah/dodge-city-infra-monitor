"""Container entrypoint: wait for the database -> apply migrations -> AUTO_SEED -> exec uvicorn.

    python -m backend.entrypoint [--wait-attempts N] [--wait-interval SECONDS]

1. Waits for PostgreSQL with a bounded number of attempts (one log line per attempt). When the database never
   answers, the process exits with code 2 and a message naming the target (host, port, database, user).
2. Applies the schema migrations (``sql/migrations``; idempotent).
3. When ``AUTO_SEED`` is true and ``infra.detection_runs`` has no finished row, runs the pipeline in-process
   exactly like ``python run_pipeline.py --skip-download --skip-export`` (the committed ``data/raw`` files are in
   the image; nothing is downloaded and the committed dashboard snapshot is not rewritten).
4. Replaces this process with uvicorn serving ``backend.app.main:create_app`` on ``API_HOST:API_PORT``.

Steps 2-3 hold a PostgreSQL advisory lock, so two containers started against one database never seed it at the
same time: the second one waits, then finds the finished run and skips the pipeline.

Connections are described in log lines by host, port, database and user only; the password never appears.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from types import FrameType

import psycopg
from psycopg.conninfo import conninfo_to_dict

from pipeline.config import ROOT, Settings, describe_dsn, get_settings
from pipeline.db.connection import connect
from pipeline.db.migrate import apply_migrations
from pipeline.logging_utils import setup_logging
from pipeline.stages import EXIT_DATABASE_UNAVAILABLE, EXIT_FAILED, EXIT_OK

logger = logging.getLogger("backend.entrypoint")

APP_FACTORY = "backend.app.main:create_app"
SEED_PIPELINE_ARGS: tuple[str, ...] = ("--skip-download", "--skip-export")
DEFAULT_WAIT_ATTEMPTS = 30
DEFAULT_WAIT_INTERVAL_S = 2.0
# pg_advisory_lock key that serialises start-up (migrations + AUTO_SEED) between containers: "DCIM" in ASCII.
STARTUP_LOCK_KEY = 0x4443494D


def _password(dsn: str) -> str:
    try:
        return conninfo_to_dict(dsn).get("password") or ""
    except psycopg.Error:
        return ""


def safe_error(exc: BaseException, dsn: str) -> str:
    """First line of an error message for a log line, with the DSN password masked should it ever appear."""
    text = str(exc).strip()
    text = text.splitlines()[0] if text else type(exc).__name__
    password = _password(dsn)
    return text.replace(password, "***") if password else text


def wait_for_database(dsn: str, attempts: int, interval_s: float) -> bool:
    """Try to connect up to ``attempts`` times, ``interval_s`` seconds apart; True once the server answers."""
    target = describe_dsn(dsn)
    for attempt in range(1, attempts + 1):
        try:
            with connect(dsn) as conn:
                version = conn.execute("SELECT current_setting('server_version')").fetchone()[0]
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                logger.error("database attempt %d/%d failed (%s): %s", attempt, attempts, target, safe_error(exc, dsn))
                break
            logger.info(
                "waiting for the database (%s): attempt %d/%d failed: %s; retrying in %.0f s",
                target, attempt, attempts, safe_error(exc, dsn), interval_s,
            )  # fmt: skip
            time.sleep(interval_s)
        else:
            logger.info("database reachable after %d attempt(s): %s (PostgreSQL %s)", attempt, target, version)
            return True
    return False


def latest_finished_run(conn: psycopg.Connection) -> tuple[int, object] | None:
    """``(run_id, finished_at)`` of the latest finished detection run, or None when there is none."""
    return conn.execute(
        "SELECT run_id, finished_at FROM infra.detection_runs WHERE finished_at IS NOT NULL "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()


def run_seed_pipeline() -> int:
    """Run the pipeline in-process (``run_pipeline.py --skip-download --skip-export``); returns its exit code."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import run_pipeline

    return run_pipeline.main(list(SEED_PIPELINE_ARGS))


def prepare_database(conn: psycopg.Connection, settings: Settings) -> int:
    """Apply the migrations, then seed the database when AUTO_SEED is on and it holds no analysed data."""
    with conn.transaction():
        applied = apply_migrations(conn)
    if applied:
        logger.info("migrations applied: %s", ", ".join(applied))

    latest = latest_finished_run(conn)
    if latest is not None:
        run_id, finished_at = latest
        logger.info("data present: detection run %d finished at %s; AUTO_SEED not needed", run_id, finished_at)
        return EXIT_OK
    if not settings.AUTO_SEED:
        logger.warning(
            "the database holds no finished detection run and AUTO_SEED is false: data endpoints answer 503 "
            "until the pipeline has run (python run_pipeline.py --skip-download)"
        )
        return EXIT_OK

    logger.info(
        "AUTO_SEED: no finished detection run; running the pipeline (run_pipeline.py %s)", " ".join(SEED_PIPELINE_ARGS)
    )
    started = time.perf_counter()
    code = run_seed_pipeline()
    if code != EXIT_OK:
        logger.error("AUTO_SEED pipeline failed with exit code %d; the API is not started", code)
        return code
    logger.info("AUTO_SEED pipeline finished in %.1f s", time.perf_counter() - started)
    return EXIT_OK


def uvicorn_command(settings: Settings) -> list[str]:
    """Command line of the API server."""
    return [
        sys.executable, "-m", "uvicorn", APP_FACTORY, "--factory",
        "--host", settings.API_HOST, "--port", str(settings.API_PORT),
    ]  # fmt: skip


def serve(settings: Settings) -> int:
    """Replace this process with uvicorn (POSIX); on Windows run it as a child process and return its exit code."""
    command = uvicorn_command(settings)
    logger.info(
        "starting uvicorn: %s on http://%s:%d (dashboard %s)",
        APP_FACTORY, settings.API_HOST, settings.API_PORT, "served at /" if settings.SERVE_DASHBOARD else "not served",
    )  # fmt: skip
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":  # os.execv on Windows starts a new process and returns to the shell at once
        return subprocess.call(command)
    os.execv(sys.executable, command)
    return EXIT_FAILED  # not reached: execv replaces the process or raises OSError


def _stop_on_sigterm(signum: int, frame: FrameType | None) -> None:
    """As PID 1 in a container the default SIGTERM action does nothing: stop the start-up instead."""
    logger.warning("received signal %d during start-up; stopping", signum)
    raise SystemExit(128 + signum)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the start-up sequence; returns an exit code only when the API is not started (or on Windows)."""
    parser = argparse.ArgumentParser(prog="backend.entrypoint", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--wait-attempts", type=int, default=DEFAULT_WAIT_ATTEMPTS, metavar="N",
        help=f"connection attempts before giving up (default {DEFAULT_WAIT_ATTEMPTS})",
    )  # fmt: skip
    parser.add_argument(
        "--wait-interval", type=float, default=DEFAULT_WAIT_INTERVAL_S, metavar="SECONDS",
        help=f"pause between attempts (default {DEFAULT_WAIT_INTERVAL_S:.0f} s)",
    )  # fmt: skip
    args = parser.parse_args(argv)
    if args.wait_attempts < 1 or args.wait_interval < 0:
        parser.error("--wait-attempts must be >= 1 and --wait-interval >= 0")

    setup_logging()
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    settings = get_settings()
    dsn = settings.dsn
    logger.info(
        "start-up: database %s; AUTO_SEED=%s; SERVE_DASHBOARD=%s",
        settings.dsn_summary(), settings.AUTO_SEED, settings.SERVE_DASHBOARD,
    )  # fmt: skip

    try:
        reachable = wait_for_database(dsn, args.wait_attempts, args.wait_interval)
    except psycopg.Error as exc:  # e.g. a malformed DATABASE_URL: retrying cannot help
        logger.error("cannot connect to the database (%s): %s", settings.dsn_summary(), safe_error(exc, dsn))
        return EXIT_DATABASE_UNAVAILABLE
    if not reachable:
        logger.error(
            "the database never became reachable (%s) after %d attempts; check that it is running and that "
            "DATABASE_URL or POSTGRES_HOST/POSTGRES_PORT point to it. Exiting.",
            settings.dsn_summary(), args.wait_attempts,
        )  # fmt: skip
        return EXIT_DATABASE_UNAVAILABLE

    try:
        with connect(dsn, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (STARTUP_LOCK_KEY,))
            code = prepare_database(conn, settings)
    except psycopg.OperationalError as exc:
        logger.error("database connection lost during start-up: %s", safe_error(exc, dsn))
        return EXIT_DATABASE_UNAVAILABLE
    except psycopg.Error as exc:
        logger.error("database start-up failed: %s: %s", type(exc).__name__, safe_error(exc, dsn))
        return EXIT_FAILED
    if code != EXIT_OK:
        return code
    return serve(settings)


if __name__ == "__main__":
    raise SystemExit(main())
