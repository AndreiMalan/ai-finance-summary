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
