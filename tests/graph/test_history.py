"""History, replay and fork (M7b Task 9): checkpoints listed, prior drafts, fork re-drafts without re-fetching."""
import pytest

from statbrief.domain.report import ReportParams
from statbrief.graph.history import UnknownCheckpoint, fork_from, get_checkpoint_draft, list_checkpoints
from statbrief.graph.runner import run_segment
from statbrief.graph.runtime import state_graph
from tests.graph.helpers import draft_for, long_csv, monthly, report_call, unemp_rows
from tests.graph.runkit import Script, pp_metric, queue_taker, resources

PARAMS = ReportParams(mode="combined", indicators=["HICP_YOY", "IRS_UNEMP"], regions=["RO"],
                      period_from="2023-01", period_to="2024-01").model_dump(mode="json")


def _segment(world, script, resume=None):
    """Events of one run segment."""
    queue = [resume]
    return list(run_segment(resources(world, script), world.user_id, world.report_id, params=PARAMS,
                            take_resume=queue_taker(world, queue), budget_s=240))


def _draft(world, caveat):
    """A lazily built, verifiable draft with an extra caveat (ids exist once the file is normalized)."""
    return lambda: report_call(draft_for(pp_metric(world, "IRS_UNEMP"), "0.6", extra_caveat=caveat))


@pytest.fixture
def at_review(world):
    """A combined report run to the review interrupt (one API fetch so far)."""
    world.api.serve("HICP_YOY", {"RO": monthly(2022, [9.0 - i * 0.2 for i in range(25)])})
    world.add_file("irs.csv", long_csv(unemp_rows()))
    script = Script()
    script.replies.append(_draft(world, "First draft."))
    events = _segment(world, script)
    assert events[-1].type == "interrupt" and events[-1].data["kind"] == "review"
    assert len(world.api.requests) == 1
    return world, script


def test_history_lists_interrupts_and_drafts(at_review):
    """Newest first; the review interrupt and the draft are visible; earlier checkpoints have no draft."""
    world, _ = at_review
    graph = state_graph(resources(world, Script()))
    entries = list_checkpoints(graph, world.report_id, world.settings)
    assert entries[0].interrupt == "review" and entries[0].has_draft and entries[0].next == ["human_review"]
    assert entries[-1].has_draft is False and [e.next[0] for e in entries] == ["human_review", "drafting", "retrieval", "analyze_dispatch"]
    assert [e.step for e in entries] == sorted([e.step for e in entries], reverse=True)
    draft = get_checkpoint_draft(graph, world.report_id, entries[0].checkpoint_id, world.settings)
    assert draft.caveats[-1] == "First draft."
    with pytest.raises(UnknownCheckpoint):
        get_checkpoint_draft(graph, world.report_id, "1f000000-0000-0000-0000-000000000000", world.settings)


def test_fork_redrafts_from_an_earlier_checkpoint_without_refetching(at_review):
    """AC 42: fork at the checkpoint before drafting; the next segment drafts again, no API call."""
    world, script = at_review
    graph = state_graph(resources(world, Script()))
    before = next(e for e in list_checkpoints(graph, world.report_id, world.settings) if e.next == ["drafting"])
    new_id = fork_from(graph, world.report_id, before.checkpoint_id, world.settings)
    assert list_checkpoints(graph, world.report_id, world.settings)[0].checkpoint_id == new_id
    script.replies.append(_draft(world, "Second draft after the fork."))
    events = _segment(world, script)
    assert events[-1].type == "interrupt" and events[-1].data["draft"]["caveats"][-1] == "Second draft after the fork."
    assert len(world.api.requests) == 1  # nothing re-fetched
    state = graph.get_state({"configurable": {"thread_id": world.report_id}}).values
    assert len(state["draft_history"]) == 1  # the branch starts from before the first draft
    done = _segment(world, script, resume={"action": "approve"})
    assert done[-1].data == {"status": "approved"}
