"""World Bank Indicators API (v2) client: paged JSON → Observations with API provenance.

Request: `GET {WORLDBANK_BASE_URL}/country/{ISO3;ISO3}/indicator/{id}?format=json&per_page=N[&date=Y1:Y2]`,
following `pages` from the first page's metadata (at most WORLDBANK_MAX_PAGES); all pages are cached
together per report. Every page is `[meta, rows]`; `[{"message": [...]}]` is an API error (SourceError).

Regions: requests take Eurostat-style codes (RO, EL, UK, EU27_2020) mapped to ISO3 via `ISO3_TO_GEO`;
response rows map back the same way. Unmapped requested regions and unmapped response countries
(e.g. Kosovo, XKX) → `unknown_geo`; `value: null` → `missing_value`; requested regions without rows →
`missing_value` with period None. The unit is the spec's (curated or user-stated), else the row's `unit`
field; World Bank rows usually leave it empty, so an uncurated indicator without a stated unit raises.
"""
import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import httpx

from statbrief.config import Settings
from statbrief.domain.observation import ApiProvenance, Observation, make_obs_id
from statbrief.ingest.periods import PeriodError, parse_period
from statbrief.ingest.regions import resolve_region
from statbrief.sources.catalog import IndicatorSpec
from statbrief.sources.http import CacheScope, HttpPolicy, SourceError, cached_fetch, fetch_json, utc_now
from statbrief.sources.results import FetchResult, SeriesMeta, SkippedValue

SOURCE = "worldbank"
_YEAR_MIN, _YEAR_MAX = 1800, 2200

# ISO 3166-1 alpha-3 (World Bank) → Eurostat geo code: EU27 members, focus neighbours, the EU aggregate.
ISO3_TO_GEO: dict[str, str] = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "CYP": "CY", "CZE": "CZ", "DEU": "DE", "DNK": "DK", "EST": "EE",
    "GRC": "EL", "ESP": "ES", "FIN": "FI", "FRA": "FR", "HRV": "HR", "HUN": "HU", "IRL": "IE", "ITA": "IT",
    "LTU": "LT", "LUX": "LU", "LVA": "LV", "MLT": "MT", "NLD": "NL", "POL": "PL", "PRT": "PT", "ROU": "RO",
    "SWE": "SE", "SVN": "SI", "SVK": "SK",
    "MDA": "MD", "UKR": "UA", "SRB": "RS", "GBR": "UK",
    "EUU": "EU27_2020",
}
GEO_TO_ISO3: dict[str, str] = {geo: iso3 for iso3, geo in ISO3_TO_GEO.items()}


def _page(payload: Any) -> tuple[dict, list]:
    """(meta, rows) of one page; SourceError for an error payload or anything malformed."""
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict) and "message" in payload[0]:
        msgs = payload[0]["message"]
        first = msgs[0] if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict) else {}
        ident, key = str(first.get("id", "?"))[:16], str(first.get("key", "error"))[:80]
        raise SourceError(SOURCE, None, f"World Bank error {ident}: {key}")
    if not (isinstance(payload, list) and len(payload) == 2 and isinstance(payload[0], dict)
            and isinstance(payload[1], list | type(None))):
        raise SourceError(SOURCE, None, "response is not a World Bank [meta, rows] page")
    return payload[0], payload[1] or []


def _unit(spec: IndicatorSpec, row: dict) -> str:
    """The spec's unit, else the row's non-empty unit; SourceError when unknown."""
    if spec.unit:
        return spec.unit
    unit = str(row.get("unit") or "").strip()
    if unit:
        return unit
    raise SourceError(SOURCE, None, "the unit of this indicator is unknown; state the unit")


def parse_worldbank(pages: Sequence[Any], spec: IndicatorSpec, query_url: str, retrieved_at: datetime, *,
                    requested_geos: Sequence[str] = ()) -> FetchResult:
    """Convert World Bank response pages into observations plus skipped values (see module doc)."""
    parsed = [_page(p) for p in pages]
    rows = [r for _, page_rows in parsed for r in page_rows]
    meta_page = parsed[0][0] if parsed else {}
    label = None
    provenance = ApiProvenance(api_name=SOURCE, series_id=spec.dataset, query_url=query_url,
                               retrieved_at=retrieved_at)
    result = FetchResult()
    seen_geos: set[str] = set()
    seen_ids: set[str] = set()
    for row in rows:
        try:
            indicator = row["indicator"]["id"]
            iso3 = str(row.get("countryiso3code") or row["country"]["id"])
            date = str(row["date"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise SourceError(SOURCE, None, "unexpected World Bank row structure") from exc
        if indicator != spec.dataset:
            raise SourceError(SOURCE, None, f"response rows are for indicator {indicator!r}, not {spec.dataset!r}")
        label = label or row["indicator"].get("value")
        flag = str(row.get("obs_status") or "").strip() or None
        geo = ISO3_TO_GEO.get(iso3)
        region = resolve_region(geo) if geo else None
        if region is None:
            result.skipped.append(SkippedValue(iso3, date, "unknown_geo", flag))
            continue
        seen_geos.add(region.code)
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            result.skipped.append(SkippedValue(iso3, date, "missing_value", flag))
            continue
        try:
            period = parse_period(date)
        except PeriodError:
            result.skipped.append(SkippedValue(iso3, date, "invalid_period", flag))
            continue
        if spec.frequency is not None and period.frequency != spec.frequency:
            result.skipped.append(SkippedValue(iso3, date, "invalid_period", flag))
            continue
        obs_id = make_obs_id(SOURCE, spec.dataset, region.code, period.period)
        if obs_id in seen_ids:
            result.skipped.append(SkippedValue(iso3, date, "duplicate_period", flag))
            continue
        seen_ids.add(obs_id)
        result.observations.append(Observation(
            obs_id=obs_id, indicator_code=spec.code,
            indicator_label=spec.label if spec.curated else (label or spec.label), region_code=region.code,
            region_level=region.level, period=period.period, frequency=period.frequency, value=float(value),
            unit=_unit(spec, row), unit_multiplier=spec.unit_multiplier, status_flag=flag, provenance=provenance))
    result.skipped += [SkippedValue(g, None, "missing_value") for g in requested_geos
                       if g in GEO_TO_ISO3 and g not in seen_geos]
    result.meta = SeriesMeta(label, None, meta_page.get("lastupdated"))
    return result


def _check_years(since: int | None, until: int | None) -> str | None:
    """`date` parameter for a year range (both or neither bound); ValueError otherwise."""
    if since is None and until is None:
        return None
    if since is None or until is None or not (_YEAR_MIN <= since <= until <= _YEAR_MAX):
        raise ValueError("give both since and until years, with since <= until")
    return f"{since}:{until}"


def _fetch_pages(client: httpx.Client, url: str, params: list[tuple[str, str]], policy: HttpPolicy,
                 max_pages: int, sleep: Callable[[float], None]) -> tuple[dict, str]:
    """All pages as {"pages": [...]} plus the first page's final URL; SourceError on bad pagination."""
    first, first_url = fetch_json(client, url, params, policy=policy, source=SOURCE, sleep=sleep)
    meta, rows = _page(first)
    try:
        pages, total = int(meta.get("pages") or 1), int(meta.get("total") or 0)
    except (TypeError, ValueError) as exc:
        raise SourceError(SOURCE, None, "invalid World Bank pagination metadata") from exc
    if pages > max_pages:
        raise SourceError(SOURCE, None, f"response has {pages} pages (limit {max_pages}); narrow the request")
    collected, count = [first], len(rows)
    for n in range(2, pages + 1):
        page, _ = fetch_json(client, url, [*params, ("page", str(n))], policy=policy, source=SOURCE, sleep=sleep)
        page_meta, page_rows = _page(page)
        if str(page_meta.get("total")) != str(meta.get("total")):
            raise SourceError(SOURCE, None, "inconsistent pagination (data changed while paging)")
        collected.append(page)
        count += len(page_rows)
    if count != total:
        raise SourceError(SOURCE, None, "inconsistent pagination (row count differs from total)")
    return {"pages": collected}, first_url


def fetch_worldbank(client: httpx.Client, spec: IndicatorSpec, geos: Sequence[str], *, settings: Settings,
                    since: int | None = None, until: int | None = None, policy: HttpPolicy | None = None,
                    cache: CacheScope | None = None, sleep: Callable[[float], None] = time.sleep,
                    now: Callable[[], datetime] = utc_now) -> FetchResult:
    """Fetch (or read from the report cache) one World Bank indicator for Eurostat-style region codes.

    Raises ValueError for invalid arguments and SourceError for API or format failures.
    """
    if spec.source != SOURCE:
        raise ValueError("not a World Bank indicator")
    date = _check_years(since, until)
    unknown = [SkippedValue(g, None, "unknown_geo") for g in geos if g not in GEO_TO_ISO3]
    iso3 = [GEO_TO_ISO3[g] for g in geos if g in GEO_TO_ISO3]
    if not iso3:
        return FetchResult(skipped=unknown)
    url = f"{settings.worldbank_base_url}/country/{';'.join(iso3)}/indicator/{spec.dataset}"
    params = [("format", "json"), ("per_page", str(settings.worldbank_per_page))]
    if date:
        params.append(("date", date))
    pol = policy or HttpPolicy.from_settings(settings)
    resp = cached_fetch(cache, SOURCE, url, params,
                        lambda: _fetch_pages(client, url, params, pol, settings.worldbank_max_pages, sleep), now=now)
    try:
        pages = resp.payload["pages"]
    except (KeyError, TypeError) as exc:
        raise SourceError(SOURCE, None, "cached World Bank response is malformed") from exc
    result = parse_worldbank(pages, spec, resp.url, resp.retrieved_at, requested_geos=geos)
    result.skipped = unknown + result.skipped
    return result
