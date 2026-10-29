"""Drafting sub-graph (AC 26-30, 34): draft -> verify -> bounded revise loop, with bounded parse retries.

    START -> draft --route_after_generation--> verify_claims --route_after_verify--> END ("human_review")
               ^  '-- "retry" (unparseable, retries left)        |   ^                 END ("fail_safe")
               '-----------'                                     v   |
                                    revise --route_after_generation--'  (revise retries itself the same way)

`draft` writes the first draft, or (after the reviewer asked for changes) a review-revision from the
reviewer's comment. `revise` rewrites a draft from the verifier's feedback. Both build the prompt context
from the state and the database (metrics, significance flags, sources, passages, column descriptions and the
undocumented columns, AC 34-35) and call the model through the controlled tools when the model supports tool
calling (ENABLE_DRAFT_TOOLS; the draft is submitted as a `ReportDraft` tool call, `run_tool_loop`), else
through the M6 structured-output chains. A new tool list (one shared row budget) is built per call.

`verify_claims` recomputes every claim (M4 `verify_draft`) and applies the unknown-column guard (M6
`check_unknown_columns`) and checks citations (every `Citation.ref_id` must be a retrieved passage or a
source of this report, and every section citation id must be among the draft's citations); violations fail
verification and are appended to the feedback.

Bounds: an unparseable reply is retried while `parse_retry_count <= MAX_PARSE_RETRIES` (consecutive
failures); a failed verification is revised while `revision_count < MAX_REVISIONS`. The sub-graph then ends;
the parent routes on the outcome (`route_after_drafting`): verified -> human review, otherwise the safe
failure, which keeps the last draft and its verification feedback ("needs manual attention", AC 27).
The counters start at zero on every entry (first draft, or each reviewer round).
"""
from collections.abc import Callable, Sequence
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.runnables.config import merge_configs
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from statbrief.analytics.store import ObservationStore
from statbrief.analytics.verify import verify_draft
from statbrief.config import Settings
from statbrief.domain.metrics import MetricResult
from statbrief.domain.observation import Observation
from statbrief.domain.report import ReportDraft
from statbrief.domain.review import VerificationResult
from statbrief.graph.deps import GraphDeps, node
from statbrief.graph.observations import load_effective, view_from_state
from statbrief.graph.state import DraftingInput, DraftingOutput, ReportState, WorkflowError
from statbrief.llm.chains import (DRAFT_SCHEMA_NAME, DraftAttempt, attempt_from_tool_call, build_draft_chain,
                                  build_review_revise_chain, build_revise_chain, build_structured_from_messages)
from statbrief.llm.guards import check_unknown_columns
from statbrief.llm.prompts import (DRAFT_PROMPT, REVIEW_REVISE_PROMPT, REVISE_PROMPT, render_context,
                                   review_revise_inputs, revise_inputs, source_infos)
from statbrief.llm.tools import build_tools, run_tool_loop
from statbrief.sources.results import SeriesMeta

Mode = Literal["draft", "revise", "review_revise"]
_PROMPTS: dict[str, tuple[ChatPromptTemplate, Callable[[BaseChatModel, Settings], Runnable]]] = {
    "draft": (DRAFT_PROMPT, build_draft_chain),
    "revise": (REVISE_PROMPT, build_revise_chain),
    "review_revise": (REVIEW_REVISE_PROMPT, build_review_revise_chain),
}
GUARD_HEADER = "Unknown-column rules (fix every item):"


class ModelUnavailable(RuntimeError):
    """GraphDeps carries no chat model (a wiring error: the runner always builds one)."""


def undocumented_columns(state: ReportState) -> set[str]:
    """Columns of the report's (non-dropped) files that have no documented meaning (validation warnings)."""
    dropped = set(state.get("dropped_sources") or [])
    described = {c for fid, m in (state.get("mappings") or {}).items() if fid not in dropped
                 for c in m.column_descriptions}
    return {i.column for fid, report in (state.get("validation") or {}).items() if fid not in dropped
            for i in report.issues if i.code == "undocumented_column" and i.column and i.column not in described}


def column_descriptions(state: ReportState) -> dict[str, str]:
    """User-supplied column meanings of the report's (non-dropped) files."""
    dropped = set(state.get("dropped_sources") or [])
    out: dict[str, str] = {}
    for fid in sorted(state.get("mappings") or {}):
        if fid not in dropped:
            out.update(state["mappings"][fid].column_descriptions)
    return out


def _period_text(state: ReportState, metrics: Sequence[MetricResult]) -> str:
    """The reporting period: the parameters' window, completed from the metric periods."""
    params = state["params"]
    periods = sorted({m.period for m in metrics} | {m.base_period for m in metrics if m.base_period})
    first = params.period_from or (periods[0] if periods else "")
    last = params.period_to or (periods[-1] if periods else "")
    return f"{first} to {last}" if first and last else first or last


def prompt_context(state: ReportState, observations: Sequence[Observation], deps: GraphDeps) -> dict[str, str]:
    """DRAFT_PROMPT variables for this report (derived data only, capped and ordered by Settings)."""
    metrics = list(state.get("metrics") or [])
    regions = list(state["params"].regions) or sorted({m.region for m in metrics})
    return render_context(regions=regions, period=_period_text(state, metrics), metrics=metrics,
                          significant=state.get("significant") or [], sources=source_infos(observations),
                          passages=state.get("retrieved") or [], column_descriptions=column_descriptions(state),
                          undocumented_columns=undocumented_columns(state), settings=deps.settings)


def supports_tools(model: BaseChatModel) -> bool:
    """Whether the model implements tool calling (bind_tools)."""
    try:
        model.bind_tools([])
    except NotImplementedError:
        return False
    return True


def _series_meta(state: ReportState) -> dict[str, SeriesMeta]:
    """Source metadata of the fetched API series by indicator code (for get_indicator_metadata)."""
    dropped = set(state.get("dropped_sources") or [])
    return {m.indicator: SeriesMeta(m.series_label, m.unit_label, m.last_update)
            for m in state.get("source_metadata") or []
            if m.kind == "api" and m.status == "ok" and m.indicator and m.source_id not in dropped}


def generate(mode: Mode, variables: dict[str, str], state: ReportState, store: ObservationStore, deps: GraphDeps,
             config: RunnableConfig) -> DraftAttempt:
    """One model attempt at a ReportDraft (tool loop when possible, else the structured chain); never raises
    on unparseable output (DraftAttempt.parse_error is set instead)."""
    model = deps.chat_model
    if model is None:
        raise ModelUnavailable("no chat model configured")
    prompt, chain_builder = _PROMPTS[mode]
    if not (deps.settings.enable_draft_tools and supports_tools(model)):
        return chain_builder(model, deps.settings).invoke(variables, config=config)
    tools = build_tools(store, state.get("metrics") or [], deps.catalog, state["user_id"], deps.settings,
                        series_meta=_series_meta(state))  # fresh row budget for this loop
    cfg = merge_configs(config, {"metadata": {"prompt_template": mode,
                                              "prompt_variables": sorted(prompt.input_variables)}})
    messages = run_tool_loop(model, tools, prompt.format_messages(**variables), deps.settings, cfg,
                             final_schema=ReportDraft)
    last = messages[-1]
    if isinstance(last, AIMessage) and any(c.get("name") == DRAFT_SCHEMA_NAME
                                           for c in [*last.tool_calls, *last.invalid_tool_calls]):
        return attempt_from_tool_call(last)
    # The model answered in prose or used up its tool steps: ask once more, forcing the ReportDraft schema.
    return build_structured_from_messages(model, deps.settings).invoke(messages, config=cfg)


def _after_attempt(attempt: DraftAttempt, state: ReportState, node_name: str) -> dict[str, Any]:
    """State update for one attempt: a parsed draft replaces the old one (its verification is cleared);
    an unparseable one only counts as a parse failure (draft and verification stay consistent)."""
    out: dict[str, Any] = {"draft_history": [attempt], "status": "drafting"}
    if attempt.draft is None:
        out["parse_retry_count"] = (state.get("parse_retry_count") or 0) + 1
        out["errors"] = [WorkflowError(node=node_name, kind="parse_error", retryable=True,
                                       message=attempt.parse_error or "unparseable model output")]
        return out
    out.update({"draft": attempt.draft, "verification": None, "parse_retry_count": 0})
    return out


def _report_data(state: ReportState, deps: GraphDeps) -> tuple[list[Observation], ObservationStore]:
    """The report's resolved observations (from the database) and a store over them."""
    obs = load_effective(deps, view_from_state(state))
    return obs, ObservationStore(obs)


def draft(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Write the first draft, or revise the current one from the reviewer's comment (request_changes)."""
    obs, store = _report_data(state, deps)
    context = prompt_context(state, obs, deps)
    review, previous = state.get("review"), state.get("draft")
    if review is not None and review.action == "request_changes" and previous is not None:
        attempt = generate("review_revise", review_revise_inputs(context, previous, review.comment or ""),
                           state, store, deps, config)
        out = _after_attempt(attempt, state, "draft")
        if attempt.draft is not None:
            out["review"] = None  # the comment has been applied
    else:
        out = _after_attempt(generate("draft", context, state, store, deps, config), state, "draft")
    out["revision_count"] = 0  # a new entry into drafting starts a fresh revision budget
    return out


def revise(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Rewrite the draft from the verifier's feedback; a parsed revision counts against MAX_REVISIONS."""
    obs, store = _report_data(state, deps)
    variables = revise_inputs(prompt_context(state, obs, deps), state["draft"], state["verification"].feedback)
    attempt = generate("revise", variables, state, store, deps, config)
    out = _after_attempt(attempt, state, "revise")
    if attempt.draft is not None:
        out["revision_count"] = (state.get("revision_count") or 0) + 1
    return out


CITATION_HEADER = "Citation rules (fix every item):"


def citable_ids(state: ReportState, observations: Sequence[Observation]) -> set[str]:
    """Ref ids a draft may cite: retrieved passages (doc_id) and the report's data sources (file / series id)."""
    return {p.doc_id for p in state.get("retrieved") or []} | {s.ref_id for s in source_infos(observations)}


def citation_problems(draft_: ReportDraft, allowed: set[str]) -> list[str]:
    """Citations naming no retrieved passage or report source, and section citation ids that cite nothing."""
    problems = [f"citation '{c.ref_id}' is not a retrieved passage or a source of this report: use a listed "
                "ref_id" for c in draft_.citations if c.ref_id not in allowed]
    cited = {c.ref_id for c in draft_.citations}
    problems += [f"section {i} lists citation id '{ref}' that is not in citations"
                 for i, sec in enumerate(draft_.sections, 1) for ref in sec.citation_ids if ref not in cited]
    return problems


def verify(draft_: ReportDraft, store: ObservationStore, undocumented: set[str],
           allowed_citations: set[str]) -> VerificationResult:
    """Deterministic verification plus the unknown-column guard and the citation check (violations fail it
    and join the feedback)."""
    result = verify_draft(draft_, store)
    violations = check_unknown_columns(draft_, undocumented, store)
    bad_citations = citation_problems(draft_, allowed_citations)
    if not violations and not bad_citations:
        return result
    parts = [result.feedback]
    if violations:
        parts += [GUARD_HEADER, *(f"- {v}" for v in violations)]
    if bad_citations:
        parts += [CITATION_HEADER, *(f"- {v}" for v in bad_citations)]
    return VerificationResult(ok=False, checks=result.checks, orphan_numbers=result.orphan_numbers,
                              feedback="\n".join(p for p in parts if p))


def verify_claims(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Verify the current draft against the report's observations and sources."""
    obs, store = _report_data(state, deps)
    return {"verification": verify(state["draft"], store, undocumented_columns(state), citable_ids(state, obs))}


def route_after_generation(state: ReportState, settings: Settings) -> Literal["verify", "retry", "fail_safe"]:
    """Parsed -> verify; unparseable -> retry while MAX_PARSE_RETRIES allows, else the safe failure."""
    history = state.get("draft_history") or []
    if history and history[-1].draft is not None:
        return "verify"
    return "retry" if (state.get("parse_retry_count") or 0) <= settings.max_parse_retries else "fail_safe"


def route_after_verify(state: ReportState, settings: Settings) -> Literal["revise", "human_review", "fail_safe"]:
    """Verified -> human review; failed -> revise while MAX_REVISIONS allows, else the safe failure."""
    verification = state.get("verification")
    if verification is not None and verification.ok:
        return "human_review"
    return "fail_safe" if (state.get("revision_count") or 0) >= settings.max_revisions else "revise"


def route_after_drafting(state: ReportState, settings: Settings) -> Literal["human_review", "fail_safe"]:
    """Parent edge after the drafting sub-graph: only a parsed, verified draft goes to the reviewer."""
    verification = state.get("verification")
    parsed = (state.get("parse_retry_count") or 0) == 0
    return "human_review" if parsed and verification is not None and verification.ok else "fail_safe"


def build_drafting_graph(deps: GraphDeps) -> CompiledStateGraph:
    """The compiled drafting sub-graph."""
    s = deps.settings
    g = StateGraph(ReportState, input_schema=DraftingInput, output_schema=DraftingOutput)
    g.add_node("draft", node(draft, deps))
    g.add_node("verify_claims", node(verify_claims, deps))
    g.add_node("revise", node(revise, deps))
    g.add_edge(START, "draft")
    g.add_conditional_edges("draft", lambda st: route_after_generation(st, s),
                            {"verify": "verify_claims", "retry": "draft", "fail_safe": END})
    g.add_conditional_edges("revise", lambda st: route_after_generation(st, s),
                            {"verify": "verify_claims", "retry": "revise", "fail_safe": END})
    g.add_conditional_edges("verify_claims", lambda st: route_after_verify(st, s),
                            {"revise": "revise", "human_review": END, "fail_safe": END})
    return g.compile(name="drafting")
