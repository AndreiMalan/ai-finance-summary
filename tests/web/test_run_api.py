"""Report run API (M7b Task 8): SSE stream, queued resume answers, user scoping, client-header guard."""
import json

import pytest

from statbrief.analytics.metrics import compute_metrics
from statbrief.graph.runtime import RunResources
from statbrief.storage.repos import ObservationRepo
from tests.graph.helpers import draft_for, long_csv, report_call, unemp_rows
from tests.graph.runkit import Script
from tests.web.test_uploads_api import H, _make_client, _report, _upload

PW = "long-enough-pw"
PARAMS = {"mode": "upload", "indicators": ["IRS_UNEMP"], "regions": ["RO"], "formats": ["xlsx"]}


def events(response) -> list[dict]:
    """Parse an SSE body into [{type, ...}] in order."""
    out = []
    for frame in response.text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "data" in lines:
            out.append(json.loads(lines["data"]))
    return out


@pytest.fixture
def app_client(settings, tmp_path):
    """Logged-in client whose runs use a scripted model and no network."""
    script = Script()
    model = script.model()
    with _make_client(settings, tmp_path) as c:
        c.app.state.run_resources_factory = lambda rt: RunResources(
            pool=rt.pool, settings=rt.settings, blobs=rt.blobs, model_factory=lambda s, cap: model,
            embeddings_factory=lambda s: None, sleep=lambda s: None)
        assert c.post("/signup", data={"email": "u@example.com", "password": PW}).status_code == 303
        yield c, script


def _pool(client):
    """The app's database pool."""
    return client.app.state.runtime_holder.get().pool


def _metric(client, rid):
    """The uploaded pp change 2024-01 vs 2023-01 (after the first segment normalized the file)."""
    pool = _pool(client)
    with pool.connection() as c:
        uid = str(c.execute("SELECT user_id FROM reports WHERE id = %s", (rid,)).fetchone()["user_id"])
    obs = ObservationRepo(pool).list_for_report(uid, rid)
    return next(m for m in compute_metrics(obs, indicator="IRS_UNEMP", region="RO")[0]
                if m.kind == "pp_change" and m.base_period == "2023-01")


def test_full_run_over_http(app_client):
    """params -> stream (resolve) -> resume -> stream (review) -> approve -> stream (done) -> preferences."""
    client, script = app_client
    rid = _report(client)
    done = _upload(client, rid, "cryptic.csv", long_csv(unemp_rows(), header=("c1", "c2", "c3", "c4", "c5")))
    fid = done.json()["file_id"]
    assert client.put(f"/api/reports/{rid}/params", json=PARAMS, headers=H).status_code == 200
    first = client.get(f"/api/reports/{rid}/run/stream")
    assert first.status_code == 200 and first.headers["content-type"].startswith("text/event-stream")
    evs = events(first)
    assert evs[0]["type"] == "started" and evs[-1]["type"] == "interrupt" and evs[-1]["data"]["kind"] == "resolve"

    bad = client.post(f"/api/reports/{rid}/resume", json={"payload": {"bogus": 1}}, headers=H)
    assert bad.status_code == 422 and any("bogus" in e for e in bad.json()["errors"])
    assert client.post(f"/api/reports/{rid}/resume", json={"payload": {}}, headers=H).status_code == 422
    ok = client.post(f"/api/reports/{rid}/resume", json={"payload": {"confirm_mappings": [fid]}}, headers=H)
    assert ok.status_code == 202 and ok.json()["queued"] == "resolve"

    script.replies.append(report_call(draft_for(_metric(client, rid), "0.6")))
    second = events(client.get(f"/api/reports/{rid}/run/stream"))
    assert second[-1]["type"] == "interrupt" and second[-1]["data"]["kind"] == "review"
    assert any(e["type"] == "progress" and e.get("node") == "draft" for e in second)
    assert client.post(f"/api/reports/{rid}/resume", json={"payload": {"action": "approve"}},
                       headers=H).status_code == 202
    third = events(client.get(f"/api/reports/{rid}/run/stream"))
    assert third[-1] == {"type": "done", "progress": 1.0, "data": {"status": "approved"}}
    assert client.get("/api/preferences").json()["formats"] == ["xlsx"]
    # Finished: answering again is a conflict, not a hang.
    assert client.post(f"/api/reports/{rid}/resume", json={"payload": {"action": "approve"}},
                       headers=H).status_code == 409


def test_other_users_report_is_404_and_guard_applies(app_client):
    """AC 6: every run route answers 404 for another user's report; state changes need the client header."""
    client, _ = app_client
    rid = _report(client)
    assert client.put(f"/api/reports/{rid}/params", json=PARAMS).status_code == 403
    assert client.post(f"/api/reports/{rid}/resume", json={"payload": {"a": 1}}).status_code == 403
    client.post("/logout", headers=H)
    client.cookies.clear()
    assert client.post("/signup", data={"email": "other@example.com", "password": PW}).status_code == 303
    assert client.put(f"/api/reports/{rid}/params", json=PARAMS, headers=H).status_code == 404
    assert client.get(f"/api/reports/{rid}/run/stream").status_code == 404
    assert client.post(f"/api/reports/{rid}/resume", json={"payload": {"action": "approve"}},
                       headers=H).status_code == 404


def test_invalid_params_and_no_pending_interrupt(app_client):
    """Contradictory links are rejected (422); resume before any interrupt is 409; a run without params errors."""
    client, _ = app_client
    rid = _report(client)
    bad = {**PARAMS, "links": [{"upload_code": "A", "catalog_code": "HICP_YOY"},
                               {"upload_code": "A", "catalog_code": "UNEMP_RATE"}]}
    assert client.put(f"/api/reports/{rid}/params", json=bad, headers=H).status_code == 422
    assert client.post(f"/api/reports/{rid}/resume", json={"payload": {"action": "approve"}},
                       headers=H).status_code == 409
    evs = events(client.get(f"/api/reports/{rid}/run/stream"))
    assert [e["type"] for e in evs] == ["error"]


def test_history_routes_scoped_disabled_and_fork(app_client, settings, tmp_path):
    """History lists checkpoints; fork answers with the new checkpoint; 404 for others and when disabled."""
    client, script = app_client
    rid = _report(client)
    _upload(client, rid, "irs.csv", long_csv(unemp_rows()))
    client.put(f"/api/reports/{rid}/params", json=PARAMS, headers=H)
    script.replies.append(lambda: report_call(draft_for(_metric(client, rid), "0.6")))
    assert events(client.get(f"/api/reports/{rid}/run/stream"))[-1]["data"]["kind"] == "review"
    entries = client.get(f"/api/reports/{rid}/history").json()
    assert entries[0]["interrupt"] == "review" and entries[0]["has_draft"]
    assert client.get(f"/api/reports/{rid}/history/{entries[0]['checkpoint_id']}/draft").json()["title"]
    assert client.get(f"/api/reports/{rid}/history/nope/draft").status_code == 404
    target = next(e for e in entries if e["next"] == ["drafting"])
    assert client.post(f"/api/reports/{rid}/history/{target['checkpoint_id']}/fork").status_code == 403
    forked = client.post(f"/api/reports/{rid}/history/{target['checkpoint_id']}/fork", headers=H)
    assert forked.status_code == 200 and client.get(f"/api/reports/{rid}/history").json()[0]["checkpoint_id"] == \
        forked.json()["checkpoint_id"]
    client.post("/logout", headers=H)
    client.cookies.clear()
    client.post("/signup", data={"email": "other@example.com", "password": PW})
    assert client.get(f"/api/reports/{rid}/history").status_code == 404
    with _make_client(settings, tmp_path, enable_replay=False) as off:
        off.post("/signup", data={"email": "third@example.com", "password": PW})
        rid2 = _report(off)
        assert off.get(f"/api/reports/{rid2}/history").status_code == 404
