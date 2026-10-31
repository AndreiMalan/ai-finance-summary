"""Full graph (M7 Task 6): happy path with two resumes, reject, request changes, edited drafts, diagram."""
import logging
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from statbrief.analytics.metrics import compute_metrics
from statbrief.domain.report import ReportParams
from statbrief.graph.builder import build_graph, graph_config, mermaid_diagram
from statbrief.graph.serde import make_serializer
from statbrief.storage.repos import ObservationRepo
from tests.graph.helpers import draft_for, long_csv, report_call, unemp_rows
from tests.unit.llm.fakes import ToolFakeChatModel

DIAGRAM = Path(__file__).resolve().parents[2] / "docs" / "graph.mmd"
PARAMS = ReportParams(mode="upload", indicators=["IRS_UNEMP"], regions=["RO"], formats=["pdf", "xlsx"])
CRYPTIC = ("c1", "c2", "c3", "c4", "c5")


class Script:
    """Replies for a fake model, appended while the test runs (drafts need ids known only after ingestion)."""

    def __init__(self) -> None:
        """Start empty."""
        self.replies: list = []

    def stream(self):
        """Generator handing out the queued replies in order."""
        while True:
            yield self.replies.pop(0)


def _model(script: Script) -> ToolFakeChatModel:
    """A tool-capable fake model reading from the script."""
    return ToolFakeChatModel(messages=script.stream())


def _metric(world):
    """The uploaded pp change 2024-01 vs 2023-01 (read from the database after normalization)."""
    obs = ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)
    return next(m for m in compute_metrics(obs, indicator="IRS_UNEMP", region="RO")[0]
                if m.kind == "pp_change" and m.base_period == "2023-01")


@pytest.fixture
def run(world):
    """A compiled graph over the world with a cryptic upload (resolve interrupt) and a scripted model."""
    world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    script = Script()

    def make(**settings):
        """Compile the graph with the given setting overrides."""
        deps = world.deps(chat_model=_model(script), **settings)
        graph = build_graph(InMemorySaver(serde=make_serializer()), InMemoryStore(), deps)
        return graph, graph_config(world.report_id, deps.settings)

    return world, script, make


def _value(out) -> dict:
    """The pending interrupt value."""
    return out["__interrupt__"][0].value


def _to_review(world, script, graph, cfg) -> dict:
    """Start, confirm the mapping (resume #1) and return the review interrupt value."""
    first = _value(graph.invoke(world.base_state(PARAMS), cfg))
    assert first["kind"] == "resolve"
    # The file is already normalized (under the shown mapping) at the first interrupt, so its ids are known.
    script.replies.append(report_call(draft_for(_metric(world), "0.6")))
    return _value(graph.invoke(Command(resume={"confirm_mappings": [first["mappings"][0]["file_id"]]}), cfg))


def test_happy_path_to_export(run, caplog):
    """Resolve -> analysis -> retrieval -> verified draft -> review -> approve -> export pending."""
    world, script, make = run
    graph, cfg = make()
    with caplog.at_level(logging.WARNING):
        review = _to_review(world, script, graph, cfg)
        assert review["kind"] == "review" and review["verification"]["ok"] and review["error"] is None
        assert review["draft"]["claims"][0]["value"] == 0.6
        skips = review["metric_skips"]  # re-review I4: the UI can show why metrics are missing
        assert skips and {"kind", "indicator", "region", "period", "reason"} <= set(skips[0])
        out = graph.invoke(Command(resume={"action": "approve"}), cfg)
    assert "__interrupt__" not in out and out["status"] == "approved"
    assert out["export"].state == "pending" and out["export"].formats == ["pdf", "xlsx"]
    assert out["review"].action == "approve" and graph.get_state(cfg).next == ()
    assert out["metrics"] and out["normalized_ref"] == world.report_id
    assert not [r for r in caplog.records if "unregistered" in r.getMessage() or "Blocked" in r.getMessage()]


def test_reject_ends_without_export(run):
    """Reject ends the run with status rejected and no export."""
    world, script, make = run
    graph, cfg = make()
    _to_review(world, script, graph, cfg)
    out = graph.invoke(Command(resume={"action": "reject", "comment": "not needed"}), cfg)
    assert out["status"] == "rejected" and out.get("export") is None and graph.get_state(cfg).next == ()


def test_request_changes_loops_through_drafting(run):
    """A change request runs the review-revise chain with the comment, then asks for review again."""
    world, script, make = run
    graph, cfg = make()
    _to_review(world, script, graph, cfg)
    metric = _metric(world)
    revised = draft_for(metric, "0.6", extra_caveat="Registered unemployment differs from the survey rate.")
    script.replies.append(report_call(revised))
    again = _value(graph.invoke(Command(resume={"action": "request_changes", "comment": "Mention the survey."}), cfg))
    assert again["kind"] == "review" and again["review_round"] == 1
    assert again["draft"]["caveats"][-1].startswith("Registered unemployment")
    state = graph.get_state(cfg).values
    assert len(state["draft_history"]) == 2 and state["review"] is None
    out = graph.invoke(Command(resume={"action": "approve"}), cfg)
    assert out["status"] == "approved"


def test_review_payload_errors_and_change_limit(run):
    """Invalid review answers re-interrupt with an error; change requests stop at MAX_REVISIONS."""
    world, script, make = run
    graph, cfg = make(max_revisions=1)
    _to_review(world, script, graph, cfg)
    for bad, expected in (({"action": "request_changes"}, "comment"), ({"action": "maybe"}, "action"),
                          ({"action": "reject", "edited_draft": draft_for(_metric(world), "0.6")}, "edited")):
        value = _value(graph.invoke(Command(resume=bad), cfg))
        assert value["kind"] == "review" and expected in value["error"]
    script.replies.append(report_call(draft_for(_metric(world), "0.6")))
    graph.invoke(Command(resume={"action": "request_changes", "comment": "shorter"}), cfg)
    limited = _value(graph.invoke(Command(resume={"action": "request_changes", "comment": "again"}), cfg))
    assert "limit of 1 change requests" in limited["error"]


def test_edited_draft_is_reverified_before_export(run):
    """An edited draft with a wrong number is not accepted; a correct edit replaces the draft and is exported."""
    world, script, make = run
    graph, cfg = make()
    _to_review(world, script, graph, cfg)
    metric = _metric(world)
    wrong = _value(graph.invoke(Command(resume={"action": "approve", "edited_draft": draft_for(metric, "0.8")}), cfg))
    assert "failed verification" in wrong["error"] and not wrong["verification"]["ok"]
    assert wrong["draft"]["claims"][0]["value"] == 0.8  # the reviewer sees their edit with the feedback
    edited = draft_for(metric, "0.6", extra_caveat="Edited by the reviewer.")
    out = graph.invoke(Command(resume={"action": "approve", "edited_draft": edited}), cfg)
    assert out["status"] == "approved" and out["draft"].caveats[-1] == "Edited by the reviewer."
    assert out["verification"].ok


def test_core_path_with_every_toggle_off(run):
    """AC 46: ENABLE_PARALLEL, ENABLE_VECTOR_SEARCH and ENABLE_REPLAY off, tools off: same path to export."""
    world, script, make = run
    graph, cfg = make(enable_parallel=False, enable_vector_search=False, enable_replay=False,
                      enable_draft_tools=False)
    review = _to_review(world, script, graph, cfg)
    assert review["verification"]["ok"]
    assert graph.invoke(Command(resume={"action": "approve"}), cfg)["status"] == "approved"


def test_mermaid_diagram_renders_and_matches_docs(world):
    """The diagram shows every node and the expanded sub-graphs; docs/graph.mmd is current."""
    text = mermaid_diagram(world.deps())
    for name in ("validate_normalize", "resolve_gate", "analyze_one", "human_review", "export", "fail_safe",
                 "ingestion", "fetch_api", "parse_file", "retrieval", "retrieve_context", "drafting",
                 "verify_claims", "revise"):
        assert name in text
    assert DIAGRAM.read_text(encoding="utf-8") == text, "regenerate docs/graph.mmd with mermaid_diagram()"


# --- review C1 / D-035 / D-036 at graph level: metrics, not only the run status --------------------------------

def _combined_run(world, rows, consent=False):
    """Upload IRS_UNEMP (linked to UNEMP_RATE) + Eurostat UNEMP_RATE with the same values; run until drafting
    fails on purpose (unparseable replies) and return the final state values."""
    from statbrief.domain.conflict import IndicatorLink
    from tests.graph.helpers import fake_model, monthly
    from tests.unit.llm.fakes import invalid_tool_call_message
    world.add_file("irs.csv", long_csv(rows))
    world.api.serve("UNEMP_RATE", {"RO": dict(zip(monthly(2023, [0.0] * 13), [r[3] for r in unemp_rows()]))})
    params = ReportParams(mode="combined", indicators=["UNEMP_RATE"], regions=["RO"], period_from="2023-01",
                          period_to="2024-01", consent_gap_fill=consent,
                          links=[IndicatorLink(upload_code="IRS_UNEMP", catalog_code="UNEMP_RATE")])
    model = fake_model(*[invalid_tool_call_message("ReportDraft", "{") for _ in range(3)])
    deps = world.deps(chat_model=model)
    graph = build_graph(InMemorySaver(serde=make_serializer()), None, deps)
    cfg = graph_config(world.report_id, deps.settings)
    out = graph.invoke(world.base_state(params), cfg)
    assert "__interrupt__" not in out  # agreeing data: nothing to resolve
    return graph.get_state(cfg).values


def _upload_ids(world) -> set[str]:
    """Ids of the uploaded (file) observations of the report."""
    from statbrief.domain.observation import UploadProvenance
    return {o.obs_id for o in ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)
            if isinstance(o.provenance, UploadProvenance)}


def test_combined_agreeing_sources_give_the_upload_only_metrics(world):
    """C1: upload + agreeing linked API series -> metrics exist, equal the upload-only ones, no ambiguity."""
    from statbrief.graph.nodes_analyze import report_window_only, sort_results
    state = _combined_run(world, unemp_rows())
    uploads = [o.model_copy(update={"indicator_code": "UNEMP_RATE"})
               for o in ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)
               if o.obs_id in _upload_ids(world)]
    expected, skips = sort_results(*report_window_only(*compute_metrics(uploads, indicator="UNEMP_RATE",
                                                                          region="RO"), state["params"]))
    assert state["metrics"] and state["metrics"] == expected
    assert not any("ambiguous" in s.reason for s in state["metric_skips"])
    assert len(state["corroborations"]) == 13 and set(state["corroborations"]) <= _upload_ids(world)
    pp = next(m for m in state["metrics"] if m.metric_id == "pp_change:UNEMP_RATE:RO:2024-01:2023-01")
    assert pp.value == pytest.approx(5.6 - 5.01)


def test_consented_gap_fill_makes_the_filled_month_usable(world):
    """C1: 2023-12 is missing from the upload; with consent it is filled once and the MoM change is computed."""
    rows = [r for r in unemp_rows() if r[0] != "2023-12"]
    state = _combined_run(world, rows, consent=True)
    step = next(m for m in state["metrics"] if m.metric_id == "pp_change:UNEMP_RATE:RO:2024-01:2023-12")
    assert step.value == pytest.approx(5.6 - 5.12)
    assert any(i not in _upload_ids(world) for i in step.observation_ids)  # the filled point (API provenance)
    assert not any("ambiguous" in s.reason for s in state["metric_skips"])
