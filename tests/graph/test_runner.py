"""Segmented runner (M7b Task 8): SSE event order, per-report lock, time-budget segments, queued resumes."""
import json

from statbrief.domain.report import ReportParams
from statbrief.graph.runner import RUN_LOCK_NAMESPACE, SseEvent, run_segment
from statbrief.storage.repos import ReportRepo, RunEventRepo
from tests.graph.helpers import draft_for, long_csv, report_call, unemp_rows
from tests.graph.runkit import Script, pp_metric, resources

CRYPTIC = ("c1", "c2", "c3", "c4", "c5")
PARAMS = ReportParams(mode="upload", indicators=["IRS_UNEMP"], regions=["RO"]).model_dump(mode="json")


def _segment(world, script, *, resume=None, budget_s=240.0, **settings):
    """All events of one segment."""
    queue = [resume]
    return list(run_segment(resources(world, script, **settings), world.user_id, world.report_id, params=PARAMS,
                            take_resume=lambda: queue.pop() if queue else None, budget_s=budget_s))


def _types(events):
    """Event types in order."""
    return [e.type for e in events]


def test_event_order_until_the_resolve_interrupt(world):
    """started, node events (top level with progress), then the interrupt; node timings in run_events."""
    world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    events = _segment(world, Script())
    types = _types(events)
    assert types[0] == "started" and types[-1] == "interrupt" and "done" not in types
    nodes = [e.node for e in events if e.type == "node"]
    assert nodes.index("ingestion.select_sources") < nodes.index("ingestion") < nodes.index("validate_normalize")
    assert next(e for e in events if e.type == "node" and e.node == "validate_normalize").progress == 0.25
    assert any(e.type == "progress" and e.node == "parse_file" for e in events)
    assert events[-1].data["kind"] == "resolve"
    logged = [e["name"] for e in RunEventRepo(world.pool).list_for_report(world.user_id, world.report_id)
              if e["kind"] == "node"]
    assert "validate_normalize" in logged


def test_pending_interrupt_is_shown_again_and_a_queued_answer_resumes(world):
    """Reconnecting without an answer re-sends the interrupt; with one, the run continues to review."""
    ref = world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    script = Script()
    _segment(world, script)
    again = _segment(world, script)
    assert _types(again) == ["interrupt"] and again[0].data["kind"] == "resolve"
    script.replies.append(report_call(draft_for(pp_metric(world, "IRS_UNEMP"), "0.6")))
    events = _segment(world, script, resume={"confirm_mappings": [ref.file_id]})
    assert events[-1].type == "interrupt" and events[-1].data["kind"] == "review"
    done = _segment(world, script, resume={"action": "approve"})
    assert done[-1].type == "done" and done[-1].data == {"status": "approved"}


def test_second_caller_gets_already_running(world):
    """R5: while another session holds the report's run lock, a segment only reports already_running."""
    world.add_file("irs.csv", long_csv(unemp_rows()))
    with world.pool.connection() as other:
        other.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", (RUN_LOCK_NAMESPACE, world.report_id))
        try:
            assert _types(_segment(world, Script())) == ["already_running"]
        finally:
            other.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (RUN_LOCK_NAMESPACE, world.report_id))
    assert _types(_segment(world, Script()))[0] == "started"  # the lock was released


def test_budget_splits_the_run_into_resumable_segments(world):
    """AC 45: a zero budget stops after the first step; reconnecting continues from the checkpoint each time."""
    world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    script = Script()
    segments = []
    for _ in range(20):
        events = _segment(world, script, budget_s=0.0)
        segments.append(events)
        if events[-1].type != "continue":
            break
    assert len(segments) > 2 and all(s[-1].type == "continue" for s in segments[:-1])
    assert segments[-1][-1].type == "interrupt" and segments[-1][-1].data["kind"] == "resolve"


def test_missing_parameters_and_run_limit_are_friendly_errors(world):
    """No stored parameters, or RUNS_PER_HOUR used up: one error event, no traceback, no run started."""
    events = list(run_segment(resources(world, Script()), world.user_id, world.report_id, params=None,
                              take_resume=lambda: None, budget_s=10))
    assert _types(events) == ["error"] and "parameters" in events[0].message
    world.add_file("irs.csv", long_csv(unemp_rows()))
    limited = _segment(world, Script(), runs_per_hour=1)
    assert limited[0].type == "started"
    ReportRepo(world.pool)  # a second report of the same user hits the hourly limit
    other = ReportRepo(world.pool).create(world.user_id, "second")
    events = list(run_segment(resources(world, Script(), runs_per_hour=1), world.user_id, str(other["id"]),
                              params=PARAMS, take_resume=lambda: None, budget_s=10))
    assert _types(events) == ["error"] and "report runs in the last hour" in events[0].message


def test_sse_frame_format():
    """event: <type> / data: JSON without empty fields."""
    frame = SseEvent(type="node", node="export", progress=1.0).frame()
    head, data, _, _ = frame.split("\n")
    assert head == "event: node" and json.loads(data.removeprefix("data: ")) == {"type": "node", "node": "export",
                                                                                 "progress": 1.0}
