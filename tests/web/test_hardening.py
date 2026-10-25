"""Hardening tests: runtime init failure, redirect safety, health check, login input caps."""
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
from pydantic import SecretStr
import pytest
from fastapi.testclient import TestClient

from statbrief.auth.users import MAX_EMAIL_LENGTH
from statbrief.web import runtime as runtime_mod
from statbrief.web.main import create_app
from statbrief.web.routes.auth import _safe_next

_PW = "long-enough-pw"


@pytest.fixture
def client(settings):
    """App client on a fresh database (redirects not followed)."""
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


def _signup(client, email="h@example.com"):
    """Create and log in a user through the signup form."""
    r = client.post("/signup", data={"email": email, "password": _PW})
    assert r.status_code == 303


def test_failed_demo_seed_closes_pool(settings, monkeypatch):
    """A WeakPassword during demo seeding closes the new pool; retries never leave pools open."""
    opened = []
    real_create_pool = runtime_mod.create_pool

    def recording_create_pool(s):
        """Create a real pool and remember it."""
        pool = real_create_pool(s)
        opened.append(pool)
        return pool

    monkeypatch.setattr(runtime_mod, "create_pool", recording_create_pool)
    bad = settings.model_copy(update={"demo_user_password": SecretStr("short")})
    app = create_app(bad)
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        assert c.get("/login").status_code == 500
        assert len(opened) == 1 and opened[0].closed
        assert c.get("/login").status_code == 500
        assert len(opened) == 2 and all(p.closed for p in opened)
    assert app.state.runtime_holder._runtime is None


@pytest.mark.parametrize("raw,expected", [
    ("/reports", "/reports"),
    ("/reports?x=1", "/reports?x=1"),
    (None, "/reports"),
    ("", "/reports"),
    ("//evil.com", "/reports"),
    ("https://evil.com", "/reports"),
    ("/\\evil.com", "/reports"),
    ("/\t/evil.com", "/reports"),
    ("/\n/evil.com", "/reports"),
    ("/reports\x00", "/reports"),
    ("evil.com", "/reports"),
])
def test_safe_next(raw, expected):
    """Only same-site relative paths survive; protocol-relative and control-char tricks fall back."""
    assert _safe_next(raw) == expected


def test_login_redirect_never_offsite(client):
    """A hostile `next` on POST /login still redirects to a same-site path."""
    for bad in ("//evil.com", "/\\evil.com","/\t/evil.com", "https://evil.com"):
        r = client.post("/login", data={"email": "demo@example.com", "password": "demo-password-123", "next": bad})
        assert r.status_code == 303 and r.headers["location"] == "/reports"


def test_next_round_trips(client):
    """`/login?next=...` carries next into the form and the POST redirects there."""
    page = client.get("/login?next=/reports")
    assert page.status_code == 200 and 'name="next" value="/reports"' in page.text
    r = client.post("/login", data={"email": "demo@example.com", "password": "demo-password-123", "next": "/reports"})
    assert r.status_code == 303 and r.headers["location"] == "/reports"


def test_home_redirects(client):
    """`/` sends anonymous visitors to /login and logged-in users to /reports."""
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    _signup(client)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/reports"


def test_login_page_when_logged_in_redirects(client):
    """GET /login with a valid session skips the form."""
    _signup(client)
    r = client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/reports"


def test_healthz_db_failure(settings, monkeypatch):
    """A failing database check yields 503 with db false, waiting only HEALTHZ_DB_TIMEOUT_S."""
    custom = settings.model_copy(update={"healthz_db_timeout_s": 1.25})
    with TestClient(create_app(custom), follow_redirects=False) as client:
        rt = client.app.state.runtime_holder.get()
        seen_kwargs = []

        def broken_connection(*_a, **kw):
            """Simulate an unreachable database and record the kwargs used."""
            seen_kwargs.append(kw)
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(rt.pool, "connection", broken_connection)
        r = client.get("/healthz")
    assert r.status_code == 503 and r.json() == {"ok": False, "db": False}
    assert seen_kwargs == [{"timeout": 1.25}]


class _RecordingConnection:
    """Stand-in pooled connection that records SQL and answers SELECT 1."""

    def __init__(self):
        """Start with no recorded statements."""
        self.executed: list[tuple[str, object]] = []
        self.in_transaction = False

    @contextmanager
    def transaction(self):
        """Mark statements executed inside the block as transactional."""
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    def execute(self, sql, params=None):
        """Record the statement (with its transaction state) and return a one-row result."""
        self.executed.append((sql, params, self.in_transaction))
        return SimpleNamespace(fetchone=lambda: {"one": 1})


def test_healthz_sets_statement_timeout(settings, monkeypatch):
    """The health query runs under a transaction-local statement_timeout from HEALTHZ_DB_TIMEOUT_S."""
    custom = settings.model_copy(update={"healthz_db_timeout_s": 1.25})
    with TestClient(create_app(custom), follow_redirects=False) as client:
        rt = client.app.state.runtime_holder.get()
        fake = _RecordingConnection()

        @contextmanager
        def fake_connection(**_kw):
            """Hand out the recording connection."""
            yield fake

        monkeypatch.setattr(rt.pool, "connection", fake_connection)
        r = client.get("/healthz")
    assert r.status_code == 200
    (set_sql, set_params, set_tx), (sel_sql, _p, sel_tx) = fake.executed
    assert "statement_timeout" in set_sql and set_params == ("1250",) and set_tx
    assert "SELECT 1" in sel_sql and sel_tx


def test_oversized_login_email_capped(client):
    """A huge email is rejected and stored no longer than MAX_EMAIL_LENGTH."""
    r = client.post("/login", data={"email": "a" * 10_000 + "@example.com", "password": "whatever-pw"})
    assert r.status_code == 400
    with client.app.state.runtime_holder.get().pool.connection() as c:
        n = c.execute("SELECT max(length(email)) AS n FROM login_attempts").fetchone()["n"]
    assert n is not None and n <= MAX_EMAIL_LENGTH
