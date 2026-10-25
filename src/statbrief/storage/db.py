"""Postgres connection pool, SQL migration runner and LangGraph table setup."""
import time
from importlib import resources

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from statbrief.config import Settings

_MIGRATIONS_PACKAGE = "statbrief.storage.migrations"
_MIGRATION_LOCK_KEY = 0x5B_0001  # pg advisory lock id serialising concurrent cold starts
_LANGGRAPH_SETUP_LOCK_KEY = 0x5B_0002  # pg advisory lock id serialising concurrent LangGraph table setup
# LangGraph's migrations use CREATE INDEX CONCURRENTLY, which waits for every open transaction -
# including a session blocked in pg_advisory_lock - so we poll pg_try_advisory_lock instead.
_LANGGRAPH_SETUP_LOCK_POLL_SECONDS = 0.1
_LANGGRAPH_SETUP_LOCK_TIMEOUT_SECONDS = 60.0


def create_pool(settings: Settings) -> ConnectionPool:
    """Open a small psycopg pool configured for serverless + Neon's pooler."""
    pool = ConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        open=False,
    )
    pool.open(wait=True)
    return pool


def _migration_files() -> list[tuple[str, str]]:
    """Return (version, sql) for every bundled migration, ordered by filename."""
    files = sorted(
        (f for f in resources.files(_MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


def run_migrations(pool: ConnectionPool) -> list[str]:
    """Apply pending migrations in order inside one locked transaction; return applied versions."""
    migrations = _migration_files()
    if not migrations:
        raise RuntimeError(f"no SQL migrations found in {_MIGRATIONS_PACKAGE}")
    applied: list[str] = []
    with pool.connection() as conn, conn.transaction():
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


def _acquire_setup_lock(conn: Connection) -> None:
    """Poll for the LangGraph setup advisory lock without holding a transaction open while waiting."""
    deadline = time.monotonic() + _LANGGRAPH_SETUP_LOCK_TIMEOUT_SECONDS
    while True:
        row = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LANGGRAPH_SETUP_LOCK_KEY,)).fetchone()
        if row["ok"]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for the LangGraph setup advisory lock")
        time.sleep(_LANGGRAPH_SETUP_LOCK_POLL_SECONDS)


def setup_langgraph(pool: ConnectionPool) -> None:
    """Create LangGraph checkpointer and store tables (idempotent, serialised across processes).

    Both setups run on one pooled connection holding a session-level advisory lock, so
    concurrent cold starts cannot race on CREATE TABLE.
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    with pool.connection() as conn:
        _acquire_setup_lock(conn)
        try:
            PostgresSaver(conn).setup()
            PostgresStore(conn).setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LANGGRAPH_SETUP_LOCK_KEY,))
