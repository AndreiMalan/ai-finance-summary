"""Top-level pages. `/reports` is a placeholder until milestone 8."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from statbrief.auth.limits import KIND_REPORT_RUN, hour_ago, remaining
from statbrief.auth.users import User
from statbrief.web.deps import current_user, get_runtime, optional_user, templates
from statbrief.web.runtime import Runtime

router = APIRouter()


@router.get("/")
def home(request: Request):
    """Send visitors to their reports or to the login page."""
    return RedirectResponse("/reports" if optional_user(request) else "/login", status_code=303)


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, user: Annotated[User, Depends(current_user)],
            rt: Annotated[Runtime, Depends(get_runtime)]):
    """List the user's reports (empty state for now) with remaining run quota."""
    left = remaining(rt.pool, user.id, KIND_REPORT_RUN, rt.settings.runs_per_hour,
                     hour_ago(datetime.now(timezone.utc)))
    return templates.TemplateResponse(request, "reports.html",
                                      {"user": user, "runs_left": left, "runs_per_hour": rt.settings.runs_per_hour})
