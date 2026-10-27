"""Combine uploaded and API observations, detect and resolve value conflicts, fill gaps with consent.

Rules (spec AC 20-22, D-010):
- Uploaded indicators are compared with API series only through user-supplied `IndicatorLink`s (one-to-one;
  never inferred). `combine` renames linked upload codes on copies; provenance and obs ids are untouched.
- Join key: (indicator, region, period); values are compared only within the same (unit, unit_multiplier)
  and only across different sources (an uploaded file, or an API series). Different units for one key are
  reported as `UnitMismatch`, never converted. Missing values (None) never conflict.
- Two values conflict when |a - b| > max(rel_tol · max(|a|, |b|), rounding unit); the rounding unit is the
  last written decimal place (3.3 → 0.1, 3.27 → 0.01) of the coarser non-integral value, and 1 only when
  both values are whole numbers (a float such as 5.0 says nothing about precision: 5.0 vs 5.9 conflicts). With
  `ignore_rounding=False` only the relative tolerance applies. Arithmetic uses decimals of the shortest
  float representation, so 3.3 vs 3.2 is exactly one unit (not a conflict).
- Every conflict must be resolved before drafting (`UnresolvedConflicts`).
- Gap filling only with consent: for each linked uploaded series (indicator, region, frequency), periods
  from its first to its last uploaded period (inclusive) that are absent or None-valued are filled from the linked API
  series when units match (else reported `unit_mismatch`). Filled points are in the naming space of
  `combine` (catalog indicator code), keep their API provenance, and get their own obs id
  `make_obs_id("gapfill", api obs_id, upload code)` so they never collide with the API point itself.
  A filled point replaces the uploaded point with a missing value (`replaced_obs_id`): callers drop that one.
"""
import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from itertools import combinations

from statbrief.domain.conflict import (Conflict, ConflictResolution, ConflictScan, ExcludedObservation,
                                       FilledPoint, GapFillReport, GapNotFilled, IndicatorLink, ResolutionResult,
                                       UnitMismatch, UnresolvedConflicts)
from statbrief.domain.observation import ApiProvenance, Observation, make_obs_id

_API_LABELS = {"eurostat": "Eurostat", "worldbank": "World Bank"}


def _link_map(links: Iterable[IndicatorLink]) -> dict[str, str]:
    """upload_code → catalog_code; ValueError unless links are one-to-one."""
    forward: dict[str, str] = {}
    backward: dict[str, str] = {}
    for link in links:
        if forward.get(link.upload_code, link.catalog_code) != link.catalog_code:
            raise ValueError(f"upload indicator {link.upload_code!r} is linked to more than one catalog indicator")
        if backward.get(link.catalog_code, link.upload_code) != link.upload_code:
            raise ValueError(f"catalog indicator {link.catalog_code!r} is linked to more than one upload indicator")
        forward[link.upload_code] = link.catalog_code
        backward[link.catalog_code] = link.upload_code
    return forward


def source_key(obs: Observation) -> str:
    """Identity of the source of an observation: one uploaded file, or one API series."""
    p = obs.provenance
    if isinstance(p, ApiProvenance):
        return f"api:{p.api_name}:{p.series_id}"
    return f"upload:{p.file_id}"


def source_label(obs: Observation) -> str:
    """Human-readable source of an observation: "Upload: <file>" or "<API>: <series id>"."""
    p = obs.provenance
    if isinstance(p, ApiProvenance):
        return f"{_API_LABELS.get(p.api_name, p.api_name)}: {p.series_id}"
    return f"Upload: {p.filename}"


def combine(upload_obs: Sequence[Observation], api_obs: Sequence[Observation],
            links: Iterable[IndicatorLink]) -> list[Observation]:
    """Uploaded observations (linked codes renamed on copies) followed by the API observations."""
    mapping = _link_map(links)
    renamed = [o.model_copy(update={"indicator_code": mapping[o.indicator_code]}) if o.indicator_code in mapping
               else o for o in upload_obs]
    return [*renamed, *api_obs]


def _dec(value: float) -> Decimal:
    """Exact decimal of the shortest representation of a float (3.3 → Decimal('3.3'))."""
    return Decimal(repr(value))


def rounding_unit(value: float) -> Decimal:
    """Last written decimal place of a value: 3.3 → 0.1, 3.27 → 0.01, whole numbers → 1."""
    if float(value).is_integer():
        return Decimal(1)
    exponent = _dec(value).as_tuple().exponent
    return Decimal(1).scaleb(min(int(exponent), 0))


def _conflicting(a: float, b: float, rel_tol: float, ignore_rounding: bool) -> bool:
    """True when |a - b| exceeds the tolerance (see module doc)."""
    da, db = _dec(a), _dec(b)
    tol = Decimal(repr(rel_tol)) * max(abs(da), abs(db))
    if ignore_rounding:
        decimals = [rounding_unit(v) for v in (a, b) if not float(v).is_integer()]
        tol = max(tol, max(decimals) if decimals else Decimal(1))
    return abs(da - db) > tol


def _rel_diff(a: float, b: float) -> float:
    """|a - b| / max(|a|, |b|) (0 when both are 0)."""
    ref = max(abs(a), abs(b))
    return abs(a - b) / ref if ref else 0.0


def _unit_label(unit: str, multiplier: int) -> str:
    """'EUR×10^6' for a non-zero multiplier, else the unit."""
    return f"{unit}×10^{multiplier}" if multiplier else unit


def _conflict_id(obs_ids: Iterable[str]) -> str:
    """Deterministic conflict id from the candidate obs ids."""
    return "cf_" + hashlib.sha1("\x1f".join(sorted(obs_ids)).encode("utf-8")).hexdigest()[:16]


def detect_conflicts(obs: Sequence[Observation], *, rel_tol: float, ignore_rounding: bool = True) -> ConflictScan:
    """Value conflicts across sources for each (indicator, region, period, unit), plus unit mismatches."""
    groups: dict[tuple[str, str, str], list[Observation]] = defaultdict(list)
    for o in obs:
        if o.value is not None:
            groups[(o.indicator_code, o.region_code, o.period)].append(o)
    scan = ConflictScan()
    for (indicator, region, period), members in groups.items():
        if len({source_key(o) for o in members}) < 2:
            continue
        by_unit: dict[tuple[str, int], list[Observation]] = defaultdict(list)
        for o in members:
            by_unit[(o.unit, o.unit_multiplier)].append(o)
        if len(by_unit) > 1:
            scan.unit_mismatches.append(UnitMismatch(
                indicator=indicator, region=region, period=period,
                units=[_unit_label(u, m) for u, m in by_unit], candidates=[o.obs_id for o in members]))
        for (unit, mult), same in by_unit.items():
            pairs = [(a, b) for a, b in combinations(same, 2) if source_key(a) != source_key(b)]
            if not any(_conflicting(a.value, b.value, rel_tol, ignore_rounding) for a, b in pairs):  # type: ignore[arg-type]
                continue
            scan.conflicts.append(Conflict(
                conflict_id=_conflict_id(o.obs_id for o in same), indicator=indicator, region=region, period=period,
                unit=unit, unit_multiplier=mult, candidates=[o.obs_id for o in same],
                values=[o.value for o in same], sources=[source_key(o) for o in same],  # type: ignore[misc]
                rel_diff=max(_rel_diff(a.value, b.value) for a, b in pairs)))  # type: ignore[arg-type]
    return scan


def apply_resolutions(obs: Sequence[Observation], conflicts: Sequence[Conflict],
                      resolutions: Sequence[ConflictResolution]) -> ResolutionResult:
    """Apply the user's resolutions: kept observations (input order), excluded ones with reasons, and source
    labels for the candidates kept under "keep_both".

    UnresolvedConflicts when a conflict has no resolution; ValueError for resolutions of unknown
    conflicts, duplicate resolutions, or a preferred obs id that is not a candidate.
    """
    by_id = {c.conflict_id: c for c in conflicts}
    chosen: dict[str, ConflictResolution] = {}
    for r in resolutions:
        if r.conflict_id not in by_id:
            raise ValueError(f"resolution for unknown conflict {r.conflict_id!r}")
        if r.conflict_id in chosen:
            raise ValueError(f"conflict {r.conflict_id!r} is resolved more than once")
        chosen[r.conflict_id] = r
    unresolved = [c.conflict_id for c in conflicts if c.conflict_id not in chosen]
    if unresolved:
        raise UnresolvedConflicts(unresolved)
    excluded: dict[str, str] = {}
    keep_both: set[str] = set()
    for cid, r in chosen.items():
        candidates = by_id[cid].candidates
        if r.choice == "keep_both":
            keep_both.update(candidates)
        elif r.choice == "prefer":
            if r.preferred_obs_id not in candidates:
                raise ValueError(f"{r.preferred_obs_id!r} is not a candidate of conflict {cid!r}")
            excluded.update({o: f"conflict {cid}: resolved in favour of {r.preferred_obs_id}"
                             for o in candidates if o != r.preferred_obs_id})
        elif r.choice == "exclude":
            excluded.update({o: f"conflict {cid}: all values excluded by the user" for o in candidates})
    kept = [o for o in obs if o.obs_id not in excluded]
    return ResolutionResult(
        kept=kept,
        excluded=[ExcludedObservation(obs_id=o.obs_id, reason=excluded[o.obs_id]) for o in obs if o.obs_id in excluded],
        labels={o.obs_id: source_label(o) for o in kept if o.obs_id in keep_both})


def fill_gaps(upload_obs: Sequence[Observation], api_obs: Sequence[Observation], links: Iterable[IndicatorLink],
              consent: bool) -> tuple[list[Observation], GapFillReport]:
    """API observations filling gaps of linked uploaded series, and the report of every filled key.

    Nothing is filled without `consent` (AC 22).
    """
    mapping = _link_map(links)
    if not consent:
        return [], GapFillReport(consent=False)
    back = {catalog: upload for upload, catalog in mapping.items()}
    series: dict[tuple[str, str, str], dict[str, Observation]] = defaultdict(dict)
    for o in upload_obs:
        if o.indicator_code in mapping:
            series[(o.indicator_code, o.region_code, o.frequency)][o.period] = o
    report = GapFillReport(consent=True)
    filled: list[Observation] = []
    for a in api_obs:
        upload_code = back.get(a.indicator_code)
        points = series.get((upload_code, a.region_code, a.frequency)) if upload_code else None
        if not points or not min(points) <= a.period <= max(points):
            continue  # not linked, no uploaded series, or outside the uploaded span
        existing = points.get(a.period)
        if existing is not None and existing.value is not None:
            continue
        units = {(o.unit, o.unit_multiplier) for o in points.values()}
        if units != {(a.unit, a.unit_multiplier)}:
            report.not_filled.append(GapNotFilled(indicator=upload_code, region=a.region_code, period=a.period,
                                                  obs_id=a.obs_id, reason="unit_mismatch"))
            continue
        prov = a.provenance
        if not isinstance(prov, ApiProvenance):
            continue  # only API data may fill gaps
        point = a.model_copy(update={"obs_id": make_obs_id("gapfill", a.obs_id, upload_code)})
        filled.append(point)
        report.filled.append(FilledPoint(indicator=a.indicator_code, upload_indicator=upload_code,
                                         region=a.region_code, period=a.period, obs_id=point.obs_id,
                                         source_obs_id=a.obs_id, api_name=prov.api_name, series_id=prov.series_id,
                                         replaced_obs_id=existing.obs_id if existing is not None else None))
    return filled, report
