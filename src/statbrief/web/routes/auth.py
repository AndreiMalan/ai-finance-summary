"""Login, signup and logout pages."""
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from statbrief.auth.limits import login_allowed, record_login
from statbrief.auth.sessions import clear_session, issue_session
from statbrief.auth.users import EmailTaken, InvalidEmail, WeakPassword, authenticate, create_user
from statbrief.web.deps import client_ip, get_runtime, optional_user, templates
from statbrief.web.runtime import Runtime

router = APIRouter()
_HOME = "/reports"


def _safe_next(next_url: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return _HOME


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None):
    """Render the login form (or skip it when already logged in)."""
    if optional_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    rt = get_runtime(request)
    return templates.TemplateResponse(request, "login.html",
                                      {"next": next or "", "signups_enabled": rt.settings.signups_enabled})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, rt: Annotated[Runtime, Depends(get_runtime)],
          email: Annotated[str, Form()], password: Annotated[str, Form()],
          remember: Annotated[str | None, Form()] = None, next: Annotated[str, Form()] = ""):
    """Authenticate with throttling; set the session cookie on success."""
    s, ip, now = rt.settings, client_ip(request), datetime.now(timezone.utc)
    ctx = {"next": next, "email": email, "signups_enabled": s.signups_enabled}
    if not login_allowed(rt.pool, email, ip, now, s.login_max_failures, timedelta(minutes=s.login_window_minutes)):
        ctx["error"] = f"Too many failed attempts. Try again in {s.login_window_minutes} minutes."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=429)
    user = authenticate(rt.pool, email, password, s.password_max_length)
    record_login(rt.pool, email, ip, user is not None, now)
    if user is None:
        ctx["error"] = "Incorrect email or password."
        return templates.TemplateResponse(request, "login.html", ctx, status_code=400)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    issue_session(resp, user.id, s, remember=remember == "on")
    return resp


def _require_signups(rt: Runtime) -> None:
    """404 when self-registration is switched off."""
    if not rt.settings.signups_enabled:
        raise HTTPException(status_code=404)


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, rt: Annotated[Runtime, Depends(get_runtime)]):
    """Render the signup form."""
    _require_signups(rt)
    return templates.TemplateResponse(request, "signup.html", {"min_length": rt.settings.password_min_length})


@router.post("/signup", response_class=HTMLResponse)
def signup(request: Request, rt: Annotated[Runtime, Depends(get_runtime)],
           email: Annotated[str, Form()], password: Annotated[str, Form()]):
    """Create an account and log the user in."""
    _require_signups(rt)
    try:
        user = create_user(rt.pool, email, password, rt.settings.password_min_length,
                           rt.settings.password_max_length)
    except (InvalidEmail, WeakPassword, EmailTaken) as exc:
        ctx = {"error": str(exc), "email": email, "min_length": rt.settings.password_min_length}
        return templates.TemplateResponse(request, "signup.html", ctx, status_code=400)
    resp = RedirectResponse(_HOME, status_code=303)
    issue_session(resp, user.id, rt.settings, remember=True)
    return resp


@router.post("/logout")
def logout(rt: Annotated[Runtime, Depends(get_runtime)]):
    """Clear the session and return to the login page."""
    resp = RedirectResponse("/login", status_code=303)
    clear_session(resp, rt.settings)
    return resp
