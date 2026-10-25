"""Postgres connection pool, SQL migration runner and LangGraph table setup."""
from importlib import resources

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from statbrief.config import Settings

_MIGRATIONS_PACKAGE = "statbrief.storage.migrations"
_MIGRATION_LOCK_KEY = 0x5B_0001  # pg advisory lock id serialising concurrent cold starts


def create_pool(settings: Settings) -> ConnectionPool:
    """Open a small psycopg pool configured for serverless + Neon's pooler."""
    pool = ConnectionPool(
        settings.database_url,
        min_size=1,
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
    applied: list[str] = []
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in _migration_files():
            if version in done:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


def setup_langgraph(pool: ConnectionPool) -> None:
    """Create LangGraph checkpointer and store tables (idempotent)."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    PostgresSaver(pool).setup()
    PostgresStore(pool).setup()
