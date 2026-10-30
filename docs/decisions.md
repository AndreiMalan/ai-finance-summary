# Decision Log

Format: **ID — Title** · date · decision · rationale · alternatives rejected.

## D-001 — Greenfield build
2026-10-25 · Build the entire app from scratch; the "Additional Requirements" brief is the whole spec.
Rationale: project folder was empty; no prior code or spec exists.

## D-002 — UI framework and hosting: FastAPI + HTMX fully on Vercel (no Streamlit)
2026-10-25 · FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind/DaisyUI + Tabulator + ECharts; SSE for streaming. Deployed entirely on Vercel.
Rationale: Streamlit (and Gradio/NiceGUI/Panel) need persistent WebSocket servers, impossible on Vercel serverless; user requires Vercel. HTMX server-rendered pages fit request/response and LangGraph interrupt/resume naturally.
Rejected: Streamlit on Render + Vercel landing page; Next.js + separate Python backend; Dash.
Consequence: brief's "Streamlit" references mean "the web UI".

## D-003 — LLM provider: OpenAI, provider-agnostic code
2026-10-25 · OpenAI for chat (mini-tier default) and embeddings; `init_chat_model` + env vars so provider/model are switchable without code changes.
Rationale: single account/key, cheapest setup. Rejected: Anthropic + Voyage (two accounts).

## D-004 — Simple login + cost protections
2026-10-25 · Email + password (Argon2) in Postgres, signed HTTP-only session cookies, demo account from env vars, `SIGNUPS_ENABLED` switch, login rate limiting. No email verification/reset, no OAuth. Cost: per-user run rate limit, global daily LLM cap, upload limits, OpenAI dashboard budget.
Rationale: self-contained, no extra accounts; everything keyed by `user_id` so OAuth can be added later.

## D-005 — Data catalogue and focus
2026-10-25 · Curated Eurostat (`prc_hicp_manr`, `une_rt_m`, `namq_10_gdp`, `sts_inpr_m`, `nama_10r_2gdp`) and World Bank (`NY.GDP.MKTP.KD.ZG`, `FP.CPI.TOTL.ZG`, `SP.POP.TOTL`) indicators + "Advanced" uncurated code entry. Default focus Romania vs EU/neighbours.

## D-006 — RAG corpus: invented documents from fictional issuers
2026-10-25 · ~9 realistic regulations/manuals/data dictionaries authored for the demo, attributed to fictional **ESMB** (European Statistical Methods Board) and **IRS** (Institutul Român de Statistică). No in-document "synthetic" label; one-line note on the About page. Real Eurostat/World Bank appear only as live API sources.
Rationale: user wants realistic official-looking documents; attributing invented regulations to real institutions (INS, Eurostat) on a public site would impersonate them — rejected.

## D-007 — Language: English everywhere

## D-008 — Report structure
2026-10-25 · PDF (reportlab, charts via reportlab.graphics): cover, executive summary, per-indicator sections with charts, data-quality notes, citations grouped Uploaded files / Public APIs / Reference documents, calculation appendix. Excel (openpyxl, values only): Summary, Data, Metrics, Provenance, Conflicts, Citations.
Rationale: WeasyPrint/headless-browser rendering unavailable on Vercel.

## D-009 — Retention policy
2026-10-25 · Raw files: 30 d after last activity (unfinished) / 7 d after approval / immediately on delete. Normalized data + provenance: lifetime of the report. Checkpoints: active + 30 d. Logs: 14 d, no raw values. Daily Vercel Cron cleanup. "Delete my account". Raw files in private Vercel Blob (fallback: Postgres bytea). All periods env-configurable.

## D-010 — Verification and revision rules
2026-10-25 · Structured `NumericalClaim`; deterministic recompute; precision-aware rounding match; prose-vs-claim check; `MAX_REVISIONS=3`, parse retries 2, then safe failure. Conflict: > max(0.5 % relative, rounding unit). Significance: ≥1 pp (rates), ≥2 % (levels), top-3 movers. Invalid calculations recorded, never guessed.

## D-011 — LangGraph feature mapping
2026-10-25 · Send fan-out for ingestion (per source) and analysis (per region×indicator); subgraphs for ingestion, retrieval, drafting; `stream_mode=["updates","custom"]` over SSE; two interrupt points (mapping/conflicts, final review); PostgresSaver with `thread_id=report_id`; History tab with replay/fork; PostgresStore for preferences. Graph executes in segments at interrupt boundaries to stay under Vercel time limits. Toggles: `ENABLE_PARALLEL`, `ENABLE_VECTOR_SEARCH`, `ENABLE_REPLAY`.

## D-012 — Observability: built-in only
2026-10-25 · Custom callback handler → Postgres `run_events`; Diagnostics tab per report; redaction of raw values. No LangSmith.

## D-013 — Concepts documentation: one local LaTeX PDF
2026-10-25 · `docs/concepts/concepts.tex` → `concepts.pdf`: cover, clickable TOC, one page per concept, LangChain then LangGraph sections. Local only, not deployed.

## D-014 — Deployment: GitHub + Vercel Git integration
2026-10-25 · Private GitHub repo connected to Vercel; Neon Postgres and Blob added via Vercel Storage; Vercel CLI used locally for env/logs.

## D-015 — Build process
2026-10-25 · Phase 0 (spec, decisions, brainstorm, architecture, UI mockup) → user approval → 9 milestones each Developer → Review (≥8/10) → QA, TDD, Opus subagents, fake LLMs in tests → Retro.

## D-016 — Empty environment values mean "unset"
2026-10-25 · `Settings` uses `env_ignore_empty=True`: a blank line such as `DEMO_USER_PASSWORD=` behaves as if the variable were absent (default or "required" error), never as an empty secret.
Rationale: `.env.example` ships blank placeholders; treating them as empty strings would create a demo user with an empty password.

## D-017 — Stateless signed session cookies (no server-side session table)
2026-10-26 · Sessions are itsdangerous-signed cookies holding only the user id and a remember flag. "Remember me" tokens are valid for `SESSION_MAX_AGE_DAYS` (30); other tokens for `SESSION_SHORT_MAX_AGE_HOURS` (12), enforced server-side. Logout clears the cookie but cannot revoke a copied token before it expires; rotating `SESSION_SECRET` logs everyone out.
Rationale: no extra table or lookup per request on serverless; acceptable for a demo app. Revocable server-side sessions can be added later (sessions table keyed by a random id) without changing callers of `issue_session/read_session`.

## D-018 — CSRF and proxy-header trust
2026-10-26 · M1 relies on `SameSite=Lax` session cookies (blocks cookie-bearing cross-site POSTs). Synchronizer CSRF tokens on all state-changing forms (including login, to stop login-CSRF) are added in M8 together with the report forms. `client_ip` trusts `X-Forwarded-For`, which is correct behind Vercel (the platform overwrites it); a `TRUST_PROXY_HEADERS` switch is added if the app is ever exposed without a proxy.

## D-019 — Vercel packaging: root `app.py`, deps in `pyproject.toml`, region `fra1`
2026-10-26 · Root `app.py` exposes the ASGI `app` (adds `src/` to `sys.path`; the package is not pip-installed on Vercel). `pyproject.toml` `[project].dependencies` mirrors `requirements.txt` exactly (test-enforced) because Vercel's uv-based builder installs from `pyproject.toml` when present. `vercel.json`: `regions: ["fra1"]` (same region as Neon), `functions."app.py".maxDuration = 300` (Hobby maximum). Bundle budget is Vercel's documented 500 MB Python limit; R1 measured 373 MB. Details and doc links: `docs/spikes.md`.

## D-020 — Uploads go directly from the browser to Vercel Blob
2026-10-26 · Vercel functions reject request bodies over 4.5 MB, so files are not uploaded through the FastAPI function. The browser uploads straight to private Vercel Blob using a short-lived client token issued by an authenticated app endpoint (scoped to the user and report, size-capped at `MAX_UPLOAD_MB`); the app then reads the blob server-side for sniffing/validation. Local dev/tests keep a direct multipart path. Planned in M2.
Rationale: keeps the agreed 10 MB file limit (D-004/AC 9) without hitting the platform limit; also avoids holding large bodies in function memory.

## D-021 — Chunked uploads through the app, stored with the official Python Blob SDK (supersedes D-020's mechanism)
2026-10-26 · The browser sends each file in chunks of ≤ 4 MB (`UPLOAD_CHUNK_MB`, below Vercel's 4.5 MB body limit) to an authenticated, user-scoped endpoint; chunks are staged in Postgres, and when the last chunk arrives the server reassembles, sniffs and validates the file, then stores it in the **private** Blob store with `vercel.blob.put_blob(..., access="private")` (package `vercel-blob` ≥ 0.5.0; auth via `BLOB_STORE_ID` + Vercel OIDC, no static token). Downloads stream server-side with `stream_blob` after an ownership check. Local dev/tests use `LocalBlobStore` (filesystem) behind the same `BlobStore` protocol.
Rationale: the Python SDK supports private put/get/delete with OIDC but not client uploads or signed URLs (Vercel docs, Python SDK reference); chunking keeps the agreed 10 MB limit with only documented APIs and no Node function.

## D-022 — A misconfigured demo account never takes the site down
2026-10-26 · Supersedes the "fail loudly at startup" choice made in M1 Task 6. If `DEMO_USER_EMAIL`/`DEMO_USER_PASSWORD` are invalid (bad email, password outside the length bounds, or the email belongs to a regular account) the error is logged, `/healthz` reports `"demo_user": "misconfigured"`, and the app keeps serving without the demo account. Infrastructure errors during start-up still fail (and close the pool).
Rationale: production incident 2026-10-26 — a 9-character demo password made every page return 500.

## D-023 — Bounded, self-healing database connections
2026-10-26 · Pooled connections are checked on checkout (`ConnectionPool.check_connection`) so backends Neon closes while idle are replaced instead of failing a request. Every connection carries libpq `connect_timeout` (`DB_CONNECT_TIMEOUT_S`, default 5 s, rounded up to whole seconds), and `pool.open` waits at most `DB_POOL_OPEN_TIMEOUT_S` (default 10 s). `/healthz` reports 503 for database errors, `PoolTimeout` and the LangGraph setup-lock `TimeoutError`. In prod a missing `DATABASE_URL_UNPOOLED` is logged as a warning at bootstrap.
Rationale: final M1 review — a cold start with the database down was unbounded and returned 500; stale pooled connections after Neon scale-to-zero caused errors.

## D-024 — Login throttle key is the normalised email
2026-10-26 · The per-email login throttle key is `normalize_email(raw)` (falling back to `raw.strip().lower()` for invalid input), then capped at 254 characters; the same key is recorded and echoed in the form.
Rationale: final M1 review — truncating before normalising let whitespace-padded or re-cased variants of one address each get a fresh failure budget (AC 4 bypass).

## D-025 — Embedded objects and external links in workbooks
2026-10-26 · `.xlsx` files containing embedded OLE objects (`xl/embeddings/*`) or external workbook links (`xl/externalLinks/*`) are accepted: embedded objects are never opened and links are never followed; only cell values stored in the workbook are read (`data_only=True`). Macro-bearing content (vbaProject.bin, XLM macro sheets, dialog sheets, ActiveX, macro-enabled content types; VBA storages in `.xls`) is rejected (Task 2 review).
Rationale: real-world statistical workbooks often carry logos or links; rejecting them would block legitimate files without improving safety, since nothing in them is executed.

## D-026 — Fast mode for this project
2026-10-26 · From M2 Task 9 onward: one developer + one reviewer (both Opus) per milestone batch, single review pass (fix Critical/Important with test evidence, re-review only for Critical or score < 6), minors deferred to the milestone's final check, independent milestones in parallel worktrees, leaner plans. Recorded in the project CLAUDE.md; the user's global pipeline is unchanged.
Rationale: per-task review loops caught real bugs but made progress too slow and repeatedly hit the usage limit; the milestone-level final review + QA remains the quality gate.
Amendment (2026-10-26): no Retro step for this project; milestones end with final review, QA and merge.

## D-027 — Duplicate rows, "fixed" errors and the blocking pre-check
2026-10-26 · Later occurrences of identical rows (`duplicate_row` warning) are excluded by default during normalization, whatever the warning's resolution, and are listed among the excluded rows; the first occurrence is kept. A "fixed" resolution never clears an error in normalization: only re-validation that no longer finds the error does (normalization re-validates and blocks on any fresh error not resolved "excluded"). `ValidationReport.blocking` (which treats "fixed" as clearing) is only the UI's pre-check. Saving a new column mapping deletes the file's validation report and observations.
Rationale: M2 final review — identical rows add no information and would double-count; trusting a user's "fixed" flag on data the server can re-check would let errors through.

## D-028 — HICP series: `prc_hicp_minr` replaces discontinued `prc_hicp_manr`
2026-10-26 · Eurostat discontinued `prc_hicp_manr` (last data 2025-12). The catalog's `HICP_YOY` uses `prc_hicp_minr` with `coicop18=TOTAL` (verified live during M3). `IND_PROD` additionally pins `indic_bt=PRD`. Supersedes the dataset code in D-005 / spec AC 8.

## D-029 — Structured output uses `function_calling` by default
2026-10-29 · `with_structured_output(ReportDraft, include_raw=True, method=LLM_STRUCTURED_OUTPUT_METHOD)` defaults to `function_calling` (a forced, non-strict tool call parsed by `PydanticToolsParser`). `json_schema` stays selectable, but `.env.example` warns that OpenAI strict mode will likely reject it.
Rationale: langchain-openai's `json_schema` path hands the Pydantic class to OpenAI strict structured outputs, and strict schemas reject the `minLength` constraints that `ReportDraft` carries (claim ids, texts, units, ref ids). The offline fake-model tests exercise the same parser path as production.

## D-030 — `LLM_TEMPERATURE` is ignored for gpt-5 reasoning models
2026-10-29 · The default model stays `gpt-5-mini` (verified in the installed openai 3.39 and langchain-openai 1.7.2 model lists). langchain-openai drops any non-default temperature for `gpt-5*` models except `-chat` models and `reasoning_effort="none"`. The factory logs one info line when that happens. Setting `LLM_MODEL` to a chat model makes `LLM_TEMPERATURE=0.2` effective.
Rationale: the provider accepts only the default temperature for reasoning models; logging the drop makes it visible instead of silent.

## D-031 — LLM-layer signatures take Settings; citation ids
2026-10-29 · The chain builders are `build_draft_chain(model, settings)`, `build_revise_chain(model, settings)` and `build_review_revise_chain(model, settings)`, so they can read the structured-output method. `build_tools(store, metrics, catalog, user_id, settings, *, series_meta=None, row_budget=None)`. Citation ids are fixed as follows:
- a retrieved passage is cited by its document `doc_id`, with section/page in `Citation.locator`;
- an upload is cited by its `file_id`, with the worksheet in `locator`;
- an API series is cited by its `series_id`, with the query URL in `locator`.

`Section.citation_ids` refer to `Citation.ref_id`. Other new settings: `LLM_MAX_TOOL_STEPS=6`, `MAX_TOOL_ROWS_PER_TURN=400` (rows across all `query_observations` calls of one tool loop, AC 35), `PROMPT_MAX_METRICS=80` and `PROMPT_MAX_PASSAGES=8`. `run_events.id` is a `bigint` identity column, which gives a stable order.
Rationale: no hardcoded tunables; one stable id per source makes citations resolvable in the UI and exports.

## D-032 — Unknown-column guard is stricter than the plan
2026-10-29 · `check_unknown_columns(draft, undocumented, store)` enforces three rules:
1. every undocumented column is listed in `unknown_columns_acknowledged`;
2. a sentence that names such a column must state that its meaning is not documented, and must not use interpretive verbs (shows, indicates, reflects, …);
3. no claim may name the column or cite an observation read from it.

The `ObservationStore` is a required argument, so rule 3 cannot be switched off by omission.
Rationale: AC 34 and ESMB Guidelines §4.4 forbid interpreting undocumented variables; "not documented, but signals X" is still an interpretation.

## D-033 — When prose "names" an undocumented column
2026-10-29 · Identifier-like headers (containing `_`, a digit, or a capital after a lower-case letter, e.g. `val_adj_x`, `Q17`, `valAdj`) are named by an exact, case-sensitive, whole-identifier match. Any header is also named when it is quoted or back-ticked exactly ('Total', "Total", `Total`, “Total”). Bare occurrences of common-word headers ("Total", "source", "unit", "LAST UPDATE") never fail a draft; for those columns the acknowledgement and provenance rules apply.
Rationale: M6 review I1. Common-word headers made ordinary prose ("Total employment…", "Source: Eurostat.") fail every revision, which pushed legitimate reports into the safe failure.

## D-034 — The daily LLM cap is attached to the model and counted once per call
2026-10-29 · `get_chat_model(settings, *, cap_guard, callbacks=None)` takes `cap_guard` as a required keyword. Application code passes `daily_llm_cap_guard(pool, settings, user_id)`. Only tests and admin scripts may pass `None`. The guard is attached to the model as an `LLMCapCallbackHandler`, so every call is counted against the global `DAILY_LLM_CALL_CAP`, even when the caller passes no callbacks.

`DailyLLMCap` de-duplicates by `run_id`. A run-events handler that shares the same cap (so it can log a blocked call as `llm_error`) therefore never counts a call twice, and a blocked run stays blocked. Provider-internal retries (`LLM_MAX_RETRIES`) are part of one counted call. A failing cap check (database down) fails closed and is logged as `llm_error` with the exception type.
Rationale: M6 review I2. An opt-in cap on each call site could be forgotten (uncapped spend) or attached twice (double counting).

## D-035 — One year of look-back before the report window
2026-10-29 · The analysis reads one year before `period_from`, frequency-aware: the same period one year earlier, which means 12 months, 4 quarters or 1 year (`graph.observations.lookback_start`). This holds for uploads and for API requests alike: `ApiRequest.period_from` is the look-back start. Change metrics therefore have their year-ago base. Metrics and skips are *reported* only when their period lies inside [`period_from`, `period_to`] (`nodes_analyze.report_window_only`), so look-back periods never appear as results. Conflicts in the look-back year are still shown to the user, because they feed the year-ago bases.
Rationale: M7a review M2. Filtering uploads to the window meant a one-year report had no YoY at all.
Amendment (M7a re-review I2, m11): the look-back rows feed only point and period-on-period metrics (latest, YoY, MoM, QoQ, and pp changes over one period or one year). Window changes (`abs_change`, `pct_change`, and the window `pp_change`) are computed from the rows inside the report window, so their base is `period_from` (`nodes_analyze.report_metrics`). A pair with rows only in the look-back year is reported as one skip, "no observations in the report window".

## D-036 — One effective row per data point; uploads take precedence over agreeing API values
2026-10-29 · After conflict resolution, the effective observation set has exactly one row per (indicator, region, period, unit, unit_multiplier) (`graph.observations.collapse_duplicates`). Where values from different sources agree, or differ only within the conflict tolerance, the **upload** row is kept, because the user's data is primary. Otherwise the row with the lowest source key, then the lowest obs id, is kept. The ids of the collapsed rows are recorded as corroborations of the kept row (`ReportState.corroborations`, for export and provenance), so nothing is silently lost.

Consented gap filling adds one row only for a key whose uploaded value is missing or absent. The filled copy (its own `gapfill` id, API provenance) replaces both the missing upload point and the API point it copies. Without consent, an uploaded missing value stays primary.

A "prefer" or "exclude" resolution leaves one row or none. "Keep both" keeps both labelled rows, and metrics touching that period are recorded as skipped ("ambiguous"): they are never averaged or picked.
Rationale: M7a review C1. Duplicate agreeing rows made M4 skip every metric as ambiguous, with no gate.
Amendment (M7a re-review I3, AC 22): without gap-fill consent, a linked API series never supplies a period that lies inside the uploaded span of that series (first to last uploaded period, same indicator, region and frequency) and has no upload row (`graph.observations.without_unconsented_gap_fills`). API periods outside the span, including the look-back year, extend the series the user selected and are kept. API rows next to an upload row are used for comparison (conflict detection, corroboration), which never needs consent. With consent, the gap-fill copy described above applies.
Amendment (M7a re-review I4): skipped metrics, with their reasons and including "keep both" periods, reach the drafting and revise prompts as a capped "NOT COMPUTED" block (`PROMPT_MAX_SKIPS`). They are also part of the review interrupt value (`metric_skips`), so gaps are stated, never filled by the model.

## D-037 — Typed graph state with an allow-listed checkpoint serializer
2026-10-29 · `ReportState` holds Pydantic models and the frozen dataclasses `DraftAttempt` and `RetrievedPassage`. Every checkpointer is created with `serde=graph.serde.make_serializer()`, a `JsonPlusSerializer` whose `allowed_msgpack_modules` lists exactly the state classes. A test walks every annotation reachable from the state, the sub-graph schemas and the Send payloads, and fails when a class is missing. Without the list, an unlisted class would come back as a plain dict. Interrupt values are plain JSON dicts.
Rationale: a checkpoint must not instantiate arbitrary classes, and LangGraph 4.3 deprecates unregistered types.

## D-038 — Graph data-flow choices (M7a)
2026-10-29 ·
- API observations are stored by `fetch_api` as file-less rows (`ObservationRepo.upsert_api_observations`), which never overwrite upload rows.
- `mappings` uses the `merge_dicts` reducer, because parallel `parse_file` tasks write it. In `merge_dicts`, a None value deletes a key, which is how stale per-file validation reports are dropped.
- A resume that changes a file's mapping cannot also resolve that file's issues (D-027: the new mapping deletes the old report). Conflict resolutions that involve the remapped file are cleared.
- Unit mismatches are acknowledged per mismatch. One that appears later is shown again.
- An unexpected exception in `validate_normalize` ends in `fail_safe` with a `WorkflowError` that names only the exception type.
- Draft citations must name a retrieved passage or a source of this report, or verification fails.
- New state keys (architecture §3): `confirmed_mappings`, `unit_mismatches`, `dropped_sources`, `acknowledged_mismatches`, `corroborations`, `needs_input`, `gate_rounds`, and status `rejected`.

Rationale: M7a implementation and review (M1, M3, M5, M6, M9).

## D-039 — Report run runtime: lease, queued answers bound to interrupts, fork rule, time budget (M7b)
2026-10-30 ·
- **Run lease.** One running segment or fork per report. It is a lease on the `reports` row: `run_lease_token` and `run_lease_until` (migration 007).
  - Acquire: one atomic `UPDATE … WHERE id AND user_id AND (until IS NULL OR until < now()) RETURNING`.
  - Renew: after every saved checkpoint.
  - Release: by token in `finally`, including when the event generator is closed.
  - Duration: `SEGMENT_BUDGET_S + RUN_LEASE_MARGIN_S`.

  Session advisory locks are not used: through Neon's transaction-mode pooler they lose mutual exclusion and leak (M7b review C1). A dead instance's lease expires by itself.
- **Queued answers bound to their interrupt.** `POST /resume` takes `{interrupt_id, payload}`.
  - The `interrupt` SSE event carries `interrupt_id`.
  - A mismatch with the pending interrupt answers 409 ("this step changed, reload").
  - The answer is stored with its id in `reports.pending_resume`. A segment applies it only if that interrupt is still pending, and drops it otherwise.
  - A fork drops any queued answer.

  This means a stale tab can never approve a later round (review I1). EventSource is GET-only, so answers are queued and the next GET segment resumes with them. Run parameters live in `reports.params`.
- **Fork rule.** Only checkpoints whose next step is exactly one node reached by an edge are listed and forkable. A fan-out step (`Send` packets, even a single one) or the end of a run is refused with 400. `update_state` does not carry `Send` packets, so such a fork would be a dead branch (review I2). A fork takes the lease and consumes one `RUNS_PER_HOUR` slot.
- **Canonical report id.** Each route resolves the report row once and uses `str(row["id"])` for `thread_id`, the lease and every repo call. Another spelling of the UUID reaches the same thread, and a non-UUID is 404 (review I3).
- **Time budget.**
  - The budget is checked only right after a saved top-level checkpoint that is newer than the segment's start: no new step starts once `elapsed + MAX_NODE_S > SEGMENT_BUDGET_S`.
  - Every segment persists at least one step. Stopping right after an `updates` chunk would drop the step being saved, and a segment that always stops there never progresses.
  - Settings validate `MAX_NODE_S < SEGMENT_BUDGET_S` and `SEGMENT_BUDGET_S + SEGMENT_SAFETY_MARGIN_S < 300`.
  - A step that is still killed loses only itself: the previous checkpoint resumes (review I4).
- **Model failures** (`MAX_MODEL_RETRIES`, Task 10) route to `fail_safe`. A database failure outside `validate_normalize` ends the segment with an `error` event and a `segment_error` row in run_events (exception type only). It is not written to the graph state, because `update_state` would fork the thread.

Rationale: M7b review C1, I1–I4 and m2–m5. The serverless runtime (Vercel functions with Neon's pooler) admits no session state, and every state transition must be checkpointed before a request ends.
