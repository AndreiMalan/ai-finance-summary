"""GET /reports/{id}/exports/{version}/{fmt}: owner-only download of stored exports."""
import pytest
from fastapi.testclient import TestClient

from statbrief.export.bundle import ReportBundle
from statbrief.export.format import ExportOptions
from statbrief.export.store import save_exports
from statbrief.web.main import create_app

H = {"X-StatBrief-Client": "1"}
_PW = "long-enough-pw"


def _signup(client, email):
    """Sign up (and log in) `email`."""
    assert client.post("/signup", data={"email": email, "password": _PW}).status_code == 303


@pytest.fixture
def setup(settings, tmp_path):
    """App client logged in as alice with one stored export version of her report; bob's credentials."""
    s = settings.model_copy(update={"local_blob_dir": str(tmp_path)})
    app = create_app(s)
    with TestClient(app, follow_redirects=False) as client:
        _signup(client, "alice@example.com")
        rid = client.post("/api/reports", json={"title": "Q1"}, headers=H).json()["id"]
        rt = app.state.runtime_holder.get()
        alice = rt.pool.connection().__enter__().execute("SELECT id FROM users WHERE email = 'alice@example.com'") \
            .fetchone()["id"]
        bundle = ReportBundle.example().model_copy(update={"report_id": rid})
        save_exports(rt.pool, rt.blobs, str(alice), bundle, ["pdf", "xlsx"], ExportOptions())
        yield client, rid, rt


def test_owner_downloads_each_format(setup):
    """The owner gets the stored bytes with attachment, nosniff and no-store headers."""
    client, rid, _ = setup
    r = client.get(f"/reports/{rid}/exports/1/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="statbrief-report-{rid[:8]}-v1.pdf"'
    assert r.headers["x-content-type-options"] == "nosniff" and "no-store" in r.headers["cache-control"]
    x = client.get(f"/reports/{rid}/exports/1/xlsx")
    assert x.status_code == 200 and x.content.startswith(b"PK")


@pytest.mark.parametrize("path", ["/exports/2/pdf", "/exports/1/docx", "/exports/x/pdf", "/exports/-1/pdf",
                                  "/exports/99999999999999/pdf", "/exports/１/pdf"])
def test_unknown_version_or_format_is_404(setup, path):
    """Unknown versions/formats and malformed or oversized version numbers are 404, never 500."""
    client, rid, _ = setup
    assert client.get(f"/reports/{rid}{path}").status_code == 404


def test_malformed_report_id_is_404(setup):
    """A malformed report id is not found."""
    client, _, _ = setup
    assert client.get("/reports/not-a-uuid/exports/1/pdf").status_code == 404


def test_other_user_gets_404(setup):
    """Bob cannot download Alice's export (404, not 403: existence is not revealed)."""
    client, rid, _ = setup
    client.post("/logout")
    _signup(client, "bob@example.com")
    assert client.get(f"/reports/{rid}/exports/1/pdf").status_code == 404


def test_anonymous_is_redirected_to_login(setup):
    """Without a session the download redirects to the login page."""
    client, rid, _ = setup
    client.post("/logout")
    r = client.get(f"/reports/{rid}/exports/1/pdf")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_missing_blob_is_410_and_failing_store_503(setup, monkeypatch):
    """A purged export answers 410; a storage outage answers 503 (both `{code, message}`)."""
    client, rid, rt = setup
    monkeypatch.setattr(rt.blobs, "get", lambda key: (_ for _ in ()).throw(FileNotFoundError(key)))
    r = client.get(f"/reports/{rid}/exports/1/pdf")
    assert r.status_code == 410 and r.json()["code"] == "file_gone"
    monkeypatch.setattr(rt.blobs, "get", lambda key: (_ for _ in ()).throw(RuntimeError("down")))
    r = client.get(f"/reports/{rid}/exports/1/pdf")
    assert r.status_code == 503 and r.json()["code"] == "storage_unavailable"
