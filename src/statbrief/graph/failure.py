"""The safe-failure node: the run stops with status "failed" and a reason for the UI, and never raises.

Reached when validation rounds are exhausted or no usable data exists, when the model output stays
unparseable after MAX_PARSE_RETRIES, or when verification still fails after MAX_REVISIONS revisions
(AC 27: "needs manual attention", never exported). The last draft and its verification feedback stay in
the state for the UI; nothing is cleared.
"""
from typing import Any

from langchain_core.runnables import RunnableConfig

from statbrief.config import Settings
from statbrief.graph.deps import GraphDeps
from statbrief.graph.state import ReportState, WorkflowError

NEEDS_ATTENTION = "needs_manual_attention"


def failure_reason(state: ReportState, settings: Settings) -> str:
    """Why the run is failing, from the counters and results in the state (most specific first)."""
    request = state.get("needs_input")
    verification = state.get("verification")
    if (state.get("parse_retry_count") or 0) > settings.max_parse_retries:
        return (f"The model's draft could not be read after {settings.max_parse_retries + 1} attempts; "
                "the report needs manual attention.")
    if verification is not None and not verification.ok:
        return (f"The draft still failed verification after {state.get('revision_count') or 0} revisions; "
                "the report needs manual attention and will not be exported.")
    if state.get("draft_history"):
        return "No verified draft could be produced; the report needs manual attention."
    if request is not None and not request.empty:
        return (f"Data problems are still unresolved after {state.get('gate_rounds') or 0} rounds; "
                "the report needs manual attention.")
    if request is not None and request.no_data:
        return "No usable observations remain for this report; check the sources and the period window."
    return "The report needs manual attention."


def fail_safe(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Mark the run failed with a reason; keep the last draft and verification for the UI."""
    return {"status": "failed", "errors": [WorkflowError(node="fail_safe", kind=NEEDS_ATTENTION,
                                                         message=failure_reason(state, deps.settings))]}
