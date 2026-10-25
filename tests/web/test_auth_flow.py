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


def test_signup_validation_errors(client, settings):
    """Weak password and duplicate email re-render the form with a message."""
    r = client.post("/signup", data={"email": "c@example.com", "password": "short"})
    assert r.status_code == 400 and f"at least {settings.password_min_length} characters" in r.text
    client.post("/signup", data={"email": "d@example.com", "password": "long-enough-pw"})
    client.post("/logout")
    r = client.post("/signup", data={"email": "d@example.com", "password": "long-enough-pw"})
    assert r.status_code == 400 and "already exists" in r.text


def test_healthz(client):
    """Health endpoint reports database connectivity."""
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": True}
