"""Tests for environment-driven settings."""
import pytest
from pydantic import ValidationError

from statbrief.config import Settings


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch):
    """Remove every Settings env var so the developer's shell can't leak into tests."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


def test_defaults_match_spec():
    """Defaults equal the values agreed in D-004 / spec AC 4-5, 9."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.login_max_failures == 5
    assert s.login_window_minutes == 15
    assert s.runs_per_hour == 10
    assert s.daily_llm_call_cap == 500
    assert s.max_upload_mb == 10
    assert s.max_files_per_report == 5
    assert s.session_max_age_days == 30
    assert s.signups_enabled is True
    assert s.app_env == "dev"


def test_env_overrides(monkeypatch):
    """UPPER_CASE env vars override field defaults."""
    monkeypatch.setenv("RUNS_PER_HOUR", "3")
    monkeypatch.setenv("SIGNUPS_ENABLED", "false")
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.runs_per_hour == 3
    assert s.signups_enabled is False


def test_short_session_secret_rejected():
    """A weak session secret is refused at startup."""
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://x/y", session_secret="short", _env_file=None)


def test_cookie_secure_only_in_prod():
    """Secure cookies are enforced in prod and relaxed for local http."""
    dev = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    prod = Settings(database_url="postgresql://x/y", session_secret="s" * 32, app_env="prod", _env_file=None)
    assert dev.cookie_secure is False
    assert prod.cookie_secure is True


def test_empty_env_value_treated_as_unset(monkeypatch):
    """A blank env value (e.g. `DEMO_USER_PASSWORD=`) falls back to the default None."""
    monkeypatch.setenv("DEMO_USER_PASSWORD", "")
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.demo_user_password is None
