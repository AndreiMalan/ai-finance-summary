"""End-to-end tests of the chunked upload and ingestion API."""
import pytest
from fastapi.testclient import TestClient

from statbrief.web.main import create_app
from tests.fixtures import XLM_MACRO_XLS, csv_bytes, xlsx_bytes

H = {"X-StatBrief-Client": "1"}
_PW = "long-enough-pw"
_CANONICAL = [["date", "region", "indicator", "value", "unit"],
              ["2024-01", "RO113", "HICP", "7.4", "%"], ["2024-02", "RO113", "HICP", "7.1", "%"]]


def _make_client(settings, tmp_path, **overrides):
    """App client (local blob store under tmp_path, tiny chunks); callers sign up their own user."""
    s = settings.model_copy(update={"local_blob_dir": str(tmp_path), "upload_chunk_mb": 0.0001, **overrides})
    return TestClient(create_app(s), follow_redirects=False)


@pytest.fixture
def client(settings, tmp_path):
    """Logged-in client with a local blob store and tiny chunks."""
    with _make_client(settings, tmp_path) as c:
        assert c.post("/signup", data={"email": "u@example.com", "password": _PW}).status_code == 303
        yield c


def _start(client, rid, name, size):
    """Open an upload; return the response."""
    return client.post(f"/api/reports/{rid}/uploads", json={"filename": name, "total_bytes": size}, headers=H)


def _upload(client, rid, name, data):
    """Upload `data` in chunks; return the completion response."""
    r = _start(client, rid, name, len(data))
    assert r.status_code == 201, r.text
    up = r.json()
    size = up["chunk_bytes"]
    for i in range(up["chunk_count"]):
        chunk = data[i * size:(i + 1) * size]
        assert client.put(f"/api/uploads/{up['upload_id']}/chunks/{i}", content=chunk,
                          headers={**H, "Content-Type": "application/octet-stream"}).status_code == 204
    return client.post(f"/api/uploads/{up['upload_id']}/complete", headers=H)


def _report(client):
    """Create a report and return its id."""
    r = client.post("/api/reports", json={"title": "Q1"}, headers=H)
    assert r.status_code == 201
    return r.json()["id"]


def _file(client, rows=None, name="d.csv"):
    """Upload a CSV (canonical long format by default) into a new report; return the file id."""
    done = _upload(client, _report(client), name, csv_bytes(rows or _CANONICAL))
    assert done.status_code == 201, done.text
    return done.json()["file_id"]


_CANON_MAP = {"header_row": 1, "date_col": "date", "region_col": "region", "indicator_col": "indicator",
              "value_col": "value", "unit_col": "unit"}


def test_full_flow_upload_preview_map_validate_resolve_normalize(client):
    """Upload → preview/detect → mapping → validate → resolve → normalize, with provenance."""
    rid = _report(client)
    rows = [["IRS"], [None], ["luna", "judet", "indicator", "valoare", "um"],
            ["2024-01", "Cluj", "HICP", 7.4, "%"], ["2024-13", "Cluj", "HICP", 7.1, "%"],
            ["2024-03", "Iași", "HICP", 6.6, "%"]]
    done = _upload(client, rid, "IRS.xlsx", xlsx_bytes({"Inflation": rows}))
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["kind"] == "xlsx" and body["worksheets"] == [{"name": "Inflation", "hidden": False}]
    fid = body["file_id"]
    prev = client.get(f"/api/files/{fid}/preview").json()
    assert prev["detected"]["header_row"] == 3 and prev["columns"][0] == "luna"
    assert prev["worksheet"] == "Inflation" and prev["row_numbers"] == [4, 5, 6]
    mapping = {"worksheet": "Inflation", "header_row": 3, "date_col": "luna", "region_col": "judet",
               "indicator_col": "indicator", "value_col": "valoare", "unit_col": "um"}
    assert client.put(f"/api/files/{fid}/mapping", json=mapping, headers=H).status_code == 200
    report = client.post(f"/api/files/{fid}/validate", headers=H).json()
    assert [i["issue_id"] for i in report["issues"]] == ["invalid_date:luna"]
    blocked = client.post(f"/api/files/{fid}/normalize", headers=H)
    assert blocked.status_code == 409 and blocked.json()["unresolved"] == ["invalid_date:luna"]
    resolved = client.post(f"/api/files/{fid}/issues/invalid_date:luna", json={"resolution": "excluded"},
                           headers=H)
    assert resolved.status_code == 200
    issue = resolved.json()["issues"][0]
    assert issue["resolution"] == "excluded" and issue["rows"] == [5]  # what the exclusion removes
    res = client.post(f"/api/files/{fid}/normalize", headers=H).json()
    assert res["observations"] == 2 and res["excluded"][0]["row_number"] == 5


def test_rejections_and_limits(client, settings):
    """Unsafe content, oversize uploads, file limits and missing CSRF header are refused."""
    rid = _report(client)
    bad = _upload(client, rid, "m.xlsx", b"not a zip")
    assert bad.status_code == 422 and bad.json()["code"] == "malformed"
    too_big = _start(client, rid, "x.csv", settings.max_upload_mb * 1024 * 1024 + 1)
    assert too_big.status_code == 413
    assert _start(client, rid, "x.exe", 3).status_code == 422
    assert _start(client, rid, "x.csv", 0).status_code == 422
    assert client.post("/api/reports", json={"title": "x"}).status_code == 403  # no X-StatBrief-Client


def test_rejected_upload_is_discarded(client):
    """After a sniffing rejection the staged upload is gone (completing again is 404)."""
    rid = _report(client)
    r = _start(client, rid, "m.xlsx", 9)
    uid = r.json()["upload_id"]
    for i in range(r.json()["chunk_count"]):
        client.put(f"/api/uploads/{uid}/chunks/{i}", content=b"not a zip"[i * 104:(i + 1) * 104], headers=H)
    assert client.post(f"/api/uploads/{uid}/complete", headers=H).status_code == 422
    assert client.post(f"/api/uploads/{uid}/complete", headers=H).status_code == 404


def test_real_xls_with_macro_sheet_rejected_on_complete(client):
    """The committed .xls with an Excel 4.0 macro sheet is refused at completion (AC 10-11)."""
    done = _upload(client, _report(client), "macro.xls", XLM_MACRO_XLS.read_bytes())
    assert done.status_code == 422 and done.json()["code"] == "macro_enabled"


def test_chunk_bounds(client):
    """Chunk count is server-derived; oversized chunks, bad indexes and missing chunks are refused."""
    rid = _report(client)
    r = _start(client, rid, "d.csv", 250)
    up = r.json()
    assert up["chunk_count"] == 3 and up["chunk_bytes"] == 104  # ceil(250 / 104), from 0.0001 MB
    url = f"/api/uploads/{up['upload_id']}"
    assert client.put(f"{url}/chunks/0", content=b"x" * 105, headers=H).status_code == 413
    assert client.put(f"{url}/chunks/3", content=b"x", headers=H).status_code == 400
    assert client.put(f"{url}/chunks/0", content=b"x" * 104, headers=H).status_code == 204
    assert client.post(f"{url}/complete", headers=H).status_code == 400  # chunks 1-2 missing
    assert client.put(f"{url}/chunks/0", content=b"x").status_code == 403  # no client header
    assert client.put("/api/uploads/not-a-uuid/chunks/0", content=b"x", headers=H).status_code == 404


def test_size_mismatch_on_complete(client):
    """Chunks that do not add up to the declared size are refused at completion."""
    r = _start(client, _report(client), "d.csv", 10).json()
    client.put(f"/api/uploads/{r['upload_id']}/chunks/0", content=b"abc", headers=H)
    assert client.post(f"/api/uploads/{r['upload_id']}/complete", headers=H).status_code == 400


def test_file_limit_per_report(settings, tmp_path):
    """MAX_FILES_PER_REPORT is enforced when starting and when completing an upload (409)."""
    with _make_client(settings, tmp_path, max_files_per_report=1) as c:
        c.post("/signup", data={"email": "l@example.com", "password": _PW})
        rid = _report(c)
        data = csv_bytes(_CANONICAL)
        pending = _start(c, rid, "b.csv", len(data)).json()  # opened while the report is still empty
        assert _upload(c, rid, "a.csv", data).status_code == 201
        assert _start(c, rid, "c.csv", len(data)).status_code == 409
        for i in range(pending["chunk_count"]):
            size = pending["chunk_bytes"]
            c.put(f"/api/uploads/{pending['upload_id']}/chunks/{i}", content=data[i * size:(i + 1) * size],
                  headers=H)
        assert c.post(f"/api/uploads/{pending['upload_id']}/complete", headers=H).status_code == 409


def test_too_many_columns_rejected_at_preview(settings, tmp_path):
    """A CSV wider than MAX_COLUMNS_PER_FILE is refused with 422 when read."""
    with _make_client(settings, tmp_path, max_columns_per_file=3) as c:
        c.post("/signup", data={"email": "w@example.com", "password": _PW})
        fid = _file(c)  # 5 columns
        r = c.get(f"/api/files/{fid}/preview")
        assert r.status_code == 422 and r.json()["code"] == "too_large"
        assert c.put(f"/api/files/{fid}/mapping", json=_CANON_MAP, headers=H).status_code == 422


def test_preview_cells_are_json_safe(client):
    """Datetimes become ISO strings and formulas without cached values become a marker object."""
    from datetime import datetime

    rows = [["date", "value", "calc"], [datetime(2024, 1, 31), 1.5, "=1+1"]]
    done = _upload(client, _report(client), "f.xlsx", xlsx_bytes({"S": rows}))
    prev = client.get(f"/api/files/{done.json()['file_id']}/preview", params={"header_row": 1})
    assert prev.status_code == 200
    assert prev.json()["rows"][0] == ["2024-01-31T00:00:00", 1.5, {"formula_without_value": True}]
    assert client.get(f"/api/files/{done.json()['file_id']}/preview",
                      params={"header_row": 9}).status_code == 422
    assert client.get(f"/api/files/{done.json()['file_id']}/preview",
                      params={"worksheet": "nope"}).status_code == 422


def test_mapping_rejects_unknown_columns(client):
    """A mapping naming columns absent from the table is refused."""
    fid = _file(client)
    assert client.put(f"/api/files/{fid}/mapping", json={**_CANON_MAP, "value_col": "nope"},
                      headers=H).status_code == 422
    assert client.put(f"/api/files/{fid}/mapping", json={"date_col": "date"}, headers=H).status_code == 422


def test_validate_and_normalize_require_previous_steps(client):
    """Validate needs a mapping; normalize needs a validation report (409)."""
    fid = _file(client)
    assert client.post(f"/api/files/{fid}/validate", headers=H).status_code == 409
    assert client.post(f"/api/files/{fid}/normalize", headers=H).status_code == 409
    client.put(f"/api/files/{fid}/mapping", json=_CANON_MAP, headers=H)
    assert client.post(f"/api/files/{fid}/normalize", headers=H).status_code == 409
    assert client.post(f"/api/files/{fid}/issues/x", json={"resolution": "excluded"}, headers=H).status_code == 409
    assert client.post(f"/api/files/{fid}/validate", headers=H).json()["issues"] == []
    assert client.post(f"/api/files/{fid}/normalize", headers=H).json() == {"observations": 2, "excluded": []}


def test_stale_validation_report_requires_revalidation(client):
    """When the mapping changes the table after validation, normalize answers 409 re-validate first."""
    rows = [_CANONICAL[0], *_CANONICAL]  # header repeated: header_row 1 and 2 give the same columns
    fid = _file(client, rows)
    client.put(f"/api/files/{fid}/mapping", json={**_CANON_MAP, "header_row": 2}, headers=H)
    assert client.post(f"/api/files/{fid}/validate", headers=H).status_code == 200
    client.put(f"/api/files/{fid}/mapping", json=_CANON_MAP, headers=H)  # now one more data row
    r = client.post(f"/api/files/{fid}/normalize", headers=H)
    assert r.status_code == 409 and r.json() == {"detail": "re-validate first"}


def test_documented_extra_columns_are_known(client):
    """Columns described in the mapping are not reported as undocumented."""
    rows = [[*r, n] for r, n in zip(_CANONICAL, ["note", "a", "b"], strict=True)]
    fid = _file(client, rows)
    client.put(f"/api/files/{fid}/mapping", json=_CANON_MAP, headers=H)
    ids = [i["issue_id"] for i in client.post(f"/api/files/{fid}/validate", headers=H).json()["issues"]]
    assert ids == ["undocumented_column:note"]
    client.put(f"/api/files/{fid}/mapping", json={**_CANON_MAP, "column_descriptions": {"note": "comment"}},
               headers=H)
    assert client.post(f"/api/files/{fid}/validate", headers=H).json()["issues"] == []


def test_issue_resolution_rules(client):
    """Errors cannot be accepted (422); unknown issues are 404; warnings can be accepted."""
    rows = [*_CANONICAL, ["2024-13", "RO113", "HICP", "7.0", "%"]]
    rows = [[*r, n] for r, n in zip(rows, ["note", "a", "b", "c"], strict=True)]
    fid = _file(client, rows)
    client.put(f"/api/files/{fid}/mapping", json=_CANON_MAP, headers=H)
    client.post(f"/api/files/{fid}/validate", headers=H)
    url = f"/api/files/{fid}/issues"
    assert client.post(f"{url}/invalid_date:date", json={"resolution": "accepted"}, headers=H).status_code == 422
    assert client.post(f"{url}/nope", json={"resolution": "excluded"}, headers=H).status_code == 404
    assert client.post(f"{url}/invalid_date:date", json={"resolution": "fixed"}, headers=H).status_code == 422
    ok = client.post(f"{url}/undocumented_column:note", json={"resolution": "accepted"}, headers=H)
    assert ok.status_code == 200
    by_id = {i["issue_id"]: i for i in ok.json()["issues"]}
    assert by_id["undocumented_column:note"]["resolution"] == "accepted"
    assert by_id["invalid_date:date"]["rows"] == [4]


def test_other_users_cannot_access(client, settings, tmp_path):
    """A second user gets 404 for the first user's report, upload and file."""
    rid = _report(client)
    fid = _upload(client, rid, "d.csv", csv_bytes([["date", "value"], ["2024", "1"]])).json()["file_id"]
    uid = _start(client, rid, "e.csv", 3).json()["upload_id"]
    with _make_client(settings, tmp_path) as other:
        other.post("/signup", data={"email": "o@example.com", "password": _PW})
        assert other.get(f"/api/files/{fid}/preview").status_code == 404
        assert _start(other, rid, "a.csv", 3).status_code == 404
        assert other.put(f"/api/uploads/{uid}/chunks/0", content=b"abc", headers=H).status_code == 404
        assert other.post(f"/api/uploads/{uid}/complete", headers=H).status_code == 404
        assert other.put(f"/api/files/{fid}/mapping", json=_CANON_MAP, headers=H).status_code == 404
        for step in ("validate", "normalize"):
            assert other.post(f"/api/files/{fid}/{step}", headers=H).status_code == 404


def test_template_downloads(client):
    """CSV and XLSX templates download with the right content types."""
    r = client.get("/api/template.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == "date,region,indicator,value,unit"
    x = client.get("/api/template.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_api_requires_login(settings, tmp_path):
    """Logged-out API calls get 401 JSON (not an HTML redirect), templates included."""
    with _make_client(settings, tmp_path) as anon:
        r = anon.get("/api/template.csv")
        assert r.status_code == 401 and r.json() == {"detail": "login required"}
        assert anon.post("/api/reports", json={"title": "x"}, headers=H).status_code == 401
