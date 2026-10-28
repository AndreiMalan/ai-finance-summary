"""Graph state (M7 Task 1): reducers, Send fan-in without loss, checkpoint round-trip with the allow-listed serde."""
import dataclasses
import logging
import operator
import typing
from datetime import datetime, timezone
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite, Send
from pydantic import BaseModel, ValidationError

from statbrief.domain.conflict import Conflict, ConflictResolution, IndicatorLink, UnitMismatch
from statbrief.domain.dataset import ColumnGuess, ColumnMapping, DatasetRef, DetectedSchema, ValidationIssue, ValidationReport
from statbrief.domain.metrics import MetricResult, MetricSkip, SignificantChange
from statbrief.domain.report import ExportStatus, ReportDraft, ReportParams
from statbrief.domain.review import ClaimCheck, ReviewDecision, VerificationResult
from statbrief.graph import state as st
from statbrief.graph.serde import STATE_CLASSES, make_serializer
from statbrief.graph.state import (ApiRequest, ObservationView, PendingMapping, ReportState, ResolveRequest,
                                   SourceMeta, WorkflowError, merge_dicts)
from statbrief.llm.chains import DraftAttempt
from statbrief.rag.retriever import RetrievedPassage
from tests.graph.helpers import draft_payload

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def test_merge_dicts_later_key_wins():
    """Keys are merged; a key written twice keeps the later value; None counts as empty."""
    assert merge_dicts({"a": 1, "b": 2}, {"b": 3, "c": 4}) == {"a": 1, "b": 3, "c": 4}
    assert merge_dicts(None, {"a": 1}) == {"a": 1}
    assert merge_dicts({"a": 1}, None) == {"a": 1}


def test_report_params_validation():
    """Codes are stripped and deduplicated; periods canonical, ordered, same frequency; API modes need regions."""
    p = ReportParams(mode="combined", indicators=[" HICP_YOY", "HICP_YOY", "UNEMP_RATE"], regions=["RO"],
                     period_from="2023-01", period_to="2024-03")
    assert p.indicators == ["HICP_YOY", "UNEMP_RATE"] and p.formats == ["pdf"]
    for bad in ({"period_from": "2024-03", "period_to": "2023-01"}, {"period_from": "2024", "period_to": "2024-03"},
                {"period_from": "March 2024"}, {"indicators": [" "]}):
        with pytest.raises(ValidationError):
            ReportParams(mode="api", regions=["RO"], **bad)
    with pytest.raises(ValidationError):
        ReportParams(mode="api", indicators=["HICP_YOY"])  # no region
    assert ReportParams(mode="upload").regions == []


# --- reducers under Send fan-in ------------------------------------------------------------------------------


class _FanState(TypedDict, total=False):
    """Minimal state using the ReportState reducers."""

    n: int
    source_metadata: Annotated[list[SourceMeta], operator.add]
    mappings: Annotated[dict[str, ColumnMapping], merge_dicts]
    errors: Annotated[list[WorkflowError], operator.add]


def _mapping(col: str) -> ColumnMapping:
    """A complete column mapping."""
    return ColumnMapping(date_col="d", value_col=col, constants={"region": "RO", "indicator": "I", "unit": "%"})


def test_reducers_merge_send_results_without_loss():
    """N parallel Send tasks each add a list item, a dict key and (for odd i) an error: nothing is lost."""
    def fan(state):
        return [Send("work", {"i": i}) for i in range(state["n"])]

    def work(task):
        i = task["i"]
        out = {"source_metadata": [SourceMeta(source_id=f"s{i}", kind="upload", label=f"f{i}", status="ok")],
               "mappings": {f"s{i}": _mapping(f"v{i}")}}
        if i % 2:
            out["errors"] = [WorkflowError(node="work", kind="x", message=f"e{i}")]
        return out

    g = StateGraph(_FanState)
    g.add_node("work", work)
    g.add_conditional_edges(START, fan, ["work"])
    g.add_edge("work", END)
    out = g.compile(checkpointer=InMemorySaver(serde=make_serializer())).invoke(
        {"n": 7}, {"configurable": {"thread_id": "t"}})
    assert sorted(m.source_id for m in out["source_metadata"]) == [f"s{i}" for i in range(7)]
    assert set(out["mappings"]) == {f"s{i}" for i in range(7)}
    assert sorted(e.message for e in out["errors"]) == ["e1", "e3", "e5"]


def test_overwrite_replaces_an_accumulated_list():
    """A node can replace an operator.add list with Overwrite (used to sort/reset metrics)."""
    class S(TypedDict, total=False):
        """State with one accumulating list."""

        xs: Annotated[list[int], operator.add]

    g = StateGraph(S)
    g.add_node("a", lambda s: {"xs": [3, 1, 2]})
    g.add_node("b", lambda s: {"xs": Overwrite(sorted(s["xs"]))})
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    assert g.compile().invoke({"xs": [9]})["xs"] == [1, 2, 3, 9]


# --- serialisation ---------------------------------------------------------------------------------------------


def _classes_in(tp, found: set[type]) -> None:
    """Collect every class (Pydantic model or dataclass) reachable from a type annotation."""
    origin = typing.get_origin(tp)
    if origin is typing.Literal:
        return
    if origin is not None:
        for arg in typing.get_args(tp):
            _classes_in(arg, found)
        return
    if not isinstance(tp, type) or tp in found:
        return
    if issubclass(tp, BaseModel):
        found.add(tp)
        for f in tp.model_fields.values():
            _classes_in(f.annotation, found)
    elif dataclasses.is_dataclass(tp):
        found.add(tp)
        for hint in typing.get_type_hints(tp).values():
            _classes_in(hint, found)
    elif typing.is_typeddict(tp):
        for hint in typing.get_type_hints(tp, include_extras=True).values():
            _classes_in(hint, found)


def test_every_state_class_is_allow_listed():
    """Every class reachable from ReportState, the sub-graph schemas and Send payloads is in the serde allow-list."""
    found: set[type] = set()
    for schema in (ReportState, st.RetrievalState, st.IngestionInput, st.IngestionOutput, st.RetrievalInput,
                   st.RetrievalOutput, st.DraftingInput, st.DraftingOutput, st.FetchTask, st.ParseTask,
                   st.AnalyzeTask):
        _classes_in(schema, found)
    missing = sorted(c.__qualname__ for c in found - set(STATE_CLASSES))
    assert missing == []


def full_state() -> ReportState:
    """A ReportState with every key set to a realistic value (every state class appears at least once)."""
    draft = ReportDraft.model_validate(draft_payload())
    mapping = _mapping("valoare")
    issue = ValidationIssue(issue_id="missing_value:valoare", code="missing_value", severity="error", rows=[3],
                            column="valoare", message="missing")
    conflict = Conflict(conflict_id="cf_1", indicator="HICP_YOY", region="RO", period="2024-03", unit="%",
                        candidates=["a", "b"], values=[1.0, 2.0], sources=["upload:f", "api:eurostat:s"], rel_diff=0.5)
    metric = MetricResult(metric_id="latest:HICP_YOY:RO:2024-03", kind="latest", indicator="HICP_YOY", region="RO",
                          period="2024-03", value=2.5, unit="%", observation_ids=["a"], formula="2.5")
    ref = DatasetRef(file_id="f1", report_id="r1", user_id="u1", filename="irs.csv", kind="csv", size_bytes=10,
                     sha256="h", blob_key="k")
    detected = DetectedSchema(worksheet=None, header_row=1, columns=[ColumnGuess(column="d", role="date",
                                                                                   confidence=0.6)])
    verification = VerificationResult(ok=False, checks=[ClaimCheck(claim_id="c1", ok=False, expected=0.6)],
                                      feedback="fix c1")
    params = ReportParams(mode="combined", indicators=["HICP_YOY"], regions=["RO"], period_from="2023-01",
                          period_to="2024-03", links=[IndicatorLink(upload_code="IPC", catalog_code="HICP_YOY")])
    mismatch = UnitMismatch(indicator="X", region="RO", period="2024", units=["%", "EUR"], candidates=["p", "q"])
    resolve = ResolveRequest(mappings=[PendingMapping(file_id="f1", filename="irs.csv", mapping=mapping,
                                                      confidence=0.6, reason="low confidence")],
                             issues=[st.BlockingIssues(file_id="f1", filename="irs.csv", issues=[issue])],
                             conflicts=[conflict], unit_mismatches=[mismatch],
                             failed_sources=[st.FailedSource(source_id="api:WB_CPI", label="WB", message="404")])
    passage = RetrievedPassage(text="t", doc_id="d", title="T", issuer="ESMB", section="2.1", page=None,
                               score=0.5, method="fts", source_file="f.md", source_type="reference")
    return {
        "report_id": "r1", "user_id": "u1", "params": params, "file_refs": [ref],
        "api_requests": [ApiRequest(indicator="HICP_YOY", regions=["RO"], period_from="2023-01")],
        "source_metadata": [SourceMeta(source_id="f1", kind="upload", label="irs.csv", status="ok",
                                       detected=detected, mapping_confidence=0.6, needs_review=True)],
        "mappings": {"f1": mapping}, "confirmed_mappings": ["f1"],
        "validation": {"f1": ValidationReport(file_id="f1", row_count=3, issues=[issue])},
        "normalized_ref": "r1", "conflicts": [conflict], "unit_mismatches": [mismatch],
        "resolutions": {"cf_1": ConflictResolution(conflict_id="cf_1", choice="keep_both")},
        "dropped_sources": ["api:WB_CPI"], "unit_mismatches_acknowledged": True, "needs_input": resolve,
        "gate_rounds": 1, "metrics": [metric],
        "metric_skips": [MetricSkip(kind="mom", indicator="HICP_YOY", region="RO", reason="no base")],
        "significant": [SignificantChange(metric_id=metric.metric_id, magnitude=2.5, rule="top_mover")],
        "retrieved": [passage], "draft": draft,
        "draft_history": [DraftAttempt(draft, "{}", None, {"input_tokens": 3}), DraftAttempt(None, "x", "bad", {})],
        "verification": verification, "revision_count": 2, "parse_retry_count": 1,
        "review": ReviewDecision(action="request_changes", comment="shorter"), "review_round": 1,
        "export": ExportStatus(state="pending", formats=["pdf"], requested_at=NOW),
        "errors": [WorkflowError(node="n", kind="k", message="m", retryable=True)], "status": "awaiting_review",
    }


def test_state_round_trips_through_the_checkpointer(caplog):
    """Every key survives an InMemorySaver checkpoint with its exact type; no unregistered/blocked type warnings."""
    state = full_state()
    view = ObservationView(user_id="u1", report_id="r1", conflicts=state["conflicts"],
                           resolutions=state["resolutions"])
    g = StateGraph(ReportState)
    g.add_node("write", lambda s: {k: (Overwrite(v) if k in ("source_metadata", "metrics", "metric_skips",
                                                            "draft_history", "errors") else v)
                                   for k, v in state.items()})
    g.add_edge(START, "write")
    g.add_edge("write", END)
    saver = InMemorySaver(serde=make_serializer())
    cfg = {"configurable": {"thread_id": "rt"}}
    with caplog.at_level(logging.WARNING):
        g.compile(checkpointer=saver).invoke({}, cfg)
        restored = saver.get_tuple(cfg).checkpoint["channel_values"]
        view_back = saver.serde.loads_typed(saver.serde.dumps_typed(view))
    assert view_back == view and isinstance(view_back, ObservationView)
    for key, value in state.items():
        assert restored[key] == value, key
        if isinstance(value, list) and value:
            assert type(restored[key][0]) is type(value[0]), key
        elif isinstance(value, dict) and value:
            assert {type(v) for v in restored[key].values()} == {type(v) for v in value.values()}, key
        else:
            assert type(restored[key]) is type(value), key
    assert isinstance(restored["draft_history"][0].draft, ReportDraft)
    assert isinstance(restored["needs_input"].issues[0].issues[0], ValidationIssue)
    noisy = [r.getMessage() for r in caplog.records if "unregistered" in r.getMessage() or "Blocked" in r.getMessage()]
    assert noisy == []
