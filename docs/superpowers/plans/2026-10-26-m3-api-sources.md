# M3 — Public API Sources, Combining, Conflicts — Plan (lean, fast mode D-026)

> One developer implements all tasks in order (TDD, commit per task); one reviewer reviews the milestone once. Runs in parallel with M5 in its own worktree/branch `m3-api-sources`.

**Goal:** Fetch Eurostat and World Bank series into the common `Observation` schema with API provenance, cache raw responses per report (resume never re-fetches), combine them with uploaded observations, detect value conflicts, and offer consent-based gap filling (AC 7–8, 18–22).

**Constraints:** every tunable in `Settings` (+ `.env.example`); docstrings; tests use recorded JSON fixtures (no network) — one optional live test per client marked `@pytest.mark.live` and skipped unless `RUN_LIVE_TESTS=1`; httpx only (no new HTTP libs); migration number **003** (M5 uses 004); never guess: unknown series/units are reported, not invented; commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Existing APIs to use:** `statbrief.domain.observation` (`Observation`, `ApiProvenance(api_name, series_id, query_url, retrieved_at)`, `make_obs_id`, `Frequency`, `RegionLevel`), `statbrief.ingest.regions.resolve_region` (Eurostat codes: EL, UK; EU27_2020), `statbrief.ingest.periods.parse_period`, `statbrief.storage.repos.ObservationRepo`, `statbrief.storage.db` migrations.

## Task 1 — Catalog (`src/statbrief/sources/catalog.py`)
- `IndicatorSpec(code, label, source: Literal["eurostat","worldbank"], dataset, filters: dict[str,str], unit, frequency, is_rate: bool, region_levels)`; `CATALOG: dict[str, IndicatorSpec]` with the D-005 set:
  - `HICP_YOY` → eurostat `prc_hicp_manr`, filters `coicop=CP00, unit=RCH_A`, M, rate.
  - `UNEMP_RATE` → eurostat `une_rt_m`, `s_adj=SA, age=TOTAL, sex=T, unit=PC_ACT`, M, rate.
  - `GDP_VOL` → eurostat `namq_10_gdp`, `na_item=B1GQ, s_adj=SCA, unit=CLV10_MEUR`, Q, level.
  - `IND_PROD` → eurostat `sts_inpr_m`, `nace_r2=B-D, s_adj=SCA, unit=I21`, M, level.
  - `GDP_PC_REG` → eurostat `nama_10r_2gdp`, `unit=EUR_HAB`, A, level, NUTS2.
  - `WB_GDP_GROWTH` (`NY.GDP.MKTP.KD.ZG`), `WB_CPI` (`FP.CPI.TOTL.ZG`), `WB_POP` (`SP.POP.TOTL`) → worldbank, A.
  - **Verify every dataset/filter code against the live Eurostat metadata** (developer may use the network once to confirm and to record fixtures); if a code differs, fix it and note it in the report.
- `advanced_spec(source, code, filters) -> IndicatorSpec` for uncurated codes (label "uncurated"; unit taken only from the API response).
- Tests: catalog codes unique; every spec has frequency/unit; advanced spec flagged uncurated.

## Task 2 — HTTP base + cache (`sources/http.py`, migration `003_api_cache.sql`, `storage/repos.py: ApiCacheRepo`)
- `fetch_json(client, url, params, *, timeout_s, retries, backoff_s) -> (json, final_url)`; retries on 429/5xx/timeouts with exponential backoff (Settings: `api_timeout_s=20`, `api_retries=3`, `api_backoff_s=1.0`); raises `SourceError(source, status, message)` otherwise (no raw body in messages).
- `api_cache(report_id uuid FK cascade, user_id, request_hash text, source, url text, payload jsonb, retrieved_at, PRIMARY KEY(report_id, request_hash))`; `ApiCacheRepo.get/put` user-scoped. `request_hash = sha256(source + canonical sorted params)`.
- Tests with `httpx.MockTransport`: retry then success; 404 → SourceError no retry; timeout retried; cache round-trip and user scoping.

## Task 3 — Eurostat client (`sources/eurostat.py`)
- Statistics API `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}` (base URL in Settings `eurostat_base_url`), params `format=JSON, lang=EN`, filters, `geo=` (repeatable), `sinceTimePeriod`/`untilTimePeriod` (verify current param names).
- `parse_jsonstat(payload, spec, query_url, retrieved_at) -> FetchResult(observations, skipped: list[SkippedValue(reason)], meta: SeriesMeta(label, unit_label, last_update))`: JSON-stat 2.0 (`id`, `size`, `dimension.*.category.index/label`, sparse `value`, `status`); missing values (no entry or ":") → skipped with reason (never invented); status flags `p`, `e`, `b`… → `status_flag`; periods via `parse_period` (`2024-03`, `2024M03`, `2024-Q1`, `2024Q1`, `2024`); geo via `resolve_region` (unknown geo → skipped with reason).
- `obs_id = make_obs_id("eurostat", dataset, canonical_filters, geo, period)`; provenance `ApiProvenance("eurostat", series_id=f"{dataset}?{canonical_filters}", query_url, retrieved_at)`.
- Tests on recorded fixtures (commit under `tests/data/eurostat/`): multi-geo HICP, quarterly GDP, NUTS2 regional, missing values + flags, unknown geo; `@live` test fetching RO HICP last 3 months.

## Task 4 — World Bank client (`sources/worldbank.py`)
- `https://api.worldbank.org/v2/country/{iso3;iso3}/indicator/{id}?format=json&date=YYYY:YYYY&per_page=1000` (base in Settings); follow pagination (`pages`); `[meta, data]`; `value: null` → skipped; ISO3 → Eurostat geo via a small mapping (`ROU→RO`, `HUN→HU`, `BGR→BG`, `GRC→EL`, `GBR→UK`, EU27 members, `MDA→MD`, `UKR→UA`, `SRB→RS`; aggregates `EUU→EU27_2020`); unknown → skipped. Error payload `[{"message": …}]` → SourceError.
- Tests on recorded fixtures incl. pagination and an error payload; `@live` RO GDP growth.

## Task 5 — Combine, conflicts, gap filling (`analytics/combine.py`, `domain/conflict.py`)
- `IndicatorLink(upload_code, catalog_code)` lets uploaded indicators be compared with API series (user-supplied; never inferred).
- `combine(upload_obs, api_obs, links) -> list[Observation]` (applies links to a copy of upload codes for comparison only; provenance untouched).
- `Conflict(conflict_id, indicator, region, period, unit, candidates: list[obs_id], values, rel_diff)`; `detect_conflicts(obs, *, rel_tol, ignore_rounding)` — same (indicator, region, period, unit, unit_multiplier) from **different sources** whose values differ by more than `max(rel_tol·|ref|, rounding unit of the coarser value)` (Settings `conflict_rel_tol=0.005`, D-010); values equal after rounding are not conflicts; different units are never compared (reported separately as `UnitMismatch`).
- `ConflictResolution(conflict_id, choice: Literal["prefer","keep_both","exclude"], preferred_obs_id=None)`; `apply_resolutions(obs, conflicts, resolutions) -> (kept, excluded_with_reasons)`; unresolved conflicts → `UnresolvedConflicts`.
- `fill_gaps(upload_obs, api_obs, links, consent: bool) -> (filled, report)` — only with consent; filled points keep `ApiProvenance`; report lists every filled key.
- Tests: rounding-equal not a conflict (3.27 vs 3.3), 6.6 vs 6.2 conflict, unit mismatch reported not compared, prefer/keep_both/exclude, unresolved raises, gap fill off by default and provenance preserved.

## Done criteria
All tests green; no network in default test run; report at `.superpowers/sdd/m3-report.md` listing verified Eurostat/World Bank codes and any deviations.
