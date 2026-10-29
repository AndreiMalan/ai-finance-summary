"""/cron/cleanup (secret required), POST /account/delete, DELETE /api/reports/{id}."""
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from statbrief.web.main import create_app

H = {"X-StatBrief-Client": "1"}
_PW = "long-enough-pw"
_SECRET = "cron-secret-for-tests-0123456789"


def _client(settings, tmp_path, **overrides):
    """App client with a local blob store."""
    s = settings.model_copy(update={"local_blob_dir": str(tmp_path), **overrides})
    return TestClient(create_app(s), follow_redirects=False)


@pytest.fixture
def client(settings, tmp_path):
    """Client logged in as u@example.com, with CRON_SECRET configured."""
    with _client(settings, tmp_path, cron_secret=SecretStr(_SECRET)) as c:
        assert c.post("/signup", data={"email": "u@example.com", "password": _PW}).status_code == 303
        yield c


def _report(client) -> str:
    """Create a report; return its id."""
    return client.post("/api/reports", json={"title": "Q1"}, headers=H).json()["id"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": _SECRET},
                                     {"X-Cron-Secret": _SECRET}])
def test_cron_rejected_without_the_secret(client, headers):
    """Missing, wrong or malformed credentials get 401."""
    assert client.get("/cron/cleanup", headers=headers).status_code == 401


def test_cron_runs_with_the_secret(client):
    """The right bearer secret runs every rule and returns counts only."""
    r = client.get("/cron/cleanup", headers={"Authorization": f"Bearer {_SECRET}"})
    assert r.status_code == 200
    body = r.json()
    assert body["failed_rules"] == [] and {"raw_files_purged", "threads_deleted", "uploads_deleted"} <= set(body)


def test_cron_rejected_when_no_secret_is_configured(settings, tmp_path):
    """With CRON_SECRET unset, even an empty bearer token is refused (the job never runs unauthenticated)."""
    with _client(settings, tmp_path) as c:
        assert c.get("/cron/cleanup", headers={"Authorization": "Bearer "}).status_code == 401
        assert c.app.state.runtime_holder._runtime is None  # the database was not even opened


def test_account_delete_requires_client_header(client):
    """The CSRF guard applies (403 without X-StatBrief-Client)."""
    assert client.post("/account/delete", data={"password": _PW, "confirm": "DELETE"}).status_code == 403


def test_account_delete_requires_confirmation_and_password(client):
    """Wrong confirmation word → 400; wrong password → 403; the account survives both."""
    assert client.post("/account/delete", data={"password": _PW, "confirm": "yes"}, headers=H).status_code == 400
    assert client.post("/account/delete", data={"password": "wrong-password", "confirm": "DELETE"},
                       headers=H).status_code == 403
    assert client.post("/api/reports", json={"title": "still here"}, headers=H).status_code == 201


def test_account_delete_is_throttled_like_login(client, settings):
    """Repeated wrong passwords hit the login throttle (429)."""
    for _ in range(settings.login_max_failures):
        client.post("/account/delete", data={"password": "wrong-password", "confirm": "DELETE"}, headers=H)
    r = client.post("/account/delete", data={"password": _PW, "confirm": "DELETE"}, headers=H)
    assert r.status_code == 429


def test_account_delete_removes_the_account_and_logs_out(client):
    """Correct password + confirmation: account deleted, session cleared, login no longer works."""
    _report(client)
    r = client.post("/account/delete", data={"password": _PW, "confirm": "DELETE"}, headers=H)
    assert r.status_code == 200 and r.json() == {"deleted": True} and r.headers["HX-Redirect"] == "/signup"
    assert client.post("/api/reports", json={"title": "x"}, headers=H).status_code == 401
    assert client.post("/login", data={"email": "u@example.com", "password": _PW}).status_code == 400


def test_demo_account_cannot_be_deleted(settings, tmp_path):
    """The shared demo account is protected."""
    with _client(settings, tmp_path) as c:
        assert c.post("/login", data={"email": "demo@example.com", "password": "demo-password-123"}).status_code == 303
        r = c.post("/account/delete", data={"password": "demo-password-123", "confirm": "DELETE"}, headers=H)
        assert r.status_code == 403


def test_report_delete_is_owner_only(client, settings, tmp_path):
    """Owner deletes (204, then 404); malformed ids 404; the guard applies."""
    rid = _report(client)
    assert client.delete(f"/api/reports/{rid}").status_code == 403
    assert client.delete("/api/reports/not-a-uuid", headers=H).status_code == 404
    client.post("/logout")
    assert client.post("/signup", data={"email": "other@example.com", "password": _PW}).status_code == 303
    assert client.delete(f"/api/reports/{rid}", headers=H).status_code == 404
    client.post("/logout")
    assert client.post("/login", data={"email": "u@example.com", "password": _PW}).status_code == 303
    assert client.delete(f"/api/reports/{rid}", headers=H).status_code == 204
    assert client.delete(f"/api/reports/{rid}", headers=H).status_code == 404


def test_storage_failure_answers_503_and_keeps_data(client, monkeypatch):
    """A blob outage during deletion answers 503 and leaves the account in place."""
    rid = _report(client)
    rt = client.app.state.runtime_holder.get()
    with rt.pool.connection() as c:
        uid = c.execute("SELECT id FROM users WHERE email = 'u@example.com'").fetchone()["id"]
        c.execute("INSERT INTO files (report_id, user_id, filename, kind, size_bytes, sha256, blob_key) "
                  "VALUES (%s, %s, 'f.csv', 'csv', 1, 'h', 'users/x/f.csv')", (rid, uid))
    monkeypatch.setattr(rt.blobs, "delete", lambda key: (_ for _ in ()).throw(RuntimeError("down")))
    r = client.post("/account/delete", data={"password": _PW, "confirm": "DELETE"}, headers=H)
    assert r.status_code == 503 and r.json()["code"] == "storage_unavailable"
    assert client.delete(f"/api/reports/{rid}", headers=H).status_code == 503
    assert client.post("/api/reports", json={"title": "still here"}, headers=H).status_code == 201
