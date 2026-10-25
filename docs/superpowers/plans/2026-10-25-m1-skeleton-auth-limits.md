# M1 — Skeleton, Config, Database, Auth, Rate Limits — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deployable FastAPI app with env-driven config, Postgres migrations, email+password login (Argon2, signed cookies, demo user, signup switch), login throttling, per-user run limits and a global daily LLM cap — plus spikes that de-risk Vercel bundle size (R1), SSE streaming (R2) and the LangGraph Postgres checkpointer (R3).

**Architecture:** `src/statbrief` package (layering per `docs/architecture.md` §1). A per-app `Runtime` (settings + psycopg pool) is created lazily on first use and stored on `app.state`, so it works both under uvicorn and Vercel's serverless runtime. Plain SQL migrations run idempotently on first use. Tests run against a real embedded Postgres (`pgserver`), one fresh database per test.

**Tech Stack:** Python 3.12, FastAPI 0.162, Starlette 1.10, Jinja2 3.1, psycopg 3.3 + psycopg-pool 3.3, argon2-cffi 25.1, itsdangerous 2.2, pydantic-settings 2.16, langgraph 1.2 + langgraph-checkpoint-postgres 3.2, pytest 9.1, pgserver 0.1.4 (tests/dev only), Tailwind 4.3 + DaisyUI 5.7 (build-time), HTMX 2.0, Alpine 3.17.

## Global Constraints

- Every tunable comes from environment variables via `Settings`; no hardcoded limits, secrets or URLs in code (CLAUDE.md "No hardcoded values").
- Every function/class has a docstring (CLAUDE.md).
- Passwords: stored only as Argon2 hashes; never logged. (D-004)
- Session cookie: signed, `HttpOnly`, `SameSite=Lax`, `Secure` when `APP_ENV=prod`. (D-004)
- Defaults: `LOGIN_MAX_FAILURES=5`, `LOGIN_WINDOW_MINUTES=15`, `RUNS_PER_HOUR=10`, `DAILY_LLM_CALL_CAP=500`, `MAX_UPLOAD_MB=10`, `MAX_FILES_PER_REPORT=5`, `SESSION_MAX_AGE_DAYS=30`, `SIGNUPS_ENABLED=true`. (D-004, spec AC 4–5, 9)
- All user data queries are scoped by `user_id`. (AC 6)
- psycopg connections: `autocommit=True`, `prepare_threshold=None` (Neon pooler compatibility, R3), `row_factory=dict_row`.
- UI copy is English. (D-007)
- Tests never need network or API keys.
- Commit after each task; messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `pyproject.toml` | package metadata, pytest config |
| `requirements.txt` | runtime deps (what Vercel installs) |
| `requirements-dev.txt` | test/dev deps |
| `.python-version` | `3.12` for Vercel |
| `.env.example` | every env var with placeholder |
| `src/statbrief/__init__.py` | package marker |
| `src/statbrief/config.py` | `Settings` |
| `src/statbrief/storage/db.py` | pool creation, migration runner, LangGraph setup |
| `src/statbrief/storage/migrations/001_auth_limits.sql` | users, login_attempts, usage_events |
| `src/statbrief/auth/passwords.py` | Argon2 hash/verify |
| `src/statbrief/auth/users.py` | `User`, user repo, authenticate, demo upsert |
| `src/statbrief/auth/sessions.py` | signed cookie issue/read/clear |
| `src/statbrief/auth/limits.py` | login throttling, usage limits |
| `src/statbrief/web/runtime.py` | `Runtime` lazy init per app |
| `src/statbrief/web/deps.py` | FastAPI dependencies (`get_runtime`, `current_user`) |
| `src/statbrief/web/main.py` | `create_app()` |
| `src/statbrief/web/routes/auth.py` | login/signup/logout |
| `src/statbrief/web/routes/pages.py` | `/`, `/reports` placeholder |
| `src/statbrief/web/routes/diag.py` | `/healthz`, `/_diag/sse` (R2 spike) |
| `src/statbrief/web/templates/*.html` | base, login, signup, reports |
| `src/statbrief/web/static/src/app.css` | Tailwind + DaisyUI source + theme |
| `package.json` | CSS build + vendor JS copy |
| `app.py` | Vercel entrypoint |
| `vercel.json` | function config |
| `scripts/dev_db.py` | start embedded Postgres for local dev |
| `scripts/measure_bundle.py`, `scripts/bundle_probe_requirements.txt` | R1 spike |
| `scripts/check_checkpointer.py` | R3 spike against a real DATABASE_URL |
| `tests/conftest.py` | pgserver + per-test DB + settings + client fixtures |
| `tests/unit/...`, `tests/web/...` | tests |

---

### Task 1: Scaffold, Settings, test harness

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.python-version`, `.env.example`, `src/statbrief/__init__.py`, `src/statbrief/config.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/unit/__init__.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `statbrief.config.Settings` (pydantic-settings, fields below), `statbrief.config.get_settings() -> Settings` (cached). Fixtures `pg_server` (session), `db_url` (function: fresh database URL), `settings` (function: `Settings` bound to `db_url`).

- [ ] **Step 1: Packaging files**

`pyproject.toml`:
```toml
[project]
name = "statbrief"
version = "0.1.0"
requires-python = ">=3.12"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-q"
```

`requirements.txt`:
```
fastapi==0.162.0
jinja2==3.1.6
python-multipart==0.0.32
pydantic-settings==2.16.1
itsdangerous==2.2.0
argon2-cffi==25.1.0
psycopg[binary]==3.3.6
psycopg-pool==3.3.3
httpx==0.28.1
langgraph==1.2.17
langgraph-checkpoint-postgres==3.2.0
```

`requirements-dev.txt`:
```
-r requirements.txt
uvicorn==0.54.0
pytest==9.1.1
pytest-asyncio==1.4.1
pgserver==0.1.4
```

`.python-version`:
```
3.12
```

`.env.example`:
```
# --- core ---
APP_ENV=dev                      # dev | test | prod
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
SESSION_SECRET=change-me-generate-with-python-secrets-token-urlsafe-48
# --- auth ---
SIGNUPS_ENABLED=true
DEMO_USER_EMAIL=demo@example.com
DEMO_USER_PASSWORD=
PASSWORD_MIN_LENGTH=10
SESSION_MAX_AGE_DAYS=30
LOGIN_MAX_FAILURES=5
LOGIN_WINDOW_MINUTES=15
# --- cost protection ---
RUNS_PER_HOUR=10
DAILY_LLM_CALL_CAP=500
MAX_UPLOAD_MB=10
MAX_FILES_PER_REPORT=5
# --- infra ---
DB_POOL_MAX=5
CRON_SECRET=change-me
```

`src/statbrief/__init__.py`:
```python
"""StatBrief — AI statistical report generator."""
```

`tests/__init__.py` and `tests/unit/__init__.py`: empty files.

- [ ] **Step 2: Write the failing test** — `tests/unit/test_config.py`
```python
"""Tests for environment-driven settings."""
import pytest
from pydantic import ValidationError

from statbrief.config import Settings


def test_defaults_match_spec(monkeypatch):
    """Defaults equal the values agreed in D-004 / spec AC 4-5, 9."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.login_max_failures == 5
    assert s.login_window_minutes == 15
    assert s.runs_per_hour == 10
    assert s.daily_llm_call_cap == 500
    assert s.max_upload_mb == 10
    assert s.max_files_per_report == 5
    assert s.session_max_age_days == 30
    assert s.signups_enabled is True
    assert s.app_env == "dev"


def test_env_overrides(monkeypatch):
    """Env vars override defaults (case-insensitive names)."""
    monkeypatch.setenv("RUNS_PER_HOUR", "3")
    monkeypatch.setenv("SIGNUPS_ENABLED", "false")
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.runs_per_hour == 3
    assert s.signups_enabled is False


def test_short_session_secret_rejected():
    """A weak session secret is refused at startup."""
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://x/y", session_secret="short", _env_file=None)


def test_cookie_secure_only_in_prod():
    """Secure cookies are enforced in prod and relaxed for local http."""
    dev = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    prod = Settings(database_url="postgresql://x/y", session_secret="s" * 32, app_env="prod", _env_file=None)
    assert dev.cookie_secure is False
    assert prod.cookie_secure is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/unit/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statbrief.config'`

- [ ] **Step 4: Implement** — `src/statbrief/config.py`
```python
"""Application settings loaded from environment variables (and `.env` locally)."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every tunable of the app. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str
    session_secret: SecretStr
    db_pool_max: int = Field(5, ge=1)

    signups_enabled: bool = True
    demo_user_email: str | None = None
    demo_user_password: SecretStr | None = None
    password_min_length: int = Field(10, ge=8)
    session_max_age_days: int = Field(30, ge=1)
    login_max_failures: int = Field(5, ge=1)
    login_window_minutes: int = Field(15, ge=1)

    runs_per_hour: int = Field(10, ge=1)
    daily_llm_call_cap: int = Field(500, ge=1)
    max_upload_mb: int = Field(10, ge=1)
    max_files_per_report: int = Field(5, ge=1)

    cron_secret: SecretStr = SecretStr("")

    @field_validator("session_secret")
    @classmethod
    def _secret_long_enough(cls, v: SecretStr) -> SecretStr:
        """Reject session secrets shorter than 32 characters."""
        if len(v.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return v

    @property
    def cookie_secure(self) -> bool:
        """Whether cookies carry the Secure flag (HTTPS only)."""
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (read once from the environment)."""
    return Settings()  # type: ignore[call-arg]
```

- [ ] **Step 5: Test fixtures** — `tests/conftest.py`
```python
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
    url = pg_server.get_uri(database=name)  # portable: also correct for Unix-socket URIs
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
```

- [ ] **Step 6: Run tests**

Run: `.venv/Scripts/python -m pip install -r requirements-dev.txt -q && .venv/Scripts/python -m pytest tests/unit/test_config.py -v`
Expected: 4 passed

- [ ] **Step 7: Commit**
```bash
git add pyproject.toml requirements*.txt .python-version .env.example src tests
git commit -m "feat(m1): project scaffold, env settings, pgserver test harness"
```

---

### Task 2: Database pool, migrations, LangGraph setup

**Files:**
- Create: `src/statbrief/storage/__init__.py`, `src/statbrief/storage/db.py`, `src/statbrief/storage/migrations/001_auth_limits.sql`, `tests/unit/test_db.py`

**Interfaces:**
- Consumes: `Settings`.
- Produces: `create_pool(settings) -> psycopg_pool.ConnectionPool` (opened); `run_migrations(pool) -> list[str]` (applied versions, idempotent); `setup_langgraph(pool) -> None` (creates checkpointer + store tables).

- [ ] **Step 1: Migration SQL** — `src/statbrief/storage/migrations/001_auth_limits.sql`
```sql
-- Users, login attempts (throttling) and usage events (rate limits / daily LLM cap).
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    is_demo       boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id        bigserial PRIMARY KEY,
    email     text NOT NULL,
    ip        text NOT NULL,
    succeeded boolean NOT NULL,
    at        timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_email_ip_at ON login_attempts (email, ip, at);

CREATE TABLE IF NOT EXISTS usage_events (
    id      bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind    text NOT NULL,
    at      timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_events_kind_at ON usage_events (kind, at);
CREATE INDEX IF NOT EXISTS usage_events_user_kind_at ON usage_events (user_id, kind, at);
```

`src/statbrief/storage/__init__.py`:
```python
"""Persistence: Postgres pool, migrations, repositories, blob storage."""
```

- [ ] **Step 2: Write the failing test** — `tests/unit/test_db.py`
```python
"""Tests for pool creation, idempotent migrations and LangGraph table setup."""
from statbrief.storage.db import bootstrap, create_pool


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
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/unit/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statbrief.storage.db'`

- [ ] **Step 4: Implement** — `src/statbrief/storage/db.py`
```python
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
        f for f in resources.files(_MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")
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
```

Also create `src/statbrief/storage/migrations/__init__.py` (empty) so `importlib.resources` can find the package.

- [ ] **Step 5: Run tests** — `.venv/Scripts/python -m pytest tests/unit/test_db.py -v` → 2 passed

- [ ] **Step 6: Commit**
```bash
git add src/statbrief/storage tests/unit/test_db.py
git commit -m "feat(m1): postgres pool, idempotent migrations, langgraph table setup"
```

---

### Task 3: Passwords and user repository (incl. demo user)

**Files:**
- Create: `src/statbrief/auth/__init__.py`, `src/statbrief/auth/passwords.py`, `src/statbrief/auth/users.py`, `tests/unit/test_users.py`

**Interfaces:**
- Consumes: `bootstrap(settings)` (migrations + LangGraph setup on the direct URL), `create_pool(settings)`.
- Produces:
  - `hash_password(pw: str) -> str`, `verify_password(stored_hash: str, pw: str) -> bool`
  - `@dataclass(frozen=True) User(id: str, email: str, is_demo: bool)`
  - `normalize_email(raw: str) -> str` (raises `InvalidEmail`)
  - `create_user(pool, email, password, min_length) -> User` (raises `EmailTaken`, `WeakPassword`, `InvalidEmail`)
  - `get_user(pool, user_id) -> User | None`
  - `authenticate(pool, email, password) -> User | None`
  - `upsert_demo_user(pool, email, password) -> User`

- [ ] **Step 1: Write the failing test** — `tests/unit/test_users.py`
```python
"""Tests for password hashing and the user repository."""
import pytest

from statbrief.auth.passwords import hash_password, verify_password
from statbrief.auth.users import (
    EmailTaken, InvalidEmail, WeakPassword,
    authenticate, create_user, get_user, normalize_email, upsert_demo_user,
)
from statbrief.storage.db import bootstrap, create_pool


@pytest.fixture
def pool(settings):
    """Migrated pool for one test."""
    bootstrap(settings)
    p = create_pool(settings)
    yield p
    p.close()


def test_hash_roundtrip_and_no_plaintext():
    """Hashes verify, reject wrong passwords, and never contain the password."""
    h = hash_password("correct horse battery")
    assert h.startswith("$argon2")
    assert "correct horse" not in h
    assert verify_password(h, "correct horse battery")
    assert not verify_password(h, "wrong")


def test_normalize_email():
    """Emails are trimmed and lower-cased; garbage is rejected."""
    assert normalize_email("  Ana@Example.COM ") == "ana@example.com"
    with pytest.raises(InvalidEmail):
        normalize_email("not-an-email")


def test_create_and_authenticate(pool):
    """A created user can log in with the right password only."""
    u = create_user(pool, "Ana@example.com", "long-enough-pw", min_length=10)
    assert u.email == "ana@example.com" and not u.is_demo
    assert authenticate(pool, "ana@example.com", "long-enough-pw") == u
    assert authenticate(pool, "ana@example.com", "nope-nope-nope") is None
    assert authenticate(pool, "ghost@example.com", "long-enough-pw") is None
    assert get_user(pool, u.id) == u


def test_duplicate_and_weak(pool):
    """Duplicate emails and short passwords are refused."""
    create_user(pool, "a@example.com", "long-enough-pw", min_length=10)
    with pytest.raises(EmailTaken):
        create_user(pool, "A@example.com", "long-enough-pw", min_length=10)
    with pytest.raises(WeakPassword):
        create_user(pool, "b@example.com", "short", min_length=10)


def test_demo_user_upsert_updates_password(pool):
    """Re-running demo upsert with a new password replaces the old one."""
    d1 = upsert_demo_user(pool, "demo@example.com", "first-password-1")
    d2 = upsert_demo_user(pool, "demo@example.com", "second-password-2")
    assert d1.id == d2.id and d2.is_demo
    assert authenticate(pool, "demo@example.com", "first-password-1") is None
    assert authenticate(pool, "demo@example.com", "second-password-2") == d2


def test_get_user_with_malformed_id_returns_none(pool):
    """A tampered/non-UUID id does not raise."""
    assert get_user(pool, "not-a-uuid") is None
```

- [ ] **Step 2: Run to verify failure** — `.venv/Scripts/python -m pytest tests/unit/test_users.py -v` → FAIL (module not found)

- [ ] **Step 3: Implement** — `src/statbrief/auth/__init__.py`:
```python
"""Authentication: passwords, users, sessions, rate limits."""
```

`src/statbrief/auth/passwords.py`:
```python
"""Argon2id password hashing (argon2-cffi defaults follow RFC 9106 recommendations)."""
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an Argon2id hash string for `password`."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Return True when `password` matches `stored_hash`; never raises on mismatch."""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False
```

`src/statbrief/auth/users.py`:
```python
"""User repository: creation, lookup, authentication and the env-configured demo user."""
import re
import uuid
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from statbrief.auth.passwords import hash_password, verify_password

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Verified against when the email is unknown, so timing does not reveal which emails exist.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


class InvalidEmail(ValueError):
    """The email address is syntactically invalid."""


class EmailTaken(ValueError):
    """An account with this email already exists."""


class WeakPassword(ValueError):
    """The password is shorter than the configured minimum."""


@dataclass(frozen=True)
class User:
    """An authenticated application user."""

    id: str
    email: str
    is_demo: bool


def _row_to_user(row: dict) -> User:
    """Map a `users` row to a `User`."""
    return User(id=str(row["id"]), email=row["email"], is_demo=row["is_demo"])


def normalize_email(raw: str) -> str:
    """Trim and lower-case an email; raise InvalidEmail if it is not plausible."""
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise InvalidEmail("Please enter a valid email address.")
    return email


def create_user(pool: ConnectionPool, email: str, password: str, min_length: int) -> User:
    """Create a regular user; raise InvalidEmail, WeakPassword or EmailTaken."""
    email = normalize_email(email)
    if len(password) < min_length:
        raise WeakPassword(f"Password must be at least {min_length} characters.")
    try:
        with pool.connection() as c:
            row = c.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id, email, is_demo",
                (email, hash_password(password)),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise EmailTaken("An account with this email already exists.") from exc
    return _row_to_user(row)


def get_user(pool: ConnectionPool, user_id: str) -> User | None:
    """Return the user with `user_id`, or None (also for malformed ids)."""
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        return None
    with pool.connection() as c:
        row = c.execute("SELECT id, email, is_demo FROM users WHERE id = %s", (uid,)).fetchone()
    return _row_to_user(row) if row else None


def authenticate(pool: ConnectionPool, email: str, password: str) -> User | None:
    """Return the user if email+password match, else None (constant-ish time)."""
    try:
        email = normalize_email(email)
    except InvalidEmail:
        verify_password(_DUMMY_HASH, password)
        return None
    with pool.connection() as c:
        row = c.execute(
            "SELECT id, email, is_demo, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()
    if row is None:
        verify_password(_DUMMY_HASH, password)
        return None
    return _row_to_user(row) if verify_password(row["password_hash"], password) else None


def upsert_demo_user(pool: ConnectionPool, email: str, password: str) -> User:
    """Create or update the demo account so its password always equals the env value."""
    email = normalize_email(email)
    with pool.connection() as c:
        row = c.execute(
            "INSERT INTO users (email, password_hash, is_demo) VALUES (%s, %s, true) "
            "ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash, is_demo = true "
            "RETURNING id, email, is_demo",
            (email, hash_password(password)),
        ).fetchone()
    return _row_to_user(row)
```

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest tests/unit/test_users.py -v` → 6 passed

- [ ] **Step 5: Commit**
```bash
git add src/statbrief/auth tests/unit/test_users.py
git commit -m "feat(m1): argon2 passwords, user repository, demo user upsert"
```

---

### Task 4: Login throttling and usage limits

**Files:**
- Create: `src/statbrief/auth/limits.py`, `tests/unit/test_limits.py`

**Interfaces:**
- Consumes: pool, `create_user` (tests).
- Produces:
  - `class LimitExceeded(Exception)` with attrs `kind: str`, `limit: int`, `scope: Literal["user","global"]`
  - `login_allowed(pool, email, ip, now, max_failures, window) -> bool`
  - `record_login(pool, email, ip, succeeded, now) -> None`
  - `consume(pool, user_id, kind, limit, since, now, scope="user") -> int` — atomically checks count of `kind` events since `since` (for the user, or globally) and records one; returns remaining after consumption; raises `LimitExceeded` without recording when at limit.
  - `remaining(pool, user_id, kind, limit, since, scope="user") -> int`
  - `hour_ago(now) -> datetime`, `utc_midnight(now) -> datetime`
  - Kind constants: `KIND_REPORT_RUN = "report_run"`, `KIND_LLM_CALL = "llm_call"`

- [ ] **Step 1: Write the failing test** — `tests/unit/test_limits.py`
```python
"""Tests for login throttling, per-user run limits and the global daily LLM cap."""
from datetime import datetime, timedelta, timezone

import pytest

from statbrief.auth.limits import (
    KIND_LLM_CALL, KIND_REPORT_RUN, LimitExceeded,
    consume, hour_ago, login_allowed, record_login, remaining, utc_midnight,
)
from statbrief.auth.users import create_user
from statbrief.storage.db import bootstrap, create_pool

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(minutes=15)


@pytest.fixture
def pool(settings):
    """Migrated pool for one test."""
    bootstrap(settings)
    p = create_pool(settings)
    yield p
    p.close()


@pytest.fixture
def two_users(pool):
    """Two regular users."""
    return (create_user(pool, "a@example.com", "long-enough-pw", 10),
            create_user(pool, "b@example.com", "long-enough-pw", 10))


def test_login_blocked_after_max_failures_then_window_expires(pool):
    """5 failures block; failures older than the window no longer count."""
    for i in range(5):
        assert login_allowed(pool, "a@example.com", "1.1.1.1", NOW, 5, WINDOW)
        record_login(pool, "a@example.com", "1.1.1.1", False, NOW - timedelta(minutes=10) + timedelta(seconds=i))
    assert not login_allowed(pool, "a@example.com", "1.1.1.1", NOW, 5, WINDOW)
    assert login_allowed(pool, "a@example.com", "2.2.2.2", NOW, 5, WINDOW)  # other IP
    assert login_allowed(pool, "a@example.com", "1.1.1.1", NOW + timedelta(minutes=6), 5, WINDOW)


def test_user_run_limit(pool, two_users):
    """Per-user limit counts only that user's events in the window."""
    a, b = two_users
    since = hour_ago(NOW)
    assert consume(pool, a.id, KIND_REPORT_RUN, 2, since, NOW) == 1
    assert consume(pool, a.id, KIND_REPORT_RUN, 2, since, NOW) == 0
    with pytest.raises(LimitExceeded) as exc:
        consume(pool, a.id, KIND_REPORT_RUN, 2, since, NOW)
    assert exc.value.scope == "user" and exc.value.limit == 2
    assert consume(pool, b.id, KIND_REPORT_RUN, 2, since, NOW) == 1
    assert remaining(pool, a.id, KIND_REPORT_RUN, 2, since) == 0


def test_old_events_fall_out_of_window(pool, two_users):
    """Events before `since` do not count."""
    a, _ = two_users
    consume(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), NOW - timedelta(hours=2))
    assert consume(pool, a.id, KIND_REPORT_RUN, 1, hour_ago(NOW), NOW) == 0


def test_global_daily_cap(pool, two_users):
    """The daily LLM cap is shared by all users and resets at UTC midnight."""
    a, b = two_users
    since = utc_midnight(NOW)
    consume(pool, a.id, KIND_LLM_CALL, 2, since, NOW, scope="global")
    consume(pool, b.id, KIND_LLM_CALL, 2, since, NOW, scope="global")
    with pytest.raises(LimitExceeded) as exc:
        consume(pool, a.id, KIND_LLM_CALL, 2, since, NOW, scope="global")
    assert exc.value.scope == "global"
    tomorrow = NOW + timedelta(days=1)
    assert consume(pool, a.id, KIND_LLM_CALL, 2, utc_midnight(tomorrow), tomorrow, scope="global") == 1


def test_window_helpers():
    """Helpers compute the expected boundaries."""
    assert hour_ago(NOW) == NOW - timedelta(hours=1)
    assert utc_midnight(NOW) == datetime(2026, 10, 25, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run to verify failure** — `.venv/Scripts/python -m pytest tests/unit/test_limits.py -v` → FAIL (module not found)

- [ ] **Step 3: Implement** — `src/statbrief/auth/limits.py`
```python
"""Login throttling and usage limits backed by Postgres (works across serverless instances)."""
import zlib
from datetime import datetime, timedelta, timezone
from typing import Literal

from psycopg_pool import ConnectionPool

KIND_REPORT_RUN = "report_run"
KIND_LLM_CALL = "llm_call"
Scope = Literal["user", "global"]


class LimitExceeded(Exception):
    """A usage limit was reached; nothing was recorded."""

    def __init__(self, kind: str, limit: int, scope: Scope):
        """Store which limit was hit for user-facing messages."""
        super().__init__(f"{scope} limit for {kind} reached ({limit})")
        self.kind, self.limit, self.scope = kind, limit, scope


def hour_ago(now: datetime) -> datetime:
    """Start of the rolling one-hour window ending at `now`."""
    return now - timedelta(hours=1)


def utc_midnight(now: datetime) -> datetime:
    """Start of the UTC calendar day containing `now`."""
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def login_allowed(pool: ConnectionPool, email: str, ip: str, now: datetime,
                  max_failures: int, window: timedelta) -> bool:
    """True when this email+IP has fewer than `max_failures` failed logins within `window`."""
    with pool.connection() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM login_attempts "
            "WHERE email = %s AND ip = %s AND NOT succeeded AND at > %s",
            (email.strip().lower(), ip, now - window),
        ).fetchone()
    return row["n"] < max_failures


def record_login(pool: ConnectionPool, email: str, ip: str, succeeded: bool, now: datetime) -> None:
    """Record one login attempt (no password or hash is stored)."""
    with pool.connection() as c:
        c.execute(
            "INSERT INTO login_attempts (email, ip, succeeded, at) VALUES (%s, %s, %s, %s)",
            (email.strip().lower(), ip, succeeded, now),
        )


def _count_sql(scope: Scope) -> str:
    """SQL counting events of a kind since a timestamp, per user or globally."""
    where_user = "user_id = %(uid)s AND " if scope == "user" else ""
    return f"SELECT count(*) AS n FROM usage_events WHERE {where_user}kind = %(kind)s AND at >= %(since)s"


def remaining(pool: ConnectionPool, user_id: str, kind: str, limit: int, since: datetime,
              scope: Scope = "user") -> int:
    """How many more `kind` events are allowed in the current window."""
    with pool.connection() as c:
        n = c.execute(_count_sql(scope), {"uid": user_id, "kind": kind, "since": since}).fetchone()["n"]
    return max(limit - n, 0)


def consume(pool: ConnectionPool, user_id: str, kind: str, limit: int, since: datetime,
            now: datetime, scope: Scope = "user") -> int:
    """Atomically check the limit and record one event; return remaining or raise LimitExceeded."""
    lock_key = zlib.crc32(f"{scope}:{kind}:{user_id if scope == 'user' else '*'}".encode())
    with pool.connection() as c, c.transaction():
        c.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        n = c.execute(_count_sql(scope), {"uid": user_id, "kind": kind, "since": since}).fetchone()["n"]
        if n >= limit:
            raise LimitExceeded(kind, limit, scope)
        c.execute("INSERT INTO usage_events (user_id, kind, at) VALUES (%s, %s, %s)", (user_id, kind, now))
    return limit - n - 1
```

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest tests/unit/test_limits.py -v` → 5 passed

- [ ] **Step 5: Commit**
```bash
git add src/statbrief/auth/limits.py tests/unit/test_limits.py
git commit -m "feat(m1): login throttling, per-user run limit, global daily LLM cap"
```

---

### Task 5: Signed session cookies

**Files:**
- Create: `src/statbrief/auth/sessions.py`, `tests/unit/test_sessions.py`

**Interfaces:**
- Consumes: `Settings`.
- Produces: `COOKIE_NAME = "sb_session"`; `issue_session(response, user_id, settings, remember: bool) -> None`; `read_session(cookie_value: str | None, settings) -> str | None` (user id); `clear_session(response, settings) -> None`.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_sessions.py`
```python
"""Tests for signed session cookies."""
from starlette.responses import Response

from statbrief.auth.sessions import COOKIE_NAME, clear_session, issue_session, read_session


def _cookie(resp: Response) -> str:
    """Extract the raw Set-Cookie header for the session cookie."""
    return next(v for k, v in resp.raw_headers if k == b"set-cookie" and v.startswith(COOKIE_NAME.encode())).decode()


def test_roundtrip(settings):
    """An issued cookie reads back to the same user id."""
    resp = Response()
    issue_session(resp, "user-123", settings, remember=True)
    header = _cookie(resp)
    value = header.split(";")[0].split("=", 1)[1]
    assert read_session(value, settings) == "user-123"
    assert "httponly" in header.lower() and "samesite=lax" in header.lower()
    assert "max-age=2592000" in header.lower()  # 30 days


def test_session_cookie_without_remember_has_no_max_age(settings):
    """Without 'remember me' the cookie ends with the browser session."""
    resp = Response()
    issue_session(resp, "u", settings, remember=False)
    assert "max-age" not in _cookie(resp).lower()


def test_tampered_or_missing_cookie_rejected(settings):
    """Garbage, tampered and missing cookies yield None."""
    resp = Response()
    issue_session(resp, "user-123", settings, remember=True)
    value = _cookie(resp).split(";")[0].split("=", 1)[1]
    assert read_session(value[:-2] + "xx", settings) is None
    assert read_session("garbage", settings) is None
    assert read_session(None, settings) is None


def test_clear(settings):
    """Clearing expires the cookie."""
    resp = Response()
    clear_session(resp, settings)
    assert 'max-age=0' in _cookie(resp).lower()
```

- [ ] **Step 2: Run to verify failure** — `.venv/Scripts/python -m pytest tests/unit/test_sessions.py -v` → FAIL

- [ ] **Step 3: Implement** — `src/statbrief/auth/sessions.py`
```python
"""Signed, HttpOnly session cookies (itsdangerous); the cookie stores only the user id."""
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.responses import Response

from statbrief.config import Settings

COOKIE_NAME = "sb_session"
_SALT = "statbrief-session-v1"


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    """Serializer bound to the configured secret."""
    return URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt=_SALT)


def _max_age_s(settings: Settings) -> int:
    """Maximum session lifetime in seconds."""
    return settings.session_max_age_days * 86400


def issue_session(response: Response, user_id: str, settings: Settings, remember: bool) -> None:
    """Set the session cookie; persistent for SESSION_MAX_AGE_DAYS when `remember`."""
    token = _serializer(settings).dumps({"uid": user_id})
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=_max_age_s(settings) if remember else None,
        httponly=True, samesite="lax", secure=settings.cookie_secure, path="/",
    )


def read_session(cookie_value: str | None, settings: Settings) -> str | None:
    """Return the user id from a valid, unexpired cookie value, else None."""
    if not cookie_value:
        return None
    try:
        data = _serializer(settings).loads(cookie_value, max_age=_max_age_s(settings))
    except BadSignature:  # also covers SignatureExpired
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, str) else None


def clear_session(response: Response, settings: Settings) -> None:
    """Expire the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)
```

- [ ] **Step 4: Run tests** — `.venv/Scripts/python -m pytest tests/unit/test_sessions.py -v` → 4 passed

- [ ] **Step 5: Commit**
```bash
git add src/statbrief/auth/sessions.py tests/unit/test_sessions.py
git commit -m "feat(m1): signed httponly session cookies"
```

---

### Task 6: Web app — runtime, auth routes, templates, CSS build

**Files:**
- Create: `src/statbrief/web/__init__.py`, `web/runtime.py`, `web/deps.py`, `web/main.py`, `web/routes/__init__.py`, `web/routes/auth.py`, `web/routes/pages.py`, `web/templates/base.html`, `web/templates/login.html`, `web/templates/signup.html`, `web/templates/reports.html`, `web/static/src/app.css`, `package.json`, `tests/web/__init__.py`, `tests/web/test_auth_flow.py` (all `web/` paths under `src/statbrief/`)
- Generated (committed): `src/statbrief/web/static/app.css`, `static/vendor/htmx.min.js`, `static/vendor/alpine.min.js`

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`; `Runtime(settings, pool)`; `get_runtime(request) -> Runtime`; `current_user(request) -> User` (raises `LoginRequired`); `optional_user(request) -> User | None`; `class LoginRequired(Exception)`; `templates: Jinja2Templates`; `client_ip(request) -> str`.

- [ ] **Step 1: Write the failing test** — `tests/web/test_auth_flow.py`
```python
"""End-to-end auth flow through the FastAPI app."""
import pytest
from fastapi.testclient import TestClient

from statbrief.web.main import create_app


@pytest.fixture
def client(settings):
    """App client on a fresh database (redirects not followed)."""
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


def test_anonymous_redirected_to_login(client):
    """Protected pages redirect to /login."""
    r = client.get("/reports")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_htmx_request_gets_hx_redirect(client):
    """HTMX requests receive HX-Redirect instead of a 303."""
    r = client.get("/reports", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/login")


def test_signup_login_logout(client):
    """Sign up logs in; logout clears; login works again."""
    r = client.post("/signup", data={"email": "ana@example.com", "password": "long-enough-pw"})
    assert r.status_code == 303 and r.headers["location"] == "/reports"
    assert client.get("/reports").status_code == 200
    client.post("/logout")
    assert client.get("/reports").status_code == 303
    r = client.post("/login", data={"email": "ana@example.com", "password": "long-enough-pw", "remember": "on"})
    assert r.status_code == 303
    page = client.get("/reports")
    assert page.status_code == 200 and "ana@example.com" in page.text


def test_bad_password_shows_error_and_throttles(client, settings):
    """Wrong passwords show an error; after the max, the form is locked."""
    client.post("/signup", data={"email": "b@example.com", "password": "long-enough-pw"})
    client.post("/logout")
    for _ in range(settings.login_max_failures):
        r = client.post("/login", data={"email": "b@example.com", "password": "wrong-wrong-1"})
        assert r.status_code == 400 and "Incorrect email or password" in r.text
    r = client.post("/login", data={"email": "b@example.com", "password": "long-enough-pw"})
    assert r.status_code == 429 and "Too many failed attempts" in r.text


def test_demo_user_seeded(client):
    """The env-configured demo account can log in."""
    r = client.post("/login", data={"email": "demo@example.com", "password": "demo-password-123"})
    assert r.status_code == 303


def test_signups_disabled(settings):
    """With SIGNUPS_ENABLED=false the signup form is unavailable."""
    app = create_app(settings.model_copy(update={"signups_enabled": False}))
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/signup").status_code == 404
        assert c.post("/signup", data={"email": "x@example.com", "password": "long-enough-pw"}).status_code == 404


def test_signup_validation_errors(client):
    """Weak password and duplicate email re-render the form with a message."""
    r = client.post("/signup", data={"email": "c@example.com", "password": "short"})
    assert r.status_code == 400 and "at least 10 characters" in r.text
    client.post("/signup", data={"email": "d@example.com", "password": "long-enough-pw"})
    client.post("/logout")
    r = client.post("/signup", data={"email": "d@example.com", "password": "long-enough-pw"})
    assert r.status_code == 400 and "already exists" in r.text


def test_healthz(client):
    """Health endpoint reports database connectivity."""
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": True}
```

- [ ] **Step 2: Run to verify failure** — `.venv/Scripts/python -m pytest tests/web -v` → FAIL (module not found)

- [ ] **Step 3: Runtime and dependencies**

`src/statbrief/web/__init__.py`:
```python
"""Web layer: FastAPI app, routes, templates, static assets."""
```

`src/statbrief/web/runtime.py`:
```python
"""Per-app runtime (settings + DB pool) initialised lazily, once per serverless instance."""
import threading
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from statbrief.auth.users import upsert_demo_user
from statbrief.config import Settings
from statbrief.storage.db import bootstrap, create_pool


@dataclass
class Runtime:
    """Live resources shared by all requests handled by one app instance."""

    settings: Settings
    pool: ConnectionPool

    def close(self) -> None:
        """Release the connection pool."""
        self.pool.close()


class RuntimeHolder:
    """Thread-safe lazy holder so cold starts initialise exactly once."""

    def __init__(self, settings: Settings):
        """Remember settings; nothing is opened until `get()`."""
        self._settings = settings
        self._runtime: Runtime | None = None
        self._lock = threading.Lock()

    def get(self) -> Runtime:
        """Open the pool, migrate, set up LangGraph tables and seed the demo user on first call."""
        if self._runtime is None:
            with self._lock:
                if self._runtime is None:
                    bootstrap(self._settings)
                    pool = create_pool(self._settings)
                    s = self._settings
                    if s.demo_user_email and s.demo_user_password:
                        upsert_demo_user(pool, s.demo_user_email, s.demo_user_password.get_secret_value())
                    self._runtime = Runtime(settings=s, pool=pool)
        return self._runtime

    def close(self) -> None:
        """Close the runtime if it was opened."""
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
```

`src/statbrief/web/deps.py`:
```python
"""FastAPI dependencies: runtime access, current user, client IP, templates."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from statbrief.auth.sessions import COOKIE_NAME, read_session
from statbrief.auth.users import User, get_user
from statbrief.web.runtime import Runtime

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequired(Exception):
    """Raised by `current_user` when no valid session exists."""


def get_runtime(request: Request) -> Runtime:
    """Return the app's lazily-initialised runtime."""
    return request.app.state.runtime_holder.get()


def optional_user(request: Request) -> User | None:
    """Return the logged-in user, or None."""
    rt = get_runtime(request)
    uid = read_session(request.cookies.get(COOKIE_NAME), rt.settings)
    return get_user(rt.pool, uid) if uid else None


def current_user(request: Request) -> User:
    """Return the logged-in user or raise LoginRequired."""
    user = optional_user(request)
    if user is None:
        raise LoginRequired()
    return user


def client_ip(request: Request) -> str:
    """Best-effort client IP (Vercel sets x-forwarded-for; first hop is the client)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
```

- [ ] **Step 4: Routes**

`src/statbrief/web/routes/__init__.py`:
```python
"""HTTP route modules."""
```

`src/statbrief/web/routes/auth.py`:
```python
"""Login, signup and logout pages."""
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from statbrief.auth.limits import login_allowed, record_login
from statbrief.auth.sessions import clear_session, issue_session
from statbrief.auth.users import EmailTaken, InvalidEmail, WeakPassword, authenticate, create_user
from statbrief.web.deps import client_ip, get_runtime, optional_user, templates
from statbrief.web.runtime import Runtime

router = APIRouter()
_HOME = "/reports"


def _safe_next(next_url: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return _HOME


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None):
    """Render the login form (or skip it when already logged in)."""
    if optional_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    rt = get_runtime(request)
    return templates.TemplateResponse(request, "login.html",
                                      {"next": next or "", "signups_enabled": rt.settings.signups_enabled})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, rt: Annotated[Runtime, Depends(get_runtime)],
          email: Annotated[str, Form()], password: Annotated[str, Form()],
          remember: Annotated[str | None, Form()] = None, next: Annotated[str, Form()] = ""):
    """Authenticate with throttling; set the session cookie on success."""
    s, ip, now = rt.settings, client_ip(request), datetime.now(timezone.utc)
    ctx = {"next": next, "email": email, "signups_enabled": s.signups_enabled}
    if not login_allowed(rt.pool, email, ip, now, s.login_max_failures, timedelta(minutes=s.login_window_minutes)):
        ctx["error"] = f"Too many failed attempts. Try again in {s.login_window_minutes} minutes."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=429)
    user = authenticate(rt.pool, email, password)
    record_login(rt.pool, email, ip, user is not None, now)
    if user is None:
        ctx["error"] = "Incorrect email or password."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=400)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    issue_session(resp, user.id, s, remember=remember == "on")
    return resp


def _require_signups(rt: Runtime) -> None:
    """404 when self-registration is switched off."""
    if not rt.settings.signups_enabled:
        raise HTTPException(status_code=404)


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, rt: Annotated[Runtime, Depends(get_runtime)]):
    """Render the signup form."""
    _require_signups(rt)
    return templates.TemplateResponse(request, "signup.html", {"min_length": rt.settings.password_min_length})


@router.post("/signup", response_class=HTMLResponse)
def signup(request: Request, rt: Annotated[Runtime, Depends(get_runtime)],
           email: Annotated[str, Form()], password: Annotated[str, Form()]):
    """Create an account and log the user in."""
    _require_signups(rt)
    try:
        user = create_user(rt.pool, email, password, rt.settings.password_min_length)
    except (InvalidEmail, WeakPassword, EmailTaken) as exc:
        ctx = {"error": str(exc), "email": email, "min_length": rt.settings.password_min_length}
        return templates.TemplateResponse(request, "signup.html", ctx, status_code=400)
    resp = RedirectResponse(_HOME, status_code=303)
    issue_session(resp, user.id, rt.settings, remember=True)
    return resp


@router.post("/logout")
def logout(rt: Annotated[Runtime, Depends(get_runtime)]):
    """Clear the session and return to the login page."""
    resp = RedirectResponse("/login", status_code=303)
    clear_session(resp, rt.settings)
    return resp
```

`src/statbrief/web/routes/pages.py`:
```python
"""Top-level pages. `/reports` is a placeholder until milestone 8."""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from statbrief.auth.limits import KIND_REPORT_RUN, hour_ago, remaining
from statbrief.auth.users import User
from statbrief.web.deps import current_user, get_runtime, optional_user, templates
from statbrief.web.runtime import Runtime

router = APIRouter()


@router.get("/")
def home(request: Request):
    """Send visitors to their reports or to the login page."""
    return RedirectResponse("/reports" if optional_user(request) else "/login", status_code=303)


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, user: Annotated[User, Depends(current_user)],
            rt: Annotated[Runtime, Depends(get_runtime)]):
    """List the user's reports (empty state for now) with remaining run quota."""
    from datetime import datetime, timezone

    left = remaining(rt.pool, user.id, KIND_REPORT_RUN, rt.settings.runs_per_hour,
                     hour_ago(datetime.now(timezone.utc)))
    return templates.TemplateResponse(request, "reports.html",
                                      {"user": user, "runs_left": left, "runs_per_hour": rt.settings.runs_per_hour})
```

- [ ] **Step 5: App factory** — `src/statbrief/web/main.py`
```python
"""FastAPI application factory."""
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from statbrief.config import Settings, get_settings
from statbrief.web.deps import LoginRequired, get_runtime
from statbrief.web.runtime import RuntimeHolder


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; resources open lazily on first request (serverless-friendly)."""
    holder = RuntimeHolder(settings or get_settings())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Close the pool on shutdown (when the host runs ASGI lifespan)."""
        yield
        holder.close()

    app = FastAPI(title="StatBrief", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime_holder = holder
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        """Redirect to login; HTMX requests get an HX-Redirect header instead."""
        target = "/login?next=" + quote(request.url.path)
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.get("/healthz")
    def healthz(request: Request) -> JSONResponse:
        """Liveness + database check."""
        with get_runtime(request).pool.connection() as c:
            ok = c.execute("SELECT 1 AS one").fetchone()["one"] == 1
        return JSONResponse({"ok": True, "db": ok})

    from statbrief.web.routes import auth, diag, pages

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(diag.router)
    return app
```

(`diag` router is created in Task 7; for this task create `src/statbrief/web/routes/diag.py` containing only:)
```python
"""Diagnostics endpoints (risk spikes)."""
from fastapi import APIRouter

router = APIRouter()
```

- [ ] **Step 6: CSS/JS build**

`package.json`:
```json
{
  "name": "statbrief-assets",
  "private": true,
  "scripts": {
    "build:css": "tailwindcss -i src/statbrief/web/static/src/app.css -o src/statbrief/web/static/app.css --minify",
    "vendor": "node -e \"const f=require('fs');f.mkdirSync('src/statbrief/web/static/vendor',{recursive:true});f.copyFileSync('node_modules/htmx.org/dist/htmx.min.js','src/statbrief/web/static/vendor/htmx.min.js');f.copyFileSync('node_modules/alpinejs/dist/cdn.min.js','src/statbrief/web/static/vendor/alpine.min.js')\"",
    "build": "npm run vendor && npm run build:css"
  },
  "devDependencies": {
    "@tailwindcss/cli": "4.3.3",
    "tailwindcss": "4.3.3",
    "daisyui": "5.7.50",
    "htmx.org": "2.0.11",
    "alpinejs": "3.17.5"
  }
}
```

`src/statbrief/web/static/src/app.css` (theme = mockup palette):
```css
@import "tailwindcss";
@source "../../templates";
@plugin "daisyui" { themes: false; }
@plugin "daisyui/theme" {
  name: "statbrief";
  default: true;
  color-scheme: light;
  --color-base-100: #FFFFFF;
  --color-base-200: #F6F7F9;
  --color-base-300: #E3E6EB;
  --color-base-content: #14181F;
  --color-primary: #0F6E6A;
  --color-primary-content: #FFFFFF;
  --color-secondary: #3A5BA0;
  --color-secondary-content: #FFFFFF;
  --color-accent: #C26A1E;
  --color-accent-content: #FFFFFF;
  --color-neutral: #14181F;
  --color-neutral-content: #FFFFFF;
  --color-info: #3A5BA0;
  --color-info-content: #FFFFFF;
  --color-success: #1F7A3A;
  --color-success-content: #FFFFFF;
  --color-warning: #9A5B00;
  --color-warning-content: #FFFFFF;
  --color-error: #B42318;
  --color-error-content: #FFFFFF;
  --radius-box: 0.875rem;
  --radius-field: 0.625rem;
  --radius-selector: 0.5rem;
}
@theme {
  --font-sans: "Inter", system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, monospace;
}
```

Run: `npm install && npm run build` → creates `static/app.css`, `static/vendor/*.js`. Add `node_modules/` to `.gitignore`.

- [ ] **Step 7: Templates**

`src/statbrief/web/templates/base.html`:
```html
<!doctype html>
<html lang="en" data-theme="statbrief">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}StatBrief{% endblock %}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{{ url_for('static', path='app.css') }}">
  <script src="{{ url_for('static', path='vendor/htmx.min.js') }}" defer></script>
  <script src="{{ url_for('static', path='vendor/alpine.min.js') }}" defer></script>
</head>
<body class="min-h-screen bg-base-200 font-sans text-base-content">
  {% block body %}{% endblock %}
</body>
</html>
```

`src/statbrief/web/templates/login.html`:
```html
{% extends "base.html" %}
{% block title %}Log in · StatBrief{% endblock %}
{% block body %}
<main class="flex min-h-screen items-center justify-center px-4">
  <div class="card w-full max-w-sm bg-base-100 border border-base-300">
    <form class="card-body gap-4" method="post" action="/login">
      <div class="flex items-center gap-2 text-lg font-bold">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-primary" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>
        StatBrief
      </div>
      <h1 class="text-2xl font-bold">Log in</h1>
      {% if error %}<div role="alert" class="alert alert-error alert-soft">{{ error }}</div>{% endif %}
      <input type="hidden" name="next" value="{{ next }}">
      <label class="flex flex-col gap-1 text-sm font-medium">Email
        <input class="input w-full" type="email" name="email" value="{{ email or '' }}" autocomplete="email" required>
      </label>
      <label class="flex flex-col gap-1 text-sm font-medium">Password
        <input class="input w-full" type="password" name="password" autocomplete="current-password" required>
      </label>
      <label class="flex items-center gap-2 text-sm"><input class="checkbox checkbox-sm" type="checkbox" name="remember" checked> Remember me for 30 days</label>
      <button class="btn btn-primary">Log in</button>
      {% if signups_enabled %}<p class="text-sm text-center">No account? <a class="link link-primary" href="/signup">Create one</a></p>{% endif %}
    </form>
  </div>
</main>
{% endblock %}
```

`src/statbrief/web/templates/signup.html`:
```html
{% extends "base.html" %}
{% block title %}Create account · StatBrief{% endblock %}
{% block body %}
<main class="flex min-h-screen items-center justify-center px-4">
  <div class="card w-full max-w-sm bg-base-100 border border-base-300">
    <form class="card-body gap-4" method="post" action="/signup">
      <h1 class="text-2xl font-bold">Create your account</h1>
      {% if error %}<div role="alert" class="alert alert-error alert-soft">{{ error }}</div>{% endif %}
      <label class="flex flex-col gap-1 text-sm font-medium">Email
        <input class="input w-full" type="email" name="email" value="{{ email or '' }}" autocomplete="email" required>
      </label>
      <label class="flex flex-col gap-1 text-sm font-medium">Password
        <input class="input w-full" type="password" name="password" minlength="{{ min_length }}" autocomplete="new-password" required>
        <span class="text-xs text-base-content/70">At least {{ min_length }} characters.</span>
      </label>
      <button class="btn btn-primary">Create account</button>
      <p class="text-sm text-center">Already registered? <a class="link link-primary" href="/login">Log in</a></p>
    </form>
  </div>
</main>
{% endblock %}
```

`src/statbrief/web/templates/reports.html`:
```html
{% extends "base.html" %}
{% block title %}My reports · StatBrief{% endblock %}
{% block body %}
<header class="flex flex-wrap items-center justify-between gap-4 bg-base-100 border-b border-base-300 px-4 py-3 md:px-8">
  <div class="flex items-center gap-2 text-lg font-bold">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-primary" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>
    StatBrief
  </div>
  <div class="flex items-center gap-3 text-sm">
    <span class="text-base-content/70">{{ runs_left }} of {{ runs_per_hour }} runs left this hour</span>
    <span class="font-medium">{{ user.email }}</span>
    <form method="post" action="/logout"><button class="btn btn-ghost btn-sm">Log out</button></form>
  </div>
</header>
<main class="mx-auto max-w-6xl px-4 py-8 md:px-8">
  <h1 class="text-2xl font-bold mb-6">My reports</h1>
  <div class="card bg-base-100 border border-base-300">
    <div class="card-body items-center text-center">
      <p class="text-base-content/70">No reports yet. The report wizard arrives in a later milestone.</p>
    </div>
  </div>
</main>
{% endblock %}
```

- [ ] **Step 8: Run tests** — `.venv/Scripts/python -m pytest tests -v` → all pass (Tasks 1–6)

- [ ] **Step 9: Manual check** — `python scripts/dev_db.py` is in Task 7; for now run against the test settings is enough. Skip.

- [ ] **Step 10: Commit**
```bash
git add package.json package-lock.json .gitignore src/statbrief/web tests/web
git commit -m "feat(m1): FastAPI app, login/signup/logout, themed templates, asset build"
```

---

### Task 7: Vercel entrypoint, local dev DB, risk spikes R1/R2/R3

**Files:**
- Create: `app.py`, `vercel.json`, `scripts/dev_db.py`, `scripts/measure_bundle.py`, `scripts/bundle_probe_requirements.txt`, `scripts/check_checkpointer.py`, `tests/web/test_diag.py`, `docs/spikes.md`
- Modify: `src/statbrief/web/routes/diag.py`

**Interfaces:**
- Consumes: `create_app`.
- Produces: `GET /_diag/sse` (text/event-stream, `SSE_DIAG_TICKS` ticks), Vercel ASGI `app`.

- [ ] **Step 1: Write the failing test** — `tests/web/test_diag.py`
```python
"""SSE diagnostics endpoint streams incremental events (R2 spike)."""
from fastapi.testclient import TestClient

from statbrief.web.main import create_app


def test_sse_streams_ticks(settings):
    """The endpoint emits N tick events and a done event with SSE framing."""
    app = create_app(settings.model_copy(update={"sse_diag_ticks": 3, "sse_diag_interval_s": 0.0}))
    with TestClient(app) as c, c.stream("GET", "/_diag/sse") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        body = "".join(r.iter_text())
    assert body.count("event: tick") == 3
    assert "event: done" in body
```

- [ ] **Step 2: Add settings fields** — in `Settings` (`config.py`) add:
```python
    sse_diag_ticks: int = Field(5, ge=1, le=60)
    sse_diag_interval_s: float = Field(1.0, ge=0.0, le=10.0)
```
and to `.env.example`:
```
SSE_DIAG_TICKS=5
SSE_DIAG_INTERVAL_S=1.0
```

- [ ] **Step 3: Run to verify failure** — `.venv/Scripts/python -m pytest tests/web/test_diag.py -v` → FAIL (404)

- [ ] **Step 4: Implement** — replace `src/statbrief/web/routes/diag.py`:
```python
"""Diagnostics endpoints. `/_diag/sse` verifies that streaming reaches the browser on Vercel (risk R2)."""
import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from statbrief.web.deps import get_runtime
from statbrief.web.runtime import Runtime

router = APIRouter()


def sse_event(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/_diag/sse")
async def sse_diag(rt: Annotated[Runtime, Depends(get_runtime)]) -> StreamingResponse:
    """Emit a few timed ticks so we can see whether events arrive incrementally."""
    ticks, interval = rt.settings.sse_diag_ticks, rt.settings.sse_diag_interval_s

    async def gen() -> AsyncIterator[str]:
        """Yield tick events then a done event."""
        start = time.monotonic()
        for i in range(ticks):
            yield sse_event("tick", {"i": i, "t": round(time.monotonic() - start, 3)})
            await asyncio.sleep(interval)
        yield sse_event("done", {"elapsed": round(time.monotonic() - start, 3)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

- [ ] **Step 5: Run tests** — `.venv/Scripts/python -m pytest tests -v` → all pass

- [ ] **Step 6: Vercel entrypoint and config**

`app.py`:
```python
"""Vercel entrypoint: exposes the ASGI `app` (src/ layout added to sys.path)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from statbrief.web.main import create_app  # noqa: E402

app = create_app()
```

`vercel.json`:
```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "functions": {
    "app.py": {
      "maxDuration": 300,
      "excludeFiles": "{tests/**,docs/**,node_modules/**,scripts/**,knowledge_base/**,sample_data/**,.venv/**}"
    }
  }
}
```

- [ ] **Step 7: Local dev database** — `scripts/dev_db.py`
```python
"""Start a persistent embedded Postgres for local development and print DATABASE_URL.

Usage: python scripts/dev_db.py   (keep it running; data lives in .devdb/)
"""
import signal
import sys
from pathlib import Path

import pgserver


def main() -> None:
    """Start the server in .devdb/ and block until Ctrl+C."""
    data_dir = Path(__file__).resolve().parent.parent / ".devdb"
    srv = pgserver.get_server(str(data_dir), cleanup_mode="stop")
    print(f"DATABASE_URL={srv.get_uri()}")
    print("Running. Press Ctrl+C to stop.")
    try:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        signal.pause() if hasattr(signal, "pause") else input()
    finally:
        srv.cleanup()


if __name__ == "__main__":
    main()
```
Add `.devdb/` to `.gitignore`.

- [ ] **Step 8: R1 bundle-size probe**

`scripts/bundle_probe_requirements.txt` (every runtime dependency planned for M1–M9):
```
-r ../requirements.txt
langchain==1.4.7
langchain-core==1.7.0
langchain-openai==1.7.2
langchain-postgres==0.0.19
langchain-community==0.4.2
langchain-text-splitters==1.1.3
pandas==3.1.0
openpyxl==3.1.5
xlrd==2.0.2
pypdf==6.21.2
reportlab==5.1.0
python-dateutil
```

`scripts/measure_bundle.py`:
```python
"""Install the planned runtime dependencies into a temp dir and report their size (risk R1).

Usage: python scripts/measure_bundle.py [requirements-file]
"""
import subprocess
import sys
import tempfile
from pathlib import Path

LIMIT_MB = 250  # Vercel Python function uncompressed limit assumed in docs/architecture.md R1


def dir_size_mb(path: Path) -> float:
    """Total size of all files under `path`, in MB."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


def main() -> None:
    """Install into a target dir (Linux wheels, as on Vercel) and print totals and top packages."""
    req = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "bundle_probe_requirements.txt")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", tmp,
                        "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
                        "--only-binary=:all:", "-r", str(req)], check=True, cwd=req.parent)
        root = Path(tmp)
        sizes = sorted(((dir_size_mb(p), p.name) for p in root.iterdir() if p.is_dir()), reverse=True)
        total = dir_size_mb(root)
        print(f"TOTAL {total:.1f} MB (limit {LIMIT_MB} MB) -> {'OK' if total < LIMIT_MB else 'OVER'}")
        for mb, name in sizes[:15]:
            print(f"{mb:8.1f} MB  {name}")


if __name__ == "__main__":
    main()
```
Run: `.venv/Scripts/python scripts/measure_bundle.py` → record output in `docs/spikes.md`.

- [ ] **Step 9: R3 checkpointer check** — `scripts/check_checkpointer.py`
```python
"""Verify LangGraph PostgresSaver + interrupt/resume against DATABASE_URL (risk R3, Neon pooler).

Usage: DATABASE_URL=... python scripts/check_checkpointer.py
"""
import os
import sys
import uuid
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402


class S(TypedDict, total=False):
    """Minimal state for the probe graph."""

    x: int
    answer: str


def main() -> None:
    """Run a 2-node graph to an interrupt, resume it from a NEW pool, and report."""
    url = os.environ["DATABASE_URL"]
    kwargs = {"autocommit": True, "prepare_threshold": None, "row_factory": dict_row}

    def build(pool: ConnectionPool):
        """Compile the probe graph on `pool`."""
        g = StateGraph(S)
        g.add_node("a", lambda s: {"x": 1})
        g.add_node("b", lambda s: {"answer": interrupt("approve?")})
        g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("b", END)
        saver = PostgresSaver(pool); saver.setup()
        return g.compile(checkpointer=saver)

    cfg = {"configurable": {"thread_id": f"probe-{uuid.uuid4().hex[:8]}"}}
    with ConnectionPool(url, kwargs=kwargs, open=True) as p1:
        out = build(p1).invoke({}, cfg)
        assert "__interrupt__" in out, out
    with ConnectionPool(url, kwargs=kwargs, open=True) as p2:  # simulates a new serverless instance
        final = build(p2).invoke(Command(resume="yes"), cfg)
    assert final == {"x": 1, "answer": "yes"}, final
    print("R3 OK: interrupt persisted and resumed across pools:", final)


if __name__ == "__main__":
    main()
```
Run locally against the dev DB now (`DATABASE_URL` from `scripts/dev_db.py`); re-run against Neon when the user has created it.

- [ ] **Step 10: Record spike results** — `docs/spikes.md`
```markdown
# Risk spikes (M1)

| Risk | How verified | Result | Date |
|---|---|---|---|
| R1 bundle size | `scripts/measure_bundle.py` | <paste TOTAL line + top 5> | 2026-10-25 |
| R2 SSE on Vercel | `curl -N https://<deploy>/_diag/sse` shows ticks ~1 s apart | pending deploy | |
| R3 checkpointer on Neon pooler | `scripts/check_checkpointer.py` | local: <paste>; Neon: pending | |
```

- [ ] **Step 11: Commit**
```bash
git add app.py vercel.json scripts docs/spikes.md src/statbrief tests .gitignore .env.example
git commit -m "feat(m1): vercel entrypoint, dev db, SSE/bundle/checkpointer spikes"
```

---

## Self-review

1. **Spec coverage (M1 scope):** AC 2 (signup/login/Argon2/cookies) → T3, T5, T6. AC 3 (demo user from env) → T3, T6 runtime seeding. AC 4 (login throttling) → T4, T6. AC 5 (run limit + daily cap) → T4 (enforcement in run routes comes in M7/M8; quota displayed in T6). AC 6 (user scoping) → user_id on all tables; enforced repos arrive with reports in M2+. AC 1 (Vercel deploy) → T7 (actual deploy needs user's Vercel account). AC 9 limits → settings in T1 (enforced in M2). R1/R2/R3 → T7.
2. **Placeholders:** `docs/spikes.md` contains "<paste …>" cells intentionally filled with measured output in Step 10 — they are results, not code.
3. **Type consistency:** `bootstrap`, `create_pool`, `run_migrations(conn)`, `setup_langgraph(conn, timeout)`, `User`, `consume`, `LimitExceeded`, `issue_session/read_session/clear_session`, `Runtime/RuntimeHolder`, `get_runtime`, `current_user` names match across tasks.
