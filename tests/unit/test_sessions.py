"""Tests for signed session cookies."""
from starlette.responses import Response

from statbrief.auth.sessions import COOKIE_NAME, clear_session, issue_session, read_session


def _cookie(resp: Response) -> str:
    """Extract the raw Set-Cookie header for the session cookie."""
    return next(v for k, v in resp.raw_headers if k == b"set-cookie" and v.startswith(COOKIE_NAME.encode())).decode()


def test_roundtrip(settings):
    """An issued cookie reads back to the same user id."""
    resp = Response()
    issue_session(resp, "user-123", settings, remember=True)
    header = _cookie(resp)
    value = header.split(";")[0].split("=", 1)[1]
    assert read_session(value, settings) == "user-123"
    assert "httponly" in header.lower() and "samesite=lax" in header.lower()
    assert "max-age=2592000" in header.lower()  # 30 days


def test_session_cookie_without_remember_has_no_max_age(settings):
    """Without 'remember me' the cookie ends with the browser session."""
    resp = Response()
    issue_session(resp, "u", settings, remember=False)
    assert "max-age" not in _cookie(resp).lower()


def test_tampered_or_missing_cookie_rejected(settings):
    """Garbage, tampered and missing cookies yield None."""
    resp = Response()
    issue_session(resp, "user-123", settings, remember=True)
    value = _cookie(resp).split(";")[0].split("=", 1)[1]
    assert read_session(value[:-2] + "xx", settings) is None
    assert read_session("garbage", settings) is None
    assert read_session(None, settings) is None


def test_clear(settings):
    """Clearing expires the cookie."""
    resp = Response()
    clear_session(resp, settings)
    assert 'max-age=0' in _cookie(resp).lower()


def test_prod_cookie_is_secure(settings):
    """In prod the issued cookie carries the Secure attribute; in test it does not."""
    prod = settings.model_copy(update={"app_env": "prod"})
    resp = Response()
    issue_session(resp, "u", prod, remember=True)
    assert "secure" in _cookie(resp).lower()
    resp_test = Response()
    issue_session(resp_test, "u", settings, remember=True)
    assert "secure" not in _cookie(resp_test).lower()
