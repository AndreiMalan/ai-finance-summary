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
