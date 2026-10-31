"""Ingestion sub-graph (M7 Task 2): Send fan-out per source, cache-first API fetches, safe failures."""
from langgraph.checkpoint.memory import InMemorySaver

from statbrief.domain.dataset import ColumnMapping
from statbrief.domain.report import ReportParams
from statbrief.graph.ingestion import build_ingestion_graph
from statbrief.graph.serde import make_serializer
from statbrief.storage.repos import MappingRepo, ObservationRepo
from tests.graph.helpers import long_csv, monthly, unemp_rows

PARAMS = ReportParams(mode="combined", indicators=["HICP_YOY", "UNEMP_RATE", "WB_CPI", "IRS_UNEMP"],
                      regions=["RO"], period_from="2023-01", period_to="2024-03")


def _serve_all(world) -> None:
    """Stub responses for the three catalog indicators of PARAMS."""
    world.api.serve("HICP_YOY", {"RO": monthly(2023, [10.0 - i * 0.3 for i in range(15)])})
    world.api.serve("UNEMP_RATE", {"RO": monthly(2023, [5.5] * 15)})
    world.api.serve("WB_CPI", {"RO": {"2023": 10.4}})


def _run(world, params=PARAMS, thread="t1", **settings):
    """Run the ingestion sub-graph with a checkpointer (Send payloads go through the serde too)."""
    graph = build_ingestion_graph(world.deps(**settings))
    compiled = graph.copy(update={"checkpointer": InMemorySaver(serde=make_serializer())})
    return compiled.invoke(world.base_state(params), {"configurable": {"thread_id": thread}})


def _by_id(out) -> dict:
    """source_metadata keyed by source id."""
    return {m.source_id: m for m in out["source_metadata"]}


def test_fan_in_collects_every_api_and_file(world):
    """Three API requests and two files give five SourceMeta; API rows are stored, file rows are not yet."""
    _serve_all(world)
    a = world.add_file("irs_unemp.csv", long_csv(unemp_rows()))
    b = world.add_file("irs_unemp_cj.csv", long_csv(unemp_rows(region="RO11")))
    out = _run(world)
    metas = _by_id(out)
    assert len(out["source_metadata"]) == 5 and all(m.status == "ok" for m in metas.values())
    assert {m.kind for m in metas.values()} == {"api", "upload"}
    assert set(out["mappings"]) == {a.file_id, b.file_id}
    assert [r.indicator for r in out["api_requests"]] == ["HICP_YOY", "UNEMP_RATE", "WB_CPI"]
    hicp = next(m for m in metas.values() if m.source_id.startswith("api:HICP_YOY"))
    assert hicp.observation_count == 15 and hicp.source_key.startswith("api:eurostat:prc_hicp_minr")
    assert hicp.query_url.startswith("https://") and hicp.series_label
    stored = ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)
    assert len(stored) == 15 + 15 + 1  # API rows only: files are normalized later
    assert out.get("errors", []) == [] and out["status"] == "collecting"


def test_cache_hit_makes_zero_client_calls(world):
    """A second run (resume / replay) reads every API response from api_cache: no HTTP request at all."""
    _serve_all(world)
    first = _run(world, thread="a")
    calls = len(world.api.requests)
    assert calls == 3 and not any(m.from_cache for m in first["source_metadata"])
    second = _run(world, thread="b")
    assert len(world.api.requests) == calls
    assert all(m.from_cache for m in second["source_metadata"])


def test_one_failing_api_does_not_lose_the_others(world):
    """A 404 becomes a failed SourceMeta and a WorkflowError; the other sources are intact; nothing raises."""
    _serve_all(world)
    world.api.fail("WB_CPI")
    world.add_file("irs_unemp.csv", long_csv(unemp_rows()))
    out = _run(world)
    metas = list(out["source_metadata"])
    failed = [m for m in metas if m.status == "failed"]
    assert len(metas) == 4 and [m.source_id for m in failed] == [r.request_id for r in out["api_requests"]
                                                                 if r.indicator == "WB_CPI"]
    assert "404" in failed[0].message and "not found" not in failed[0].message  # no response body
    assert [(e.node, e.kind, e.retryable) for e in out["errors"]] == [("fetch_api", "api_failed", False)]


def test_parallel_on_and_off_give_the_same_sources(world):
    """ENABLE_PARALLEL off runs the same tasks in one node with the same results."""
    _serve_all(world)
    world.api.fail("WB_CPI")
    world.add_file("irs_unemp.csv", long_csv(unemp_rows()))
    on = _run(world, thread="on", enable_parallel=True)
    off = _run(world, thread="off", enable_parallel=False)

    def norm(out):
        """Order-free view without the cache flag (the second run hits the cache)."""
        metas = sorted(out["source_metadata"], key=lambda m: m.source_id)
        return ([m.model_dump(exclude={"from_cache"}) for m in metas], out["mappings"],
                sorted(e.message for e in out["errors"]))

    assert norm(on) == norm(off)


def test_stored_mapping_wins_and_missing_blob_fails_safely(world):
    """A mapping saved in the wizard is used as confirmed; a file whose blob is gone is a failed source."""
    good = world.add_file("irs_unemp.csv", long_csv(unemp_rows()))
    gone = world.add_file("gone.csv", long_csv(unemp_rows()))
    world.blobs.delete(gone.blob_key)
    mapping = ColumnMapping(date_col="date", value_col="value", region_col="region", indicator_col="indicator",
                            unit_col="unit")
    MappingRepo(world.pool).save(world.user_id, good.file_id, mapping)
    out = _run(world, params=ReportParams(mode="upload"))
    metas = _by_id(out)
    assert metas[good.file_id].mapping_source == "stored" and metas[good.file_id].mapping_confidence == 1.0
    assert out["mappings"] == {good.file_id: mapping}
    assert metas[gone.file_id].status == "failed" and "no longer stored" in metas[gone.file_id].message
    assert out["api_requests"] == []


def test_detected_mapping_with_confidence(world):
    """Without a saved mapping the schema is detected; a clean long CSV maps every role."""
    ref = world.add_file("irs_unemp.csv", long_csv(unemp_rows()))
    out = _run(world, params=ReportParams(mode="upload"))
    meta = _by_id(out)[ref.file_id]
    assert meta.mapping_source == "detected" and meta.detected is not None
    m = out["mappings"][ref.file_id]
    assert (m.date_col, m.value_col, m.region_col, m.indicator_col, m.unit_col) == \
        ("date", "value", "region", "indicator", "unit")


def test_api_mode_unknown_indicator_is_an_error(world):
    """In API mode a code outside the catalog is reported, not fetched."""
    _serve_all(world)
    out = _run(world, params=ReportParams(mode="api", indicators=["HICP_YOY", "NOPE"], regions=["RO"]))
    assert [r.indicator for r in out["api_requests"]] == ["HICP_YOY"]
    assert [e.kind for e in out["errors"]] == ["unknown_indicator"]


def test_no_sources_ends_cleanly(world):
    """Upload mode without files: no tasks, no error, empty metadata."""
    out = _run(world, params=ReportParams(mode="upload"))
    assert out.get("source_metadata", []) == [] and out["file_refs"] == []


def test_api_source_without_data_is_a_visible_warning(world):
    """Review M7: a fetched series with no observations for the request is not silently 'ok'."""
    world.api.serve("HICP_YOY", {"RO": {}})  # Eurostat: an empty time dimension is a valid, empty selection
    out = _run(world, params=ReportParams(mode="api", indicators=["HICP_YOY"], regions=["RO"]))
    assert out["source_metadata"][0].observation_count == 0
    assert [e.kind for e in out["errors"]] == ["api_no_data"]
