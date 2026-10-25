"""Signed, HttpOnly session cookies (itsdangerous); the cookie stores only the user id."""
from itsdangerous import BadSignature, URLSafeTimedSerializer
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


def issue_session(response: Response, user_id: str, settings: Settings, remember: bool) -> None:
    """Set the session cookie; persistent for SESSION_MAX_AGE_DAYS when `remember`."""
    token = _serializer(settings).dumps({"uid": user_id})
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=_max_age_s(settings) if remember else None,
        httponly=True, samesite="lax", secure=settings.cookie_secure, path="/",
    )


def read_session(cookie_value: str | None, settings: Settings) -> str | None:
    """Return the user id from a valid, unexpired cookie value, else None."""
    if not cookie_value:
        return None
    try:
        data = _serializer(settings).loads(cookie_value, max_age=_max_age_s(settings))
    except BadSignature:  # also covers SignatureExpired
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, str) else None


def clear_session(response: Response, settings: Settings) -> None:
    """Expire the session cookie."""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)
