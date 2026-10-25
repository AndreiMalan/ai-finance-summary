"""Tests for pool creation, idempotent migrations and LangGraph table setup."""
from statbrief.storage.db import create_pool, run_migrations, setup_langgraph


def test_migrations_apply_once(settings):
    """First run applies 001; second run applies nothing."""
    pool = create_pool(settings)
    try:
        assert run_migrations(pool) == ["001_auth_limits"]
        assert run_migrations(pool) == []
        with pool.connection() as c:
            row = c.execute("SELECT count(*) AS n FROM users").fetchone()
        assert row["n"] == 0
    finally:
        pool.close()


def test_langgraph_tables_created(settings):
    """Checkpointer and store tables exist after setup."""
    pool = create_pool(settings)
    try:
        setup_langgraph(pool)
        with pool.connection() as c:
            names = {r["tablename"] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert {"checkpoints", "checkpoint_writes", "store"} <= names
    finally:
        pool.close()
