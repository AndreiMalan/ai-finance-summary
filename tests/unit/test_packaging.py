"""Deployment manifests stay consistent (Vercel installs from pyproject.toml, pip from requirements.txt)."""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _requirements() -> list[str]:
    """Non-comment, non-blank lines of requirements.txt."""
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def test_pyproject_dependencies_mirror_requirements():
    """pyproject.toml [project].dependencies equals requirements.txt exactly, in order."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == _requirements()


def test_vercel_function_key_matches_entrypoint():
    """vercel.json configures the function keyed by the root app.py entrypoint that exists."""
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert list(cfg["functions"]) == ["app.py"]
    assert (ROOT / "app.py").is_file()


def test_vercel_single_region():
    """Functions are pinned to exactly one region (Hobby allows one; co-located with Neon)."""
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert isinstance(cfg["regions"], list) and len(cfg["regions"]) == 1


def test_vercel_max_duration_exceeds_sse_diag_cap():
    """The function maxDuration leaves room above the SSE diagnostics cap (SSE_DIAG_MAX_TOTAL_S)."""
    from statbrief.config import SSE_DIAG_MAX_TOTAL_S

    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert cfg["functions"]["app.py"]["maxDuration"] > SSE_DIAG_MAX_TOTAL_S


def test_static_assets_bundled():
    """The compiled stylesheet and vendored htmx/Alpine ship inside the package."""
    static = ROOT / "src" / "statbrief" / "web" / "static"
    for rel in ("app.css", "vendor/htmx.min.js", "vendor/alpine.min.js"):
        f = static / rel
        assert f.is_file() and f.stat().st_size > 0, rel


def test_app_entrypoint_imports(monkeypatch):
    """Root app.py imports with only env configuration and exposes an ASGI `app` (no DB opened)."""
    import importlib.util
    import sys

    from fastapi import FastAPI

    from statbrief.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.setattr(sys, "path", list(sys.path))  # app.py prepends src/; undo afterwards
    get_settings.cache_clear()
    try:
        spec = importlib.util.spec_from_file_location("statbrief_app_smoke", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert isinstance(module.app, FastAPI)
        assert module.app.state.runtime_holder._runtime is None
    finally:
        get_settings.cache_clear()


def test_reportlab_is_a_pinned_runtime_dependency():
    """PDF export runs in production: reportlab is pinned in requirements.txt and not duplicated in dev."""
    assert any(r.startswith("reportlab==") for r in _requirements())
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    assert not any(ln.strip().startswith("reportlab") for ln in dev)


def test_vercel_daily_cleanup_cron():
    """vercel.json schedules the retention cleanup once a day (Hobby allows daily crons)."""
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    crons = cfg["crons"]
    assert [c["path"] for c in crons] == ["/cron/cleanup"]
    minute, hour, dom, month, dow = crons[0]["schedule"].split()
    assert minute.isdigit() and hour.isdigit() and (dom, month, dow) == ("*", "*", "*")
