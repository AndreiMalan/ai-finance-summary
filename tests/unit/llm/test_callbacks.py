"""Redacting callback handler -> run_events (Task 5): timings, tokens, tools, retrieval, errors, daily cap."""
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from statbrief.auth.limits import LimitExceeded
from statbrief.auth.users import create_user
from statbrief.llm.callbacks import RunEventsCallbackHandler, daily_llm_cap_guard
from statbrief.llm.chains import PassageRetriever, build_context_chain, build_draft_chain
from statbrief.llm.prompts import render_context, source_infos
from statbrief.llm.tools import build_tools
from statbrief.sources.catalog import CATALOG
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.repos import RUN_EVENT_KINDS, ReportRepo, RunEvent, RunEventRepo
from tests.unit.llm import data
from tests.unit.llm.fakes import ToolFakeChatModel, make_settings, scripted, tool_call_message
from tests.unit.llm.test_chains import USAGE, valid_draft

FORBIDDEN = [str(v) for v in data.RAW_VALUES] + [
    "6.987654", "0.135802", "percentage points", "Unemployment rose", "Registered unemployment differs",
    "IRS_regional_monthly.xlsx", "5.6"]


class MemorySink:
    """In-memory RunEventRepo stand-in."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[tuple[str, str, RunEvent]] = []

    def add(self, user_id: str, report_id: str, event: RunEvent) -> None:
        """Keep the event."""
        self.events.append((user_id, report_id, event))

    def kinds(self) -> list[str]:
        """Kinds in order."""
        return [e.kind for _, _, e in self.events]

    def of(self, kind: str) -> list[RunEvent]:
        """Events of one kind."""
        return [e for _, _, e in self.events if e.kind == kind]


class FailingSink:
    """A sink whose writes always fail (observability must never break a run)."""

    def add(self, *args: Any) -> None:
        """Fail."""
        raise RuntimeError("db down")


class ExplodingModel(GenericFakeChatModel):
    """A chat model whose provider call fails with a message containing a secret."""

    def _generate(self, *args: Any, **kwargs: Any):
        """Raise like a provider auth error would."""
        raise RuntimeError("Incorrect API key provided: sk-secret-123")


def _handler(sink, settings=None, **kw) -> RunEventsCallbackHandler:
    """A handler for user u1 / report r1."""
    return RunEventsCallbackHandler(sink, user_id="u1", report_id="r1", settings=settings or make_settings(), **kw)


def _context(settings) -> dict[str, str]:
    """A rendered draft context."""
    metrics, sig = data.metrics()
    return render_context(regions=["RO"], period="2023-01 to 2024-01", metrics=metrics, significant=sig,
                          sources=source_infos(data.observations()), passages=data.passages(),
                          column_descriptions={"valoare": "rate"}, undocumented_columns=["val_adj_x"],
                          settings=settings)


def _run_everything(handler, settings) -> None:
    """Context chain (retrieval), draft chain (LLM) and one tool call, all traced by `handler`."""
    cfg = {"callbacks": [handler]}
    metrics, sig = data.metrics()
    retriever = PassageRetriever(search_fn=lambda q: data.passages())
    build_context_chain(retriever, settings).invoke(
        {"query": "unemployment", "regions": ["RO"], "period": "2023", "metrics": metrics, "significant": sig,
         "sources": [], "column_descriptions": {}, "undocumented_columns": []}, config=cfg)
    model = ToolFakeChatModel(messages=scripted(tool_call_message("ReportDraft", valid_draft(), usage=USAGE)))
    build_draft_chain(model, settings).invoke(_context(settings), config=cfg)
    tools = {t.name: t for t in build_tools(data.store(), metrics, CATALOG, "u1", settings)}
    tools["query_observations"].invoke({"name": "query_observations", "id": "t1", "type": "tool_call",
                                        "args": {"indicator_code": "UNEMP_RATE", "region": "RO"}}, config=cfg)


def test_llm_tool_and_retrieval_events_with_timings_and_tokens():
    """Model calls, tokens, latency, tool names + arg keys and retrieved doc ids/scores are recorded."""
    sink, settings = MemorySink(), make_settings()
    _run_everything(_handler(sink, settings), settings)
    assert sink.kinds() == ["retrieval", "llm_start", "llm_end", "tool_start", "tool_end"]
    assert all(u == "u1" and r == "r1" for u, r, _ in sink.events)
    ret = sink.of("retrieval")[0]
    assert ret.detail["doc_ids"] == ["esmb-g-2023-02", "irs-dict"] and ret.detail["scores"] == [0.91, 0.5]
    assert ret.latency_ms is not None and ret.latency_ms >= 0
    start, end = sink.of("llm_start")[0], sink.of("llm_end")[0]
    assert start.detail["prompt_template"] == "draft" and "metrics_table" in start.detail["prompt_variables"]
    assert (end.prompt_tokens, end.completion_tokens) == (1200, 300)
    assert end.latency_ms >= 0 and end.detail["tool_calls"] == ["ReportDraft"]
    assert end.model == start.model
    tool_start, tool_end = sink.of("tool_start")[0], sink.of("tool_end")[0]
    assert tool_start.name == "query_observations" and tool_start.detail["arg_keys"] == ["indicator_code", "region"]
    assert tool_end.detail["ok"] is True and tool_end.latency_ms >= 0


def test_no_raw_values_or_prompt_text_in_details():
    """Details hold names, ids, keys and counts only: no prompt text, values, file names or tool output."""
    sink, settings = MemorySink(), make_settings()
    _run_everything(_handler(sink, settings), settings)
    blob = json.dumps([e.detail for _, _, e in sink.events])
    for bad in FORBIDDEN:
        assert bad not in blob, bad


def test_log_prompts_debug_mode_stores_prompt_text():
    """With LOG_PROMPTS on, prompt and response text are stored (owner-only diagnostics)."""
    sink, settings = MemorySink(), make_settings(log_prompts=True)
    _run_everything(_handler(sink, settings), settings)
    assert "Use only numbers given in the METRICS table" in json.dumps(sink.of("llm_start")[0].detail)
    assert "Unemployment rose" in json.dumps(sink.of("llm_end")[0].detail)


def test_llm_error_records_type_only():
    """A failing model call is recorded as llm_error with the exception type, never its message."""
    sink = MemorySink()
    with pytest.raises(RuntimeError):
        ExplodingModel(messages=iter([])).invoke("hi", config={"callbacks": [_handler(sink)]})
    err = sink.of("llm_error")[0]
    assert err.detail == {"error_type": "RuntimeError"} and "sk-secret" not in json.dumps(err.detail)


def test_tool_validation_error_recorded_as_not_ok():
    """A tool call with invalid args is logged as a tool_end with ok=False (keys only)."""
    sink, settings = MemorySink(), make_settings()
    metrics, _ = data.metrics()
    q = {t.name: t for t in build_tools(data.store(), metrics, CATALOG, "u1", settings)}["query_observations"]
    q.invoke({"name": "query_observations", "id": "t", "type": "tool_call",
              "args": {"indicator_code": "X", "region": "RO", "limit": -5}}, config={"callbacks": [_handler(sink)]})
    assert sink.kinds()[-1] in ("tool_end", "tool_error")
    last = sink.events[-1][2]
    assert last.detail.get("ok") is False or last.kind == "tool_error"
    assert "-5" not in json.dumps([e.detail for _, _, e in sink.events])


def test_sink_failure_never_breaks_the_run():
    """If run_events cannot be written, the chain still completes."""
    settings = make_settings()
    model = ToolFakeChatModel(messages=scripted(tool_call_message("ReportDraft", valid_draft())))
    out = build_draft_chain(model, settings).invoke(_context(settings),
                                                    config={"callbacks": [_handler(FailingSink(), settings)]})
    assert out.draft is not None


def test_cap_guard_called_before_each_model_call():
    """The guard runs before the model; LimitExceeded aborts the call and is logged as llm_error."""
    calls = []

    def guard():
        calls.append(1)
        if len(calls) > 1:
            raise LimitExceeded("llm_call", 1, "global")

    sink, settings = MemorySink(), make_settings()
    model = ToolFakeChatModel(messages=scripted(AIMessage(content="one"), AIMessage(content="two")))
    h = _handler(sink, settings, cap_guard=guard)
    assert model.invoke("a", config={"callbacks": [h]}).content == "one"
    with pytest.raises(LimitExceeded):
        model.invoke("b", config={"callbacks": [h]})
    assert sink.kinds() == ["llm_start", "llm_end", "llm_error"]
    assert sink.of("llm_error")[0].detail == {"error_type": "LimitExceeded", "limit": 1, "scope": "global"}
    assert next(model.messages).content == "two"  # the second reply was never consumed


def test_node_events_recorded():
    """Graph nodes (M7) can record their own timing events."""
    sink = MemorySink()
    _handler(sink).record_node("verify", latency_ms=12, detail={"ok": False, "orphans": 2})
    ev = sink.of("node")[0]
    assert (ev.name, ev.latency_ms, ev.detail) == ("verify", 12, {"ok": False, "orphans": 2})


# --- Postgres: repository + real daily cap ---------------------------------------------------------------

@pytest.fixture
def db(settings):
    """Migrated pool, two users and a report owned by the first."""
    bootstrap(settings)
    pool = create_pool(settings)
    alice = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    bob = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    report = ReportRepo(pool).create(alice.id, "R")
    yield pool, alice, bob, str(report["id"])
    pool.close()


def test_run_events_written_to_postgres_and_user_scoped(db):
    """Events land in run_events; other users can neither write to nor read the report's events."""
    pool, alice, bob, rid = db
    repo = RunEventRepo(pool)
    settings = make_settings()
    h = RunEventsCallbackHandler(repo, user_id=alice.id, report_id=rid, settings=settings)
    _run_everything(h, settings)
    rows = repo.list_for_report(alice.id, rid)
    assert [r["kind"] for r in rows] == ["retrieval", "llm_start", "llm_end", "tool_start", "tool_end"]
    end = rows[2]
    assert (end["prompt_tokens"], end["completion_tokens"]) == (1200, 300) and end["latency_ms"] >= 0
    assert end["ts"].tzinfo is not None
    assert repo.list_for_report(bob.id, rid) == []
    with pytest.raises(PermissionError):
        repo.add(bob.id, rid, RunEvent(kind="node", name="x"))
    assert repo.list_for_report(alice.id, "not-a-uuid") == []


def test_run_event_kind_checked(db):
    """Only the documented kinds are accepted."""
    pool, alice, _, rid = db
    assert set(RUN_EVENT_KINDS) == {"llm_start", "llm_end", "llm_error", "tool_start", "tool_end", "tool_error",
                                    "retrieval", "node"}
    with pytest.raises(ValueError):
        RunEventRepo(pool).add(alice.id, rid, RunEvent(kind="bogus"))


def test_daily_cap_enforced_with_postgres_counter(db):
    """DAILY_LLM_CALL_CAP (global) blocks the call after the cap; the blocked call reaches no model."""
    pool, alice, _, rid = db
    settings = make_settings(daily_llm_call_cap=2)
    fixed = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    guard = daily_llm_cap_guard(pool, settings, alice.id, now=lambda: fixed)
    h = RunEventsCallbackHandler(RunEventRepo(pool), user_id=alice.id, report_id=rid, settings=settings,
                                 cap_guard=guard)
    model = ToolFakeChatModel(messages=scripted(*[AIMessage(content=str(i)) for i in range(3)]))
    model.invoke("a", config={"callbacks": [h]})
    model.invoke("b", config={"callbacks": [h]})
    with pytest.raises(LimitExceeded) as exc:
        model.invoke("c", config={"callbacks": [h]})
    assert exc.value.scope == "global" and exc.value.limit == 2
    kinds = [r["kind"] for r in RunEventRepo(pool).list_for_report(alice.id, rid)]
    assert kinds.count("llm_end") == 2 and kinds[-1] == "llm_error"
