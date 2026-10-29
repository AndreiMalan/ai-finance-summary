"""Validation, conflicts and interrupt #1 (M7 Task 3): skip when clean, resume with fixes, invalid resumes."""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from statbrief.domain.dataset import ColumnMapping
from statbrief.domain.report import ReportParams
from statbrief.graph.deps import node
from statbrief.graph.failure import fail_safe
from statbrief.graph.ingestion import build_ingestion_graph
from statbrief.graph.nodes_validate import resolve_gate, route_after_validation, validate_normalize
from statbrief.graph.serde import make_serializer
from statbrief.graph.state import ReportState
from statbrief.storage.repos import MappingRepo, ObservationRepo
from tests.graph.helpers import long_csv, monthly, unemp_rows

CRYPTIC = ("c1", "c2", "c3", "c4", "c5")
UPLOAD = ReportParams(mode="upload")


def _graph(deps):
    """Ingestion -> validation loop, with stub analysis (the real nodes come later)."""
    g = StateGraph(ReportState)
    g.add_node("ingestion", build_ingestion_graph(deps))
    g.add_node("validate_normalize", node(validate_normalize, deps))
    g.add_node("resolve_gate", node(resolve_gate, deps))
    g.add_node("analyze_dispatch", lambda s: {"status": "analyzing"})
    g.add_node("fail_safe", node(fail_safe, deps))
    g.add_edge(START, "ingestion")
    g.add_edge("ingestion", "validate_normalize")
    g.add_conditional_edges("validate_normalize", lambda s: route_after_validation(s, deps.settings),
                            ["resolve_gate", "analyze_dispatch", "fail_safe"])
    g.add_edge("resolve_gate", "validate_normalize")
    g.add_edge("analyze_dispatch", END)
    g.add_edge("fail_safe", END)
    return g.compile(checkpointer=InMemorySaver(serde=make_serializer()))


CFG = {"configurable": {"thread_id": "r"}}


def _interrupt(out) -> dict:
    """The single pending interrupt value."""
    assert len(out["__interrupt__"]) == 1
    return out["__interrupt__"][0].value


def _mapping() -> ColumnMapping:
    """The correct mapping of the cryptic CSV."""
    return ColumnMapping(date_col="c1", region_col="c2", indicator_col="c3", value_col="c4", unit_col="c5")


def test_clean_data_skips_the_interrupt(world):
    """A confidently detected, error-free file goes straight to analysis; its observations are stored."""
    ref = world.add_file("irs.csv", long_csv(unemp_rows()))
    out = _graph(world.deps()).invoke(world.base_state(UPLOAD), CFG)
    assert "__interrupt__" not in out and out["status"] == "analyzing"
    assert out["normalized_ref"] == world.report_id and out["validation"][ref.file_id].row_count == 13
    assert len(ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)) == 13


def test_low_confidence_mapping_interrupts_and_resume_with_fixes_reaches_analysis(world):
    """Cryptic headers: the gate shows the detected mapping; a corrected mapping is saved and analysis follows."""
    ref = world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    graph = _graph(world.deps())
    value = _interrupt(graph.invoke(world.base_state(UPLOAD), CFG))
    assert value["kind"] == "resolve" and value["error"] is None and value["round"] == 1
    assert [(m["file_id"], m["confidence"]) for m in value["mappings"]] == [(ref.file_id, 0.5)]
    assert value["mappings"][0]["mapping"]["value_col"] == "c4"  # JSON-ready for the browser
    out = graph.invoke(Command(resume={"mappings": {ref.file_id: _mapping().model_dump(mode="json")}}), CFG)
    assert "__interrupt__" not in out and out["status"] == "analyzing" and out["gate_rounds"] == 1
    assert MappingRepo(world.pool).get(world.user_id, ref.file_id) == _mapping()
    assert ref.file_id in out["confirmed_mappings"]


def test_confirming_the_detected_mapping_is_enough(world):
    """confirm_mappings accepts the shown mapping without retyping it."""
    ref = world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    graph = _graph(world.deps())
    graph.invoke(world.base_state(UPLOAD), CFG)
    out = graph.invoke(Command(resume={"confirm_mappings": [ref.file_id]}), CFG)
    assert out["status"] == "analyzing"


def test_invalid_resume_payload_interrupts_again_with_an_error(world):
    """Unknown fields, unknown files and malformed mappings re-interrupt with a message; nothing raises."""
    ref = world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    graph = _graph(world.deps())
    graph.invoke(world.base_state(UPLOAD), CFG)
    for bad, expected in (({"bogus": 1}, "bogus"), ({"confirm_mappings": ["nope"]}, "unknown file"),
                          ({"mappings": {ref.file_id: {"date_col": "c1"}}}, "value_col"), ("not a dict", "")):
        value = _interrupt(graph.invoke(Command(resume=bad), CFG))
        assert value["kind"] == "resolve" and value["error"] and expected in value["error"]
        assert value["round"] == 1  # an invalid answer does not use up a round
    out = graph.invoke(Command(resume={"confirm_mappings": [ref.file_id]}), CFG)
    assert out["status"] == "analyzing"


def test_blocking_issue_is_excluded_on_resume(world):
    """A non-numeric value blocks the file; excluding the issue normalizes the remaining rows."""
    rows = unemp_rows()
    rows[3] = (rows[3][0], "RO", "IRS_UNEMP", "n/a?", "%")
    ref = world.add_file("irs.csv", long_csv(rows))
    graph = _graph(world.deps())
    value = _interrupt(graph.invoke(world.base_state(UPLOAD), CFG))
    issues = value["issues"][0]["issues"]
    assert value["issues"][0]["file_id"] == ref.file_id and [i["code"] for i in issues] == ["non_numeric"]
    assert ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id) == []
    out = graph.invoke(Command(resume={"issue_resolutions": {ref.file_id: {issues[0]["issue_id"]: "excluded"}}}),
                       CFG)
    assert out["status"] == "analyzing"
    assert len(ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)) == 12


def test_failed_api_offers_to_continue_without_it(world):
    """AC 44: a failed source interrupts with failed_sources; dropping it continues with the rest."""
    world.api.serve("HICP_YOY", {"RO": monthly(2023, [10.0 - i * 0.3 for i in range(15)])})
    world.api.fail("WB_CPI")
    params = ReportParams(mode="api", indicators=["HICP_YOY", "WB_CPI"], regions=["RO"])
    graph = _graph(world.deps())
    value = _interrupt(graph.invoke(world.base_state(params), CFG))
    assert [f["label"] for f in value["failed_sources"]] == ["Inflation, consumer prices (annual %) (worldbank)"]
    out = graph.invoke(Command(resume={"drop_sources": [value["failed_sources"][0]["source_id"]]}), CFG)
    assert out["status"] == "analyzing" and len(out["dropped_sources"]) == 1


def test_conflicts_must_be_resolved(world):
    """Upload and API disagree on the same key: every conflict is shown and must be resolved."""
    world.api.serve("UNEMP_RATE", {"RO": monthly(2023, [5.8] * 13)})
    world.add_file("irs.csv", long_csv(unemp_rows(indicator="UNEMP_RATE")))
    params = ReportParams(mode="combined", indicators=["UNEMP_RATE"], regions=["RO"], period_from="2023-01",
                          period_to="2024-01")
    graph = _graph(world.deps())
    value = _interrupt(graph.invoke(world.base_state(params), CFG))
    conflicts = value["conflicts"]
    assert len(conflicts) == 13 and all(len(c["candidates"]) == 2 for c in conflicts)
    partial = [{"conflict_id": c["conflict_id"], "choice": "keep_both"} for c in conflicts[:5]]
    value = _interrupt(graph.invoke(Command(resume={"conflict_resolutions": partial}), CFG))
    assert len(value["conflicts"]) == 8 and value["round"] == 2
    rest = [{"conflict_id": c["conflict_id"], "choice": "exclude"} for c in value["conflicts"]]
    out = graph.invoke(Command(resume={"conflict_resolutions": rest}), CFG)
    assert out["status"] == "analyzing" and len(out["resolutions"]) == 13


def test_rounds_are_bounded_then_fail_safe(world):
    """With MAX_REVISIONS=1 an unanswered problem after one round ends in the safe failure."""
    world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    graph = _graph(world.deps(max_revisions=1))
    graph.invoke(world.base_state(UPLOAD), CFG)
    # A valid answer that resolves nothing. (A bare {} never reaches the node: LangGraph reads an empty dict
    # as an empty interrupt-id -> value map, so the gate would simply be shown again.)
    out = graph.invoke(Command(resume={"drop_sources": []}), CFG)
    assert "__interrupt__" not in out and out["status"] == "failed"
    assert out["errors"][-1].kind == "needs_manual_attention" and "unresolved" in out["errors"][-1].message


def test_no_data_fails_safely(world):
    """Upload mode without files: nothing to analyse, safe failure with a reason."""
    out = _graph(world.deps()).invoke(world.base_state(UPLOAD), CFG)
    assert out["status"] == "failed" and "No usable observations" in out["errors"][-1].message


# --- review fixes ------------------------------------------------------------------------------------------------

def test_unexpected_failure_in_validation_fails_safe(world):
    """Review I1: any unexpected exception in validate_normalize ends in fail_safe with a WorkflowError."""
    import dataclasses

    class BrokenObservations(ObservationRepo):
        """Reads fail like a database outage."""

        def list_for_report(self, user_id, report_id):
            """Simulate an outage (the message must not reach the user)."""
            raise RuntimeError("connection to secret-host failed")

    world.add_file("irs.csv", long_csv(unemp_rows()))
    deps = dataclasses.replace(world.deps(), observations=BrokenObservations(world.pool))
    out = _graph(deps).invoke(world.base_state(UPLOAD), CFG)
    assert out["status"] == "failed" and "__interrupt__" not in out
    internal = [e for e in out["errors"] if e.kind == "internal_error"]
    assert internal and "RuntimeError" in internal[0].message and "secret" not in internal[0].message


@pytest.mark.parametrize("payload_key", ["confirm_mappings", "mappings", "conflict_resolutions", "drop_sources",
                                         "issue_resolutions"])
def test_resume_naming_another_reports_ids_is_rejected(world, payload_key):
    """Review M10: ids from another user's or report's data are 'unknown' and re-interrupt; nothing is written."""
    ref = world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    foreign = "00000000-0000-0000-0000-00000000beef"
    graph = _graph(world.deps())
    graph.invoke(world.base_state(UPLOAD), CFG)
    payload = {"confirm_mappings": {"confirm_mappings": [foreign]},
               "mappings": {"mappings": {foreign: _mapping().model_dump(mode="json")}},
               "conflict_resolutions": {"conflict_resolutions": [{"conflict_id": "cf_foreign", "choice": "exclude"}]},
               "drop_sources": {"drop_sources": [foreign]},
               "issue_resolutions": {"issue_resolutions": {foreign: {"x": "excluded"}}}}[payload_key]
    value = _interrupt(graph.invoke(Command(resume=payload), CFG))
    assert value["error"] and value["round"] == 1
    assert MappingRepo(world.pool).get(world.user_id, ref.file_id) is None


def test_unit_mismatch_acknowledgement_is_per_mismatch(world):
    """Review M1: accepting records the keys of the mismatches shown, not a global flag."""
    world.api.serve("UNEMP_RATE", {"RO": monthly(2023, [5.8] * 13)})
    world.add_file("irs.csv", long_csv([(p, r, "UNEMP_RATE", v, "PERS") for p, r, _, v, _ in unemp_rows()]))
    params = ReportParams(mode="combined", indicators=["UNEMP_RATE"], regions=["RO"], period_from="2023-01",
                          period_to="2024-01")
    graph = _graph(world.deps())
    value = _interrupt(graph.invoke(world.base_state(params), CFG))
    assert len(value["unit_mismatches"]) == 13
    out = graph.invoke(Command(resume={"accept_unit_mismatches": True}), CFG)
    assert out["status"] == "analyzing" and len(out["acknowledged_mismatches"]) == 13
    assert all(k.startswith("UNEMP_RATE|RO|") for k in out["acknowledged_mismatches"])


def test_remapping_a_file_clears_its_conflict_resolutions(world):
    """Review M6: choices made under a file's old mapping do not carry over to its new values."""
    world.api.serve("UNEMP_RATE", {"RO": monthly(2023, [5.8] * 13)})
    ref = world.add_file("irs.csv", long_csv(unemp_rows(indicator="UNEMP_RATE")))
    params = ReportParams(mode="combined", indicators=["UNEMP_RATE"], regions=["RO"], period_from="2023-01",
                          period_to="2024-01")
    graph = _graph(world.deps())
    conflicts = _interrupt(graph.invoke(world.base_state(params), CFG))["conflicts"]
    mapping = ColumnMapping(date_col="date", region_col="region", indicator_col="indicator", value_col="value",
                            unit_col="unit")
    value = _interrupt(graph.invoke(Command(resume={
        "mappings": {ref.file_id: mapping.model_dump(mode="json")},
        "conflict_resolutions": [{"conflict_id": c["conflict_id"], "choice": "exclude"} for c in conflicts]}), CFG))
    assert len(value["conflicts"]) == 13 and graph.get_state(CFG).values["resolutions"] == {}
