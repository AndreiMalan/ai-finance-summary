"""Analysis fan-out (M7 Task 4): Send per (indicator, region), deterministic aggregation, skips, toggles."""
import random

import pytest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from statbrief.analytics.combine import detect_conflicts
from statbrief.analytics.metrics import compute_metrics
from statbrief.domain.conflict import ConflictResolution, IndicatorLink
from statbrief.domain.observation import UploadProvenance, make_obs_id
from statbrief.domain.report import ReportParams
from statbrief.graph.deps import node
from statbrief.graph.nodes_analyze import (aggregate_metrics, analysis_pairs, analyze_all, analyze_dispatch,
                                           analyze_one, flag_significant, sort_results)
from statbrief.graph.serde import make_serializer
from statbrief.graph.state import ReportState, SourceMeta
from statbrief.storage.repos import ObservationRepo
from tests.graph.helpers import api_obs, monthly

PARAMS = ReportParams(mode="api", indicators=["HICP_YOY", "IND_PROD"], regions=["RO", "HU", "BG"])


def _graph(deps):
    """analyze_dispatch -> analyze_one x K | analyze_all -> aggregate_metrics -> flag_significant."""
    g = StateGraph(ReportState)
    g.add_node("analyze_dispatch", node(analyze_dispatch, deps), destinations=("analyze_one", "analyze_all"))
    g.add_node("analyze_one", node(analyze_one, deps))
    g.add_node("analyze_all", node(analyze_all, deps))
    g.add_node("aggregate_metrics", node(aggregate_metrics, deps))
    g.add_node("flag_significant", node(flag_significant, deps))
    g.add_edge(START, "analyze_dispatch")
    g.add_edge("analyze_one", "aggregate_metrics")
    g.add_edge("analyze_all", "aggregate_metrics")
    g.add_edge("aggregate_metrics", "flag_significant")
    g.add_edge("flag_significant", END)
    return g.compile(checkpointer=InMemorySaver(serde=make_serializer()))


def _seed(world):
    """HICP (rate) for three regions and an industrial production index for two; one single-point series."""
    obs = []
    for i, region in enumerate(["RO", "HU", "BG"]):
        obs += api_obs("HICP_YOY", region, monthly(2023, [8.0 - i - m * 0.25 for m in range(15)]))
    for region in ["RO", "HU"]:
        obs += api_obs("IND_PROD", region, monthly(2023, [100 + m for m in range(15)]), unit="I21")
    obs += api_obs("IND_PROD", "BG", {"2024-03": 99.0}, unit="I21")
    obs += api_obs("HICP_YOY", "DE", monthly(2023, [3.0] * 15))  # not selected by the parameters
    ObservationRepo(world.pool).upsert_api_observations(world.user_id, world.report_id, obs)
    return obs


def _run(world, params=PARAMS, state_extra=None, **settings):
    """Run the analysis part of the graph."""
    state = {**world.base_state(params), **(state_extra or {})}
    return _graph(world.deps(**settings)).invoke(state, {"configurable": {"thread_id": "a"}})


def _expected(obs, params):
    """compute_metrics per selected pair, sorted like the aggregation."""
    metrics, skips = [], []
    for ind, reg in analysis_pairs(obs, params):
        res, sk = compute_metrics(obs, indicator=ind, region=reg)
        metrics += res
        skips += sk
    return metrics, skips


def test_pairs_aggregate_deterministically(world):
    """Six pairs: results equal compute_metrics per pair, sorted by metric id; DE is not analysed."""
    obs = _seed(world)
    out = _run(world)
    exp_metrics, exp_skips = sort_results(*_expected(obs, PARAMS))
    assert out["metrics"] == exp_metrics and out["metric_skips"] == exp_skips
    assert [m.metric_id for m in out["metrics"]] == sorted(m.metric_id for m in out["metrics"])
    assert {m.region for m in out["metrics"]} == {"RO", "HU", "BG"}
    assert out["significant"] and "top_mover" in {s.rule for s in out["significant"]}


def test_aggregation_is_independent_of_completion_order(world):
    """Whatever order the fanned-in results arrive in, aggregation gives the same lists."""
    metrics, skips = _expected(_seed(world), PARAMS)
    baseline = sort_results(metrics, skips)
    rng = random.Random(7)
    for _ in range(5):
        rng.shuffle(metrics)
        rng.shuffle(skips)
        assert sort_results(metrics, skips) == baseline


def test_skips_are_preserved(world):
    """A one-point series yields recorded skips (never guessed metrics)."""
    _seed(world)
    out = _run(world)
    bg = [s for s in out["metric_skips"] if s.indicator == "IND_PROD" and s.region == "BG"]
    assert any("only one period" in s.reason for s in bg)


def test_parallel_on_and_off_identical(world):
    """ENABLE_PARALLEL off computes the same metrics, skips and flags in one node."""
    _seed(world)
    on, off = _run(world, enable_parallel=True), _run(world, enable_parallel=False)
    assert (on["metrics"], on["metric_skips"], on["significant"]) == \
        (off["metrics"], off["metric_skips"], off["significant"])


def test_resolutions_and_links_apply_per_slice(world):
    """A linked upload series conflicting with the API: the preferred value is the one analysed."""
    ref = world.add_file("irs.csv", b"x")
    prov = UploadProvenance(file_id=ref.file_id, filename="irs.csv", row_number=2, column="value")
    upload = [o.model_copy(update={"obs_id": make_obs_id(ref.file_id, o.period), "provenance": prov})
              for o in api_obs("IPC", "RO", monthly(2023, [9.9] * 15))]
    repo = ObservationRepo(world.pool)
    repo.replace_for_file(world.user_id, world.report_id, ref.file_id, upload)
    api = api_obs("HICP_YOY", "RO", monthly(2023, [5.0] * 15))
    repo.upsert_api_observations(world.user_id, world.report_id, api)
    params = ReportParams(mode="combined", indicators=["IPC"], regions=["RO"],
                          links=[IndicatorLink(upload_code="IPC", catalog_code="HICP_YOY")])
    renamed = [o.model_copy(update={"indicator_code": "HICP_YOY"}) for o in upload]
    conflicts = detect_conflicts(renamed + api, rel_tol=0.005).conflicts
    assert len(conflicts) == 15
    api_ids = {o.obs_id for o in api}
    resolutions = {c.conflict_id: ConflictResolution(conflict_id=c.conflict_id, choice="prefer",
                                                     preferred_obs_id=next(i for i in c.candidates if i in api_ids))
                   for c in conflicts}
    meta = [SourceMeta(source_id=ref.file_id, kind="upload", label="irs.csv", status="ok")]
    for parallel in (True, False):
        out = _run(world, params, {"conflicts": conflicts, "resolutions": resolutions, "source_metadata": meta},
                   enable_parallel=parallel)
        latest = next(m for m in out["metrics"] if m.kind == "latest")
        assert latest.metric_id == "latest:HICP_YOY:RO:2024-03" and latest.value == 5.0
        assert latest.observation_ids[0] in api_ids


def test_no_pairs_yields_empty_results(world):
    """Nothing selected: empty metrics, no error."""
    out = _run(world, ReportParams(mode="api", indicators=["GDP_VOL"], regions=["RO"]))
    assert out["metrics"] == [] and out["significant"] == []


def test_one_period_report_uses_the_lookback_and_reports_only_its_window(world):
    """D-035: a 2024-03-only report reads 2023-03 as the year-ago base, but reports only 2024-03 results."""
    _seed(world)
    out = _run(world, ReportParams(mode="api", indicators=["HICP_YOY"], regions=["RO"], period_from="2024-03",
                                   period_to="2024-03"))
    ids = {m.metric_id for m in out["metrics"]}
    assert "pp_change:HICP_YOY:RO:2024-03:2023-03" in ids
    assert {m.period for m in out["metrics"]} == {"2024-03"}
    assert all(s.period in (None, "2024-03") for s in out["metric_skips"])


# --- re-review I2 / m11: window changes start at period_from; look-back only feeds year-ago bases ---------------

def test_window_change_uses_period_from_as_base(world):
    """Re-review I2 probe: levels 2019..2024, report 2021..2024 -> the 2021->2024 change, not 2020->2024."""
    obs = api_obs("LVL", "RO", {str(y): 100.0 + 10 * (y - 2019) for y in range(2019, 2025)}, unit="I21")
    ObservationRepo(world.pool).upsert_api_observations(world.user_id, world.report_id, obs)
    out = _run(world, ReportParams(mode="api", indicators=["LVL"], regions=["RO"], period_from="2021",
                                   period_to="2024"))
    by_id = {m.metric_id: m for m in out["metrics"]}
    assert by_id["abs_change:LVL:RO:2024:2021"].value == 30.0
    assert by_id["pct_change:LVL:RO:2024:2021"].value == pytest.approx(30 / 120 * 100)
    assert not any(m.base_period == "2020" and m.kind in ("abs_change", "pct_change") for m in out["metrics"])
    assert "yoy:LVL:RO:2024" in by_id


def test_rate_series_keeps_year_ago_and_window_pp_changes(world):
    """Rates: the year-ago pp change uses the look-back base; the window pp change starts at period_from."""
    _seed(world)
    out = _run(world, ReportParams(mode="api", indicators=["HICP_YOY"], regions=["RO"], period_from="2024-01",
                                   period_to="2024-03"))
    ids = {m.metric_id for m in out["metrics"]}
    assert {"pp_change:HICP_YOY:RO:2024-03:2023-03", "pp_change:HICP_YOY:RO:2024-03:2024-01"} <= ids
    assert not any(m.kind == "pp_change" and m.base_period == "2023-01" for m in out["metrics"])


def test_pair_with_only_lookback_data_leaves_a_skip(world):
    """Re-review m11: a series with data only in the look-back year is reported as skipped, not dropped."""
    obs = api_obs("LVL", "RO", {"2023-02": 1.0, "2023-03": 2.0}, unit="I21")
    ObservationRepo(world.pool).upsert_api_observations(world.user_id, world.report_id, obs)
    for parallel in (True, False):
        out = _run(world, ReportParams(mode="api", indicators=["LVL"], regions=["RO"], period_from="2024-01",
                                       period_to="2024-03"), enable_parallel=parallel)
        assert out["metrics"] == []
        assert [s.reason for s in out["metric_skips"]] == ["no observations in the report window"]
