"""The report's effective observations, rebuilt from the database (the state never holds rows, R6).

`effective_set` is the one place that turns stored rows into the data a report analyses:
1. observations of dropped sources are left out (`dropped_keys`, combine.source_key identities);
2. observations outside the analysis window are left out. An observation is kept when its whole period lies
   inside [period_from, period_to] ("2024-Q1" is inside 2024-01..2024-03, "2024" is not). The view's
   `period_from` already includes the one-year look-back (D-035), so change metrics have their base period;
   metrics are REPORTED only for periods inside the report's own window (`nodes_analyze.report_window_only`);
3. uploaded and API observations are combined through the user's indicator links (never inferred);
4. with consent, gaps of linked uploaded series are filled from API data: the filled copy (its own "gapfill"
   id, API provenance) replaces both the uploaded point with a missing value and the API point it copies,
   so a filled key has exactly one row;
5. with `resolve=True`, the user's conflict resolutions are applied (UnresolvedConflicts when a conflict of
   these observations has none: analysis never runs on unresolved conflicts), and then every
   (indicator, region, period, unit, unit_multiplier) key is collapsed to exactly ONE row (D-036). Values
   that agree, or differ only within the conflict tolerance, keep the UPLOAD row (the user's data is
   primary; otherwise the lowest source key, then obs id). The ids of the collapsed rows are recorded as
   corroborations of the kept row (`EffectiveSet.corroborations`), never silently lost. Candidates the user
   chose to "keep both" stay side by side: metrics on those periods are recorded as skipped (ambiguous),
   never averaged or picked.
Conflicts are detected per (indicator, region, period), so the pipeline gives the same result for one
(indicator, region) slice as for the whole report: `load_effective` reads only that slice when asked.
"""
from collections.abc import Sequence
from dataclasses import dataclass, field

from statbrief.analytics.combine import apply_resolutions, combine, fill_gaps, source_key
from statbrief.analytics.store import ObservationStore
from statbrief.domain.metrics import period_frequency
from statbrief.domain.observation import ApiProvenance, Observation
from statbrief.domain.report import ReportParams
from statbrief.graph.deps import GraphDeps
from statbrief.graph.state import ObservationView, ReportState

_MONTHS_PER_QUARTER = 3
_MONTHS_PER_YEAR = 12
LOOKBACK_YEARS = 1  # protocol (D-035): one year of base periods for YoY and year-ago changes


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


def lookback_start(period_from: str | None) -> str | None:
    """The same period one year earlier (12 months / 4 quarters / 1 year): the analysis window's start."""
    if period_from is None:
        return None
    period_frequency(period_from)  # ValueError for a non-canonical period
    return f"{int(period_from[:4]) - LOOKBACK_YEARS:04d}{period_from[4:]}"


def dropped_keys(state: ReportState) -> list[str]:
    """Source keys of the sources the user dropped (from their ingestion metadata)."""
    dropped = set(state.get("dropped_sources") or [])
    return sorted({m.source_key for m in state.get("source_metadata") or []
                   if m.source_id in dropped and m.source_key})


def view_from_state(state: ReportState) -> ObservationView:
    """How to read this report's effective observations (owner, links, consent, drops, look-back window,
    conflicts and resolutions)."""
    params: ReportParams = state["params"]
    return ObservationView(user_id=state["user_id"], report_id=state["report_id"], links=params.links,
                           consent_gap_fill=params.consent_gap_fill, dropped_keys=dropped_keys(state),
                           period_from=lookback_start(params.period_from), period_to=params.period_to,
                           conflicts=state.get("conflicts") or [], resolutions=state.get("resolutions") or {})


@dataclass(frozen=True)
class EffectiveSet:
    """Effective observations plus, per kept row, the ids of agreeing rows collapsed into it (provenance)."""

    observations: list[Observation]
    corroborations: dict[str, list[str]] = field(default_factory=dict)


def _is_upload(o: Observation) -> bool:
    """True for an observation read from an uploaded file."""
    return not isinstance(o.provenance, ApiProvenance)


def _key(o: Observation) -> tuple:
    """Identity of one data point: (indicator, region, period, unit, unit_multiplier)."""
    return o.indicator_code, o.region_code, o.period, o.unit, o.unit_multiplier


def collapse_duplicates(obs: Sequence[Observation],
                        keep_both: set[str]) -> tuple[list[Observation], dict[str, list[str]]]:
    """One row per key: the upload first, then the lowest (source_key, obs_id); "keep both" rows stay together.

    Returns the kept rows (in input order) and {kept obs_id: sorted ids of the rows collapsed into it}.
    """
    groups: dict[tuple, list[Observation]] = {}
    for o in obs:
        groups.setdefault(_key(o), []).append(o)
    kept: set[str] = set()
    corroborations: dict[str, list[str]] = {}
    for members in groups.values():
        if len(members) == 1 or any(m.obs_id in keep_both for m in members):
            kept.update(m.obs_id for m in members)
            continue
        rep = min(members, key=lambda m: (not _is_upload(m), source_key(m), m.obs_id))
        kept.add(rep.obs_id)
        corroborations[rep.obs_id] = sorted(m.obs_id for m in members if m.obs_id != rep.obs_id)
    return [o for o in obs if o.obs_id in kept], corroborations


def effective_set(raw: Sequence[Observation], view: ObservationView, *, resolve: bool = True) -> EffectiveSet:
    """Apply drops, the analysis window, links and consented gap filling; with `resolve`, also the
    resolutions and the one-row-per-key collapse (see the module docstring)."""
    dropped = set(view.dropped_keys)
    kept = [o for o in raw if source_key(o) not in dropped
            and in_window(o.period, view.period_from, view.period_to)]
    uploads = [o for o in kept if _is_upload(o)]
    apis = [o for o in kept if not _is_upload(o)]
    combined = combine(uploads, apis, view.links)
    filled, report = fill_gaps(uploads, apis, view.links, view.consent_gap_fill)
    replaced = ({f.replaced_obs_id for f in report.filled if f.replaced_obs_id}
                | {f.source_obs_id for f in report.filled})
    obs = [o for o in combined if o.obs_id not in replaced] + filled
    if not resolve:
        return EffectiveSet(obs)
    ids = {o.obs_id for o in obs}
    relevant = [c for c in view.conflicts if set(c.candidates) <= ids]
    chosen = [view.resolutions[c.conflict_id] for c in relevant if c.conflict_id in view.resolutions]
    obs = apply_resolutions(obs, relevant, chosen).kept
    by_id = {c.conflict_id: c for c in relevant}
    keep_both = {i for r in chosen if r.choice == "keep_both" for i in by_id[r.conflict_id].candidates}
    rows, corroborations = collapse_duplicates(obs, keep_both)
    return EffectiveSet(rows, corroborations)


def effective_observations(raw: Sequence[Observation], view: ObservationView, *,
                           resolve: bool = True) -> list[Observation]:
    """The observations of `effective_set`."""
    return effective_set(raw, view, resolve=resolve).observations


def load_effective_set(deps: GraphDeps, view: ObservationView, *, resolve: bool = True,
                       indicator: str | None = None, region: str | None = None) -> EffectiveSet:
    """Effective set of the whole report, or of one (indicator, region) slice when both are given.

    The slice reads the indicator itself plus every uploaded code linked to it (renamed by `combine`).
    """
    if indicator is not None and region is not None:
        codes = sorted({indicator} | {link.upload_code for link in view.links if link.catalog_code == indicator})
        raw = deps.observations.list_for_series(view.user_id, view.report_id, codes, region)
        full = effective_set(raw, view, resolve=resolve)
        rows = [o for o in full.observations if o.indicator_code == indicator and o.region_code == region]
        ids = {o.obs_id for o in rows}
        return EffectiveSet(rows, {k: v for k, v in full.corroborations.items() if k in ids})
    return effective_set(deps.observations.list_for_report(view.user_id, view.report_id), view, resolve=resolve)


def load_effective(deps: GraphDeps, view: ObservationView, *, resolve: bool = True, indicator: str | None = None,
                   region: str | None = None) -> list[Observation]:
    """The observations of `load_effective_set`."""
    return load_effective_set(deps, view, resolve=resolve, indicator=indicator, region=region).observations


def load_store(deps: GraphDeps, view: ObservationView) -> ObservationStore:
    """ObservationStore over the report's resolved effective observations (verifier and tools)."""
    return ObservationStore(load_effective(deps, view))
