"""Eurostat Statistics API client: JSON-stat 2.0 responses → Observations with API provenance.

Request: `GET {EUROSTAT_BASE_URL}/{dataset}?format=JSON&lang=EN&<filters>&geo=..&geo=..` plus optional
`sinceTimePeriod` / `untilTimePeriod` (canonical periods such as 2024-03, 2024-Q1, 2024) or
`lastTimePeriod=n` (parameter names verified against the live API on 2026-10-26).

Parsing rules (nothing is invented or silently dropped):
- A size-0 dimension other than `time` (e.g. an unknown filter value) → SourceError "no data for filter k=v";
  only an empty `time` dimension is a valid empty result.
- Every dimension other than `geo` and `time` must have exactly one category, and each filter must be a
  dimension whose returned category equals the filter value; otherwise SourceError (several series
  would be merged, or a filter was ignored).
- `freq` must be A, Q or M (and equal the curated frequency); other frequencies raise SourceError, so
  daily or semester data is never collapsed into months. Periods are parsed with `parse_period` and must
  match that frequency (else `invalid_period`); two time codes for one period → `duplicate_period`.
- Unit: the spec's unit (curated, or stated by the user), else the single `unit` category code; when
  neither exists, SourceError (never guessed).
- Cells without a finite numeric value (absent from the sparse `value`, null, ":") → `missing_value` with
  their status flag; geos unknown to `resolve_region` → `unknown_geo`; requested geos absent from the
  response → `missing_value` with period None. Status flags (p, e, b, ...) are kept in `status_flag`.
"""
import math
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from itertools import product
from math import prod
from typing import Any

import httpx

from statbrief.config import Settings
from statbrief.domain.observation import ApiProvenance, Observation, make_obs_id
from statbrief.ingest.periods import PeriodError, parse_period
from statbrief.ingest.regions import resolve_region
from statbrief.sources.catalog import IndicatorSpec
from statbrief.sources.http import CacheScope, HttpPolicy, SourceError, cached_fetch, fetch_json, utc_now
from statbrief.sources.results import FetchResult, SeriesMeta, SkippedValue

SOURCE = "eurostat"
_GEO_CODE = re.compile(r"[A-Z0-9_]{2,12}")
_SUPPORTED_FREQ = frozenset({"A", "Q", "M"})


def build_params(spec: IndicatorSpec, geos: Sequence[str], *, since: str | None = None, until: str | None = None,
                 last_n: int | None = None) -> list[tuple[str, str]]:
    """Query parameters for one series request (ValueError for bad geos or time bounds)."""
    if not geos:
        raise ValueError("at least one region is required")
    for g in geos:
        if not _GEO_CODE.fullmatch(g):
            raise ValueError(f"invalid Eurostat geo code {g!r}")
    params = [("format", "JSON"), ("lang", "EN")]
    params += [(k, spec.filters[k]) for k in sorted(spec.filters)]
    params += [("geo", g) for g in geos]
    try:
        if since is not None:
            params.append(("sinceTimePeriod", parse_period(since).period))
        if until is not None:
            params.append(("untilTimePeriod", parse_period(until).period))
    except PeriodError as exc:
        raise ValueError(f"invalid time bound: {exc}") from exc
    if last_n is not None:
        if last_n < 1:
            raise ValueError("last_n must be at least 1")
        params.append(("lastTimePeriod", str(last_n)))
    return params


def _categories(dimension: dict) -> list[str]:
    """Category codes of a JSON-stat dimension in index order (index as dict or list)."""
    index = dimension["category"]["index"]
    if isinstance(index, list):
        return [str(c) for c in index]
    return [str(c) for c, _ in sorted(index.items(), key=lambda kv: kv[1])]


def _cell(container: Any, flat: int) -> Any:
    """Entry of a sparse (dict keyed by str index) or dense (list) JSON-stat array, or None."""
    if isinstance(container, dict):
        return container.get(str(flat))
    if isinstance(container, list):
        return container[flat] if flat < len(container) else None
    return None


def _structure(payload: Any) -> tuple[list[str], list[int], dict[str, list[str]]]:
    """(ids, sizes, categories per dimension) or SourceError for anything that is not JSON-stat 2.0."""
    try:
        ids = [str(i) for i in payload["id"]]
        sizes = [int(s) for s in payload["size"]]
        cats = {d: _categories(payload["dimension"][d]) for d in ids}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SourceError(SOURCE, None, "response is not a JSON-stat dataset") from exc
    if len(ids) != len(sizes) or any(len(cats[d]) != n for d, n in zip(ids, sizes, strict=True)):
        raise SourceError(SOURCE, None, "response is not a JSON-stat dataset")
    if "geo" not in cats or "time" not in cats:
        raise SourceError(SOURCE, None, "response has no geo or time dimension")
    return ids, sizes, cats


def _check_series(spec: IndicatorSpec, cats: dict[str, list[str]]) -> None:
    """Exactly one category per non-geo/time dimension, matching every filter (else SourceError)."""
    for key, value in spec.filters.items():
        if key not in cats:
            raise SourceError(SOURCE, None, f"filter {key!r} is not a dimension of {spec.dataset}")
        if cats[key] != [value]:
            raise SourceError(SOURCE, None, f"response does not honour filter {key}={value}")
    for dim, codes in cats.items():
        if dim not in ("geo", "time") and len(codes) != 1:
            raise SourceError(SOURCE, None, f"dimension {dim!r} has {len(codes)} categories; add a filter "
                                            "so that one series is returned")


def _frequency(spec: IndicatorSpec, cats: dict[str, list[str]]) -> str | None:
    """The series frequency from `freq` (checked against the spec), or the spec's, or None."""
    freq = cats["freq"][0] if "freq" in cats else spec.frequency
    if freq is not None and freq not in _SUPPORTED_FREQ:
        raise SourceError(SOURCE, None, f"frequency {freq!r} is not supported (A, Q or M only)")
    if spec.frequency is not None and freq != spec.frequency:
        raise SourceError(SOURCE, None, f"frequency {freq!r} differs from the catalog's {spec.frequency!r}")
    return freq


def _unit(spec: IndicatorSpec, cats: dict[str, list[str]]) -> str:
    """The spec's unit, else the response's single unit code; SourceError when unknown."""
    if spec.unit:
        return spec.unit
    if "unit" in cats:
        return cats["unit"][0]
    raise SourceError(SOURCE, None, "the unit of this series is unknown; add a unit filter or state the unit")


def _label(payload: dict, dim: str, code: str) -> str | None:
    """Category label of one code, if present."""
    labels = payload.get("dimension", {}).get(dim, {}).get("category", {}).get("label", {})
    label = labels.get(code) if isinstance(labels, dict) else None
    return str(label) if label is not None else None


def parse_jsonstat(payload: Any, spec: IndicatorSpec, query_url: str, retrieved_at: datetime, *,
                   requested_geos: Sequence[str] = ()) -> FetchResult:
    """Convert one Eurostat JSON-stat 2.0 response into observations plus skipped values (see module doc)."""
    ids, sizes, cats = _structure(payload)
    meta = SeriesMeta(payload.get("label"), _label(payload, "unit", cats["unit"][0]) if cats.get("unit") else None,
                      payload.get("updated"))
    result = FetchResult(meta=meta)
    result.skipped += [SkippedValue(g, None, "missing_value") for g in requested_geos if g not in cats["geo"]]
    empty = [d for d, n in zip(ids, sizes, strict=True) if n == 0]
    if empty and empty != ["time"]:
        dim = next(d for d in empty if d != "time")
        what = f"filter {dim}={spec.filters[dim]}" if dim in spec.filters else f"dimension {dim!r}"
        raise SourceError(SOURCE, None, f"no data for {what}")
    if empty:
        return result  # only the time range is empty: a valid, empty selection
    _check_series(spec, cats)
    freq = _frequency(spec, cats)
    unit = _unit(spec, cats)
    canon = spec.canonical_filters
    provenance = ApiProvenance(api_name=SOURCE, series_id=f"{spec.dataset}?{canon}", query_url=query_url,
                               retrieved_at=retrieved_at)
    label = spec.label if spec.curated else (meta.label or spec.label)
    strides = [prod(sizes[i + 1:]) for i in range(len(sizes))]
    geo_axis, time_axis = ids.index("geo"), ids.index("time")
    values, status = payload.get("value"), payload.get("status")
    seen: set[str] = set()
    for (gi, geo), (ti, tcode) in product(enumerate(cats["geo"]), enumerate(cats["time"])):
        flat = gi * strides[geo_axis] + ti * strides[time_axis]  # every other axis has size 1 (position 0)
        flag = _cell(status, flat)
        flag = str(flag) if flag not in (None, "") else None
        region = resolve_region(geo)
        if region is None:
            result.skipped.append(SkippedValue(geo, tcode, "unknown_geo", flag))
            continue
        raw = _cell(values, flat)
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):  # ":", null, NaN
            result.skipped.append(SkippedValue(geo, tcode, "missing_value", flag))
            continue
        try:
            period = parse_period(tcode)
        except PeriodError:
            result.skipped.append(SkippedValue(geo, tcode, "invalid_period", flag))
            continue
        if freq is not None and period.frequency != freq:
            result.skipped.append(SkippedValue(geo, tcode, "invalid_period", flag))
            continue
        obs_id = make_obs_id(SOURCE, spec.dataset, canon, region.code, period.period)
        if obs_id in seen:
            result.skipped.append(SkippedValue(geo, tcode, "duplicate_period", flag))
            continue
        seen.add(obs_id)
        result.observations.append(Observation(
            obs_id=obs_id, indicator_code=spec.code, indicator_label=label, region_code=region.code,
            region_level=region.level, period=period.period, frequency=period.frequency, value=float(raw),
            unit=unit, unit_multiplier=spec.unit_multiplier, status_flag=flag, provenance=provenance))
    return result


def fetch_eurostat(client: httpx.Client, spec: IndicatorSpec, geos: Sequence[str], *, settings: Settings,
                   since: str | None = None, until: str | None = None, last_n: int | None = None,
                   policy: HttpPolicy | None = None, cache: CacheScope | None = None,
                   sleep: Callable[[float], None] = time.sleep,
                   now: Callable[[], datetime] = utc_now) -> FetchResult:
    """Fetch (or read from the report cache) one Eurostat series and parse it.

    Raises ValueError for invalid arguments and SourceError for API or format failures.
    """
    if spec.source != SOURCE:
        raise ValueError("not a Eurostat indicator")
    params = build_params(spec, geos, since=since, until=until, last_n=last_n)
    url = f"{settings.eurostat_base_url}/{spec.dataset}"
    pol = policy or HttpPolicy.from_settings(settings)
    resp = cached_fetch(cache, SOURCE, url, params,
                        lambda: fetch_json(client, url, params, policy=pol, source=SOURCE, sleep=sleep), now=now)
    return parse_jsonstat(resp.payload, spec, resp.url, resp.retrieved_at, requested_geos=geos)
