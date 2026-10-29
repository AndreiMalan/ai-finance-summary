"""Drafting sub-graph (M7 Task 5): verify, bounded revisions, parse retries, unknown-column guard, safe failure."""
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from statbrief.analytics.metrics import compute_metrics
from statbrief.domain.dataset import ValidationIssue, ValidationReport
from statbrief.domain.report import ReportParams
from statbrief.graph.deps import node
from statbrief.graph.drafting import build_drafting_graph, route_after_drafting, undocumented_columns
from statbrief.graph.failure import fail_safe
from statbrief.graph.serde import make_serializer
from statbrief.graph.state import ReportState
from statbrief.storage.repos import ObservationRepo
from tests.graph.helpers import api_obs, draft_for, fake_model, report_call
from tests.unit.llm.fakes import invalid_tool_call_message, tool_call_message

PARAMS = ReportParams(mode="api", indicators=["UNEMP_RATE"], regions=["RO"], period_from="2023-01",
                      period_to="2024-01")
UNEMP = {**{f"2023-{m:02d}": round(5.0 + m / 100, 2) for m in range(1, 13)}, "2024-01": 5.6}
UNDOCUMENTED = ValidationReport(file_id="f1", row_count=13, issues=[ValidationIssue(
    issue_id="undocumented_column:val_adj_x", code="undocumented_column", severity="warning", column="val_adj_x",
    message="no documented meaning")])


def _graph(deps):
    """drafting sub-graph -> human_review (stub) | fail_safe."""
    g = StateGraph(ReportState)
    g.add_node("drafting", build_drafting_graph(deps))
    g.add_node("human_review", lambda s: {"status": "awaiting_review"})
    g.add_node("fail_safe", node(fail_safe, deps))
    g.add_edge(START, "drafting")
    g.add_conditional_edges("drafting", lambda s: route_after_drafting(s, deps.settings), ["human_review", "fail_safe"])
    g.add_edge("human_review", END)
    g.add_edge("fail_safe", END)
    return g.compile(checkpointer=InMemorySaver(serde=make_serializer()))


def _setup(world):
    """Store the unemployment series and return (initial state, the pp-change metric the drafts state)."""
    obs = api_obs("UNEMP_RATE", "RO", UNEMP)
    ObservationRepo(world.pool).upsert_api_observations(world.user_id, world.report_id, obs)
    metrics, _ = compute_metrics(obs, indicator="UNEMP_RATE", region="RO")
    metric = next(m for m in metrics if m.metric_id == "pp_change:UNEMP_RATE:RO:2024-01:2023-01")
    state = {**world.base_state(PARAMS), "metrics": sorted(metrics, key=lambda m: m.metric_id), "significant": [],
             "retrieved": [], "validation": {}, "mappings": {}}
    return state, metric


def _run(world, model, state_extra=None, **settings):
    """Run drafting with a scripted model."""
    state, _ = _setup(world)
    graph = _graph(world.deps(chat_model=model, **settings))
    return graph.invoke({**state, **(state_extra or {})}, {"configurable": {"thread_id": "d"}})


def _metric(world):
    """The pp-change metric (without storing data twice)."""
    obs = api_obs("UNEMP_RATE", "RO", UNEMP)
    return next(m for m in compute_metrics(obs, indicator="UNEMP_RATE", region="RO")[0] if m.kind == "pp_change"
                and m.base_period == "2023-01")


def test_verified_first_time_goes_to_human_review(world):
    """A correct draft is verified once and handed to the reviewer; one model call."""
    m = _metric(world)
    out = _run(world, fake_model(report_call(draft_for(m, "0.6"))))
    assert out["status"] == "awaiting_review" and out["verification"].ok
    assert len(out["draft_history"]) == 1 and out["revision_count"] == 0
    assert out["draft"].claims[0].value == 0.6


def test_wrong_number_is_revised_then_verified(world):
    """0.9 fails verification; the revision (fed the feedback) states 0.6 and passes."""
    m = _metric(world)
    out = _run(world, fake_model(report_call(draft_for(m, "0.9")), report_call(draft_for(m, "0.6"))))
    assert out["status"] == "awaiting_review" and out["revision_count"] == 1 and len(out["draft_history"]) == 2


def test_always_wrong_fails_safe_after_exactly_max_revisions(world):
    """Every draft wrong: one draft + MAX_REVISIONS revisions, then the safe failure keeping the last draft."""
    m = _metric(world)
    model = fake_model(*[report_call(draft_for(m, "0.9")) for _ in range(10)])
    out = _run(world, model)
    assert out["status"] == "failed" and out["revision_count"] == 3 and len(out["draft_history"]) == 4
    assert out["draft"] is not None and not out["verification"].ok and out["verification"].feedback
    assert "after 3 revisions" in out["errors"][-1].message


def test_malformed_output_is_retried_then_fails_safe(world):
    """Unparseable output: MAX_PARSE_RETRIES retries, then the safe failure; never an exception."""
    model = fake_model(*[invalid_tool_call_message("ReportDraft", '{"title": ') for _ in range(5)])
    out = _run(world, model)
    assert out["status"] == "failed" and len(out["draft_history"]) == 3 and out["parse_retry_count"] == 3
    assert [e.kind for e in out["errors"]].count("parse_error") == 3
    assert "could not be read after 3 attempts" in out["errors"][-1].message


def test_malformed_then_valid_recovers(world):
    """One retry is enough when the next reply parses."""
    m = _metric(world)
    out = _run(world, fake_model(invalid_tool_call_message("ReportDraft", "{"), report_call(draft_for(m, "0.6"))))
    assert out["status"] == "awaiting_review" and out["parse_retry_count"] == 0


def test_undocumented_column_interpreted_is_revised(world):
    """A draft that interprets val_adj_x fails the guard; the revision only acknowledges it and passes."""
    m = _metric(world)
    bad = draft_for(m, "0.6", extra_caveat="The val_adj_x column indicates seasonal adjustment.")
    good = draft_for(m, "0.6", acknowledged=["val_adj_x"],
                     extra_caveat="The meaning of column val_adj_x is not documented.")
    extra = {"validation": {"f1": UNDOCUMENTED}}
    out = _run(world, fake_model(report_call(bad), report_call(good)), extra)
    # The numbers were right both times: only the guard can have sent the first draft back.
    assert out["status"] == "awaiting_review" and out["revision_count"] == 1
    assert out["draft_history"][0].draft.claims == out["draft"].claims
    assert undocumented_columns({**extra, "mappings": {}}) == {"val_adj_x"}


def test_tool_calls_before_the_draft(world):
    """The model may call controlled tools first; the ReportDraft call ends the loop."""
    m = _metric(world)
    model = fake_model(tool_call_message("compute_metric", {"metric_id": m.metric_id}, call_id="t1"),
                       report_call(draft_for(m, "0.6")))
    out = _run(world, model)
    assert out["status"] == "awaiting_review"
    assert [getattr(t, "name", getattr(t, "__name__", None)) for t in model.bound_tools][-1] == "ReportDraft"


def test_prose_reply_is_forced_into_the_schema(world):
    """A plain-text reply ends the tool loop; one forced structured call then yields the draft."""
    m = _metric(world)
    out = _run(world, fake_model(AIMessage(content="Here is my analysis."), report_call(draft_for(m, "0.6"))))
    assert out["status"] == "awaiting_review" and len(out["draft_history"]) == 1


def test_without_tools_the_structured_chain_is_used(world):
    """ENABLE_DRAFT_TOOLS off: the M6 draft chain (with_structured_output) produces the draft."""
    m = _metric(world)
    model = fake_model(report_call(draft_for(m, "0.6")))
    out = _run(world, model, enable_draft_tools=False)
    assert out["status"] == "awaiting_review" and model.structured_method == "function_calling"


def test_unknown_citation_is_revised(world):
    """Review M3: a citation that is neither a retrieved passage nor a report source fails verification."""
    m = _metric(world)
    bad = draft_for(m, "0.6")
    bad["citations"] = [{"kind": "reference", "ref_id": "made-up-doc", "label": "Invented"}]
    bad["sections"][0]["citation_ids"] = ["made-up-doc"]
    good = draft_for(m, "0.6")
    good["citations"] = [{"kind": "api", "ref_id": "UNEMP_RATE?test", "label": "Eurostat series"}]
    good["sections"][0]["citation_ids"] = ["UNEMP_RATE?test"]
    out = _run(world, fake_model(report_call(bad), report_call(good)))
    assert out["status"] == "awaiting_review" and out["revision_count"] == 1
