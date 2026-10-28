"""Controlled tools (Task 4): validated args, row clamp, deterministic compute_metric, tool-calling loop."""
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from statbrief.analytics.store import ObservationStore, compute_metric
from statbrief.llm.tools import TOOL_NAMES, build_tools, run_tool_loop
from statbrief.sources.catalog import CATALOG
from statbrief.sources.results import SeriesMeta
from tests.unit.llm import data
from tests.unit.llm.fakes import ToolFakeListChatModel, invalid_tool_call_message, make_settings, tool_call_message


def _tools(settings=None, store=None, **kw):
    """Tools over the sample store, keyed by name."""
    metrics, _ = data.metrics()
    tools = build_tools(store or data.store(), metrics, CATALOG, "user-1", settings or make_settings(), **kw)
    return {t.name: t for t in tools}


def _call(tool, args, call_id="c1") -> ToolMessage:
    """Invoke a tool the way the model does (a ToolCall) and return the ToolMessage."""
    return tool.invoke({"name": tool.name, "args": args, "id": call_id, "type": "tool_call"})


def test_four_tools_with_pydantic_schemas():
    """The four controlled tools exist, each with a Pydantic args schema and user metadata."""
    tools = _tools()
    assert set(tools) == set(TOOL_NAMES) == {"get_indicator_metadata", "list_available_metrics",
                                             "query_observations", "compute_metric"}
    for t in tools.values():
        assert t.args_schema is not None and t.metadata["user_id"] == "user-1"


def test_indicator_metadata_from_catalog_series_meta_and_data():
    """Catalog spec, API series metadata and the regions present in the user's data are combined."""
    meta = {"HICP_YOY": SeriesMeta(label="HICP - annual rate", unit_label="Annual rate of change",
                                   last_update="2026-10-01")}
    out = json.loads(_tools(series_meta=meta)["get_indicator_metadata"].invoke({"indicator_code": "HICP_YOY"}))
    assert out["catalog"]["source"] == "eurostat" and out["catalog"]["is_rate"] is True
    assert out["series_meta"]["last_update"] == "2026-10-01"
    assert out["regions_in_data"] == ["RO"]


def test_indicator_metadata_for_uploaded_indicator_and_unknown():
    """An uploaded indicator outside the catalog is described from data; an unknown code is an error message."""
    out = json.loads(_tools()["get_indicator_metadata"].invoke({"indicator_code": "ADJ"}))
    assert out["catalog"] is None and out["units_in_data"] == ["IDX"]
    msg = _tools()["get_indicator_metadata"].invoke({"indicator_code": "NOPE"})
    assert msg.startswith("error: unknown indicator 'NOPE'")


def test_list_available_metrics_filters():
    """Metrics can be filtered by indicator and region."""
    out = json.loads(_tools()["list_available_metrics"].invoke({"indicator_code": "HICP_YOY"}))
    assert {m["metric_id"] for m in out["metrics"]} == {"latest:HICP_YOY:RO:2024-03",
                                                         "pp_change:HICP_YOY:RO:2024-03:2023-03"}
    assert _tools()["list_available_metrics"].invoke({"region": "BG"}) == "error: no metrics match; known regions: RO"
    assert len(json.loads(_tools()["list_available_metrics"].invoke({}))["metrics"]) == 5


def test_query_observations_clamped_to_max_tool_rows():
    """A limit above MAX_TOOL_ROWS (or none) is clamped; truncation is reported."""
    tools = _tools(settings=make_settings(max_tool_rows=3))
    out = json.loads(tools["query_observations"].invoke(
        {"indicator_code": "UNEMP_RATE", "region": "RO", "limit": 1000}))
    assert len(out["rows"]) == 3 and out["truncated"] is True and out["total"] == 13
    assert set(out["rows"][0]) == {"obs_id", "period", "value", "unit", "unit_multiplier", "status_flag", "source"}


def test_query_observations_period_window():
    """period_from/period_to bound the rows (inclusive)."""
    out = json.loads(_tools()["query_observations"].invoke(
        {"indicator_code": "UNEMP_RATE", "region": "RO", "period_from": "2023-11", "period_to": "2024-01"}))
    assert [r["period"] for r in out["rows"]] == ["2023-11", "2023-12", "2024-01"]
    assert out["rows"][0]["source"] == "IRS_regional_monthly.xlsx"


def test_invalid_args_return_error_message_not_exception():
    """Bad periods, a zero limit, reversed windows, extra args and missing args come back as error text."""
    q = _tools()["query_observations"]
    for args in ({"indicator_code": "UNEMP_RATE", "region": "RO", "period_from": "2024/01"},
                 {"indicator_code": "UNEMP_RATE", "region": "RO", "limit": 0},
                 {"indicator_code": "UNEMP_RATE", "region": "RO", "period_from": "2024-02", "period_to": "2024-01"},
                 {"indicator_code": "UNEMP_RATE", "region": "RO", "period_from": "2024", "period_to": "2024-01"},
                 {"indicator_code": "UNEMP_RATE", "region": "RO", "sql": "DROP TABLE x"},
                 {"region": "RO"}):
        msg = _call(q, args)
        assert isinstance(msg, ToolMessage) and msg.content.startswith("error: invalid arguments"), args
        assert "DROP TABLE" not in msg.content


def test_unknown_series_is_error_message():
    """A series not in the user's data is reported, not raised."""
    msg = _tools()["query_observations"].invoke({"indicator_code": "UNEMP_RATE", "region": "BG"})
    assert msg.startswith("error: no observations for indicator 'UNEMP_RATE' in region 'BG'")


def test_compute_metric_equals_m4_result():
    """compute_metric recomputes deterministically through M4 (the model never does arithmetic)."""
    mid = "pp_change:UNEMP_RATE:RO:2024-01:2023-01"
    out = json.loads(_tools()["compute_metric"].invoke({"metric_id": mid}))
    expected = compute_metric(mid, data.store())
    assert out["value"] == expected.value and out["observation_ids"] == expected.observation_ids
    assert out["formula"] == expected.formula


def test_compute_metric_unavailable_is_error_message():
    """Malformed or unsupported metric ids give the M4 reason as an error message."""
    assert _tools()["compute_metric"].invoke({"metric_id": "bogus"}).startswith("error: bogus: malformed metric id")
    msg = _tools()["compute_metric"].invoke({"metric_id": "pct_change:UNEMP_RATE:RO:2024-01:2023-01"})
    assert msg.startswith("error:") and "rate" in msg


def test_tools_only_see_the_given_store():
    """Tools are built over one report's store: another user's data is not reachable."""
    other = ObservationStore([o for o in data.observations() if o.indicator_code == "HICP_YOY"])
    tools = _tools(store=other)
    assert tools["query_observations"].invoke({"indicator_code": "UNEMP_RATE", "region": "RO"}).startswith("error:")


def test_tool_calling_loop_with_fake_model():
    """bind_tools + loop: tool calls (valid, invalid, malformed, unknown) are answered, then the final reply."""
    model = ToolFakeListChatModel(responses=[
        tool_call_message("compute_metric", {"metric_id": "latest:UNEMP_RATE:RO:2024-01"}, call_id="a"),
        AIMessage(content="", tool_calls=[
            {"name": "query_observations", "args": {"region": "RO"}, "id": "b", "type": "tool_call"},
            {"name": "delete_everything", "args": {}, "id": "c", "type": "tool_call"}]),
        invalid_tool_call_message("compute_metric", "{oops", call_id="d"),
        AIMessage(content="Unemployment was 5.6 %."),
    ])
    tools = list(_tools().values())
    msgs = run_tool_loop(model, tools, [HumanMessage(content="What is unemployment?")], make_settings())
    assert [t.name for t in model.bound_tools] == [t.name for t in tools]
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b", "c", "d"]
    assert json.loads(tool_msgs[0].content)["value"] == 5.6
    assert tool_msgs[1].content.startswith("error: invalid arguments")
    assert tool_msgs[2].content.startswith("error: unknown tool")
    assert tool_msgs[3].content.startswith("error: malformed tool-call arguments")
    assert msgs[-1].content == "Unemployment was 5.6 %."


def test_tool_loop_bounded_by_max_steps():
    """A model that keeps calling tools stops after LLM_MAX_TOOL_STEPS turns."""
    model = ToolFakeListChatModel(responses=[
        tool_call_message("list_available_metrics", {}, call_id="x")])
    msgs = run_tool_loop(model, list(_tools().values()), [HumanMessage(content="?")],
                         make_settings(llm_max_tool_steps=2))
    assert sum(isinstance(m, AIMessage) for m in msgs) == 2


# --- review fixes M3 / M4 / M11 ---------------------------------------------------------------------------

def test_row_budget_caps_rows_across_calls():
    """Review M4: MAX_TOOL_ROWS_PER_TURN bounds the rows of all query_observations calls of one tool list."""
    assert make_settings().max_tool_rows_per_turn == 400
    q = _tools(settings=make_settings(max_tool_rows=3, max_tool_rows_per_turn=5))["query_observations"]
    args = {"indicator_code": "UNEMP_RATE", "region": "RO"}
    first, second = json.loads(q.invoke(args)), json.loads(q.invoke(args))
    assert len(first["rows"]) == 3 and len(second["rows"]) == 2 and second["truncated"] is True
    assert second["row_budget_remaining"] == 0
    assert q.invoke(args).startswith("error: row budget exhausted")


def test_row_budget_shared_in_tool_loop():
    """Parallel calls in one loop draw on the same budget."""
    calls = [{"name": "query_observations", "args": {"indicator_code": "UNEMP_RATE", "region": "RO"},
              "id": f"q{i}", "type": "tool_call"} for i in range(3)]
    model = ToolFakeListChatModel(responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")])
    tools = list(_tools(settings=make_settings(max_tool_rows=10, max_tool_rows_per_turn=15)).values())
    msgs = run_tool_loop(model, tools, [HumanMessage(content="?")], make_settings())
    contents = [m.content for m in msgs if isinstance(m, ToolMessage)]
    assert len(json.loads(contents[0])["rows"]) == 10 and len(json.loads(contents[1])["rows"]) == 5
    assert contents[2].startswith("error: row budget exhausted")


def test_wrong_frequency_window_is_an_error_not_empty():
    """Review M3: a quarterly bound on a monthly series explains the frequency instead of returning nothing."""
    msg = _tools()["query_observations"].invoke(
        {"indicator_code": "UNEMP_RATE", "region": "RO", "period_from": "2024-Q1"})
    assert msg.startswith("error: series UNEMP_RATE/RO has frequency M") and "2024-01" in msg


def test_metric_filter_without_match_lists_known_codes():
    """Review M3: a non-matching filter (e.g. wrong case) lists the known codes."""
    msg = _tools()["list_available_metrics"].invoke({"indicator_code": "hicp_yoy"})
    assert msg.startswith("error: no metrics match; known indicators: HICP_YOY, UNEMP_RATE")


def test_unexpected_tool_exception_becomes_error_message(monkeypatch):
    """Review M11: an unexpected exception inside a tool is returned as its type, never raised."""
    from statbrief.llm import tools as tools_mod

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(tools_mod, "compute_metric", boom)
    msg = _call(_tools()["compute_metric"], {"metric_id": "latest:UNEMP_RATE:RO:2024-01"})
    assert msg.content == "error: RuntimeError" and "secret" not in msg.content
