"""Retrieval sub-graph (M7 Task 5): queries, round-robin merge, API metadata passages, safe failures."""
from statbrief.domain.metrics import SignificantChange
from statbrief.domain.report import ReportParams
from statbrief.graph.retrieval import GUIDELINES_QUERY, build_retrieval_graph, merge_passages
from statbrief.graph.state import SourceMeta
from statbrief.rag.retriever import RetrievedPassage

PARAMS = ReportParams(mode="api", indicators=["HICP_YOY", "UNEMP_RATE"], regions=["RO"])


def _p(doc_id: str, text: str = "t", score: float = 0.5) -> RetrievedPassage:
    """One reference passage."""
    return RetrievedPassage(text=text, doc_id=doc_id, title=doc_id, issuer="ESMB", section="1", page=None,
                            score=score, method="fts", source_file=f"{doc_id}.md", source_type="reference")


def _api_meta(source_id: str = "api:HICP_YOY:RO::", status: str = "ok") -> SourceMeta:
    """Metadata of one fetched Eurostat series."""
    return SourceMeta(source_id=source_id, kind="api", label="HICP (eurostat)", status=status, indicator="HICP_YOY",
                      query_url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr",
                      series_label="HICP monthly data", unit_label="Annual rate of change", last_update="2026-10-16")


def _run(world, retriever, state_extra=None, **settings):
    """Run the retrieval sub-graph."""
    graph = build_retrieval_graph(world.deps(retriever=retriever, **settings))
    return graph.invoke({**world.base_state(PARAMS), **(state_extra or {})})


def test_queries_cover_indicators_significant_changes_and_guidelines(world):
    """One query per indicator label, per significant (indicator, kind), and the guidelines query."""
    seen: list[str] = []

    def retriever(q):
        """Record the query."""
        seen.append(q)
        return []

    sig = [SignificantChange(metric_id="pp_change:HICP_YOY:RO:2024-03:2023-03", magnitude=1.5, rule="rate_threshold")]
    out = _run(world, retriever, {"significant": sig})
    assert seen[0].startswith("HICP inflation") and seen[1].startswith("Unemployment rate")
    assert seen[2].endswith("change in percentage points") and seen[-1] == GUIDELINES_QUERY and len(seen) == 4
    assert out["retrieved"] == []


def test_round_robin_merge_dedupes_and_caps():
    """Each list contributes in turn; duplicates (doc_id, text) are dropped; the cap applies."""
    a, b = [_p("a1"), _p("a2"), _p("x")], [_p("x"), _p("b2")]
    assert [p.doc_id for p in merge_passages([a, b], 10)] == ["a1", "x", "a2", "b2"]
    assert [p.doc_id for p in merge_passages([a, b], 2)] == ["a1", "x"]
    assert merge_passages([], 3) == []


def test_api_metadata_is_citable_and_dropped_sources_are_not(world):
    """Fetched API series become 'api-eurostat-…' passages; failed or dropped ones do not."""
    out = _run(world, lambda q: [_p("esmb-guide")],
               {"source_metadata": [_api_meta(), _api_meta("api:WB_CPI:RO::", status="failed"),
                                    _api_meta("api:OTHER:RO::")],
                "dropped_sources": ["api:OTHER:RO::"]})
    ids = [p.doc_id for p in out["retrieved"]]
    api = [p for p in out["retrieved"] if p.source_type == "api_metadata"]
    assert ids[0] == "esmb-guide" and len(api) == 1 and api[0].doc_id.startswith("api-eurostat-")
    assert api[0].issuer == "Eurostat" and "Annual rate of change" in api[0].text


def test_failing_retriever_is_recorded_not_raised(world):
    """A retrieval failure becomes a retryable WorkflowError; the API metadata is still returned."""
    def broken(q):
        """Simulate a database outage."""
        raise ConnectionError("db down: secret-dsn")

    out = _run(world, broken, {"source_metadata": [_api_meta()]})
    assert len(out["retrieved"]) == 1 and out["errors"][0].retryable
    assert "ConnectionError" in out["errors"][0].message and "secret" not in out["errors"][0].message


def test_passage_cap_from_settings(world):
    """PROMPT_MAX_PASSAGES bounds the merged list."""
    out = _run(world, lambda q: [_p(f"{q}-{i}") for i in range(5)], prompt_max_passages=3)
    assert len(out["retrieved"]) == 3
