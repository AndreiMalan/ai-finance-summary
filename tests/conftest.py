"""Shared fixtures: embedded Postgres, one fresh database per test, settings."""
import tempfile
import uuid

import psycopg
import pytest

from statbrief.config import Settings


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
    url = base.rsplit("/", 1)[0] + f"/{name}"
    yield url
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def settings(db_url) -> Settings:
    """Settings for tests: fresh DB, fixed secret, demo user configured."""
    return Settings(
        app_env="test",
        database_url=db_url,
        session_secret="test-secret-" + "x" * 32,
        demo_user_email="demo@example.com",
        demo_user_password="demo-password-123",
        _env_file=None,
    )
