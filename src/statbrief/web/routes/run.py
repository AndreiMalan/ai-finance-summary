"""Report run API (M7b, AC 39-45): parameters, the SSE run stream, resume answers, preferences.

All routes need login and are scoped to the current user: another user's report answers 404 (AC 6).
State-changing routes use the upload API's `Guard` (custom `X-StatBrief-Client` header, D-018) until the
M8 CSRF scheme replaces it.

- `PUT  /api/reports/{id}/params`: store the run parameters (`ReportParams`).
- `GET  /api/reports/{id}/run/stream`: one run segment as `text/event-stream` (`graph.runner.SseEvent`);
  the browser reconnects after `continue` and after an answered interrupt.
- `POST /api/reports/{id}/resume`: `{"interrupt_id": "...", "payload": {...}}`. The id is the one the
  `interrupt` event carried; when it is not the pending interrupt any more (another round, another branch,
  another tab answered first) the answer is refused with 409 ("this step changed, reload"). The payload is
  validated against the pending interrupt (resolve -> `ResolvePayload`, review -> `ReviewPayload`); 409
  without a pending interrupt, 422 for an invalid or empty answer (a bare `{}` must never reach
  LangGraph). The answer is queued with its interrupt id; the next stream segment applies it only if that
  interrupt is still pending.
- `GET  /api/preferences`: the user's saved report defaults (form pre-fill only, AC 43).
- `GET  /api/reports/{id}/history`, `GET .../history/{checkpoint}/draft`, `POST .../history/{checkpoint}/fork`:
  forkable checkpoints, prior drafts and fork-from-checkpoint (AC 42); 404 when ENABLE_REPLAY is off; a fork
  takes the run lease (409 while running), counts against RUNS_PER_HOUR, is refused with 400 inside a
  fan-out step, and drops any queued answer.

Report ids are canonicalised once (`_own_report` returns the row; `str(row["id"])` is used for the thread,
the lease and every repository call), so another spelling of the same UUID can never open a second run.
"""
from typing import Annotated, Any

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from statbrief.auth.limits import KIND_REPORT_RUN, LimitExceeded, consume
from statbrief.auth.users import User
from statbrief.domain.report import ReportParams
from statbrief.graph.builder import graph_config
from statbrief.graph.history import (NotForkable, UnknownCheckpoint, fork_from, get_checkpoint_draft,
                                     list_checkpoints)
from statbrief.graph.nodes_review import ReviewPayload
from statbrief.graph.nodes_validate import ResolvePayload
from statbrief.graph.preferences import PreferencesRepo
from statbrief.graph.runner import RUN_WINDOW, run_lease, run_segment
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
STEP_CHANGED = "This step changed (another tab or round answered it). Reload the report."


class ResumeBody(BaseModel):
    """Request body: the answer to one pending interrupt, quoting that interrupt's id."""

    interrupt_id: str = Field(min_length=1)
    payload: dict[str, Any]


def default_run_resources(rt: Runtime) -> RunResources:
    """Production run resources for the app runtime (real model, HTTP, embeddings)."""
    return RunResources(pool=rt.pool, settings=rt.settings, blobs=rt.blobs)


def _resources(request: Request, rt: Runtime) -> RunResources:
    """Run resources from the app's factory (tests install fakes there)."""
    factory = getattr(request.app.state, "run_resources_factory", default_run_resources)
    return factory(rt)


def _own_report(rt: Runtime, user: User, report_id: str) -> tuple[dict, str]:
    """The user's report row and its canonical id (lower-case hyphenated UUID), or 404 (also for non-UUIDs)."""
    row = ReportRepo(rt.pool).get(user.id, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return row, str(row["id"])


@router.put("/reports/{report_id}/params", dependencies=[Guard])
def set_params(report_id: str, params: ReportParams, user: UserDep, rt: RtDep) -> dict:
    """Store the report's run parameters (validated; links one-to-one)."""
    _, rid = _own_report(rt, user, report_id)
    ReportRepo(rt.pool).set_params(user.id, rid, params.model_dump(mode="json"))
    return {"ok": True}


async def _frames(events: Any) -> Any:
    """SSE frames of a run segment; the segment is closed explicitly (lease released) on disconnect."""
    try:
        async for event in iterate_in_threadpool(events):
            yield event.frame()
    finally:
        try:
            await run_in_threadpool(events.close)
        except ValueError:  # still executing in its worker thread: its lease expires by itself
            pass


@router.get("/reports/{report_id}/run/stream")
def run_stream(report_id: str, request: Request, user: UserDep, rt: RtDep) -> StreamingResponse:
    """One run segment of the user's report as server-sent events."""
    row, rid = _own_report(rt, user, report_id)
    res = _resources(request, rt)
    repo = ReportRepo(rt.pool)
    events = run_segment(res, user.id, rid, params=row["params"],
                         take_resume=lambda: repo.take_pending_resume(user.id, rid),
                         budget_s=res.settings.segment_budget_s)
    return StreamingResponse(_frames(events), media_type="text/event-stream", headers=SSE_HEADERS)


def _errors(exc: ValidationError) -> list[str]:
    """Validation problems as 'location: reason' lines (input values are not echoed)."""
    return [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]


@router.post("/reports/{report_id}/resume", dependencies=[Guard], response_model=None)
def resume(report_id: str, body: ResumeBody, request: Request, user: UserDep, rt: RtDep) -> JSONResponse:
    """Validate the answer against the pending interrupt it quotes and queue it for the next segment."""
    _, rid = _own_report(rt, user, report_id)
    if not body.payload:
        raise HTTPException(status_code=422, detail="the answer is empty")
    res = _resources(request, rt)
    snapshot = state_graph(res).get_state(graph_config(rid, res.settings))
    if not snapshot.interrupts:
        raise HTTPException(status_code=409, detail="the report is not waiting for an answer")
    pending = snapshot.interrupts[0]
    if body.interrupt_id != pending.id:
        raise HTTPException(status_code=409, detail=STEP_CHANGED)
    model = PAYLOAD_MODELS.get(pending.value.get("kind") if isinstance(pending.value, dict) else None)
    if model is None:
        raise HTTPException(status_code=409, detail="the report is not waiting for an answer")
    try:
        model.model_validate(body.payload)
    except ValidationError as exc:
        return JSONResponse({"detail": "invalid answer", "errors": _errors(exc)}, status_code=422)
    ReportRepo(rt.pool).set_pending_resume(user.id, rid, {"interrupt_id": pending.id, "payload": body.payload})
    return JSONResponse({"queued": pending.value["kind"], "stream": f"/api/reports/{rid}/run/stream"},
                        status_code=202)


@router.get("/preferences")
def preferences(user: UserDep, rt: RtDep) -> dict:
    """The user's saved report defaults (empty when none)."""
    prefs = PreferencesRepo(make_store(rt.pool)).get(user.id)
    return prefs.model_dump(mode="json") if prefs else {}


def _replay_graph(request: Request, rt: Runtime, user: User, report_id: str) -> tuple[Any, RunResources, str]:
    """The model-free graph, run resources and canonical id of the user's report; 404 when ENABLE_REPLAY is
    off or the report is foreign."""
    if not rt.settings.enable_replay:
        raise HTTPException(status_code=404, detail="history is disabled")
    _, rid = _own_report(rt, user, report_id)
    res = _resources(request, rt)
    return state_graph(res), res, rid


@router.get("/reports/{report_id}/history")
def history(report_id: str, request: Request, user: UserDep, rt: RtDep) -> list[dict]:
    """The report's forkable checkpoints, newest first (AC 42)."""
    graph, res, rid = _replay_graph(request, rt, user, report_id)
    return [c.model_dump(mode="json") for c in list_checkpoints(graph, rid, res.settings)]


@router.get("/reports/{report_id}/history/{checkpoint_id}/draft")
def history_draft(report_id: str, checkpoint_id: str, request: Request, user: UserDep, rt: RtDep) -> dict:
    """The draft stored in one checkpoint ({} when it has none); 404 for an unknown checkpoint."""
    graph, res, rid = _replay_graph(request, rt, user, report_id)
    try:
        draft = get_checkpoint_draft(graph, rid, checkpoint_id, res.settings)
    except UnknownCheckpoint:
        raise HTTPException(status_code=404, detail="checkpoint not found") from None
    return draft.model_dump(mode="json") if draft is not None else {}


@router.post("/reports/{report_id}/history/{checkpoint_id}/fork", dependencies=[Guard])
def fork(report_id: str, checkpoint_id: str, request: Request, user: UserDep, rt: RtDep) -> dict:
    """Continue the report from an earlier checkpoint; the next stream segment runs from there.

    409 while a segment runs (the fork takes the run lease); 400 inside a fan-out step; 429 when
    RUNS_PER_HOUR is used up (a fork re-runs model steps); a queued answer for the old branch is dropped.
    """
    graph, res, rid = _replay_graph(request, rt, user, report_id)
    with run_lease(res, user.id, rid) as token:
        if token is None:
            raise HTTPException(status_code=409, detail="the report is running")
        try:
            new_id = fork_from(graph, rid, checkpoint_id, res.settings)
        except UnknownCheckpoint:
            raise HTTPException(status_code=404, detail="checkpoint not found") from None
        except NotForkable:
            raise HTTPException(status_code=400, detail="this step cannot start a new branch") from None
        now = res.now()
        try:
            consume(rt.pool, user.id, KIND_REPORT_RUN, res.settings.runs_per_hour, now - RUN_WINDOW, now)
        except LimitExceeded as exc:
            raise HTTPException(status_code=429, detail=f"{exc.limit} report runs per hour reached") from None
        ReportRepo(rt.pool).take_pending_resume(user.id, rid)
    return {"checkpoint_id": new_id, "stream": f"/api/reports/{rid}/run/stream"}
