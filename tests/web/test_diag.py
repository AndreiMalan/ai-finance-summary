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


def _prod(settings, **update):
    """Production-mode copy of the test settings."""
    return settings.model_copy(update={"app_env": "prod", "sse_diag_ticks": 1,
                                       "sse_diag_interval_s": 0.0, **update})


def test_sse_hidden_in_prod_by_default(settings):
    """In prod the diagnostics endpoint is a 404 unless explicitly enabled."""
    with TestClient(create_app(_prod(settings))) as c:
        assert c.get("/_diag/sse").status_code == 404


def test_sse_available_in_prod_when_enabled(settings):
    """ENABLE_DIAG_ENDPOINTS=true exposes the endpoint in prod."""
    with TestClient(create_app(_prod(settings, enable_diag_endpoints=True))) as c:
        r = c.get("/_diag/sse")
    assert r.status_code == 200 and "event: done" in r.text


def test_sse_prod_404_does_not_bootstrap_db(settings, monkeypatch):
    """The prod 404 gate reads settings only; it never opens the database."""
    from statbrief.web import runtime as runtime_mod

    def no_bootstrap(_s):
        """Fail the test if the request tries to bootstrap the database."""
        raise AssertionError("bootstrap must not run for a hidden diag endpoint")

    monkeypatch.setattr(runtime_mod, "bootstrap", no_bootstrap)
    app = create_app(_prod(settings))
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/_diag/sse").status_code == 404
    assert app.state.runtime_holder._runtime is None
