"""Segmented runner (AC 41, 45; R4, R5): stream one time-bounded segment of a report run as SSE events.

`run_segment` runs the report graph from its checkpoint until it reaches an interrupt, ends, or uses up
SEGMENT_BUDGET_S (checked between streamed steps); the browser then reconnects and the next segment
continues from the checkpoint. One segment at a time per report: a session-level
`pg_try_advisory_lock(namespace, hashtext(report_id))` on a held pool connection; a second caller gets
`already_running` (R5). The graph is rebuilt per segment (`prepare_run`).

Input of a segment:
- a queued resume answer (`take_resume()`, stored by POST /resume): `Command(resume=answer)`; a bare `{}` is
  never sent (LangGraph would read it as an empty interrupt map), the route rejects empty answers;
- no checkpoint yet: the report's stored parameters start a new run (counted against RUNS_PER_HOUR);
- a checkpoint that is not finished (next nodes, or tasks whose writes are pending because the previous
  segment was cut mid-step): continue (`None` input); a pending interrupt without an answer is
  shown again.

SSE events (`SseEvent.frame()`): `started`, `node` (a node finished; sub-graph nodes are "parent.node"),
`progress` (custom events from nodes via `emit_progress`), `interrupt` (data = the interrupt value),
`continue` (budget used; reconnect), `done` (data.status), `error` (a friendly message; details are logged
by type only), `already_running`. Node timings are written to run_events through the run's handler.
"""
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Literal

from langgraph.types import Command
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from statbrief.auth.limits import KIND_REPORT_RUN, LimitExceeded, consume
from statbrief.domain.report import ReportParams
from statbrief.graph.runtime import RunResources, prepare_run
from statbrief.llm.models import LLMNotConfigured, friendly_llm_error

logger = logging.getLogger(__name__)

RUN_LOCK_NAMESPACE = 0x5B20  # first key of the two-int advisory lock (separates run locks from others)
RUN_WINDOW = timedelta(hours=1)  # RUNS_PER_HOUR window (AC 5)
GENERIC_ERROR = "The report run stopped unexpectedly. Please try again."
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


def _node_name(namespace: tuple[str, ...], node: str) -> str:
    """'parent.node' for a sub-graph node ("ingestion:<task id>" -> "ingestion"), else the node."""
    return ".".join([*(part.split(":")[0] for part in namespace), node])


def _start_input(user_id: str, report_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Initial graph input from the stored parameters; ValueError when none are stored."""
    if params is None:
        raise ValueError("no parameters")
    return {"report_id": report_id, "user_id": user_id, "params": ReportParams.model_validate(params)}


@contextmanager
def run_lock(pool: ConnectionPool, report_id: str) -> Iterator[bool]:
    """Try the report's session advisory lock on a held pool connection; yields whether it was acquired.

    Released on exit. Used by run segments and by forks (a fork never races a running segment).
    """
    with pool.connection() as conn:
        got = conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS ok",
                           (RUN_LOCK_NAMESPACE, report_id)).fetchone()["ok"]
        try:
            yield got
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (RUN_LOCK_NAMESPACE, report_id))


def run_segment(res: RunResources, user_id: str, report_id: str, *, params: dict[str, Any] | None,
                take_resume: Callable[[], Any], budget_s: float,
                clock: Callable[[], float] = time.monotonic) -> Iterator[SseEvent]:
    """Stream one segment of the report run (see the module docstring); never raises."""
    with run_lock(res.pool, report_id) as got:
        if not got:
            yield SseEvent(type="already_running", message="This report is already running in another tab.")
            return
        yield from _segment(res, user_id, report_id, params, take_resume, budget_s, clock)


def _segment(res: RunResources, user_id: str, report_id: str, params: dict[str, Any] | None,
             take_resume: Callable[[], Any], budget_s: float, clock: Callable[[], float]) -> Iterator[SseEvent]:
    """The body of `run_segment`, run while holding the report's lock."""
    try:
        run = prepare_run(res, user_id, report_id)
        snapshot = run.graph.get_state(run.config)
        answer = take_resume()
        if answer is not None and answer != {}:
            graph_input: Any = Command(resume=answer)
        elif not snapshot.values:
            graph_input = _start_input(user_id, report_id, params)
            now = res.now()
            consume(res.pool, user_id, KIND_REPORT_RUN, res.settings.runs_per_hour, now - RUN_WINDOW, now)
        elif snapshot.interrupts:
            yield SseEvent(type="interrupt", node=snapshot.next[0] if snapshot.next else None,
                           data=snapshot.interrupts[0].value)
            return
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
    yield from _stream(run, graph_input, budget_s, clock)


def _stream(run: Any, graph_input: Any, budget_s: float, clock: Callable[[], float]) -> Iterator[SseEvent]:
    """Stream the graph until an interrupt, the end, or the time budget."""
    yield SseEvent(type="started", progress=0.0)
    start = last = clock()
    stream = run.graph.stream(graph_input, run.config, stream_mode=["updates", "custom"], subgraphs=True)
    interrupted = False
    try:
        for namespace, mode, chunk in stream:
            if mode == "custom":
                yield SseEvent(type="progress", node=chunk.get("node"), message=chunk.get("message"))
            else:
                for node, _update in chunk.items():
                    if node == "__interrupt__":
                        interrupted = True
                        for item in _update:
                            yield SseEvent(type="interrupt", data=item.value)
                        continue
                    name = _node_name(namespace, node)
                    now = clock()
                    run.handler.record_node(name, latency_ms=round((now - last) * 1000))
                    last = now
                    top = not namespace
                    yield SseEvent(type="node", node=name, message=MESSAGES.get(node) if top else None,
                                   progress=PROGRESS.get(node) if top else None)
            if clock() - start > budget_s and not interrupted:
                yield SseEvent(type="continue", message="Still working: continuing in a new request.")
                return
    except LimitExceeded as exc:
        yield SseEvent(type="error", message=friendly_llm_error(exc))
        return
    except Exception as exc:  # node failure not handled inside the graph: the type only
        logger.warning("run segment failed: %s", type(exc).__name__)
        yield SseEvent(type="error", message=GENERIC_ERROR)
        return
    finally:
        stream.close()
    if interrupted:
        return
    final = run.graph.get_state(run.config)
    if not final.next and not final.tasks:
        yield SseEvent(type="done", progress=1.0, data={"status": final.values.get("status")})
