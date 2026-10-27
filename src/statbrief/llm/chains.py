"""LCEL chains: context preparation, drafting, revision and dataset interpretation (AC 29–30, 32).

- `build_context_chain`: ``RunnableParallel(passages = query -> retriever, metrics = format_metrics,
  static = static_context) | RunnableLambda(merge)`` — retrieval and metric formatting run side by side.
- `build_draft_chain` / `build_revise_chain` / `build_review_revise_chain`:
  ``PROMPT | model.with_structured_output(ReportDraft, include_raw=True) | RunnableLambda(check_parsed)``.
  ``include_raw=True`` keeps the raw AIMessage, so a malformed or incomplete reply becomes a `DraftAttempt`
  with `parse_error` set (the graph retries it, AC 30) instead of an exception.
- `PassageRetriever`: the M5 user-scoped `retrieve()` as a LangChain `BaseRetriever`, so retrieval runs
  are traced by callbacks (`on_retriever_end` logs document ids and scores).
"""
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from operator import itemgetter
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda, RunnableParallel
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from statbrief.config import Settings
from statbrief.domain.report import ReportDraft
from statbrief.llm.prompts import (
    DRAFT_PROMPT, INTERPRET_DATASET_PROMPT, REVIEW_REVISE_PROMPT, REVISE_PROMPT, format_metrics,
    format_passages, static_context,
)
from statbrief.rag.retriever import RetrievedPassage, retrieve

USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


@dataclass(frozen=True)
class DraftAttempt:
    """One structured-output attempt: the parsed draft, or why parsing failed, plus raw text and token usage."""

    draft: ReportDraft | None
    raw_text: str
    parse_error: str | None
    usage: dict[str, int] = field(default_factory=dict)


def passage_to_document(p: RetrievedPassage) -> Document:
    """A retrieved passage as a LangChain Document (score and method kept in metadata)."""
    return Document(page_content=p.text, metadata={
        "doc_id": p.doc_id, "title": p.title, "issuer": p.issuer, "section": p.section, "page": p.page,
        "score": p.score, "method": p.method, "source_file": p.source_file, "source_type": p.source_type})


def document_to_passage(doc: Document) -> RetrievedPassage:
    """Inverse of `passage_to_document`."""
    return RetrievedPassage.from_document(doc, doc.metadata.get("score", 0.0), doc.metadata.get("method", ""))


class PassageRetriever(BaseRetriever):
    """LangChain retriever over a passage search function (normally M5's user-scoped `retrieve`)."""

    search_fn: Callable[[str], Sequence[RetrievedPassage]]

    @classmethod
    def for_user(cls, pool: ConnectionPool, user_id: str, settings: Settings,
                 embeddings: Embeddings | None) -> "PassageRetriever":
        """Retriever over shared documents plus `user_id`'s own uploads (RAG_TOP_K passages)."""
        return cls(search_fn=lambda query: retrieve(pool, query, user_id, settings, embeddings))

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        """Run the search and wrap each passage as a Document."""
        return [passage_to_document(p) for p in self.search_fn(query)]


def build_context_chain(retriever: BaseRetriever, settings: Settings) -> Runnable[dict, dict[str, str]]:
    """Request dict -> DRAFT_PROMPT variables.

    Input keys: query, regions, period, metrics, significant, sources, column_descriptions,
    undocumented_columns. Retrieval and metric formatting run in parallel (RunnableParallel).
    """
    def _passages(docs: list[Document]) -> str:
        """Format the retrieved Documents as prompt passages."""
        return format_passages([document_to_passage(d) for d in docs], settings)

    def _metrics(req: dict) -> dict[str, str]:
        """Format the metrics table and significance flags."""
        return format_metrics(req["metrics"], req["significant"], settings)

    def _static(req: dict) -> dict[str, str]:
        """Format regions, period, sources and columns."""
        return static_context(regions=req["regions"], period=req["period"], sources=req["sources"],
                              column_descriptions=req["column_descriptions"],
                              undocumented_columns=req["undocumented_columns"])

    def _merge(parts: dict) -> dict[str, str]:
        """Combine the parallel branches into one variables dict."""
        return {**parts["static"], **parts["metrics"], "passages": parts["passages"]}

    parallel = RunnableParallel(
        passages=itemgetter("query") | retriever | RunnableLambda(_passages),
        metrics=RunnableLambda(_metrics),
        static=RunnableLambda(_static),
    )
    return (parallel | RunnableLambda(_merge)).with_config(run_name="context_chain")


def _raw_text(raw: BaseMessage | None) -> str:
    """The reply's text, else its tool-call arguments (JSON), else malformed argument text."""
    if raw is None:
        return ""
    if isinstance(raw.content, str) and raw.content:
        return raw.content
    if isinstance(raw, AIMessage):
        if raw.tool_calls:
            return json.dumps(raw.tool_calls[0]["args"], ensure_ascii=False)
        if raw.invalid_tool_calls:
            return str(raw.invalid_tool_calls[0].get("args") or "")
    return str(raw.content or "")


def _usage(raw: BaseMessage | None) -> dict[str, int]:
    """Token usage of the reply (input/output/total) when the provider reported it."""
    meta = getattr(raw, "usage_metadata", None) or {}
    return {k: int(meta[k]) for k in USAGE_KEYS if meta.get(k) is not None}


def _describe_error(err: BaseException) -> str:
    """A parse error as feedback; validation errors list location and reason only (never the input values)."""
    if isinstance(err, ValidationError):
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in err.errors())
        return f"output does not match the ReportDraft schema: {problems}"
    return f"{type(err).__name__}: {err}"


def check_parsed(output: Mapping[str, Any]) -> DraftAttempt:
    """Turn `with_structured_output(include_raw=True)` output into a DraftAttempt; never raises."""
    raw = output.get("raw")
    text, usage = _raw_text(raw), _usage(raw)
    parsed, err = output.get("parsed"), output.get("parsing_error")
    if err is not None:
        return DraftAttempt(None, text, _describe_error(err), usage)
    if isinstance(parsed, ReportDraft):
        return DraftAttempt(parsed, text, None, usage)
    if isinstance(raw, AIMessage) and raw.invalid_tool_calls:
        detail = raw.invalid_tool_calls[0].get("error") or "invalid arguments"
        return DraftAttempt(None, text, f"malformed JSON in the structured output ({detail})", usage)
    return DraftAttempt(None, text, "no structured output: the model did not return a ReportDraft", usage)


def _structured_chain(prompt: ChatPromptTemplate, model: BaseChatModel, settings: Settings,
                      name: str) -> Runnable[dict, DraftAttempt]:
    """PROMPT | structured model (raw kept) | check_parsed, tagged with the template name for callbacks."""
    structured = model.with_structured_output(ReportDraft, include_raw=True,
                                              method=settings.llm_structured_output_method)
    chain = prompt | structured | RunnableLambda(check_parsed)
    return chain.with_config(run_name=f"{name}_chain", metadata={
        "prompt_template": name, "prompt_variables": sorted(prompt.input_variables)})


def build_draft_chain(model: BaseChatModel, settings: Settings) -> Runnable[dict, DraftAttempt]:
    """DRAFT_PROMPT variables -> DraftAttempt."""
    return _structured_chain(DRAFT_PROMPT, model, settings, "draft")


def build_revise_chain(model: BaseChatModel, settings: Settings) -> Runnable[dict, DraftAttempt]:
    """REVISE_PROMPT variables (context + previous_draft + feedback) -> DraftAttempt."""
    return _structured_chain(REVISE_PROMPT, model, settings, "revise")


def build_review_revise_chain(model: BaseChatModel, settings: Settings) -> Runnable[dict, DraftAttempt]:
    """REVIEW_REVISE_PROMPT variables (context + previous_draft + reviewer_comment) -> DraftAttempt."""
    return _structured_chain(REVIEW_REVISE_PROMPT, model, settings, "review_revise")


def build_interpret_chain(model: BaseChatModel) -> Runnable[dict, str]:
    """INTERPRET_DATASET_PROMPT variables -> plain-text dataset description."""
    chain = INTERPRET_DATASET_PROMPT | model | StrOutputParser()
    return chain.with_config(run_name="interpret_dataset_chain", metadata={
        "prompt_template": "interpret_dataset",
        "prompt_variables": sorted(INTERPRET_DATASET_PROMPT.input_variables)})
