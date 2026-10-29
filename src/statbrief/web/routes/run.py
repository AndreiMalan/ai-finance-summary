"""Report run API (M7b, AC 39-45): parameters, the SSE run stream, resume answers, preferences.

All routes need login and are scoped to the current user: another user's report answers 404 (AC 6).
State-changing routes use the upload API's `Guard` (custom `X-StatBrief-Client` header, D-018) until the
M8 CSRF scheme replaces it.

- `PUT  /api/reports/{id}/params`: store the run parameters (`ReportParams`).
- `GET  /api/reports/{id}/run/stream`: one run segment as `text/event-stream` (`graph.runner.SseEvent`);
  the browser reconnects after `continue` and after an answered interrupt.
- `POST /api/reports/{id}/resume`: `{"payload": {...}}`, validated against the pending interrupt (resolve ->
  `ResolvePayload`, review -> `ReviewPayload`); 409 without a pending interrupt, 422 for an invalid or
  empty answer (a bare `{}` must never reach LangGraph). The answer is queued; the next stream segment
  resumes with it.
- `GET  /api/preferences`: the user's saved report defaults (form pre-fill only, AC 43).
"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from statbrief.auth.users import User
from statbrief.domain.report import ReportParams
from statbrief.graph.builder import graph_config
from statbrief.graph.nodes_review import ReviewPayload
from statbrief.graph.nodes_validate import ResolvePayload
from statbrief.graph.preferences import PreferencesRepo
from statbrief.graph.runner import run_segment
from statbrief.graph.runtime import RunResources, make_store, state_graph
from statbrief.storage.repos import ReportRepo
from statbrief.web.deps import current_user, get_runtime
from statbrief.web.routes.uploads import Guard
from statbrief.web.runtime import Runtime

router = APIRouter(prefix="/api")
UserDep = Annotated[User, Depends(current_user)]
RtDep = Annotated[Runtime, Depends(get_runtime)]
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {"resolve": ResolvePayload, "review": ReviewPayload}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ResumeBody(BaseModel):
    """Request body: the user's answer to the pending interrupt."""

    payload: dict[str, Any]


def default_run_resources(rt: Runtime) -> RunResources:
    """Production run resources for the app runtime (real model, HTTP, embeddings)."""
    return RunResources(pool=rt.pool, settings=rt.settings, blobs=rt.blobs)


def _resources(request: Request, rt: Runtime) -> RunResources:
    """Run resources from the app's factory (tests install fakes there)."""
    factory = getattr(request.app.state, "run_resources_factory", default_run_resources)
    return factory(rt)


def _own_report(rt: Runtime, user: User, report_id: str) -> dict:
    """The user's report row, or 404."""
    row = ReportRepo(rt.pool).get(user.id, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return row


@router.put("/reports/{report_id}/params", dependencies=[Guard])
def set_params(report_id: str, params: ReportParams, user: UserDep, rt: RtDep) -> dict:
    """Store the report's run parameters (validated; links one-to-one)."""
    _own_report(rt, user, report_id)
    ReportRepo(rt.pool).set_params(user.id, report_id, params.model_dump(mode="json"))
    return {"ok": True}


@router.get("/reports/{report_id}/run/stream")
def run_stream(report_id: str, request: Request, user: UserDep, rt: RtDep) -> StreamingResponse:
    """One run segment of the user's report as server-sent events."""
    row = _own_report(rt, user, report_id)
    res = _resources(request, rt)
    repo = ReportRepo(rt.pool)
    events = run_segment(res, user.id, report_id, params=row["params"],
                         take_resume=lambda: repo.take_pending_resume(user.id, report_id),
                         budget_s=res.settings.segment_budget_s)
    return StreamingResponse((e.frame() for e in events), media_type="text/event-stream", headers=SSE_HEADERS)


def _errors(exc: ValidationError) -> list[str]:
    """Validation problems as 'location: reason' lines (input values are not echoed)."""
    return [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]


@router.post("/reports/{report_id}/resume", dependencies=[Guard], response_model=None)
def resume(report_id: str, body: ResumeBody, request: Request, user: UserDep, rt: RtDep) -> JSONResponse:
    """Validate the answer against the pending interrupt and queue it for the next stream segment."""
    _own_report(rt, user, report_id)
    if not body.payload:
        raise HTTPException(status_code=422, detail="the answer is empty")
    res = _resources(request, rt)
    snapshot = state_graph(res).get_state(graph_config(report_id, res.settings))
    if not snapshot.interrupts:
        raise HTTPException(status_code=409, detail="the report is not waiting for an answer")
    kind = snapshot.interrupts[0].value.get("kind")
    try:
        PAYLOAD_MODELS[kind].model_validate(body.payload)
    except ValidationError as exc:
        return JSONResponse({"detail": "invalid answer", "errors": _errors(exc)}, status_code=422)
    ReportRepo(rt.pool).set_pending_resume(user.id, report_id, body.payload)
    return JSONResponse({"queued": kind, "stream": f"/api/reports/{report_id}/run/stream"}, status_code=202)


@router.get("/preferences")
def preferences(user: UserDep, rt: RtDep) -> dict:
    """The user's saved report defaults (empty when none)."""
    prefs = PreferencesRepo(make_store(rt.pool)).get(user.id)
    return prefs.model_dump(mode="json") if prefs else {}
