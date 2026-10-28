"""Redacting LangChain callback handler that writes run_events, and the global daily LLM-call cap (AC 51).

`RunEventsCallbackHandler` is passed in ``config["callbacks"]`` for one report run. It records:
- llm_start / llm_end / llm_error: model name, latency, prompt/completion tokens, prompt template name and
  variable NAMES (from the chain's metadata), names of requested tool calls, error type;
- tool_start / tool_end / tool_error: tool name, argument KEYS, latency, ok flag (output starts "error:");
- retrieval: document ids, scores and methods, latency;
- node: graph-node timings recorded by the workflow (M7) through `record_node`.

Redaction: never prompt or response text, argument values, tool outputs, retrieved text, queries or
exception messages (provider errors may echo keys or data). With LOG_PROMPTS on (debug), prompt and
response text and the retrieval query are added; events are only readable by the report owner.

Before every model call the optional `cap_guard` runs (normally `daily_llm_cap_guard`, which counts the
call against DAILY_LLM_CALL_CAP across all users). `raise_error = True` lets its `LimitExceeded` abort the
call; the model is never reached. Failures to WRITE events are logged and swallowed: observability never
breaks a report run.
"""
import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.documents import Document
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from psycopg_pool import ConnectionPool

from statbrief.auth.limits import KIND_LLM_CALL, LimitExceeded, consume, utc_midnight
from statbrief.config import Settings
from statbrief.storage.repos import RunEvent

logger = logging.getLogger(__name__)
_MS_PER_S = 1000


class RunEventSink(Protocol):
    """Where events go (RunEventRepo in production)."""

    def add(self, user_id: str, report_id: str, event: RunEvent) -> None:
        """Store one event."""


def _utc_now() -> datetime:
    """Current time, timezone-aware."""
    return datetime.now(timezone.utc)


def daily_llm_cap_guard(pool: ConnectionPool, settings: Settings, user_id: str, *,
                        now: Callable[[], datetime] = _utc_now) -> Callable[[], None]:
    """A guard that records one LLM call against the global daily cap, or raises LimitExceeded."""
    def guard() -> None:
        """Consume one call of today's (UTC) global budget."""
        t = now()
        consume(pool, user_id, KIND_LLM_CALL, settings.daily_llm_call_cap, utc_midnight(t), t, scope="global")
    return guard


def _model_name(serialized: dict | None, kwargs: dict, metadata: dict | None) -> str | None:
    """Model id from invocation params, LangSmith metadata or the serialized class name."""
    params = kwargs.get("invocation_params") or {}
    for value in (params.get("model"), params.get("model_name"), (metadata or {}).get("ls_model_name"),
                  params.get("_type"), (serialized or {}).get("name")):
        if value:
            return str(value)
    return None


def _usage(response: LLMResult) -> tuple[int | None, int | None]:
    """(prompt, completion) tokens from usage_metadata, else from llm_output['token_usage']."""
    for gens in response.generations:
        for g in gens:
            meta = getattr(g.message, "usage_metadata", None) if isinstance(g, ChatGeneration) else None
            if meta:
                return meta.get("input_tokens"), meta.get("output_tokens")
    usage = (response.llm_output or {}).get("token_usage") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


def _tool_call_names(response: LLMResult) -> list[str]:
    """Names (only) of the tool calls the model requested."""
    return [tc["name"] for gens in response.generations for g in gens if isinstance(g, ChatGeneration)
            for tc in getattr(g.message, "tool_calls", []) or []]


def _response_text(response: LLMResult) -> list[str]:
    """Response text and tool-call arguments (debug mode only)."""
    out = []
    for gens in response.generations:
        for g in gens:
            out.append(g.text)
            if isinstance(g, ChatGeneration):
                out.extend(json.dumps(tc["args"], ensure_ascii=False) for tc in getattr(g.message, "tool_calls", []))
    return out


def _arg_keys(input_str: str, inputs: dict | None) -> list[str]:
    """Sorted argument names of a tool call (values are never kept)."""
    if isinstance(inputs, dict):
        return sorted(str(k) for k in inputs)
    try:
        parsed = json.loads(input_str)
    except (TypeError, ValueError):
        return []
    return sorted(str(k) for k in parsed) if isinstance(parsed, dict) else []


def _output_text(output: Any) -> str:
    """Text content of a tool output (str or ToolMessage)."""
    content = getattr(output, "content", output)
    return content if isinstance(content, str) else str(content)


class RunEventsCallbackHandler(BaseCallbackHandler):
    """Writes redacted run_events for one report run and enforces the daily LLM-call cap."""

    raise_error = True  # LimitExceeded from the cap guard must abort the model call
    run_inline = True  # keep events in call order

    def __init__(self, sink: RunEventSink, *, user_id: str, report_id: str, settings: Settings,
                 cap_guard: Callable[[], None] | None = None, clock: Callable[[], float] = time.perf_counter,
                 now: Callable[[], datetime] = _utc_now) -> None:
        """Bind to a sink, the run's owner and report, and an optional cap guard."""
        self.sink, self.user_id, self.report_id = sink, user_id, report_id
        self.log_prompts = settings.log_prompts
        self.cap_guard, self.clock, self.now = cap_guard, clock, now
        self._starts: dict[UUID, tuple[float, str | None, str | None, dict]] = {}

    # --- plumbing ---------------------------------------------------------------------------------------

    def _write(self, event: RunEvent) -> None:
        """Store an event; storage failures are logged (type only) and swallowed."""
        try:
            self.sink.add(self.user_id, self.report_id, event)
        except Exception as exc:  # observability must never break the run
            logger.warning("run event %s not stored (%s)", event.kind, type(exc).__name__)

    def _begin(self, run_id: UUID, name: str | None, model: str | None, base: dict) -> None:
        """Remember when a run started."""
        self._starts[run_id] = (self.clock(), name, model, base)

    def _finish(self, run_id: UUID) -> tuple[int | None, str | None, str | None, dict]:
        """Latency (ms), name, model and base detail of a finished run."""
        t0, name, model, base = self._starts.pop(run_id, (None, None, None, {}))
        latency = None if t0 is None else max(0, round((self.clock() - t0) * _MS_PER_S))
        return latency, name, model, base

    def record_node(self, name: str, *, latency_ms: int | None = None, detail: dict | None = None) -> None:
        """Record a workflow node event (M7); `detail` must already be free of raw values."""
        self._write(RunEvent(kind="node", name=name, latency_ms=latency_ms, detail=dict(detail or {}), ts=self.now()))

    # --- model calls ------------------------------------------------------------------------------------

    def _model_start(self, serialized: dict, texts: list[str], *, run_id: UUID, metadata: dict | None,
                     kwargs: dict) -> None:
        """Common start handling for chat and text models: cap check, then an llm_start event."""
        model = _model_name(serialized, kwargs, metadata)
        name = kwargs.get("name") or (serialized or {}).get("name")
        meta = metadata or {}
        base = {k: meta[k] for k in ("prompt_template", "prompt_variables") if k in meta}
        if self.cap_guard is not None:
            try:
                self.cap_guard()
            except LimitExceeded as exc:
                self._write(RunEvent(kind="llm_error", name=name, model=model, ts=self.now(), detail={
                    "error_type": "LimitExceeded", "limit": exc.limit, "scope": exc.scope}))
                raise
        detail = {**base, "messages": len(texts)}
        if self.log_prompts:
            detail["prompt"] = texts
        self._begin(run_id, name, model, base)
        self._write(RunEvent(kind="llm_start", name=name, model=model, detail=detail, ts=self.now()))

    def on_chat_model_start(self, serialized: dict, messages: list[list[BaseMessage]], *, run_id: UUID,
                            metadata: dict | None = None, **kwargs: Any) -> None:
        """Chat-model call starting."""
        texts = [f"{m.type}: {m.content}" for batch in messages for m in batch]
        self._model_start(serialized, texts, run_id=run_id, metadata=metadata, kwargs=kwargs)

    def on_llm_start(self, serialized: dict, prompts: list[str], *, run_id: UUID, metadata: dict | None = None,
                     **kwargs: Any) -> None:
        """Text-model call starting."""
        self._model_start(serialized, list(prompts), run_id=run_id, metadata=metadata, kwargs=kwargs)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """Model call finished: latency, tokens, requested tool names."""
        latency, name, model, base = self._finish(run_id)
        prompt_tokens, completion_tokens = _usage(response)
        model = (response.llm_output or {}).get("model_name") or model
        detail = {**base, "tool_calls": _tool_call_names(response)}
        if self.log_prompts:
            detail["response"] = _response_text(response)
        self._write(RunEvent(kind="llm_end", name=name, model=model, latency_ms=latency, prompt_tokens=prompt_tokens,
                             completion_tokens=completion_tokens, detail=detail, ts=self.now()))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """Model call failed: the exception type only (messages may echo keys or data)."""
        latency, name, model, _ = self._finish(run_id)
        self._write(RunEvent(kind="llm_error", name=name, model=model, latency_ms=latency, ts=self.now(),
                             detail={"error_type": type(error).__name__}))

    # --- tools ------------------------------------------------------------------------------------------

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: UUID, inputs: dict | None = None,
                      **kwargs: Any) -> None:
        """Tool call starting: name and argument keys only."""
        name = (serialized or {}).get("name") or kwargs.get("name")
        self._begin(run_id, name, None, {})
        self._write(RunEvent(kind="tool_start", name=name, ts=self.now(),
                             detail={"arg_keys": _arg_keys(input_str, inputs)}))

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """Tool call finished: latency and whether it returned an error message (output not stored)."""
        latency, name, _, _ = self._finish(run_id)
        text = _output_text(output)
        self._write(RunEvent(kind="tool_end", name=name, latency_ms=latency, ts=self.now(),
                             detail={"ok": not text.startswith("error"), "output_chars": len(text)}))

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """Tool call raised: the exception type only."""
        latency, name, _, _ = self._finish(run_id)
        self._write(RunEvent(kind="tool_error", name=name, latency_ms=latency, ts=self.now(),
                             detail={"error_type": type(error).__name__}))

    # --- retrieval --------------------------------------------------------------------------------------

    def on_retriever_start(self, serialized: dict, query: str, *, run_id: UUID, **kwargs: Any) -> None:
        """Retrieval starting (no event yet; the query is kept only in debug mode)."""
        name = (serialized or {}).get("name") or kwargs.get("name")
        self._begin(run_id, name, None, {"query": query} if self.log_prompts else {})

    def on_retriever_end(self, documents: Sequence[Document], *, run_id: UUID, **kwargs: Any) -> None:
        """Retrieval finished: document ids, scores and methods (never the text)."""
        latency, name, _, base = self._finish(run_id)
        detail = {**base, "doc_ids": [d.metadata.get("doc_id") for d in documents],
                  "scores": [d.metadata.get("score") for d in documents],
                  "methods": sorted({str(d.metadata.get("method")) for d in documents if d.metadata.get("method")})}
        self._write(RunEvent(kind="retrieval", name=name, latency_ms=latency, detail=detail, ts=self.now()))

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """Retrieval failed: the exception type only."""
        latency, name, _, _ = self._finish(run_id)
        self._write(RunEvent(kind="retrieval", name=name, latency_ms=latency, ts=self.now(),
                             detail={"error_type": type(error).__name__}))
