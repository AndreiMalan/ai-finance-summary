# Specification — AI Statistical Report Generator ("StatBrief")

> Requirements Agent output. Sources: the "Additional Requirements: Local Data Sources and File Uploads" brief (sections 1–8) and the 15 design decisions recorded in [decisions.md](decisions.md). Where the two conflict, decisions.md wins (e.g. Streamlit → FastAPI/HTMX).

## 1. One-sentence summary

A public, login-protected web app on Vercel that lets a user combine uploaded CSV/Excel/JSON statistics with Eurostat and World Bank data, validates and normalizes everything into one provenance-tracked schema, and runs a LangGraph workflow that computes metrics deterministically, drafts RAG-grounded commentary with an OpenAI model, verifies every number, pauses for human approval, and exports a cited PDF/Excel report.

## 2. Scope

**In scope:** everything in brief sections 1–8, adapted per decisions.md.
**Out of scope (v1):** email verification / password-reset emails, OAuth, LangSmith, Romanian-language reports, real INS/Eurostat methodology PDFs, multi-tenant admin UI.

## 3. Acceptance criteria

### A. Platform, auth, cost protection
1. App is deployed at a public `*.vercel.app` URL from a GitHub repo via Vercel Git integration.
2. Users can sign up (when `SIGNUPS_ENABLED=true`) and log in with email + password; passwords stored only as Argon2 hashes; sessions via signed HTTP-only cookies.
3. A demo user is created/updated on startup from `DEMO_USER_EMAIL` / `DEMO_USER_PASSWORD`.
4. Failed logins are rate-limited (default 5 / 15 min per email+IP).
5. Report runs are rate-limited per user (default 10/hour) and globally capped per day (`DAILY_LLM_CALL_CAP`); exceeding either shows a clear message and makes no LLM call.
6. Every file, report, preference, vector chunk and graph thread is scoped by `user_id`; a user can never read another user's data (enforced in queries, tested).

### B. Data sources & upload
7. User chooses source mode: **API only**, **Uploaded files only**, or **Combined**.
8. API mode supports the curated catalogue (Eurostat `prc_hicp_minr` — replaces the discontinued `prc_hicp_manr`, D-028 —, `une_rt_m`, `namq_10_gdp`, `sts_inpr_m`, `nama_10r_2gdp`; World Bank `NY.GDP.MKTP.KD.ZG`, `FP.CPI.TOTL.ZG`, `SP.POP.TOTL`) plus an "Advanced" free-entry dataset/indicator code marked *uncurated*.
9. Upload area accepts one or more `.csv`, `.xlsx`, `.xls`, `.json` files via drag-and-drop or picker; limits `MAX_UPLOAD_MB` (10) and `MAX_FILES_PER_REPORT` (5).
10. Files are validated by extension **and** content sniffing (magic bytes / parse attempt); `.xlsm`, macro-bearing or malformed files are rejected with a specific reason.
11. Macros, formulas, scripts are never executed: Excel read with `data_only=True` (cached values); a formula cell without a cached value is reported as an issue, not computed. CSV cells starting with `= + - @` are treated as text, and are escaped on Excel export.
12. Auto-detection proposes: header row, worksheet, column types, date column + format/frequency, region column (ISO-2 / NUTS code / name), indicator column, value column, unit column — each with a confidence score.
13. The user can override every mapping (worksheet, header row, date, region, indicator, value, unit) and set constants (e.g. "all rows are unit = %") when a column is absent.
14. A preview grid shows the first N rows before processing, with invalid cells highlighted after validation.
15. Validation reports: missing required fields, duplicate records, missing values, invalid dates, non-numeric values, inconsistent units, unknown region codes, columns with no documented meaning.
16. No record is silently dropped: every excluded row appears in the issue list with its reason, and the user must accept exclusions explicitly.
17. A downloadable CSV/XLSX template with the expected long format is available.

### C. Normalization, provenance, conflicts
18. API responses and uploaded files are converted to one `Observation` schema (Pydantic) with: `obs_id, indicator_code, indicator_label, region_code, region_level, period (ISO), frequency (A/Q/M), value, unit, unit_multiplier, status_flag, source`.
19. Each observation's `source` carries provenance: `source_type (upload|api)`, and either `filename, worksheet, row_number` or `api_name, dataset/series id, query URL, retrieved_at`.
20. Datasets join by `(indicator, region, period, unit)`; frequency mismatches are surfaced, never silently resampled (aggregation only when the user opts in and the method is recorded).
21. Conflicts (same key, different values beyond tolerance: >0.5 % relative or > source rounding unit, whichever larger) are detected and shown side-by-side; drafting is blocked until each is resolved (choose source / keep both labelled / exclude).
22. Missing-data enrichment: in Combined mode, gaps in uploaded data may be filled from API data only with explicit user consent, and filled points keep API provenance.

### D. Analysis & verification (deterministic)
23. Metrics computed with pandas only: latest value, YoY, MoM, absolute change, % change, percentage-point change for rates.
24. A metric is computed only when valid: YoY needs t−1y; MoM needs monthly frequency; % change not applied to rates (pp instead); no cross-unit math. Skipped metrics record a reason.
25. Significant changes flagged by configurable thresholds (default |ΔYoY| ≥ 1 pp for rates, ≥ 2 % for levels; plus top-3 movers).
26. The LLM returns `NumericalClaim` objects; a deterministic verifier recomputes each claim from its `observation_ids` / `metric_id`, applies precision-aware rounding comparison, checks unit/period/region, checks prose number == claim value, and rejects numbers in prose without a claim.
27. Failed verification routes back to drafting with targeted feedback; max `MAX_REVISIONS` (3); exhausted → safe failure state "needs manual attention", never exported.

### E. LLM, RAG, tools
28. Chat model and embeddings configured via env (`LLM_PROVIDER`, `LLM_MODEL`, `EMBEDDINGS_MODEL`); default OpenAI mini-tier.
29. Reusable prompt templates for drafting, data interpretation, and revision include region, period, metrics, source metadata and retrieved methodological notes.
30. Draft output is a validated Pydantic `ReportDraft` (findings, sections, claims, citations); malformed output is retried (max 2) then routed to failure.
31. RAG corpus: ~9 invented reference documents from fictional issuers **ESMB** and **IRS** (PDF/TXT/MD), plus live API metadata, plus user-uploaded reference docs (PDF/TXT/MD, per-user). Loaded with LangChain loaders, split with text splitters, embedded, stored in pgvector with source metadata (doc, page/section, issuer, owner).
32. Retrieved passages are cited in the report with document + section/page.
33. Controlled tools: `get_indicator_metadata`, `list_available_metrics`, `query_observations` (bounded row count, user-scoped, Pydantic-validated args), `compute_metric` (deterministic). Invalid args are rejected with an error message to the model.
34. Columns/units without metadata are flagged "meaning unknown"; the LLM is instructed and checked not to interpret them (demo: `val_adj_x`).
35. Only summaries/selected rows are sent to the LLM — never whole uploaded files.

### F. LangGraph workflow
36. One `StateGraph` with typed state (`ReportState` TypedDict) containing parameters, file refs, source metadata, normalized data refs, metrics, retrieved context, drafts, validation errors, retry counts, reviewer decision, export status.
37. Nodes: source selection, ingestion (subgraph, Send fan-out per source), validation & normalization, conflict resolution, analysis (Send fan-out per region×indicator), retrieval (subgraph), drafting, claim verification, human review, export, failure.
38. Conditional edges: invalid dataset → mapping/correction interrupt; failed verification → drafting; exhausted retries → failure; verified → human review; rejected → drafting with comments or end.
39. `interrupt()` pause points: (1) mapping/issue/conflict resolution when needed, (2) final review (approve / reject / request changes with comment).
40. Postgres checkpointer with `thread_id = report_id`; resume continues without re-fetching APIs or losing mappings/validation results.
41. Streaming (`updates` + `custom`) to the browser via SSE as a live progress timeline.
42. History tab lists checkpoints, shows prior drafts, supports fork-from-checkpoint (`ENABLE_REPLAY`).
43. Long-term preferences (regions, indicators, style, export format) in LangGraph `PostgresStore` namespaced by user; never used as a statistical source.
44. Errors (API failure, timeout, model error, malformed upload) handled with bounded retries and explicit fallback (e.g. "continue without World Bank?" interrupt).
45. Graph runs in request-sized segments split at interrupts so no Vercel request exceeds the function time limit.
46. `ENABLE_PARALLEL`, `ENABLE_VECTOR_SEARCH` (fallback: Postgres full-text), `ENABLE_REPLAY` toggles; core path works with all off.

### G. Reports, retention, observability
47. PDF (reportlab) with cover, executive summary, per-indicator sections + charts, data-quality notes, citations grouped *Uploaded files / Public APIs / Reference documents*, calculation appendix.
48. Excel (openpyxl, values only) with sheets Summary, Data, Metrics, Provenance, Conflicts, Citations.
49. Every figure in the report traces to observations → file/sheet/row or API series/URL (clickable in the review UI).
50. Retention: raw files 30 d after last activity on unfinished reports / 7 d after approval / immediately on delete; checkpoints +30 d; logs 14 d; daily Vercel Cron cleanup; "Delete my account" removes everything.
51. Callback handler logs model calls, latency, tokens, tool calls, retrieval doc IDs, errors to `run_events`, without raw data values; Diagnostics tab per report.

### H. Demonstration scenarios (brief §7)
52. Upload CSV/XLSX and preview. 53. Map columns and resolve issues. 54. Report from uploads only. 55. Report from uploads + API. 56. Trace figures to file/sheet/row or API series. 57. Detect and resolve conflicts before drafting. 58. Resume a paused report with mappings intact. 59. Export an approved report whose citations distinguish local files from APIs.

### I. Deliverables outside the app
60. `docs/concepts/concepts.tex` + compiled `concepts.pdf` (local only): cover, clickable TOC, one page per LangChain/LangGraph concept (what it is · how we used it · where in code · how to demo · tests · core/optional).
61. Dummy datasets (IRS workbook, CSV with planted issues and conflicts, JSON sample) and the RAG corpus committed under `sample_data/` and `knowledge_base/`.
62. Automated tests cover: structured-output failures, retrieval relevance + source metadata, tool argument validation, conditional routing, revision limits, parallel aggregation, interrupted-workflow resumption, numerical claim verification. Tests run without an API key (fake chat models / fake embeddings).

## 4. Edge cases to handle

- Excel with multiple sheets, merged header cells, header not in row 1, trailing totals rows, hidden sheets.
- `.xls` (legacy) files; password-protected workbooks (reject); empty files; files with only headers.
- CSV with `;` delimiter, decimal comma (`3,4`), BOM, non-UTF-8 encoding (cp1250 / latin-1), thousands separators.
- Dates as Excel serials, `2024M03`, `2024-Q1`, `03/2024`, `Mar 2024`, ambiguous `01/02/2024` (ask, don't assume).
- Region given as name ("Cluj"), code ("RO113"), ISO-2 ("RO"), or Eurostat `EL`/`UK` vs ISO `GR`/`GB`.
- Units: "%", "percent", "pct"; index bases (2015=100 vs 2021=100) treated as different units → conflict, not merge.
- Duplicate rows (exact) vs duplicate keys with different values (conflict within one file).
- Eurostat flags (`p` provisional, `e` estimated, `:` missing); World Bank `null` values; API pagination; API 5xx / timeout.
- JSON as list-of-records, `{data: [...]}` wrapper, or deeply nested → reject with mapping guidance if not tabular.
- Series too short for YoY; a gap in the middle of the series; single-period datasets.
- LLM returns invalid JSON, missing fields, a claim citing a nonexistent `obs_id`, or a number in prose without a claim.
- User closes the browser mid-run; a second tab resumes the same thread concurrently (lock per thread).
- Vercel function timeout mid-segment → checkpoint-safe resume.
- Daily cap reached mid-revision → pause, not crash.
- Account deletion while a run is in progress.

## 5. Dependencies affected

Greenfield — no existing code. New external dependencies:

| Area | Dependency |
|---|---|
| Web | FastAPI, Jinja2, uvicorn (local), HTMX, Alpine.js, Tailwind + DaisyUI, Tabulator, ECharts (CDN) |
| LLM | langchain-core, langchain, langchain-openai, langchain-text-splitters, langchain-community (loaders), langchain-postgres (PGVector) |
| Graph | langgraph, langgraph-checkpoint-postgres (PostgresSaver + PostgresStore) |
| Data | pandas, openpyxl, xlrd (.xls), pypdf, python-dateutil |
| DB | psycopg[binary] 3, Neon Postgres + pgvector |
| Storage | Vercel Blob (HTTP API) |
| Auth | argon2-cffi, itsdangerous |
| Export | reportlab, openpyxl |
| Test | pytest, pytest-asyncio, httpx, langchain fake models |
| External APIs | Eurostat Statistics API (JSON-stat 2.0), World Bank API v2 |
| Docs | MiKTeX (local) |

**Constraint watch:** Vercel Python bundle ≤ ~250 MB unzipped and function duration cap — no local ML models, no FAISS/Chroma, no headless browser.

## 6. Verdict: **GO**

**Reasoning:** every brief requirement maps to a concrete, testable feature; the one blocking conflict (Streamlit vs Vercel) is resolved by decision 2; the main risks — Vercel duration/bundle limits and serverless DB connections — have explicit mitigations (segmented graph execution at interrupt boundaries, API-based embeddings, pgvector on Neon, pooled connections). Risks to monitor: bundle size with pandas + langchain + reportlab (measure at milestone 1), Eurostat API latency, and LangGraph PostgresSaver behaviour with Neon's pooler (verify at milestone 1/7).
