"""The report's effective observations, rebuilt from the database (the state never holds rows, R6).

`effective_observations` is the one place that turns stored rows into the data a report analyses:
1. observations of dropped sources are left out (`dropped_keys`, combine.source_key identities);
2. observations outside the report's period window are left out (an observation is kept when its whole
   period lies inside [period_from, period_to]: "2024-Q1" is inside 2024-01..2024-03, "2024" is not);
3. uploaded and API observations are combined through the user's indicator links (never inferred);
4. with consent, gaps of linked uploaded series are filled from API data (filled points replace the
   uploaded points with a missing value);
5. with `resolve=True`, the user's conflict resolutions are applied (UnresolvedConflicts when a conflict of
   these observations has none — analysis never runs on unresolved conflicts).
Conflicts are detected per (indicator, region, period), so the pipeline gives the same result for one
(indicator, region) slice as for the whole report: `load_effective` reads only that slice when asked.
"""
from collections.abc import Sequence

from statbrief.analytics.combine import apply_resolutions, combine, fill_gaps, source_key
from statbrief.analytics.store import ObservationStore
from statbrief.domain.metrics import period_frequency
from statbrief.domain.observation import ApiProvenance, Observation
from statbrief.domain.report import ReportParams
from statbrief.graph.deps import GraphDeps
from statbrief.graph.state import ObservationView, ReportState

_MONTHS_PER_QUARTER = 3
_MONTHS_PER_YEAR = 12


def month_span(period: str) -> tuple[int, int]:
    """First and last month (as year*12 + month - 1) covered by a canonical period."""
    freq = period_frequency(period)
    year = int(period[:4])
    if freq == "A":
        return year * _MONTHS_PER_YEAR, year * _MONTHS_PER_YEAR + _MONTHS_PER_YEAR - 1
    if freq == "Q":
        first = year * _MONTHS_PER_YEAR + (int(period[-1]) - 1) * _MONTHS_PER_QUARTER
        return first, first + _MONTHS_PER_QUARTER - 1
    month = year * _MONTHS_PER_YEAR + int(period[5:7]) - 1
    return month, month


def in_window(period: str, period_from: str | None, period_to: str | None) -> bool:
    """True when the whole period lies inside the (inclusive, possibly open) window."""
    first, last = month_span(period)
    if period_from is not None and first < month_span(period_from)[0]:
        return False
    return period_to is None or last <= month_span(period_to)[1]


def dropped_keys(state: ReportState) -> list[str]:
    """Source keys of the sources the user dropped (from their ingestion metadata)."""
    dropped = set(state.get("dropped_sources") or [])
    return sorted({m.source_key for m in state.get("source_metadata") or []
                   if m.source_id in dropped and m.source_key})


def view_from_state(state: ReportState) -> ObservationView:
    """How to read this report's effective observations (owner, links, consent, drops, conflicts)."""
    params: ReportParams = state["params"]
    return ObservationView(user_id=state["user_id"], report_id=state["report_id"], links=params.links,
                           consent_gap_fill=params.consent_gap_fill, dropped_keys=dropped_keys(state),
                           period_from=params.period_from, period_to=params.period_to,
                           conflicts=state.get("conflicts") or [], resolutions=state.get("resolutions") or {})


def effective_observations(raw: Sequence[Observation], view: ObservationView, *,
                           resolve: bool = True) -> list[Observation]:
    """Apply drops, the period window, links, consented gap filling and (optionally) resolutions."""
    dropped = set(view.dropped_keys)
    kept = [o for o in raw if source_key(o) not in dropped
            and in_window(o.period, view.period_from, view.period_to)]
    uploads = [o for o in kept if not isinstance(o.provenance, ApiProvenance)]
    apis = [o for o in kept if isinstance(o.provenance, ApiProvenance)]
    combined = combine(uploads, apis, view.links)
    filled, report = fill_gaps(uploads, apis, view.links, view.consent_gap_fill)
    replaced = {f.replaced_obs_id for f in report.filled if f.replaced_obs_id}
    obs = [o for o in combined if o.obs_id not in replaced] + filled
    if not resolve:
        return obs
    ids = {o.obs_id for o in obs}
    relevant = [c for c in view.conflicts if set(c.candidates) <= ids]
    chosen = [view.resolutions[c.conflict_id] for c in relevant if c.conflict_id in view.resolutions]
    return apply_resolutions(obs, relevant, chosen).kept


def load_effective(deps: GraphDeps, view: ObservationView, *, resolve: bool = True, indicator: str | None = None,
                   region: str | None = None) -> list[Observation]:
    """Effective observations of the whole report, or of one (indicator, region) slice when both are given.

    The slice reads the indicator itself plus every uploaded code linked to it (renamed by `combine`).
    """
    if indicator is not None and region is not None:
        codes = sorted({indicator} | {link.upload_code for link in view.links if link.catalog_code == indicator})
        raw = deps.observations.list_for_series(view.user_id, view.report_id, codes, region)
        return [o for o in effective_observations(raw, view, resolve=resolve)
                if o.indicator_code == indicator and o.region_code == region]
    raw = deps.observations.list_for_report(view.user_id, view.report_id)
    return effective_observations(raw, view, resolve=resolve)


def load_store(deps: GraphDeps, view: ObservationView) -> ObservationStore:
    """ObservationStore over the report's resolved effective observations (verifier and tools)."""
    return ObservationStore(load_effective(deps, view))
