"""Segmented runner (AC 41, 45; R4, R5): stream one time-bounded segment of a report run as SSE events.

`run_segment` runs the report graph from its checkpoint until it reaches an interrupt, ends, or would pass
SEGMENT_BUDGET_S; the browser then reconnects and the next segment continues from the checkpoint. The graph
is rebuilt per segment (`prepare_run`).

One segment (or fork) at a time per report: a **run lease** on the report row (`run_lease`), taken with one
atomic UPDATE when free or expired, renewed after every streamed step, released by token in `finally`
(D-039). It works through Neon's transaction-mode pooler (no session state) and expires by itself after
SEGMENT_BUDGET_S + RUN_LEASE_MARGIN_S when an instance is frozen or killed. A second caller gets
`already_running` (R5).

Time budget (R4): checked right after each saved top-level checkpoint newer than the segment's starting
step (`stream_mode="checkpoints"`; the re-emitted starting checkpoint does not count): no
further step starts when elapsed + MAX_NODE_S would pass SEGMENT_BUDGET_S (event `continue`). Stopping only
there means every segment persists at least one step (stopping right after an `updates` chunk would drop
the step being saved, and a segment that always stopped there would never progress). MAX_NODE_S < SEGMENT_BUDGET_S and SEGMENT_BUDGET_S + SEGMENT_SAFETY_MARGIN_S < maxDuration are
validated by Settings. A node that still overruns (the function is killed) loses only its own step: the
checkpoint of the previous step is intact and the next segment re-runs it.

Input of a segment:
- a queued answer `{"interrupt_id", "payload"}` (`take_resume()`, stored by POST /resume): resumed with
  `Command(resume=payload)` only if that interrupt is still the pending one; a stale answer (another round,
  another branch) is dropped and the current interrupt shown again. A bare `{}` is never sent;
- no checkpoint yet: the report's stored parameters start a new run (counted against RUNS_PER_HOUR);
- a checkpoint that is not finished (next nodes, or tasks whose writes are pending because the previous
  segment was cut mid-step): continue (`None` input); a pending interrupt without an answer is shown again.
An answer taken just before a segment that then fails before the graph's first write is lost: the
interrupt is shown again and the user answers again (never silently applied elsewhere).

SSE events (`SseEvent.frame()`): `started`, `node` (a node finished; sub-graph nodes are "parent.node"),
`progress` (custom events from nodes via `emit_progress`), `interrupt` (data = the interrupt value plus
`interrupt_id`), `continue` (budget used; reconnect), `done` (data.status), `error` (a friendly message;
the exception type is logged and recorded in run_events as a `segment_error` node event),
`already_running`. Node timings are written to run_events through the run's handler.
"""
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Literal

from langgraph.types import Command
from pydantic import BaseModel

from statbrief.auth.limits import KIND_REPORT_RUN, LimitExceeded, consume
from statbrief.domain.report import ReportParams
from statbrief.graph.runtime import PreparedRun, RunResources, prepare_run
from statbrief.llm.models import LLMNotConfigured, friendly_llm_error
from statbrief.storage.repos import ReportRepo

logger = logging.getLogger(__name__)

RUN_WINDOW = timedelta(hours=1)  # RUNS_PER_HOUR window (AC 5)
GENERIC_ERROR = "The report run stopped unexpectedly. Please try again."
LEASE_LOST = "Another request took over this report's run. Reload the page."
# Share of the workflow done when a top-level node finishes (for the progress bar).
PROGRESS = {"ingestion": 0.15, "validate_normalize": 0.25, "resolve_gate": 0.3, "analyze_dispatch": 0.35,
            "analyze_one": 0.4, "analyze_all": 0.4, "aggregate_metrics": 0.45, "flag_significant": 0.5,
            "retrieval": 0.6, "drafting": 0.85, "human_review": 0.9, "export": 1.0, "fail_safe": 1.0}
MESSAGES = {"ingestion": "Sources collected", "validate_normalize": "Data validated", "resolve_gate": "Answer applied",
            "analyze_dispatch": "Analysis started", "analyze_one": "Metrics computed", "analyze_all": "Metrics computed",
            "aggregate_metrics": "Metrics combined", "flag_significant": "Significant changes flagged",
            "retrieval": "Reference passages retrieved", "drafting": "Draft written and verified",
            "human_review": "Review recorded", "export": "Approved: export pending", "fail_safe": "Stopped safely"}

EventType = Literal["started", "node", "progress", "interrupt", "continue", "done", "error", "already_running"]


class SseEvent(BaseModel):
    """One server-sent event of a run segment."""

    type: EventType
    node: str | None = None
    message: str | None = None
    progress: float | None = None
    data: dict[str, Any] | None = None

    def frame(self) -> str:
        """The event as an SSE frame (`event:` type, `data:` JSON of the fields)."""
        return f"event: {self.type}\ndata: {json.dumps(self.model_dump(exclude_none=True))}\n\n"


def lease_seconds(res: RunResources) -> float:
    """How long a run lease lasts: SEGMENT_BUDGET_S + RUN_LEASE_MARGIN_S."""
    return res.settings.segment_budget_s + res.settings.run_lease_margin_s


@contextmanager
def run_lease(res: RunResources, user_id: str, report_id: str) -> Iterator[str | None]:
    """Hold the report's run lease (yields its token) or yield None when another holder has it.

    Released by token on exit, including generator close; a crashed holder's lease simply expires.
    """
    repo = ReportRepo(res.pool)
    token = str(uuid.uuid4())
    if not repo.acquire_lease(user_id, report_id, token, lease_seconds(res)):
        yield None
        return
    try:
        yield token
    finally:
        repo.release_lease(report_id, token)


def _node_name(namespace: tuple[str, ...], node: str) -> str:
    """'parent.node' for a sub-graph node ("ingestion:<task id>" -> "ingestion"), else the node."""
    return ".".join([*(part.split(":")[0] for part in namespace), node])


def _start_input(user_id: str, report_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Initial graph input from the stored parameters; ValueError when none are stored."""
    if params is None:
        raise ValueError("no parameters")
    return {"report_id": report_id, "user_id": user_id, "params": ReportParams.model_validate(params)}


def interrupt_event(value: Any, interrupt_id: str, node: str | None = None) -> SseEvent:
    """The `interrupt` event: the JSON interrupt value plus the id an answer must quote."""
    return SseEvent(type="interrupt", node=node, data={**(value if isinstance(value, dict) else {"value": value}),
                                                       "interrupt_id": interrupt_id})


def run_segment(res: RunResources, user_id: str, report_id: str, *, params: dict[str, Any] | None,
                take_resume: Callable[[], Any], budget_s: float,
                clock: Callable[[], float] = time.monotonic) -> Iterator[SseEvent]:
    """Stream one segment of the report run (see the module docstring); never raises an Exception."""
    with run_lease(res, user_id, report_id) as token:
        if token is None:
            yield SseEvent(type="already_running", message="This report is already running in another tab.")
            return
        yield from _segment(res, user_id, report_id, token, params, take_resume, budget_s, clock)


def _segment(res: RunResources, user_id: str, report_id: str, token: str, params: dict[str, Any] | None,
             take_resume: Callable[[], Any], budget_s: float, clock: Callable[[], float]) -> Iterator[SseEvent]:
    """The body of `run_segment`, run while holding the report's lease."""
    try:
        run = prepare_run(res, user_id, report_id)
        snapshot = run.graph.get_state(run.config)
        answer = take_resume()
        pending = snapshot.interrupts[0] if snapshot.interrupts else None
        if (pending is not None and isinstance(answer, dict) and answer.get("interrupt_id") == pending.id
                and answer.get("payload")):
            graph_input: Any = Command(resume=answer["payload"])
        elif pending is not None:  # no answer, or a stale one (another round / branch): ask again
            yield interrupt_event(pending.value, pending.id, snapshot.next[0] if snapshot.next else None)
            return
        elif not snapshot.values:
            graph_input = _start_input(user_id, report_id, params)
            now = res.now()
            consume(res.pool, user_id, KIND_REPORT_RUN, res.settings.runs_per_hour, now - RUN_WINDOW, now)
        elif not snapshot.next and not snapshot.tasks:  # finished (pending writes of a cut segment count as work)
            yield SseEvent(type="done", progress=1.0, data={"status": snapshot.values.get("status")})
            return
        else:
            graph_input = None
    except LimitExceeded as exc:
        yield SseEvent(type="error", message=(f"You have started {exc.limit} report runs in the last hour; "
                                              "please try again later."))
        return
    except LLMNotConfigured as exc:
        yield SseEvent(type="error", message=friendly_llm_error(exc))
        return
    except ValueError:
        yield SseEvent(type="error", message="Choose the report's sources and parameters before running it.")
        return
    except Exception as exc:  # database / setup failure: the type only
        logger.warning("run segment setup failed: %s", type(exc).__name__)
        yield SseEvent(type="error", message=GENERIC_ERROR)
        return
    start_step = (snapshot.metadata or {}).get("step")
    yield from _stream(res, run, report_id, token, graph_input, budget_s, clock,
                       -2 if start_step is None else start_step)


def _record_failure(run: PreparedRun, exc: BaseException) -> None:
    """Leave a timeline trace of a failed segment in run_events (exception type only)."""
    run.handler.record_node("segment_error", detail={"error_type": type(exc).__name__})


def _stream(res: RunResources, run: PreparedRun, report_id: str, token: str, graph_input: Any, budget_s: float,
            clock: Callable[[], float], start_step: int) -> Iterator[SseEvent]:
    """Stream the graph until an interrupt, the end, or the point where another step could pass the budget."""
    yield SseEvent(type="started", progress=0.0)
    repo, reserve = ReportRepo(res.pool), res.settings.max_node_s or 0.0
    start = last = clock()
    stream = run.graph.stream(graph_input, run.config, stream_mode=["updates", "custom", "checkpoints"],
                              subgraphs=True)
    interrupted = False
    try:
        for namespace, mode, chunk in stream:
            if mode == "custom":
                yield SseEvent(type="progress", node=chunk.get("node"), message=chunk.get("message"))
                continue
            if mode == "checkpoints":
                # Budget and lease are checked only right after a top-level checkpoint was SAVED: closing the
                # stream at any other point would drop the step being finished, and a segment that always
                # stops there would never progress.
                step = (chunk.get("metadata") or {}).get("step")
                if namespace or interrupted or step is None or step <= start_step:  # not new progress
                    continue
                if not repo.renew_lease(report_id, token, lease_seconds(res)):
                    yield SseEvent(type="error", message=LEASE_LOST)
                    return
                if clock() - start + reserve > budget_s:  # another long step might not fit
                    yield SseEvent(type="continue", message="Still working: continuing in a new request.")
                    return
                continue
            for node, update in chunk.items():
                if node == "__interrupt__":
                    interrupted = True
                    for item in update:
                        yield interrupt_event(item.value, item.id)
                    continue
                if node.startswith("__"):  # stream metadata (e.g. replayed pending writes), not a node
                    continue
                name = _node_name(namespace, node)
                now = clock()
                run.handler.record_node(name, latency_ms=round((now - last) * 1000))
                last = now
                top = not namespace
                yield SseEvent(type="node", node=name, message=MESSAGES.get(node) if top else None,
                               progress=PROGRESS.get(node) if top else None)
    except LimitExceeded as exc:
        _record_failure(run, exc)
        yield SseEvent(type="error", message=friendly_llm_error(exc))
        return
    except Exception as exc:  # node failure not handled inside the graph (e.g. a database outage): type only
        logger.warning("run segment failed: %s", type(exc).__name__)
        _record_failure(run, exc)
        yield SseEvent(type="error", message=GENERIC_ERROR)
        return
    finally:
        stream.close()
    if interrupted:
        return
    final = run.graph.get_state(run.config)
    if not final.next and not final.tasks:
        yield SseEvent(type="done", progress=1.0, data={"status": final.values.get("status")})
