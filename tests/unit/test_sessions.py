"""Tests for signed session cookies."""
import time

import pytest
from itsdangerous.timed import TimestampSigner
from starlette.responses import Response

from statbrief.auth.sessions import COOKIE_NAME, _serializer, clear_session, issue_session, read_session

_HOUR_S = 3600
_DAY_S = 86400


def _cookie(resp: Response) -> str:
    """Extract the raw Set-Cookie header for the session cookie."""
    return next(v for k, v in resp.raw_headers if k == b"set-cookie" and v.startswith(COOKIE_NAME.encode())).decode()


def _attrs(header: str) -> list[str]:
    """Lower-cased, stripped cookie attributes (everything after the name=value pair)."""
    return [a.strip().lower() for a in header.split(";")[1:]]


def _value(resp: Response) -> str:
    """The signed session token carried by the response's session cookie."""
    return _cookie(resp).split(";")[0].split("=", 1)[1]


def _issue_aged(monkeypatch, settings, remember: bool, age_s: int) -> str:
    """Issue a session token whose signing timestamp lies `age_s` seconds in the past."""
    issued_at = int(time.time()) - age_s
    with monkeypatch.context() as m:
        m.setattr(TimestampSigner, "get_timestamp", lambda self: issued_at)
        resp = Response()
        issue_session(resp, "user-123", settings, remember=remember)
    return _value(resp)


def test_roundtrip(settings):
    """An issued cookie reads back to the same user id."""
    resp = Response()
    issue_session(resp, "user-123", settings, remember=True)
    header = _cookie(resp)
    value = header.split(";")[0].split("=", 1)[1]
    assert read_session(value, settings) == "user-123"
    assert "httponly" in header.lower() and "samesite=lax" in header.lower()
    assert "max-age=2592000" in header.lower()  # 30 days
    assert "path=/" in _attrs(header)


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
    assert "path=/" in _attrs(_cookie(resp))


def test_prod_cookie_is_secure(settings):
    """In prod the issued cookie carries the Secure attribute; in test it does not."""
    prod = settings.model_copy(update={"app_env": "prod"})
    resp = Response()
    issue_session(resp, "u", prod, remember=True)
    assert "secure" in _attrs(_cookie(resp))
    resp_test = Response()
    issue_session(resp_test, "u", settings, remember=True)
    assert "secure" not in _attrs(_cookie(resp_test))


def test_non_remember_session_valid_when_fresh(settings):
    """A just-issued non-remember token reads back."""
    resp = Response()
    issue_session(resp, "user-123", settings, remember=False)
    assert read_session(_value(resp), settings) == "user-123"


def test_remember_token_older_than_max_age_rejected(monkeypatch, settings):
    """A remember-me token older than SESSION_MAX_AGE_DAYS is expired."""
    old = _issue_aged(monkeypatch, settings, True, settings.session_max_age_days * _DAY_S + _HOUR_S)
    assert read_session(old, settings) is None


def test_remember_token_younger_than_max_age_accepted(monkeypatch, settings):
    """A remember-me token past the short limit but within the long limit stays valid."""
    aged = _issue_aged(monkeypatch, settings, True, (settings.session_short_max_age_hours + 1) * _HOUR_S)
    assert read_session(aged, settings) == "user-123"


def test_non_remember_token_older_than_short_max_age_rejected(monkeypatch, settings):
    """A non-remember token older than SESSION_SHORT_MAX_AGE_HOURS (but < 30 days) is expired."""
    aged = _issue_aged(monkeypatch, settings, False, (settings.session_short_max_age_hours + 1) * _HOUR_S)
    assert read_session(aged, settings) is None


def test_token_without_remember_flag_treated_as_non_remember(monkeypatch, settings):
    """Legacy tokens lacking the `r` flag get the short lifetime."""
    issued_at = int(time.time()) - (settings.session_short_max_age_hours + 1) * _HOUR_S
    with monkeypatch.context() as m:
        m.setattr(TimestampSigner, "get_timestamp", lambda self: issued_at)
        token = _serializer(settings).dumps({"uid": "user-123"})
    assert read_session(token, settings) is None
    assert read_session(_serializer(settings).dumps({"uid": "user-123"}), settings) == "user-123"


@pytest.mark.parametrize("payload", [["x"], {"uid": 1}, {"r": True}, "user-123", None])
def test_signed_wrong_type_payload_rejected(settings, payload):
    """Correctly signed payloads of the wrong shape yield None."""
    assert read_session(_serializer(settings).dumps(payload), settings) is None


def test_signed_undecodable_payload_rejected(settings):
    """A validly signed but non-JSON payload yields None rather than raising."""
    signer = _serializer(settings).make_signer()
    token = signer.sign(b"not-base64-json!").decode()
    assert read_session(token, settings) is None
