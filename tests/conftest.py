"""Shared fixtures: embedded Postgres, one fresh database per test, settings; live-API test gating."""
import os
import tempfile
import uuid

import psycopg
import pytest

from statbrief.config import Settings


def pytest_collection_modifyitems(config, items):
    """Skip tests marked `live` (real network calls) unless RUN_LIVE_TESTS=1."""
    if os.environ.get("RUN_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live API test: set RUN_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch):
    """Remove every Settings env var for every test so the developer's shell can't leak in.

    E.g. DATABASE_URL_UNPOOLED left over from `vercel env pull` would otherwise make DB tests
    bootstrap against Neon. Names are derived from Settings.model_fields, so new fields are covered.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


@pytest.fixture(scope="session")
def pg_server():
    """Start one embedded PostgreSQL (with pgvector) for the whole test session."""
    import pgserver

    srv = pgserver.get_server(tempfile.mkdtemp(prefix="sb-pg-"), cleanup_mode="stop")
    yield srv
    srv.cleanup()


@pytest.fixture
def db_url(pg_server):
    """Create an empty database for one test and drop it afterwards."""
    base = pg_server.get_uri()
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = pg_server.get_uri(database=name)
    yield url
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def settings(db_url) -> Settings:
    """Settings for tests: fresh DB, fixed secret, demo user configured."""
    return Settings(
        app_env="test",
        database_url=db_url,
        database_url_unpooled=None,  # explicit: tests never bootstrap through another URL
        session_secret="test-secret-" + "x" * 32,
        demo_user_email="demo@example.com",
        demo_user_password="demo-password-123",
        _env_file=None,
    )
