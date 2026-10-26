"""Postgres connection pool, SQL migration runner and LangGraph table setup."""
import logging
import math
import time
from importlib import resources
from types import MappingProxyType
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from statbrief.config import Settings

logger = logging.getLogger(__name__)

_MIGRATIONS_PACKAGE = "statbrief.storage.migrations"
_MIGRATION_LOCK_KEY = 0x5B_0001  # pg advisory lock id serialising concurrent cold starts
_LANGGRAPH_SETUP_LOCK_KEY = 0x5B_0002  # pg advisory lock id serialising concurrent LangGraph table setup
# LangGraph's migrations use CREATE INDEX CONCURRENTLY, which waits for every open transaction -
# including a session blocked in pg_advisory_lock - so we poll pg_try_advisory_lock instead.
_LANGGRAPH_SETUP_LOCK_POLL_SECONDS = 0.1
# Every connection (pooled or direct): no implicit transactions, no server-side prepared
# statements (unsupported by Neon's transaction-mode pooler), rows as dicts.
# Read-only and shared; per-call kwargs are built from a copy by `connection_kwargs`.
CONN_KWARGS = MappingProxyType({"autocommit": True, "prepare_threshold": None, "row_factory": dict_row})


def connection_kwargs(connect_timeout_s: float | None = None) -> dict[str, Any]:
    """Return a fresh kwargs dict: CONN_KWARGS plus libpq `connect_timeout` when given.

    libpq only accepts whole seconds, so the timeout is rounded up (and is at least 1).
    """
    kwargs = dict(CONN_KWARGS)
    if connect_timeout_s is not None:
        kwargs["connect_timeout"] = max(1, math.ceil(connect_timeout_s))
    return kwargs


def connect(url: str, connect_timeout_s: float | None = None) -> Connection:
    """Open a single direct psycopg connection with the app's standard settings."""
    return Connection.connect(url, **connection_kwargs(connect_timeout_s))


def create_pool(settings: Settings) -> ConnectionPool:
    """Open a small psycopg pool on the (pooled) runtime URL, sized from settings.

    Connections are health-checked on checkout (`check_connection`) so backends killed while
    idle (e.g. Neon scale-to-zero) are replaced transparently. Opening waits at most
    DB_POOL_OPEN_TIMEOUT_S and raises PoolTimeout otherwise; the failed pool is closed.
    """
    pool = ConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs=connection_kwargs(settings.db_connect_timeout_s),
        check=ConnectionPool.check_connection,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=settings.db_pool_open_timeout_s)
    except BaseException:
        pool.close()
        raise
    return pool


def bootstrap(settings: Settings) -> None:
    """Run app migrations and LangGraph setup on one short-lived direct connection.

    Uses `settings.bootstrap_database_url` (Neon's unpooled URL when configured) because the
    session-level advisory lock is unsafe through a transaction-mode pooler.
    """
    with connect(settings.bootstrap_database_url, settings.db_connect_timeout_s) as conn:
        run_migrations(conn)
        setup_langgraph(conn, settings.db_setup_lock_timeout_s)


def _migration_files() -> list[tuple[str, str]]:
    """Return (version, sql) for every bundled migration, ordered by filename."""
    files = sorted(
        (f for f in resources.files(_MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


def run_migrations(conn: Connection) -> list[str]:
    """Apply pending migrations in order inside one locked transaction; return applied versions."""
    migrations = _migration_files()
    if not migrations:
        raise RuntimeError(f"no SQL migrations found in {_MIGRATIONS_PACKAGE}")
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in migrations:
            if version in done:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


def _acquire_setup_lock(conn: Connection, timeout_s: float) -> None:
    """Poll for the LangGraph setup advisory lock without holding a transaction open while waiting."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LANGGRAPH_SETUP_LOCK_KEY,)).fetchone()
        if row["ok"]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for the LangGraph setup advisory lock")
        time.sleep(_LANGGRAPH_SETUP_LOCK_POLL_SECONDS)


def _release_setup_lock(conn: Connection) -> None:
    """Release the LangGraph setup advisory lock; raise if this session did not hold it."""
    row = conn.execute("SELECT pg_advisory_unlock(%s) AS ok", (_LANGGRAPH_SETUP_LOCK_KEY,)).fetchone()
    if not row["ok"]:
        raise RuntimeError("LangGraph setup advisory lock was not held by this session")


def setup_langgraph(conn: Connection, lock_timeout_s: float) -> None:
    """Create LangGraph checkpointer and store tables (idempotent, serialised across processes).

    Must run on a direct (unpooled) connection: it holds a session-level advisory lock.
    An unlock failure never masks an error raised by the setup itself.
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    _acquire_setup_lock(conn, lock_timeout_s)
    try:
        PostgresSaver(conn).setup()
        PostgresStore(conn).setup()
    except BaseException:
        try:
            _release_setup_lock(conn)
        except Exception:
            logger.warning("failed to release LangGraph setup lock after setup error", exc_info=True)
        raise
    _release_setup_lock(conn)
