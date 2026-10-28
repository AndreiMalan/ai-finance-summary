"""Tests for pool creation, bootstrap (migrations + LangGraph setup) and the setup lock."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from statbrief.storage import db
from statbrief.storage.db import bootstrap, connect, create_pool, run_migrations, setup_langgraph

_LANGGRAPH_TABLES = {"checkpoints", "checkpoint_writes", "store"}


def _public_tables(url: str) -> set[str]:
    """Return the names of all tables in the public schema of the database at `url`."""
    with connect(url) as c:
        return {r["tablename"] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}


def test_migrations_apply_once(db_url):
    """First run applies every bundled migration (001-005) in order; second run applies nothing."""
    with connect(db_url) as conn:
        assert run_migrations(conn) == [
            "001_auth_limits", "002_reports_files", "003_api_cache", "004_knowledge_base", "005_run_events",
        ]
        assert run_migrations(conn) == []
        row = conn.execute("SELECT count(*) AS n FROM users").fetchone()
    assert row["n"] == 0


def test_migrations_fail_loudly_when_none_bundled(db_url, monkeypatch):
    """A missing migrations bundle raises instead of silently doing nothing."""
    monkeypatch.setattr(db, "_migration_files", lambda: [])
    with connect(db_url) as conn, pytest.raises(RuntimeError, match="no SQL migrations found"):
        run_migrations(conn)


def test_pool_connection_settings(settings):
    """Pooled connections are autocommit with server-side prepares disabled."""
    pool = create_pool(settings)
    try:
        with pool.connection() as c:
            assert c.autocommit is True
            assert c.prepare_threshold is None
    finally:
        pool.close()


def test_langgraph_tables_created(db_url, settings):
    """Checkpointer and store tables exist after setup."""
    with connect(db_url) as conn:
        setup_langgraph(conn, settings.db_setup_lock_timeout_s)
    assert _LANGGRAPH_TABLES <= _public_tables(db_url)


def test_bootstrap_uses_unpooled_url_and_creates_everything(settings, db_url):
    """bootstrap() runs migrations and LangGraph setup against the direct URL."""
    s = settings.model_copy(update={"database_url": "postgresql://invalid-host-not-used/x", "database_url_unpooled": db_url})
    bootstrap(s)
    tables = _public_tables(db_url)
    assert {"users", "schema_migrations"} | _LANGGRAPH_TABLES <= tables


def test_bootstrap_concurrent_is_safe(settings, db_url):
    """Two simultaneous cold-start bootstraps both succeed."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in [ex.submit(bootstrap, settings) for _ in range(2)]:
            f.result()
    assert {"users"} | _LANGGRAPH_TABLES <= _public_tables(db_url)


def test_setup_lock_times_out_when_held(db_url):
    """Acquiring the setup lock gives up with TimeoutError while another session holds it."""
    with connect(db_url) as holder, connect(db_url) as waiter:
        holder.execute("SELECT pg_advisory_lock(%s)", (db._LANGGRAPH_SETUP_LOCK_KEY,))
        with pytest.raises(TimeoutError):
            db._acquire_setup_lock(waiter, timeout_s=0.3)


def test_unlock_without_lock_raises(db_url):
    """Releasing a lock this session does not hold is reported, not ignored."""
    with connect(db_url) as conn, pytest.raises(RuntimeError, match="advisory lock"):
        db._release_setup_lock(conn)


def test_unlock_failure_does_not_mask_setup_error(db_url, monkeypatch, caplog):
    """If setup fails and the unlock also fails, the setup error propagates and the unlock error is logged."""
    from langgraph.checkpoint.postgres import PostgresSaver

    def _boom(self):
        """Simulate a failing checkpointer setup."""
        raise ValueError("setup exploded")

    def _bad_release(conn):
        """Simulate a failing unlock."""
        raise RuntimeError("unlock failed")

    monkeypatch.setattr(PostgresSaver, "setup", _boom)
    monkeypatch.setattr(db, "_release_setup_lock", _bad_release)
    with connect(db_url) as conn, pytest.raises(ValueError, match="setup exploded"):
        setup_langgraph(conn, 5.0)
    assert "unlock failed" in caplog.text


def test_conn_kwargs_public():
    """CONN_KWARGS is the public, shared connection configuration (Neon-pooler safe)."""
    assert db.CONN_KWARGS["prepare_threshold"] is None and db.CONN_KWARGS["autocommit"] is True


def test_pool_recovers_from_terminated_backend(settings, db_url):
    """A pooled connection killed server-side (e.g. Neon idling) is replaced on the next checkout."""
    pool = create_pool(settings.model_copy(update={"db_pool_min": 1, "db_pool_max": 1}))
    try:
        with pool.connection() as c:
            pid = c.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        with connect(db_url) as killer:
            assert killer.execute("SELECT pg_terminate_backend(%s) AS ok", (pid,)).fetchone()["ok"]
        with pool.connection() as c:
            row = c.execute("SELECT 1 AS one, pg_backend_pid() AS pid").fetchone()
        assert row["one"] == 1 and row["pid"] != pid
    finally:
        pool.close()


def test_connect_timeout_applied_to_pool_and_direct_connections(settings, db_url, monkeypatch):
    """Both connect() and the pool pass libpq connect_timeout from DB_CONNECT_TIMEOUT_S."""
    from psycopg import Connection

    seen = []
    real = Connection.connect.__func__

    def recording_connect(cls, conninfo="", **kwargs):
        """Record connection kwargs, then connect for real."""
        seen.append(kwargs)
        return real(cls, conninfo, **kwargs)

    monkeypatch.setattr(Connection, "connect", classmethod(recording_connect))
    s = settings.model_copy(update={"db_connect_timeout_s": 2.5})
    with connect(db_url, s.db_connect_timeout_s):
        pass
    pool = create_pool(s)
    pool.close()
    assert len(seen) >= 2 and all(kw["connect_timeout"] == 3 for kw in seen)
    assert "connect_timeout" not in db.CONN_KWARGS


def test_conn_kwargs_read_only():
    """CONN_KWARGS is shared and immutable; per-call kwargs are built from a copy."""
    with pytest.raises(TypeError):
        db.CONN_KWARGS["connect_timeout"] = 1  # type: ignore[index]
    kw = db.connection_kwargs(4.0)
    kw["extra"] = 1
    assert "extra" not in db.CONN_KWARGS and kw["connect_timeout"] == 4


def test_pool_open_bounded_when_db_unreachable(settings):
    """create_pool gives up after DB_POOL_OPEN_TIMEOUT_S with PoolTimeout and leaves no pool running."""
    import time

    from psycopg_pool import PoolTimeout

    s = settings.model_copy(update={"database_url": "postgresql://u:p@127.0.0.1:1/x",
                                    "db_pool_open_timeout_s": 0.5, "db_connect_timeout_s": 1.0})
    start = time.monotonic()
    with pytest.raises(PoolTimeout):
        create_pool(s)
    assert time.monotonic() - start < 5


def test_bootstrap_warns_in_prod_without_unpooled_url(settings, caplog):
    """In prod, bootstrapping through the pooled URL (no DATABASE_URL_UNPOOLED) logs a WARNING."""
    import logging

    with caplog.at_level(logging.WARNING, logger="statbrief.storage.db"):
        bootstrap(settings.model_copy(update={"app_env": "prod"}))
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "DATABASE_URL_UNPOOLED" in r.message]
    assert len(warned) == 1
    assert settings.database_url not in caplog.text  # never log the URL itself


def test_bootstrap_no_warning_when_unpooled_set_or_not_prod(settings, db_url, caplog):
    """No warning outside prod, nor in prod when DATABASE_URL_UNPOOLED is configured."""
    import logging

    with caplog.at_level(logging.WARNING, logger="statbrief.storage.db"):
        bootstrap(settings)
        bootstrap(settings.model_copy(update={"app_env": "prod", "database_url_unpooled": db_url}))
    assert "DATABASE_URL_UNPOOLED" not in caplog.text
