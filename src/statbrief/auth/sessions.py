"""Signed, HttpOnly session cookies (itsdangerous); the cookie stores only the user id.

The signed token also carries a "remember me" flag so the server can enforce a short
lifetime (SESSION_SHORT_MAX_AGE_HOURS) on browser-session cookies, independent of
whether the browser actually discards them.
"""
import time

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.responses import Response

from statbrief.config import Settings

COOKIE_NAME = "sb_session"
_SALT = "statbrief-session-v1"


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    """Serializer bound to the configured secret."""
    return URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt=_SALT)


def _max_age_s(settings: Settings) -> int:
    """Maximum session lifetime in seconds."""
    return settings.session_max_age_days * 86400


def _short_max_age_s(settings: Settings) -> int:
    """Maximum lifetime in seconds of a session issued without "remember me"."""
    return settings.session_short_max_age_hours * 3600


def issue_session(response: Response, user_id: str, settings: Settings, remember: bool) -> None:
    """Set the session cookie; persistent for SESSION_MAX_AGE_DAYS when `remember`."""
    token = _serializer(settings).dumps({"uid": user_id, "r": bool(remember)})
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=_max_age_s(settings) if remember else None,
        httponly=True, samesite="lax", secure=settings.cookie_secure, path="/",
    )


def read_session(cookie_value: str | None, settings: Settings) -> str | None:
    """Return the user id from a valid, unexpired cookie value, else None.

    Remember-me tokens live up to SESSION_MAX_AGE_DAYS; tokens without a truthy `r`
    flag (including any lacking it) live only SESSION_SHORT_MAX_AGE_HOURS.
    """
    if not cookie_value:
        return None
    try:
        data, signed_at = _serializer(settings).loads(
            cookie_value, max_age=_max_age_s(settings), return_timestamp=True
        )
    except BadData:  # BadSignature, SignatureExpired, BadPayload, BadTimeSignature
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("r") and time.time() - signed_at.timestamp() > _short_max_age_s(settings):
        return None
    uid = data.get("uid")
    return uid if isinstance(uid, str) else None


def clear_session(response: Response, settings: Settings) -> None:
    """Expire the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)
