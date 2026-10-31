"""Controlled tools the model may call (AC 33, 35), and a bounded tool-calling loop.

`build_tools` closes over ONE report's `ObservationStore` (the requesting user's data only), its computed
metrics and the indicator catalog, so a tool can never reach another user's data: there is no argument
that selects a user or a report. Every tool has a Pydantic `args_schema` (extra arguments forbidden);
invalid arguments come back to the model as an "error: invalid arguments ..." message it can correct,
never as an exception. Results are compact JSON with selected fields only (AC 35): `query_observations`
returns at most MAX_TOOL_ROWS rows per call and at most MAX_TOOL_ROWS_PER_TURN rows across all calls of
one tool list (`RowBudget`; build the tools once per tool loop), and `compute_metric` recomputes
deterministically with M4 — the model never does arithmetic. An unexpected exception inside a tool is
returned as "error: <ExceptionType>" (the message is not echoed), so it never escapes into the graph.
"""
import functools
import json
import threading
from collections.abc import Callable
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from statbrief.analytics.store import MetricUnavailable, ObservationStore, compute_metric
from statbrief.config import Settings
from statbrief.domain.metrics import MetricResult, period_frequency
from statbrief.domain.observation import ApiProvenance, Observation
from statbrief.sources.catalog import IndicatorSpec
from statbrief.sources.results import SeriesMeta

TOOL_NAMES = ("get_indicator_metadata", "list_available_metrics", "query_observations", "compute_metric")
_CODE_MAX = 200  # longest indicator/region code or metric id accepted (protocol bound, not a tunable)


def _code(description: str) -> Any:
    """A non-blank code field."""
    return Field(min_length=1, max_length=_CODE_MAX, description=description)


class _Args(BaseModel):
    """Base for tool argument schemas: unknown arguments are rejected."""

    model_config = ConfigDict(extra="forbid")


class IndicatorMetadataArgs(_Args):
    """Arguments of get_indicator_metadata."""

    indicator_code: str = _code("Indicator code, e.g. HICP_YOY")


class ListMetricsArgs(_Args):
    """Arguments of list_available_metrics (both filters optional)."""

    indicator_code: str | None = Field(None, min_length=1, max_length=_CODE_MAX, description="Indicator code")
    region: str | None = Field(None, min_length=1, max_length=_CODE_MAX, description="Region code, e.g. RO")


class QueryObservationsArgs(_Args):
    """Arguments of query_observations; periods are canonical (2024, 2024-Q1, 2024-03) and inclusive."""

    indicator_code: str = _code("Indicator code")
    region: str = _code("Region code")
    period_from: str | None = Field(None, description="First period (inclusive), e.g. 2023-01")
    period_to: str | None = Field(None, description="Last period (inclusive), e.g. 2024-01")
    limit: int | None = Field(None, ge=1, description="Maximum rows (capped by the server)")

    @field_validator("period_from", "period_to")
    @classmethod
    def _canonical(cls, v: str | None) -> str | None:
        """Periods must use a canonical format."""
        if v is not None:
            period_frequency(v)  # ValueError for a non-canonical period
        return v

    @model_validator(mode="after")
    def _window(self) -> "QueryObservationsArgs":
        """Both bounds share one frequency and are in order."""
        if self.period_from and self.period_to:
            if period_frequency(self.period_from) != period_frequency(self.period_to):
                raise ValueError("period_from and period_to must have the same frequency")
            if self.period_from > self.period_to:
                raise ValueError("period_from must not be after period_to")
        return self


class ComputeMetricArgs(_Args):
    """Arguments of compute_metric."""

    metric_id: str = _code("Metric id, e.g. pp_change:HICP_YOY:RO:2024-03:2023-03")


def validation_error_message(err: ValidationError) -> str:
    """Tool-argument errors for the model: location and reason only (the submitted values are not echoed)."""
    problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in err.errors())
    return f"error: invalid arguments: {problems}"


class RowBudget:
    """Total observation rows the tools of one tool loop may still return (MAX_TOOL_ROWS_PER_TURN)."""

    def __init__(self, limit: int) -> None:
        """Start with `limit` rows."""
        self.remaining = limit
        self._lock = threading.Lock()

    def take(self, wanted: int) -> int:
        """Grant up to `wanted` rows and deduct them; 0 when the budget is spent."""
        with self._lock:
            granted = max(0, min(wanted, self.remaining))
            self.remaining -= granted
            return granted


def _safe(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn an unexpected exception inside a tool into an error message naming only its type."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        """Run the tool body."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # defence in depth: tool failures must reach the model, not the graph
            return f"error: {type(exc).__name__}"
    return wrapper


def _json(obj: Any) -> str:
    """Compact, deterministic JSON."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source(o: Observation) -> str:
    """Short source label of an observation (file name or API series id)."""
    p = o.provenance
    return p.series_id if isinstance(p, ApiProvenance) else p.filename


def _metric_row(m: MetricResult) -> dict[str, Any]:
    """The fields of a metric the model needs to cite it."""
    return {"metric_id": m.metric_id, "kind": m.kind, "period": m.period, "base_period": m.base_period,
            "value": m.value, "unit": m.unit, "unit_multiplier": m.unit_multiplier,
            "observation_ids": m.observation_ids}


def _spec_dict(spec: IndicatorSpec) -> dict[str, Any]:
    """Catalog entry as JSON-ready data."""
    return {"code": spec.code, "label": spec.label, "source": spec.source, "dataset": spec.dataset,
            "filters": dict(spec.filters), "unit": spec.unit, "unit_multiplier": spec.unit_multiplier,
            "frequency": spec.frequency, "is_rate": spec.is_rate, "curated": spec.curated}


def build_tools(store: ObservationStore, metrics: Sequence[MetricResult], catalog: Mapping[str, IndicatorSpec],
                user_id: str, settings: Settings, *,
                series_meta: Mapping[str, SeriesMeta] | None = None,
                row_budget: RowBudget | None = None) -> list[BaseTool]:
    """The four controlled tools over one report's data (the store must hold only `user_id`'s report).

    `row_budget` (default: a fresh RowBudget of MAX_TOOL_ROWS_PER_TURN) is shared by every call of these
    tools, so build the tools once per tool loop.
    """
    series_meta = series_meta or {}
    budget = row_budget or RowBudget(settings.max_tool_rows_per_turn)
    keys = store.keys
    indicators = {ind for ind, _ in keys}

    @tool("get_indicator_metadata", args_schema=IndicatorMetadataArgs)
    @_safe
    def get_indicator_metadata(indicator_code: str) -> str:
        """Describe an indicator: catalog definition (source, dataset, unit, frequency, rate or level),
        source metadata, and the regions and units present in this report's data."""
        spec = catalog.get(indicator_code)
        if spec is None and indicator_code not in indicators:
            known = ", ".join(sorted(indicators | set(catalog)))
            return f"error: unknown indicator {indicator_code!r}; known indicators: {known}"
        regions = sorted(r for ind, r in keys if ind == indicator_code)
        units = sorted({o.unit for r in regions for o in store.series(indicator_code, r)})
        meta = series_meta.get(indicator_code)
        return _json({"indicator_code": indicator_code, "catalog": _spec_dict(spec) if spec else None,
                      "series_meta": ({"label": meta.label, "unit_label": meta.unit_label,
                                       "last_update": meta.last_update} if meta else None),
                      "regions_in_data": regions, "units_in_data": units})

    @tool("list_available_metrics", args_schema=ListMetricsArgs)
    @_safe
    def list_available_metrics(indicator_code: str | None = None, region: str | None = None) -> str:
        """List the computed metrics (id, kind, period, value, unit, observation ids), optionally filtered
        by indicator and/or region. Cite these ids in claims."""
        rows = [_metric_row(m) for m in sorted(metrics, key=lambda m: m.metric_id)
                if (indicator_code is None or m.indicator == indicator_code)
                and (region is None or m.region == region)]
        if not rows and (indicator_code is not None or region is not None):
            hint = ([f"known indicators: {', '.join(sorted({m.indicator for m in metrics}))}"]
                    if indicator_code is not None else []) + (
                   [f"known regions: {', '.join(sorted({m.region for m in metrics}))}"] if region is not None else [])
            return f"error: no metrics match; {'; '.join(hint)}"
        shown = rows[:settings.max_tool_rows]
        return _json({"metrics": shown, "total": len(rows), "truncated": len(rows) > len(shown)})

    @tool("query_observations", args_schema=QueryObservationsArgs)
    @_safe
    def query_observations(indicator_code: str, region: str, period_from: str | None = None,
                           period_to: str | None = None, limit: int | None = None) -> str:
        """Return observations of one indicator and region (selected fields, bounded row count), optionally
        within a period window."""
        series = store.series(indicator_code, region)
        if not series:
            return f"error: no observations for indicator {indicator_code!r} in region {region!r}"
        bound = period_from or period_to
        freq = period_frequency(bound) if bound else None
        if freq is not None and all(o.frequency != freq for o in series):
            have = sorted({o.frequency for o in series})
            examples = ", ".join(sorted({o.period for o in series})[-1:])
            return (f"error: series {indicator_code}/{region} has frequency {'/'.join(have)}, not {freq}; "
                    f"use periods like {examples}")
        rows = [o for o in series if (freq is None or o.frequency == freq)
                and (period_from is None or o.period >= period_from) and (period_to is None or o.period <= period_to)]
        if budget.remaining <= 0:
            return "error: row budget exhausted for this turn; use compute_metric or list_available_metrics"
        cap = min(limit or settings.max_tool_rows, settings.max_tool_rows)
        shown = rows[:budget.take(min(cap, len(rows)))]
        return _json({"rows": [{"obs_id": o.obs_id, "period": o.period, "value": o.value, "unit": o.unit,
                                "unit_multiplier": o.unit_multiplier, "status_flag": o.status_flag,
                                "source": _source(o)} for o in shown],
                      "total": len(rows), "truncated": len(rows) > len(shown),
                      "row_budget_remaining": budget.remaining})

    @tool("compute_metric", args_schema=ComputeMetricArgs)
    @_safe
    def compute_metric_tool(metric_id: str) -> str:
        """Recompute one metric deterministically from its id (kind:indicator:region:period[:base]); returns
        value, unit, input observation ids and formula. Use this instead of doing arithmetic."""
        try:
            m = compute_metric(metric_id, store)
        except MetricUnavailable as exc:
            return f"error: {exc.metric_id}: {exc.reason}"
        return _json({**_metric_row(m), "formula": m.formula})

    tools = [get_indicator_metadata, list_available_metrics, query_observations, compute_metric_tool]
    for t in tools:
        t.handle_validation_error = validation_error_message
        t.metadata = {"user_id": user_id}
    return tools


def _error_message(call_id: str, name: str, text: str) -> ToolMessage:
    """A ToolMessage reporting an error to the model."""
    return ToolMessage(content=text, tool_call_id=call_id, name=name, status="error")


def _calls_final(reply: AIMessage, final: str | None) -> bool:
    """True when the reply calls the final-answer schema (well-formed or malformed)."""
    return final is not None and any(c.get("name") == final for c in [*reply.tool_calls, *reply.invalid_tool_calls])


def run_tool_loop(model: BaseChatModel, tools: Sequence[BaseTool], messages: Sequence[BaseMessage],
                  settings: Settings, config: RunnableConfig | None = None, *,
                  final_schema: type[BaseModel] | None = None) -> list[BaseMessage]:
    """Let the model call tools until it answers without tool calls, for at most LLM_MAX_TOOL_STEPS turns.

    Returns the whole conversation. Unknown tools and malformed tool-call arguments are answered with an
    error ToolMessage so the model can correct itself. If the step budget runs out, the last message is a
    ToolMessage (callers treat that as "no final answer"). With `final_schema` (a Pydantic class offered to
    the model as one more tool), a reply that calls it ends the loop unexecuted: its arguments are the final
    answer, which the caller parses (other tool calls in that same reply are not run).
    """
    final = final_schema.__name__ if final_schema is not None else None
    bound = model.bind_tools([*tools, *([final_schema] if final_schema is not None else [])])
    by_name = {t.name: t for t in tools}
    msgs = list(messages)
    for _ in range(settings.llm_max_tool_steps):
        reply = bound.invoke(msgs, config=config)
        msgs.append(reply)
        if not isinstance(reply, AIMessage) or not (reply.tool_calls or reply.invalid_tool_calls):
            break
        if _calls_final(reply, final):
            break
        for call in reply.tool_calls:
            t = by_name.get(call["name"])
            if t is None:
                msgs.append(_error_message(call["id"], call["name"],
                                           f"error: unknown tool {call['name']!r}; available: {', '.join(by_name)}"))
            else:
                msgs.append(t.invoke(call, config=config))
        for bad in reply.invalid_tool_calls:
            msgs.append(_error_message(bad.get("id") or "", bad.get("name") or "",
                                       "error: malformed tool-call arguments (not valid JSON)"))
    return msgs
