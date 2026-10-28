"""Human review (interrupt #2, AC 39) and the export hand-off.

    drafting --(verified)--> human_review --route_after_review--> export -> END      (approve)
                                  |                         '--> END               (reject: status "rejected")
                                  '--(request_changes + comment)--> drafting       (review-revise, bounded)

`human_review` pauses with the verified draft, its verification and its citations. The resume value is
validated with `ReviewPayload` (approve / reject / request_changes with a comment, optionally an edited draft
from the UI); an invalid value interrupts again with the error and never raises. An edited draft is verified
again (numbers and the unknown-column guard) before it is accepted: if it fails, the reviewer sees the
feedback and decides again, so nothing unverified is ever exported. Change requests are bounded by
MAX_REVISIONS rounds; at the limit the reviewer must approve or reject.

`export` is the M7 stub: it records `ExportStatus(pending)` with the requested formats; the PDF/Excel
exporters (M9) pick it up.
"""
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from statbrief.domain.report import ExportStatus, ReportDraft
from statbrief.domain.review import ReviewDecision, VerificationResult
from statbrief.graph.deps import GraphDeps
from statbrief.graph.drafting import undocumented_columns, verify
from statbrief.graph.observations import load_store, view_from_state
from statbrief.graph.state import ReportState


class ReviewPayload(BaseModel):
    """The reviewer's answer: a decision, a comment (required for changes), optionally an edited draft."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject", "request_changes"]
    comment: str | None = None
    edited_draft: ReportDraft | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ReviewPayload":
        """A change request needs a comment; a rejection carries no edited draft."""
        if self.action == "request_changes" and not (self.comment or "").strip():
            raise ValueError("a change request needs a comment")
        if self.action == "reject" and self.edited_draft is not None:
            raise ValueError("a rejected report takes no edited draft")
        return self


def _error_text(exc: Exception) -> str:
    """A resume error for the reviewer: validation locations and reasons, or the message."""
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors())
    return str(exc)


def _review_value(state: ReportState, draft: ReportDraft, verification: VerificationResult | None,
                  error: str | None, deps: GraphDeps) -> dict[str, Any]:
    """The JSON-ready interrupt value shown to the reviewer (draft, verification, citations, passages)."""
    passages = [{"doc_id": p.doc_id, "citation": p.citation(), "section": p.section, "page": p.page,
                 "source_type": p.source_type} for p in state.get("retrieved") or []]
    return {"kind": "review", "draft": draft.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json") if verification is not None else None,
            "citations": [c.model_dump(mode="json") for c in draft.citations], "passages": passages,
            "review_round": state.get("review_round") or 0, "max_rounds": deps.settings.max_revisions,
            "error": error}


def human_review(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Interrupt #2: approve, reject or request changes; edited drafts are re-verified first."""
    draft, verification = state["draft"], state.get("verification")
    rounds = state.get("review_round") or 0
    error: str | None = None
    while True:
        answer = interrupt(_review_value(state, draft, verification, error, deps))
        try:
            payload = ReviewPayload.model_validate(answer)
        except ValidationError as exc:
            error = _error_text(exc)
            continue
        if payload.action == "request_changes" and rounds >= deps.settings.max_revisions:
            error = (f"The limit of {deps.settings.max_revisions} change requests is reached: "
                     "approve or reject the draft.")
            continue
        if payload.edited_draft is not None:
            result = verify(payload.edited_draft, load_store(deps, view_from_state(state)),
                            undocumented_columns(state))
            draft, verification = payload.edited_draft, result
            if not result.ok:
                error = "The edited draft failed verification; correct it and decide again."
                continue
        break
    decision = ReviewDecision(action=payload.action, comment=(payload.comment or "").strip() or None)
    out: dict[str, Any] = {"review": decision, "draft": draft, "verification": verification}
    if decision.action == "approve":
        out["status"] = "approved"
    elif decision.action == "reject":
        out["status"] = "rejected"
    else:
        out.update({"review_round": rounds + 1, "status": "drafting"})
    return out


def route_after_review(state: ReportState) -> Literal["drafting", "export", "__end__"]:
    """approve -> export; request_changes -> drafting (review-revise); reject -> END."""
    review = state.get("review")
    if review is not None and review.action == "approve":
        return "export"
    if review is not None and review.action == "request_changes":
        return "drafting"
    return END


def export(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """M7 stub: record that the approved report awaits export in the requested formats (M9 builds them)."""
    return {"export": ExportStatus(state="pending", formats=list(state["params"].formats), requested_at=deps.now()),
            "status": "approved"}
