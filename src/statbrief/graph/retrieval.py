"""Retrieval sub-graph (AC 31-32): build search queries, retrieve reference passages and API metadata.

    START -> build_queries -> retrieve_context -> END

Queries: one per covered indicator (its catalog label), one per significantly changed indicator and kind
("<label> annual change"), and the commentary guidelines. Each query goes to the user-scoped retriever
(M5 `retrieve`: pgvector, or Postgres full-text search when ENABLE_VECTOR_SEARCH is off or embeddings are
unavailable). The metadata of every fetched, non-dropped API series becomes citable passages
(`rag.api_metadata`). Result lists are merged round-robin (each query and the API metadata contribute in
turn), de-duplicated by (doc_id, text) and capped at PROMPT_MAX_PASSAGES, so the outcome is deterministic.
A failing retriever is recorded as a retryable WorkflowError and drafting continues without those passages.
"""
from collections.abc import Sequence
from typing import Any

from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from statbrief.domain.metrics import parse_metric_id
from statbrief.graph.deps import GraphDeps, node
from statbrief.graph.state import RetrievalInput, RetrievalOutput, RetrievalState, SourceMeta, WorkflowError
from statbrief.llm.chains import document_to_passage
from statbrief.rag.api_metadata import SOURCE_TYPE, metadata_documents
from statbrief.rag.retriever import RetrievedPassage
from statbrief.sources.results import SeriesMeta

GUIDELINES_QUERY = "guidelines for statistical commentary"
# How a significant metric kind is named in a search query.
KIND_WORDS = {"yoy": "annual change", "pp_change": "change in percentage points", "mom": "monthly change",
              "qoq": "quarterly change", "abs_change": "change", "pct_change": "percent change", "latest": "level"}
_API_METADATA_SCORE = 1.0  # API metadata is not ranked by similarity; it is cited by URL


def _label(code: str, deps: GraphDeps) -> str:
    """Catalog label of an indicator, else its code."""
    spec = deps.catalog.get(code)
    return spec.label if spec is not None else code


def build_queries(state: RetrievalState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Search queries for the covered indicators, the significant changes and the commentary guidelines."""
    queries: list[str] = []
    significant = [parse_metric_id(s.metric_id) for s in state.get("significant") or []]
    indicators = list(state["params"].indicators) or sorted({p.indicator for p in significant})
    for code in indicators:
        queries.append(_label(code, deps))
    for p in significant:
        queries.append(f"{_label(p.indicator, deps)} {KIND_WORDS.get(p.kind, 'change')}")
    queries.append(GUIDELINES_QUERY)
    return {"queries": list(dict.fromkeys(queries))}


def _search(query: str, deps: GraphDeps, config: RunnableConfig) -> list[RetrievedPassage]:
    """Run one query (a LangChain retriever is invoked with the run config, so callbacks trace it)."""
    if isinstance(deps.retriever, BaseRetriever):
        return [document_to_passage(d) for d in deps.retriever.invoke(query, config=config)]
    return list(deps.retriever(query))


def api_metadata_passages(metas: Sequence[SourceMeta], dropped: set[str]) -> list[RetrievedPassage]:
    """Citable metadata passages of the fetched API series (failed and dropped sources excluded)."""
    out: list[RetrievedPassage] = []
    for m in metas:
        if m.kind != "api" or m.status != "ok" or m.source_id in dropped or not m.query_url:
            continue
        meta = SeriesMeta(m.series_label or m.label, m.unit_label, m.last_update)
        try:
            docs = metadata_documents(meta, m.query_url)
        except ValueError:  # not an https URL of a known API host: never cite it
            continue
        out += [RetrievedPassage.from_document(d, _API_METADATA_SCORE, SOURCE_TYPE) for d in docs]
    return out


def merge_passages(lists: Sequence[Sequence[RetrievedPassage]], cap: int) -> list[RetrievedPassage]:
    """Round-robin merge of ranked lists, de-duplicated by (doc_id, text), at most `cap` passages."""
    merged: list[RetrievedPassage] = []
    seen: set[tuple[str, str]] = set()
    for i in range(max((len(lst) for lst in lists), default=0)):
        for lst in lists:
            if i < len(lst) and (lst[i].doc_id, lst[i].text) not in seen:
                seen.add((lst[i].doc_id, lst[i].text))
                merged.append(lst[i])
    return merged[:cap]


def retrieve_context(state: RetrievalState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Retrieve passages for every query plus API metadata; a failing search is recorded, not raised."""
    lists: list[list[RetrievedPassage]] = []
    errors: list[WorkflowError] = []
    for query in state.get("queries") or []:
        try:
            lists.append(_search(query, deps, config))
        except Exception as exc:  # database or provider failure: the type only (messages may echo the query)
            errors.append(WorkflowError(node="retrieve_context", kind="retrieval_failed", retryable=True,
                                        message=f"reference search failed ({type(exc).__name__})"))
    lists.append(api_metadata_passages(state.get("source_metadata") or [], set(state.get("dropped_sources") or [])))
    return {"retrieved": merge_passages(lists, deps.settings.prompt_max_passages), "errors": errors}


def build_retrieval_graph(deps: GraphDeps) -> CompiledStateGraph:
    """The compiled retrieval sub-graph (reads params/significant/sources, returns `retrieved`)."""
    g = StateGraph(RetrievalState, input_schema=RetrievalInput, output_schema=RetrievalOutput)
    g.add_node("build_queries", node(build_queries, deps))
    g.add_node("retrieve_context", node(retrieve_context, deps))
    g.add_edge(START, "build_queries")
    g.add_edge("build_queries", "retrieve_context")
    g.add_edge("retrieve_context", END)
    return g.compile(name="retrieval")
