"""M7b review fixes: run lease (C1), interrupt-bound answers (I1), fork rules (I2), node reserve (I4), m2."""
import threading
from typing import Any

import pytest

from statbrief.domain.report import ReportParams
from statbrief.graph.builder import graph_config
from statbrief.graph.history import NotForkable, fork_from, list_checkpoints
from statbrief.graph.runner import run_segment
from statbrief.graph.runtime import state_graph
from statbrief.storage.repos import ObservationRepo, ReportRepo, RunEventRepo
from tests.graph.helpers import draft_for, long_csv, monthly, report_call, unemp_rows
from tests.graph.runkit import Script, pp_metric, queue_taker, quoted, resources
from tests.unit.llm.fakes import ToolFakeChatModel

PARAMS = ReportParams(mode="combined", indicators=["HICP_YOY", "IRS_UNEMP"], regions=["RO"],
                      period_from="2023-01", period_to="2024-01").model_dump(mode="json")
TOKEN_A, TOKEN_B = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"


def _segment(world, script, *, take=None, clock=None, budget_s=240.0, res=None, **settings):
    """Events of one segment."""
    kwargs = {"clock": clock} if clock else {}
    return list(run_segment(res or resources(world, script, **settings), world.user_id, world.report_id,
                            params=PARAMS, take_resume=take or (lambda: None), budget_s=budget_s, **kwargs))


def _draft(world, caveat="Draft."):
    """A lazily built verifiable draft."""
    return lambda: report_call(draft_for(pp_metric(world, "IRS_UNEMP"), "0.6", extra_caveat=caveat))


@pytest.fixture
def combined(world):
    """A combined report (HICP API + clean IRS upload) ready to run."""
    world.api.serve("HICP_YOY", {"RO": monthly(2022, [9.0 - i * 0.2 for i in range(25)])})
    world.add_file("irs.csv", long_csv(unemp_rows()))
    return world


# --- C1: the run lease ----------------------------------------------------------------------------------------

def test_two_concurrent_acquirers_exactly_one_wins(world):
    """Atomic UPDATE: of many simultaneous callers exactly one takes the lease."""
    repo, barrier, wins = ReportRepo(world.pool), threading.Barrier(4), []

    def grab(i):
        """Race for the lease."""
        barrier.wait()
        wins.append(repo.acquire_lease(world.user_id, world.report_id, f"00000000-0000-0000-0000-00000000000{i}", 60))

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(wins) == [False, False, False, True]


def test_crashed_holder_expires_and_non_holder_cannot_release_or_renew(world):
    """A lease nobody releases expires; release/renew with another token are no-ops; others' reports refused."""
    repo = ReportRepo(world.pool)
    assert repo.acquire_lease(world.user_id, world.report_id, TOKEN_A, 60)
    repo.release_lease(world.report_id, TOKEN_B)  # not the holder: no-op
    assert not repo.renew_lease(world.report_id, TOKEN_B, 60)
    assert not repo.acquire_lease(world.user_id, world.report_id, TOKEN_B, 60)
    assert not repo.acquire_lease(world.other_user_id, world.report_id, TOKEN_B, 60)
    with world.pool.connection() as c:  # the holder "crashed" long ago
        c.execute("UPDATE reports SET run_lease_until = now() - interval '1 second' WHERE id = %s",
                  (world.report_id,))
    assert repo.acquire_lease(world.user_id, world.report_id, TOKEN_B, 60)
    assert not repo.renew_lease(world.report_id, TOKEN_A, 60)  # the old holder lost it


def test_segment_releases_the_lease_even_when_the_stream_is_closed_early(combined):
    """Closing the event generator (client disconnect) releases the lease at once."""
    world = combined
    events = run_segment(resources(world, Script()), world.user_id, world.report_id, params=PARAMS,
                         take_resume=lambda: None, budget_s=240)
    assert next(events).type == "started"
    events.close()
    assert ReportRepo(world.pool).acquire_lease(world.user_id, world.report_id, TOKEN_A, 60)


# --- I1: answers are bound to their interrupt -----------------------------------------------------------------

def test_stale_answer_for_an_earlier_round_is_dropped(combined):
    """Round 1 approve queued after round 2 started: the segment drops it, round 2 is shown, nothing approved."""
    world, script = combined, Script()
    script.replies.append(_draft(world, "First round."))
    first = _segment(world, script)
    round1 = first[-1].data["interrupt_id"]
    script.replies.append(_draft(world, "Second round."))
    queue = [{"action": "request_changes", "comment": "Add a caveat."}]
    second = _segment(world, script, take=queue_taker(world, queue))
    assert second[-1].data["kind"] == "review" and second[-1].data["interrupt_id"] != round1
    stale = {"interrupt_id": round1, "payload": {"action": "approve"}}
    again = _segment(world, script, take=lambda: stale)
    assert [e.type for e in again] == ["interrupt"] and again[0].data["draft"]["caveats"][-1] == "Second round."
    state = state_graph(resources(world, Script())).get_state(graph_config(world.report_id, world.settings)).values
    assert state["status"] != "approved" and state.get("export") is None and state["review_round"] == 1


# --- I2: forks only at single-node checkpoints ------------------------------------------------------------------

def test_fan_out_checkpoint_is_not_listed_and_cannot_be_forked(combined):
    """The analyze_one x K checkpoint would fork into a dead branch: hidden in history and refused."""
    world, script = combined, Script()
    script.replies.append(_draft(world))
    _segment(world, script)
    graph = state_graph(resources(world, Script()))
    raw = list(graph.get_state_history(graph_config(world.report_id, world.settings)))
    fan_out = next(s for s in raw if len(s.next) > 1)
    assert set(fan_out.next) == {"analyze_one"}
    listed = list_checkpoints(graph, world.report_id, world.settings)
    assert all(len(c.next) == 1 and c.next != ["analyze_one"] for c in listed)
    assert fan_out.config["configurable"]["checkpoint_id"] not in {c.checkpoint_id for c in listed}
    with pytest.raises(NotForkable):
        fork_from(graph, world.report_id, fan_out.config["configurable"]["checkpoint_id"], world.settings)
    # Forking just before the fan-out re-plans it and completes with metrics.
    dispatch = next(c for c in listed if c.next == ["analyze_dispatch"])
    fork_from(graph, world.report_id, dispatch.checkpoint_id, world.settings)
    script.replies.append(_draft(world, "After the fork."))
    events = _segment(world, script)
    assert events[-1].data["kind"] == "review" and events[-1].data["draft"]["caveats"][-1] == "After the fork."
    state = graph.get_state(graph_config(world.report_id, world.settings)).values
    assert state["metrics"]


# --- I4: no long step started past the budget; an overrun leaves the run resumable -----------------------------

class _Clock:
    """A fake monotonic clock advancing `step` seconds per reading."""

    def __init__(self, step: float) -> None:
        """Start at zero."""
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        """The next reading."""
        self.t += self.step
        return self.t


def test_segment_stops_when_another_long_node_would_not_fit(combined):
    """MAX_NODE_S=100, budget 150: once 50 s have passed, no new step starts (continue), and runs resume."""
    world, script = combined, Script()
    script.replies.append(_draft(world))
    events = _segment(world, script, clock=_Clock(30.0), budget_s=150, segment_budget_s=150, max_node_s=100)
    assert events[-1].type == "continue"
    for _ in range(40):  # real clock: the rest runs to the review
        events = _segment(world, script)
        if events[-1].type != "continue":
            break
    assert events[-1].data["kind"] == "review"


class KilledByPlatform(BaseException):
    """Stands for the function being killed mid-node (not an Exception: nothing may catch it)."""


class _DyingModel(ToolFakeChatModel):
    """Dies once (simulated maxDuration kill), then answers the script."""

    deaths: list = []

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        """Die if queued, else answer."""
        if self.deaths:
            raise self.deaths.pop(0)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_node_overrun_leaves_the_previous_checkpoint_resumable(combined):
    """A kill inside `draft` loses only that step: the next segment re-runs drafting and reaches the review."""
    world, script = combined, Script()
    script.replies.append(_draft(world, "After the kill."))
    model = _DyingModel(messages=script.stream(), deaths=[KilledByPlatform()])
    res = resources(world, script)
    res.model_factory = lambda s, cap: model
    with pytest.raises(KilledByPlatform):
        _segment(world, script, res=res)
    with world.pool.connection() as c:  # a killed instance never releases: let the lease expire
        c.execute("UPDATE reports SET run_lease_until = now() - interval '1 second' WHERE id = %s",
                  (world.report_id,))
    events = _segment(world, script, res=res)
    assert events[-1].data["kind"] == "review" and events[-1].data["draft"]["caveats"][-1] == "After the kill."


# --- m2: a database failure leaves a timeline trace --------------------------------------------------------------

def test_database_failure_is_recorded_in_run_events(combined, monkeypatch):
    """A node failing on the database ends the segment with an error event and a segment_error run event."""
    world = combined

    def broken(self, *args, **kwargs):
        """Simulate an outage in the analysis reads."""
        raise ConnectionError("db down")

    monkeypatch.setattr(ObservationRepo, "list_for_series", broken)
    events = _segment(world, Script())
    assert events[-1].type == "error" and "unexpectedly" in events[-1].message
    rows = [e for e in RunEventRepo(world.pool).list_for_report(world.user_id, world.report_id)
            if e["name"] == "segment_error"]
    assert rows and rows[-1]["detail"] == {"error_type": "ConnectionError"}


def test_quoted_answer_matches_the_pending_interrupt(combined):
    """The test helper quotes the id the segment checks (sanity for the other tests)."""
    world, script = combined, Script()
    script.replies.append(_draft(world))
    review = _segment(world, script)[-1]
    assert quoted(world, {"action": "approve"})["interrupt_id"] == review.data["interrupt_id"]


# --- M7b re-review I2b / I6 / I7 ----------------------------------------------------------------------------------

FORK_POINTS = {"analyze_dispatch", "retrieval", "drafting", "human_review"}


def test_only_safe_resume_points_are_listed_and_each_one_runs_on(combined):
    """I2b: history lists only the allow-listed points; a fork at each of them reaches a review again."""
    world, script = combined, Script()
    script.replies.append(_draft(world, "Original."))
    _segment(world, script)
    graph = state_graph(resources(world, Script()))
    listed = list_checkpoints(graph, world.report_id, world.settings)
    assert {c.next[0] for c in listed} == FORK_POINTS
    raw = list(graph.get_state_history(graph_config(world.report_id, world.settings)))
    fan_in = next(s for s in raw if s.next == ("aggregate_metrics",)).config["configurable"]["checkpoint_id"]
    with pytest.raises(NotForkable):
        fork_from(graph, world.report_id, fan_in, world.settings)
    for point in sorted(FORK_POINTS):
        target = next(c for c in list_checkpoints(graph, world.report_id, world.settings) if c.next == [point])
        fork_from(graph, world.report_id, target.checkpoint_id, world.settings)
        if point != "human_review":
            script.replies.append(_draft(world, f"Fork at {point.replace('_', ' ')}."))
        events = _segment(world, script)
        assert events[-1].type == "interrupt" and events[-1].data["kind"] == "review", (point, events[-1])


def test_lease_outlives_the_function_and_is_renewed_inside_drafting(combined, monkeypatch):
    """I7: lease >= maxDuration + margin; sub-graph checkpoints inside one drafting step renew it."""
    from statbrief.config import FUNCTION_MAX_DURATION_S
    from statbrief.graph.runner import lease_seconds
    world = combined
    assert lease_seconds(resources(world, Script())) >= FUNCTION_MAX_DURATION_S + world.settings.run_lease_margin_s
    renewals: list[str] = []
    original = ReportRepo.renew_lease

    def spy(self, report_id, token, seconds):
        """Record where in the run each renewal happened (the draft count so far)."""
        renewals.append(state_marker["phase"])
        return original(self, report_id, token, seconds)

    state_marker = {"phase": "before drafting"}
    monkeypatch.setattr(ReportRepo, "renew_lease", spy)
    script = Script()

    def wrong_then_mark():
        """First draft states a wrong number (forces a revise inside the same drafting step)."""
        state_marker["phase"] = "inside drafting"
        return report_call(draft_for(pp_metric(world, "IRS_UNEMP"), "0.9"))

    script.replies.extend([wrong_then_mark, _draft(world)])
    events = _segment(world, script)
    assert events[-1].data["kind"] == "review"
    # draft -> verify_claims -> revise -> verify_claims are sub-graph checkpoints of ONE top-level step.
    assert renewals.count("inside drafting") >= 3

