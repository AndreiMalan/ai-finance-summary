"""Tests for environment-driven settings."""
import pytest
from pydantic import ValidationError

from statbrief.config import Settings

# The autouse env-clearing fixture now lives in tests/conftest.py so it covers every test.


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


def test_pool_min_defaults_to_one():
    """DB_POOL_MIN defaults to 1."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.db_pool_min == 1


def test_pool_min_above_max_rejected():
    """A pool minimum larger than the maximum is refused at startup."""
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://x/y",
            session_secret="s" * 32,
            db_pool_min=6,
            db_pool_max=5,
            _env_file=None,
        )


def test_bootstrap_url_falls_back_to_pooled():
    """Without DATABASE_URL_UNPOOLED, bootstrap uses DATABASE_URL."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.database_url_unpooled is None
    assert s.bootstrap_database_url == "postgresql://x/y"


def test_bootstrap_url_prefers_unpooled():
    """DATABASE_URL_UNPOOLED (direct Neon URL) wins for bootstrap work."""
    s = Settings(
        database_url="postgresql://pooled/y",
        database_url_unpooled="postgresql://direct/y",
        session_secret="s" * 32,
        _env_file=None,
    )
    assert s.bootstrap_database_url == "postgresql://direct/y"


def test_setup_lock_timeout_default_and_validation():
    """DB_SETUP_LOCK_TIMEOUT_S defaults to 60 and must be positive."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.db_setup_lock_timeout_s == 60.0
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://x/y", session_secret="s" * 32, db_setup_lock_timeout_s=0, _env_file=None)


def test_password_max_length_default_and_bounds():
    """PASSWORD_MAX_LENGTH defaults to 128, must be >= 16 and not below the minimum."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.password_max_length == 128
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://x/y", session_secret="s" * 32, password_max_length=15, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://x/y",
            session_secret="s" * 32,
            password_min_length=40,
            password_max_length=32,
            _env_file=None,
        )


def test_session_short_max_age_hours_default_and_validation():
    """Non-remember session lifetime defaults to 12 h and must be at least 1 h."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.session_short_max_age_hours == 12
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://x/y", session_secret="s" * 32,
            session_short_max_age_hours=0, _env_file=None,
        )


def _base(**kw) -> Settings:
    """Settings with the two required fields plus overrides, ignoring any .env file."""
    return Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None, **kw)


def test_sse_diag_total_duration_capped_below_max_duration():
    """SSE_DIAG_TICKS * SSE_DIAG_INTERVAL_S must stay under the function maxDuration margin."""
    assert _base(sse_diag_ticks=28, sse_diag_interval_s=10.0).sse_diag_ticks == 28  # 280 s ok
    for ticks, interval in ((29, 10.0), (60, 5.0)):  # 290 s, 300 s
        with pytest.raises(ValidationError, match="SSE_DIAG"):
            _base(sse_diag_ticks=ticks, sse_diag_interval_s=interval)


def test_diag_endpoints_disabled_by_default():
    """ENABLE_DIAG_ENDPOINTS defaults to false and can be switched on."""
    assert _base().enable_diag_endpoints is False
    assert _base(enable_diag_endpoints=True).enable_diag_endpoints is True


def test_validation_error_hides_secret_inputs():
    """A rejected SESSION_SECRET (or other input) is never echoed in the ValidationError text."""
    url = "postgresql://u:Sup3rS3cretDbPw@h/db"
    with pytest.raises(ValidationError) as short_secret:
        Settings(database_url=url, session_secret="tiny-s3cret", _env_file=None)
    with pytest.raises(ValidationError) as model_level:  # model validators see every input
        Settings(database_url=url, session_secret="s" * 32, db_pool_min=9, db_pool_max=1, _env_file=None)
    for exc in (short_secret, model_level):
        text = str(exc.value)
        assert "tiny-s3cret" not in text and "Sup3rS3cretDbPw" not in text


def test_db_timeouts_defaults_and_validation():
    """DB_CONNECT_TIMEOUT_S defaults to 5, DB_POOL_OPEN_TIMEOUT_S to 10; both must be positive."""
    s = _base()
    assert s.db_connect_timeout_s == 5.0 and s.db_pool_open_timeout_s == 10.0
    for field in ("db_connect_timeout_s", "db_pool_open_timeout_s"):
        with pytest.raises(ValidationError):
            _base(**{field: 0})


def test_short_session_must_be_shorter_than_remember_session():
    """SESSION_SHORT_MAX_AGE_HOURS must be below SESSION_MAX_AGE_DAYS * 24."""
    assert _base(session_max_age_days=1, session_short_max_age_hours=23).session_short_max_age_hours == 23
    for days, hours in ((1, 24), (30, 720), (2, 100)):
        with pytest.raises(ValidationError, match="SESSION_SHORT_MAX_AGE_HOURS"):
            _base(session_max_age_days=days, session_short_max_age_hours=hours)


def test_get_settings_is_cached(monkeypatch):
    """get_settings() reads the environment once and returns the same instance afterwards."""
    from statbrief.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    get_settings.cache_clear()
    try:
        first = get_settings()
        monkeypatch.setenv("RUNS_PER_HOUR", "7")
        assert get_settings() is first and first.runs_per_hour == 10
    finally:
        get_settings.cache_clear()
