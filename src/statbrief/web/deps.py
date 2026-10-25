"""FastAPI dependencies: runtime access, current user, client IP, templates."""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from statbrief.auth.sessions import COOKIE_NAME, read_session
from statbrief.auth.users import User, get_user
from statbrief.web.runtime import Runtime

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequired(Exception):
    """Raised by `current_user` when no valid session exists."""


def get_runtime(request: Request) -> Runtime:
    """Return the app's lazily-initialised runtime."""
    return request.app.state.runtime_holder.get()


def optional_user(request: Request) -> User | None:
    """Return the logged-in user, or None."""
    rt = get_runtime(request)
    uid = read_session(request.cookies.get(COOKIE_NAME), rt.settings)
    return get_user(rt.pool, uid) if uid else None


def current_user(request: Request) -> User:
    """Return the logged-in user or raise LoginRequired."""
    user = optional_user(request)
    if user is None:
        raise LoginRequired()
    return user


def client_ip(request: Request) -> str:
    """Best-effort client IP (Vercel sets x-forwarded-for; first hop is the client)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
