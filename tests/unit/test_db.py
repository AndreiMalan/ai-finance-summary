"""Tests for pool creation, idempotent migrations and LangGraph table setup."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from statbrief.storage import db
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


def test_migrations_fail_loudly_when_none_bundled(settings, monkeypatch):
    """A missing migrations bundle raises instead of silently doing nothing."""
    monkeypatch.setattr(db, "_migration_files", lambda: [])
    pool = create_pool(settings)
    try:
        with pytest.raises(RuntimeError, match="no SQL migrations found"):
            run_migrations(pool)
    finally:
        pool.close()


def test_pool_connection_settings(settings):
    """Pooled connections are autocommit with server-side prepares disabled."""
    pool = create_pool(settings)
    try:
        with pool.connection() as c:
            assert c.autocommit is True
            assert c.prepare_threshold is None
    finally:
        pool.close()


def _public_tables(pool) -> set[str]:
    """Return the names of all tables in the public schema."""
    with pool.connection() as c:
        return {r["tablename"] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}


def test_langgraph_tables_created(settings):
    """Checkpointer and store tables exist after setup."""
    pool = create_pool(settings)
    try:
        setup_langgraph(pool)
        assert {"checkpoints", "checkpoint_writes", "store"} <= _public_tables(pool)
    finally:
        pool.close()


def test_langgraph_setup_concurrent_is_safe(settings):
    """Two simultaneous cold-start setups on one pool both succeed."""
    pool = create_pool(settings.model_copy(update={"db_pool_max": 4}))
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = [ex.submit(setup_langgraph, pool) for _ in range(2)]
            for f in futures:
                f.result()
        assert {"checkpoints", "checkpoint_writes", "store"} <= _public_tables(pool)
    finally:
        pool.close()
