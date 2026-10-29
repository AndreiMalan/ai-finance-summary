"""Retention cron, account deletion and report deletion routes (M9, D-009, AC 50).

- `GET /cron/cleanup`: Vercel Cron calls it daily with `Authorization: Bearer <CRON_SECRET>`; any other
  request (no header, wrong secret, or CRON_SECRET unset) gets 401 and nothing runs.
- `POST /account/delete` (form: `password`, `confirm` = "DELETE"): deletes the logged-in user's account
  and everything it owns (Blob objects first). The password is checked like a login (same throttle). The
  shared demo account cannot be deleted. The settings page that calls it is M8. Known trade-off: a wrong
  password counts as a failed login for the owner's email, so a holder of the session can lock the owner's
  login for LOGIN_WINDOW_MINUTES (acceptable for the demo; throttle by user id if that changes).
- `DELETE /api/reports/{report_id}`: deletes one report immediately (Blob objects first).
CSRF: the state-changing routes require the `X-StatBrief-Client: 1` header (`Guard`, D-018), like the
upload API. M8 introduces the form CSRF token and retrofits it here.
"""
import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from statbrief.auth.limits import login_allowed, record_login
from statbrief.auth.sessions import clear_session
from statbrief.auth.users import User, authenticate
from statbrief.config import Settings
from statbrief.maintenance.deletion import DeletionIncomplete, delete_account, delete_report
from statbrief.maintenance.retention import cleanup_expired
from statbrief.web.deps import client_ip, current_user, get_runtime
from statbrief.web.routes.uploads import Guard
from statbrief.web.runtime import Runtime

logger = logging.getLogger(__name__)
router = APIRouter()

UserDep = Annotated[User, Depends(current_user)]
RtDep = Annotated[Runtime, Depends(get_runtime)]
ACCOUNT_DELETE_CONFIRMATION = "DELETE"  # typed by the user to confirm (protocol, not a tunable)
_BEARER = "Bearer "
_AFTER_ACCOUNT_DELETE = "/signup"
_STORAGE_RETRY = "Some stored files could not be deleted; nothing was removed. Try again shortly."


def _cron_authorized(request: Request, settings: Settings) -> bool:
    """True only for `Authorization: Bearer <CRON_SECRET>` with a non-empty configured secret."""
    secret = settings.cron_secret.get_secret_value()
    header = request.headers.get("authorization", "")
    if not secret or not header.startswith(_BEARER):
        return False
    return hmac.compare_digest(header[len(_BEARER):].encode(), secret.encode())


@router.get("/cron/cleanup")
def cron_cleanup(request: Request) -> JSONResponse:
    """Run the retention rules once; 401 without the cron secret, 500 when a rule failed (counts only).

    The secret is checked against the settings before the runtime (database) is opened.
    """
    if not _cron_authorized(request, request.app.state.runtime_holder.settings):
        raise HTTPException(status_code=401, detail="unauthorized")
    rt = get_runtime(request)
    result = cleanup_expired(rt.pool, rt.blobs, rt.settings, datetime.now(timezone.utc))
    return JSONResponse(result.as_dict(), status_code=500 if result.failed_rules else 200)


@router.post("/account/delete", dependencies=[Guard])
def delete_my_account(request: Request, user: UserDep, rt: RtDep, password: Annotated[str, Form()],
                      confirm: Annotated[str, Form()]) -> Response:
    """Delete the current account after the confirmation word and the password; clears the session."""
    s = rt.settings
    if user.is_demo:
        raise HTTPException(status_code=403, detail="The demo account cannot be deleted.")
    if confirm.strip() != ACCOUNT_DELETE_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"Type {ACCOUNT_DELETE_CONFIRMATION} to confirm.")
    ip, now = client_ip(request), datetime.now(timezone.utc)
    if not login_allowed(rt.pool, user.email, ip, now, s.login_max_failures,
                         timedelta(minutes=s.login_window_minutes)):
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in "
                                                    f"{s.login_window_minutes} minutes.")
    checked = authenticate(rt.pool, user.email, password, s.password_max_length)
    if checked is None or checked.id != user.id:
        record_login(rt.pool, user.email, ip, False, now)
        raise HTTPException(status_code=403, detail="Incorrect password.")
    try:
        delete_account(rt.pool, rt.blobs, user.id)
    except DeletionIncomplete:
        return JSONResponse({"code": "storage_unavailable", "message": _STORAGE_RETRY}, status_code=503)
    resp = JSONResponse({"deleted": True}, headers={"HX-Redirect": _AFTER_ACCOUNT_DELETE})
    clear_session(resp, s)
    return resp


@router.delete("/api/reports/{report_id}", status_code=204, dependencies=[Guard])
def delete_my_report(report_id: str, user: UserDep, rt: RtDep) -> Response:
    """Delete one of the user's reports with its files, exports and checkpoints (404 otherwise)."""
    try:
        uuid.UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="report not found") from None
    try:
        found = delete_report(rt.pool, rt.blobs, user.id, report_id)
    except DeletionIncomplete:
        return JSONResponse({"code": "storage_unavailable", "message": _STORAGE_RETRY}, status_code=503)
    if not found:
        raise HTTPException(status_code=404, detail="report not found")
    return Response(status_code=204)
