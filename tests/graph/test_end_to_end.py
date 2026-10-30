"""End-to-end (M7b Task 10): IRS-style upload with val_adj_x + stubbed Eurostat HICP, on Postgres via the runner.

conflict interrupt -> resolve -> verified draft (acknowledging val_adj_x) -> review approve -> export pending;
run_events written without any cell value.
"""
import json

from statbrief.domain.conflict import IndicatorLink
from statbrief.domain.report import ReportParams
from statbrief.graph.builder import graph_config
from statbrief.graph.preferences import PreferencesRepo
from statbrief.graph.runner import run_segment
from statbrief.graph.runtime import make_store, state_graph
from statbrief.storage.repos import RunEventRepo
from tests.graph.helpers import draft_for, long_csv, monthly, report_call
from tests.graph.runkit import Script, queue_taker, resources

UPLOAD = [round(6.0 + i * 0.137, 3) for i in range(13)]  # 2023-01 .. 2024-01, distinctive decimals
ADJ = [round(1.234567 + i * 0.001, 6) for i in range(13)]  # val_adj_x: undocumented, never interpreted
PARAMS = ReportParams(mode="combined", indicators=["HICP_YOY"], regions=["RO"], period_from="2023-01",
                      period_to="2024-01", links=[IndicatorLink(upload_code="IPC", catalog_code="HICP_YOY")])


def _upload_csv() -> bytes:
    """The IRS-style monthly file with an undocumented adjustment column."""
    header = ("date", "region", "indicator", "value", "unit", "val_adj_x")
    rows = [(p, "RO", "IPC", v, "%", a) for p, v, a in zip(monthly(2023, [0] * 13), UPLOAD, ADJ, strict=True)]
    lines = [",".join(header)] + [",".join(str(c) for c in r) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def _api_series() -> dict[str, float]:
    """Eurostat HICP 2022-01 .. 2024-01: agrees with the upload except 2023-06 and 2023-09 (conflicts)."""
    series = monthly(2022, [5.0 + i * 0.05 for i in range(12)])
    for period, value in zip(monthly(2023, [0] * 13), UPLOAD, strict=True):
        series[period] = value + (0.9 if period in ("2023-06", "2023-09") else 0.0)
    return series


def test_upload_plus_api_to_approved_export(world):
    """The whole workflow over three segments on the Postgres checkpointer."""
    world.add_file("IRS_regional_monthly.csv", _upload_csv())
    world.api.serve("HICP_YOY", {"RO": _api_series()})
    params_json = PARAMS.model_dump(mode="json")  # as stored by PUT /params
    script = Script()
    res = resources(world, script)
    queue: list = []

    def segment():
        """One run segment, answering with the queued resume (if any)."""
        return list(run_segment(res, world.user_id, world.report_id, params=params_json,
                                take_resume=queue_taker(world, queue), budget_s=240))

    first = segment()
    gate = first[-1].data
    assert first[-1].type == "interrupt" and gate["kind"] == "resolve"
    assert sorted(c["period"] for c in gate["conflicts"]) == ["2023-06", "2023-09"]
    queue.append({"conflict_resolutions": [
        {"conflict_id": c["conflict_id"], "choice": "prefer",
         "preferred_obs_id": next(i for i, s in zip(c["candidates"], c["sources"], strict=True)
                                  if s.startswith("upload:"))} for c in gate["conflicts"]],
        "confirm_mappings": [m["file_id"] for m in gate["mappings"]]})

    def draft():
        """Built when the model is called: state the year-on-year pp change the analysis computed."""
        values = state_graph(res).get_state(graph_config(world.report_id, world.settings)).values
        metric = next(m for m in values["metrics"] if m.metric_id == "pp_change:HICP_YOY:RO:2024-01:2023-01")
        return report_call(draft_for(metric, f"{metric.value:.1f}", acknowledged=["val_adj_x"],
                                     extra_caveat="The meaning of column val_adj_x is not documented."))

    script.replies.append(draft)
    second = segment()
    review = second[-1].data
    assert second[-1].type == "interrupt" and review["kind"] == "review" and review["verification"]["ok"]
    assert review["draft"]["claims"][0]["value"] == round(UPLOAD[12] - UPLOAD[0], 1)
    assert review["draft"]["unknown_columns_acknowledged"] == ["val_adj_x"]

    queue.append({"action": "approve"})
    third = segment()
    assert third[-1].type == "done" and third[-1].data == {"status": "approved"}
    final = state_graph(res).get_state(graph_config(world.report_id, world.settings)).values
    assert final["export"].state == "pending" and final["corroborations"]
    assert len(world.api.requests) == 1  # three segments, one fetch
    assert PreferencesRepo(make_store(world.pool)).get(world.user_id).indicators == ["HICP_YOY"]

    events = RunEventRepo(world.pool).list_for_report(world.user_id, world.report_id)
    kinds = {e["kind"] for e in events}
    assert {"node", "llm_start", "llm_end"} <= kinds
    details = json.dumps([e["detail"] for e in events])
    assert not any(str(v) in details for v in UPLOAD + ADJ)  # no cell value is ever logged
