"""Analysis fan-out per (indicator, region): deterministic metrics, aggregation and significance flags (AC 23-25).

    analyze_dispatch --Command(goto=[Send("analyze_one", AnalyzeTask) x K])--> analyze_one --+
                     '--Command(goto="analyze_all") (ENABLE_PARALLEL off) --> analyze_all ----+--> aggregate_metrics
                                                                                                   -> flag_significant

`analyze_dispatch` reads the report's effective (resolved) observations from the database, picks the
(indicator, region) pairs the report covers, resets the accumulated metric lists (`Overwrite`) and fans out
one task per pair. Each `analyze_one` task re-reads only its slice from the database (never from the state)
and runs `compute_metrics`; the `operator.add` reducers collect results and skips in whatever order the tasks
finish, so `aggregate_metrics` sorts them (metric id; skips by their fields) and the outcome is identical
for any completion order and for the sequential path. `flag_significant` applies D-010's rules.
"""
from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Overwrite, Send

from statbrief.analytics.metrics import compute_metrics
from statbrief.analytics.significance import flag_significant as flag_changes
from statbrief.domain.metrics import MetricResult, MetricSkip
from statbrief.domain.observation import Observation
from statbrief.domain.report import ReportParams
from statbrief.graph.deps import GraphDeps
from statbrief.graph.observations import in_window, load_effective, view_from_state
from statbrief.graph.state import AnalyzeTask, ObservationView, ReportState


def report_indicators(params: ReportParams) -> set[str]:
    """Indicator codes the report covers, in the combined naming space (linked upload codes -> catalog codes)."""
    renamed = {link.upload_code: link.catalog_code for link in params.links}
    return {renamed.get(code, code) for code in params.indicators}


def analysis_pairs(observations: Iterable[Observation], params: ReportParams) -> list[tuple[str, str]]:
    """Sorted (indicator, region) pairs present in the data and selected by the parameters (empty = all)."""
    indicators, regions = report_indicators(params), set(params.regions)
    return sorted({(o.indicator_code, o.region_code) for o in observations
                   if (not indicators or o.indicator_code in indicators) and (not regions or o.region_code in regions)})


def slice_view(view: ObservationView, indicator: str, region: str) -> ObservationView:
    """The view restricted to the conflicts (and resolutions) of one pair, to keep Send payloads small."""
    conflicts = [c for c in view.conflicts if c.indicator == indicator and c.region == region]
    ids = {c.conflict_id for c in conflicts}
    return view.model_copy(update={"conflicts": conflicts,
                                   "resolutions": {k: v for k, v in view.resolutions.items() if k in ids}})


def analyze_dispatch(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> Command:
    """Reset the metric lists and fan out one `analyze_one` per pair (or run `analyze_all`)."""
    view = view_from_state(state)
    pairs = analysis_pairs(load_effective(deps, view), state["params"])
    reset = {"metrics": Overwrite([]), "metric_skips": Overwrite([]), "status": "analyzing"}
    if not deps.settings.enable_parallel or not pairs:
        return Command(update=reset, goto="analyze_all")
    return Command(update=reset, goto=[Send("analyze_one", AnalyzeTask(indicator=i, region=r,
                                                                       view=slice_view(view, i, r)))
                                       for i, r in pairs])


def analyze_one(task: AnalyzeTask, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Metrics and recorded skips of one (indicator, region), from that slice of the database."""
    obs = load_effective(deps, task["view"], indicator=task["indicator"], region=task["region"])
    results, skips = compute_metrics(obs, indicator=task["indicator"], region=task["region"])
    return {"metrics": results, "metric_skips": skips}


def analyze_all(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Sequential path (ENABLE_PARALLEL off): every pair in one node, from one read of the observations."""
    obs = load_effective(deps, view_from_state(state))
    metrics: list[MetricResult] = []
    skips: list[MetricSkip] = []
    for indicator, region in analysis_pairs(obs, state["params"]):
        results, skipped = compute_metrics(obs, indicator=indicator, region=region)
        metrics += results
        skips += skipped
    return {"metrics": metrics, "metric_skips": skips}


def _skip_key(s: MetricSkip) -> tuple[str, ...]:
    """Total order of skips."""
    return (s.indicator, s.region, s.kind, s.period or "", s.base_period or "", s.reason)


def sort_results(metrics: Sequence[MetricResult],
                 skips: Sequence[MetricSkip]) -> tuple[list[MetricResult], list[MetricSkip]]:
    """Metrics by id (duplicates dropped) and skips by their fields: independent of completion order."""
    by_id = {m.metric_id: m for m in metrics}
    unique_skips = {_skip_key(s): s for s in skips}
    return [by_id[k] for k in sorted(by_id)], [unique_skips[k] for k in sorted(unique_skips)]


def report_window_only(metrics: Sequence[MetricResult], skips: Sequence[MetricSkip],
                       params: ReportParams) -> tuple[list[MetricResult], list[MetricSkip]]:
    """Metrics and skips whose (reporting) period lies inside the report's window (D-035).

    The analysis reads one extra year before `period_from` so year-ago bases exist; results for those
    look-back periods are not reported (their base periods may lie in the look-back year).
    """
    def inside(period: str | None) -> bool:
        """Whether a reporting period belongs to the report (a skip without a period always does)."""
        return period is None or in_window(period, params.period_from, params.period_to)

    return [m for m in metrics if inside(m.period)], [s for s in skips if inside(s.period)]


def aggregate_metrics(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Keep the report window's results, in a deterministic, de-duplicated order (replaces the fan-in)."""
    metrics, skips = report_window_only(state.get("metrics") or [], state.get("metric_skips") or [],
                                        state["params"])
    metrics, skips = sort_results(metrics, skips)
    return {"metrics": Overwrite(metrics), "metric_skips": Overwrite(skips)}


def flag_significant(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Significant changes among the metrics (thresholds and top movers, D-010)."""
    return {"significant": flag_changes(list(state.get("metrics") or []), deps.settings)}
