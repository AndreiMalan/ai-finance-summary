# Architecture — StatBrief

Architect Agent output. Approach: **segmented in-request streaming** ([brainstorm.md](brainstorm.md), Approach 1). Requirements: [spec.md](spec.md) (AC-n = acceptance criterion n). Decisions: [decisions.md](decisions.md).

Pinned stack (latest at 2026-10-25): Python 3.12 · FastAPI 0.162 · langgraph 1.2 · langgraph-checkpoint-postgres 3.2 · langchain 1.4 / core 1.7 · langchain-openai 1.7 · langchain-postgres 0.0.19 · langchain-community 0.4 · langchain-text-splitters 1.1 · pandas 3.1 · openpyxl 3.1 · xlrd 2.0 · reportlab 5.1 · psycopg 3.3 · argon2-cffi 25.1 · pypdf 6.

---

## 1. File and module structure

```
AI_Finance_summary/
├── app.py                      # Vercel entrypoint: `from statbrief.web.main import app`
├── vercel.json                 # crons (cleanup), function maxDuration, includeFiles
├── pyproject.toml / requirements.txt
├── .env.example
├── src/statbrief/
│   ├── config.py               # Settings (pydantic-settings) — every tunable from env
│   ├── domain/                 # Pydantic contracts between stages (no I/O)
│   │   ├── observation.py      # Observation, Provenance (UploadProvenance | ApiProvenance)
│   │   ├── dataset.py          # DatasetRef, ColumnMapping, DetectedSchema, ValidationIssue, ValidationReport
│   │   ├── conflict.py         # Conflict, ConflictResolution
│   │   ├── metrics.py          # MetricResult, MetricSkip, SignificantChange
│   │   ├── report.py           # ReportParams, ReportDraft, Finding, Section, NumericalClaim, Citation
│   │   ├── review.py           # ReviewDecision, VerificationResult, ClaimCheck
│   │   └── preferences.py      # UserPreferences
│   ├── ingest/
│   │   ├── sniff.py            # validate_file_bytes(): extension + magic bytes + size, reject xlsm/encrypted
│   │   ├── readers.py          # read_csv / read_excel / read_json -> RawTable (+ row numbers)
│   │   ├── detect.py           # detect_schema(RawTable) -> DetectedSchema (header row, roles, date fmt, confidence)
│   │   ├── validate.py         # validate_table(RawTable, ColumnMapping) -> ValidationReport
│   │   ├── normalize.py        # to_observations(RawTable, ColumnMapping, ...) -> list[Observation]
│   │   ├── regions.py          # region name/code resolution (ISO2, NUTS, Eurostat EL/UK aliases)
│   │   ├── periods.py          # parse_period() for 2024M03, 2024-Q1, Excel serials, ambiguity detection
│   │   ├── template.py         # build_template_xlsx()/csv
│   │   └── apis/
│   │       ├── base.py         # ApiClient protocol, retry/backoff, timeout, ApiError
│   │       ├── eurostat.py     # JSON-stat 2.0 -> Observations (+ status flags)
│   │       ├── worldbank.py    # v2 JSON (paginated) -> Observations
│   │       └── catalog.py      # curated IndicatorSpec list (D-005)
│   ├── analytics/
│   │   ├── combine.py          # merge datasets, detect_conflicts(), apply_resolutions()
│   │   ├── metrics.py          # compute_metrics(), compute_single() — pandas only
│   │   ├── store.py            # ObservationStore, compute_metric(metric_id, store)
│   │   ├── significance.py     # flag_significant()
│   │   └── verify.py           # verify_draft(ReportDraft, ObservationStore) -> VerificationResult
│   ├── rag/
│   │   ├── loaders.py          # load_reference(path|bytes) -> list[Document] (PyPDF/Text/Markdown loaders)
│   │   ├── indexer.py          # split + embed + upsert into PGVector with metadata
│   │   ├── retriever.py        # retrieve(query, user_id, k) -> list[RetrievedPassage]; FTS fallback
│   │   └── api_metadata.py     # API metadata -> Documents (genuine, cited by URL)
│   ├── llm/
│   │   ├── models.py           # get_chat_model(), get_embeddings() via init_chat_model / env
│   │   ├── prompts.py          # ChatPromptTemplates: draft, interpret_dataset, revise, review_revise
│   │   ├── chains.py           # LCEL: context_prep_chain, draft_chain, revise_chain (with_structured_output)
│   │   ├── tools.py            # @tool get_indicator_metadata, list_available_metrics, query_observations, compute_metric
│   │   └── callbacks.py        # RunEventsCallbackHandler (redacting) -> run_events
│   ├── graph/
│   │   ├── state.py            # ReportState TypedDict + reducers; sub-states
│   │   ├── ingestion.py        # ingestion subgraph (Send per source)
│   │   ├── retrieval.py        # retrieval subgraph
│   │   ├── drafting.py         # drafting subgraph (draft -> verify -> revise loop)
│   │   ├── nodes.py            # top-level nodes (select_sources, normalize, resolve_gate, analyze, review, export, fail)
│   │   ├── routing.py          # all conditional-edge functions (pure, unit-tested)
│   │   ├── builder.py          # build_graph(checkpointer, store) -> CompiledStateGraph
│   │   └── runner.py           # run_segment(): stream with time budget, lock, events -> SSE
│   ├── storage/
│   │   ├── db.py               # psycopg pool (Neon pooled URL), migrations runner
│   │   ├── migrations/*.sql    # users, reports, files, observations, run_events, rate_limits, report_versions
│   │   ├── repos.py            # ReportRepo, FileRepo, ObservationRepo, VersionRepo (all user-scoped)
│   │   ├── blobs.py            # BlobStore protocol: VercelBlobStore | PostgresBlobStore | LocalBlobStore
│   │   └── retention.py        # cleanup_expired()
│   ├── auth/
│   │   ├── passwords.py        # hash/verify (argon2)
│   │   ├── sessions.py         # signed cookie (itsdangerous), current_user dependency
│   │   └── limits.py           # rate limits + daily LLM cap (Postgres counters)
│   ├── export/
│   │   ├── pdf.py              # build_pdf(ReportBundle) -> bytes (reportlab + graphics charts)
│   │   └── excel.py            # build_xlsx(ReportBundle) -> bytes (values only, formula-escape)
│   └── web/
│       ├── main.py             # FastAPI app, startup (migrations, demo user), routers
│       ├── routes/             # auth.py, reports.py (wizard), uploads.py, run.py (SSE), review.py, history.py, diagnostics.py, prefs.py, cron.py
│       ├── templates/          # Jinja: base, wizard steps, partials (timeline, grid, issues, conflicts, review, history)
│       └── static/             # app.css (Tailwind build), app.js (Alpine helpers), vendor via CDN
├── knowledge_base/             # 9 ESMB/IRS reference docs (PDF/MD/TXT) + build script for PDFs
├── sample_data/                # IRS_regional_monthly.xlsx, company_sector.csv, irs_api_export.json, cryptic_columns.csv
├── scripts/                    # index_knowledge_base.py, make_sample_data.py, reset_demo.py, admin_reset_password.py
├── tests/                      # unit/ (pure), graph/ (MemorySaver + fakes), integration/ (Postgres, opt-in), live/ (OPENAI key)
└── docs/                       # spec, decisions, brainstorm, architecture, concepts/concepts.tex (local only)
```

Layering rule: `domain` imports nothing internal; `ingest/analytics/rag/llm` import `domain`; `graph` imports all of those; `web` imports `graph`, `storage`, `auth`, `export`. No layer imports `web`.

## 2. Core contracts (Pydantic, `domain/`)

```python
class UploadProvenance(BaseModel):
    source_type: Literal["upload"] = "upload"
    file_id: str; filename: str; worksheet: str | None; row_number: int | None; column: str

class ApiProvenance(BaseModel):
    source_type: Literal["api"] = "api"
    api_name: Literal["eurostat", "worldbank"]; series_id: str; query_url: HttpUrl; retrieved_at: datetime

class Observation(BaseModel):
    obs_id: str                              # stable hash(source, key, row)
    indicator_code: str; indicator_label: str | None
    region_code: str; region_level: Literal["country", "nuts1", "nuts2", "nuts3", "other"]
    period: str                              # "2024-03" | "2024-Q1" | "2024"
    frequency: Literal["M", "Q", "A"]
    value: float | None; unit: str; unit_multiplier: int = 0
    status_flag: str | None                  # p, e, b, ...
    provenance: UploadProvenance | ApiProvenance = Field(discriminator="source_type")

class ColumnMapping(BaseModel):
    worksheet: str | None; header_row: int
    date_col: str; region_col: str | None; indicator_col: str | None; value_col: str; unit_col: str | None
    constants: dict[Literal["region", "indicator", "unit"], str] = {}
    date_format: str | None; column_descriptions: dict[str, str] = {}   # user-supplied meanings

class ValidationIssue(BaseModel):
    code: Literal["missing_required","duplicate_row","duplicate_key_conflict","missing_value","invalid_date",
                  "ambiguous_date","non_numeric","inconsistent_unit","unknown_region","undocumented_column",
                  "formula_without_value","frequency_mismatch"]
    severity: Literal["error","warning"]; rows: list[int]; column: str | None; message: str
    resolution: Literal["pending","excluded","fixed","accepted"] = "pending"

class NumericalClaim(BaseModel):
    claim_id: str; text: str; value: float; unit: str
    metric_id: str | None                    # e.g. "pp_change:HICP_YOY:RO:2024-03:2023-03" (rate YoY)
    observation_ids: list[str] = Field(min_length=1)

class Citation(BaseModel):
    kind: Literal["upload", "api", "reference"]; ref_id: str; label: str; locator: str | None

class Finding(BaseModel):    headline: str; claim_ids: list[str]
class Section(BaseModel):    indicator_code: str; title: str; paragraphs: list[str]; claim_ids: list[str]; citation_ids: list[str]
class ReportDraft(BaseModel):
    title: str; executive_summary: list[Finding] = Field(min_length=1, max_length=5)
    sections: list[Section]; claims: list[NumericalClaim]; citations: list[Citation]
    caveats: list[str]; unknown_columns_acknowledged: list[str] = []

class ClaimCheck(BaseModel):  claim_id: str; ok: bool; expected: float | None; reason: str | None
class VerificationResult(BaseModel):
    ok: bool; checks: list[ClaimCheck]; orphan_numbers: list[str]; feedback: str   # feedback -> revise prompt

class ReviewDecision(BaseModel):
    action: Literal["approve", "reject", "request_changes"]; comment: str | None = None
```

## 3. Graph state (`graph/state.py`)

```python
class ReportState(TypedDict, total=False):
    # parameters
    report_id: str; user_id: str
    params: ReportParams                               # mode, indicators, regions, period, style, formats
    # sources & ingestion
    file_refs: list[DatasetRef]                        # blob keys, never bytes
    api_requests: list[ApiRequest]
    source_metadata: Annotated[list[SourceMeta], operator.add]       # fan-in from Send
    mappings: dict[str, ColumnMapping]                 # file_id -> mapping (survives resume)
    validation: Annotated[dict[str, ValidationReport], merge_dicts]  # per source
    normalized_ref: str | None                         # observations persisted in DB, state holds the ref
    conflicts: list[Conflict]; resolutions: dict[str, ConflictResolution]
    # analysis
    metrics: Annotated[list[MetricResult], operator.add]             # fan-in from per-region×indicator Send
    metric_skips: Annotated[list[MetricSkip], operator.add]
    significant: list[SignificantChange]
    # retrieval & drafting
    retrieved: list[RetrievedPassage]
    draft: ReportDraft | None; draft_history: Annotated[list[DraftAttempt], operator.add]
    verification: VerificationResult | None
    revision_count: int; parse_retry_count: int
    # review & export
    review: ReviewDecision | None; review_round: int
    export: ExportStatus | None
    errors: Annotated[list[WorkflowError], operator.add]
    status: Literal["collecting","needs_input","analyzing","drafting","awaiting_review","approved","exported","failed"]
```

Large data (observations, raw files) is stored in Postgres/Blob; state carries references so checkpoints stay small (Vercel time + Neon size).

## 4. Data flow

```
 Browser (HTMX)                                FastAPI (one Vercel function)                      Neon / Blob / APIs
 ─────────────                                 ─────────────────────────────                      ──────────────────
 Upload files ── POST /reports/{id}/files ──▶ sniff → Blob.put → FileRepo ───────────────────────▶ Blob (private), files
 Preview/map  ── GET  …/files/{f}/preview ──▶ readers + detect_schema → grid partial
 Run          ── GET  …/run/stream (SSE) ───▶ runner.run_segment(thread_id=report_id)
                                                │  pg advisory lock, time budget
                                                ▼
 ┌──────────────────────────────── LangGraph StateGraph (checkpointed per super-step) ────────────────────────────┐
 │ select_sources                                                                                                 │
 │   └─▶ [ingestion subgraph]  Send(fetch_api, req)×N ‖ Send(parse_file, ref)×M  → fan-in source_metadata       │
 │         └─▶ validate_normalize ──(issues / low-confidence mapping / conflicts?)──▶ resolve_gate ◀─┐            │
 │                │ ok                                               interrupt(#1: mapping+issues+conflicts)   │
 │                ▼                                                      │ Command(resume=fixes) ─────────────┘   │
 │         analyze_dispatch ─▶ Send(analyze_one, region×indicator)×K ─▶ aggregate_metrics → flag_significant      │
 │                ▼                                                                                               │
 │   [retrieval subgraph]  build_queries → retrieve(pgvector | FTS) + api_metadata → prepare_context (LCEL)      │
 │                ▼                                                                                               │
 │   [drafting subgraph]  draft (LCEL + structured output + tools)                                               │
 │         ├─ parse error ─▶ retry (≤2) ─▶ fail                                                                    │
 │         ▼                                                                                                      │
 │      verify_claims (deterministic) ──fail & revisions<3──▶ revise (feedback) ─▶ verify_claims (cycle)         │
 │         │ fail & revisions==3 ─▶ fail_safe                                                                     │
 │         ▼ ok                                                                                                   │
 │   human_review  interrupt(#2: draft + citations) ── request_changes ─▶ drafting (with comment)                │
 │         │ reject ─▶ END(rejected)        approve ▼                                                             │
 │   export ─▶ build_pdf / build_xlsx → Blob + report_versions ─▶ END                                            │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                │ stream_mode=["updates","custom"] → SSE events → timeline partial
                                                │ callbacks → run_events (redacted)
 Review form ── POST …/resume (Command) ──────▶ runner (next segment)
```

## 5. Key function signatures

```python
# ingest
def validate_file_bytes(name: str, data: bytes, settings: Settings) -> FileCheck            # raises UploadRejected
def read_table(data: bytes, kind: FileKind, worksheet: str | None, header_row: int | None) -> RawTable
def list_worksheets(data: bytes, kind: FileKind) -> list[str]
def detect_schema(table: RawTable) -> DetectedSchema
def validate_table(table: RawTable, mapping: ColumnMapping, known_docs: set[str]) -> ValidationReport
def to_observations(table: RawTable, mapping: ColumnMapping, file: DatasetRef, report: ValidationReport) -> list[Observation]
async def fetch_eurostat(req: ApiRequest, client: httpx.AsyncClient) -> ApiFetchResult
async def fetch_worldbank(req: ApiRequest, client: httpx.AsyncClient) -> ApiFetchResult
# analytics
def detect_conflicts(obs: list[Observation], tol: ConflictTolerance) -> list[Conflict]
def apply_resolutions(obs, conflicts, resolutions) -> list[Observation]
def compute_metrics(observations: list[Observation], *, indicator: str, region: str) -> tuple[list[MetricResult], list[MetricSkip]]
def compute_metric(metric_id: str, store: ObservationStore) -> MetricResult  # store.py; MetricUnavailable(KeyError); used by tool + verifier
def verify_draft(draft: ReportDraft, store: ObservationStore) -> VerificationResult
def matches_at_stated_precision(stated_text_number: str, computed: float) -> bool
# rag
def index_documents(docs: list[Document], owner: str | None, store: VectorStore) -> int
def retrieve(query: str, user_id: str, k: int, settings: Settings) -> list[RetrievedPassage]
# llm
def get_chat_model(settings: Settings) -> BaseChatModel
def build_draft_chain(model: BaseChatModel) -> Runnable[DraftInput, ReportDraft]
def build_tools(store: ObservationStore, user_id: str) -> list[BaseTool]
# graph
def build_graph(checkpointer: BaseCheckpointSaver, store: BaseStore, deps: GraphDeps) -> CompiledStateGraph
def route_after_validation(state: ReportState) -> Literal["resolve_gate", "analyze_dispatch", "fail_safe"]
def route_after_verify(state: ReportState) -> Literal["revise", "human_review", "fail_safe"]
def route_after_review(state: ReportState) -> Literal["drafting", "export", "__end__"]
async def run_segment(report_id: str, user: User, command: Command | None, budget_s: float) -> AsyncIterator[SseEvent]
# export
def build_pdf(bundle: ReportBundle) -> bytes
def build_xlsx(bundle: ReportBundle) -> bytes
```

`GraphDeps` (dataclass) injects model, embeddings/retriever, repos, blob store, API clients, clock — so tests swap in `GenericFakeChatModel`, fake embeddings, `MemorySaver`/`InMemoryStore`, and stub API clients.

## 6. Integration points

| Point | Mechanism |
|---|---|
| Vercel ↔ FastAPI | root `app.py` exposes ASGI `app`; `vercel.json` sets `maxDuration`, cron `/cron/cleanup` (secret header `CRON_SECRET`) |
| LangGraph ↔ Postgres | `PostgresSaver` / `PostgresStore` from `langgraph-checkpoint-postgres` on a psycopg pool (`autocommit=True`, `prepare_threshold=None` for Neon pooler); `.setup()` at migration time |
| RAG ↔ pgvector | `langchain_postgres.PGVector` collection `kb_shared` + per-user filter `owner in (null, user_id)` |
| LLM ↔ OpenAI | `init_chat_model(f"{LLM_PROVIDER}:{LLM_MODEL}")`, `.with_structured_output(ReportDraft, include_raw=True)` so parse failures are inspectable |
| Graph ↔ browser | SSE (`text/event-stream`) of `{type, node, message, progress}`; interrupts render HTMX forms; resume via POST `Command(resume=...)` |
| Callbacks ↔ DB | `RunEventsCallbackHandler` passed in `config["callbacks"]`, tagged with `report_id`, `user_id` |
| Blob | Vercel Blob REST (`BLOB_READ_WRITE_TOKEN`), private access; `PostgresBlobStore` fallback; `LocalBlobStore` for dev/tests |

## 7. Milestones and tasks

Each milestone gets its own detailed plan in `docs/superpowers/plans/` and every task runs Developer → Review (≥ 8/10) → QA. Task lists for M2–M9 are the planned breakdown; they are refined (and may be split or merged) when that milestone's plan is written. Status: ✅ done · 🔄 in progress · ⬜ not started.

| # | Milestone | Acceptance criteria | Plan |
|---|---|---|---|
| M1 | Skeleton, config, Postgres, auth, rate limits, Vercel entrypoint, risk spikes R1–R3 | AC 1–6 | [m1 plan](superpowers/plans/2026-10-25-m1-skeleton-auth-limits.md) |
| M2 | Common schema + file ingestion + blob storage | AC 9–19 | — |
| M3 | API clients, normalization, combining, conflicts | AC 7–8, 18–22 | — |
| M4 | Deterministic analytics + claim verifier | AC 23–27 | — |
| M5 | RAG knowledge base and retrieval | AC 31–32 | — |
| M6 | LLM layer | AC 28–30, 33–35, 51 | — |
| M7 | LangGraph workflow | AC 36–46 | — |
| M8 | Web UI | AC 13–14, 41–42, 49, 52–59 | — |
| M9 | Exports, sample data, retention, deployment, concepts PDF, retro | AC 47–48, 50, 60–62 | — |

### M1 — Skeleton, config, database, auth, limits ✅
1. ✅ Scaffold, `Settings` from env, embedded-Postgres test harness
2. ✅ Postgres pool, idempotent SQL migrations, LangGraph checkpointer/store tables
3. ✅ Argon2 passwords, user repository, demo user from env
4. ✅ Login throttling, per-user run limit, global daily LLM cap
5. ✅ Signed HttpOnly session cookies
6. ✅ FastAPI app: lazy runtime, login/signup/logout, themed templates, Tailwind/DaisyUI build
7. ✅ Vercel entrypoint + config, local dev DB, spikes: bundle size (R1), SSE (R2), checkpointer (R3)

### M2 — Common schema and file ingestion
1. ⬜ Domain contracts: `Observation`, provenance (upload/API), `ColumnMapping`, `ValidationIssue`, `DatasetRef`
2. ⬜ Upload checks: size, extension, magic bytes, reject `.xlsm`/encrypted/malformed, no formula or macro execution
3. ⬜ Readers: CSV (delimiter, decimal comma, encodings, BOM), XLSX/XLS (worksheets, header row, cached values only), JSON (records / wrapped)
4. ⬜ Period and region parsing: `2024M03`, `2024-Q1`, Excel serials, ambiguous dates; ISO-2 / NUTS / county names, Eurostat aliases
5. ⬜ Schema detection: header row, column roles, date format, frequency, confidence scores
6. ⬜ Validation: missing, duplicates, invalid dates, non-numeric, unit inconsistency, unknown regions, undocumented columns
7. ⬜ Normalization to `Observation` with file / sheet / row provenance; explicit exclusions, never silent drops
8. ⬜ Blob storage (private Vercel Blob / Postgres fallback / local), file + report tables, user scoping
9. ⬜ Downloadable CSV/XLSX template

### M3 — Public APIs, combining, conflicts
1. ⬜ API client base: timeouts, bounded retries with backoff, typed errors, response caching per report
2. ⬜ Eurostat client (JSON-stat 2.0 → `Observation`, status flags, query URL provenance)
3. ⬜ World Bank client (v2 JSON, pagination, nulls → `Observation`)
4. ⬜ Curated indicator catalogue + "Advanced" uncurated codes
5. ⬜ Combining datasets by indicator / region / period / unit; frequency-mismatch reporting
6. ⬜ Conflict detection (tolerance rules) and resolutions (choose source / keep both / exclude)
7. ⬜ Consent-based gap filling from API data, with provenance preserved

### M4 — Deterministic analytics and verification
1. ⬜ Metrics with pandas: latest, YoY, MoM, absolute, % change, percentage-point change
2. ⬜ Validity rules + recorded skip reasons (no MoM on quarterly, no % change on rates, no cross-unit maths)
3. ⬜ Significance flags (thresholds per indicator type) and top movers
4. ⬜ `compute_metric(metric_id)` and the observation store used by tools and the verifier
5. ⬜ Claim verifier: recompute, precision-aware rounding, unit/period/region checks, prose ↔ claim match, orphan numbers

### M5 — RAG knowledge base
1. ⬜ Write the 9 ESMB/IRS reference documents (MD/TXT, PDFs generated)
2. ⬜ Document loaders (PDF/TXT/MD) and text splitters with section/page metadata
3. ⬜ Embeddings + PGVector store; shared vs per-user collections; indexing script
4. ⬜ Retriever with owner filtering, Postgres full-text fallback (`ENABLE_VECTOR_SEARCH`), API metadata as documents
5. ⬜ User-uploaded reference documents (index, list, delete)
6. ⬜ Retrieval relevance + source-metadata tests

### M6 — LLM layer
1. ⬜ Model factory via `init_chat_model` / embeddings from env (provider-agnostic)
2. ⬜ Prompt templates: draft, data interpretation, revision, review-comment revision
3. ⬜ Pydantic `ReportDraft` structured output with parse-failure detection (`include_raw`)
4. ⬜ LCEL chains: context preparation, drafting, revision
5. ⬜ Controlled tools with validated arguments and user scoping (metadata, metrics, records, compute)
6. ⬜ Redacting callback handler → `run_events` (latency, tokens, tools, retrieval, errors)
7. ⬜ "Unknown column meaning" guard + tests with fake chat models

### M7 — LangGraph workflow
1. ⬜ `ReportState` TypedDict, reducers, sub-states
2. ⬜ Ingestion subgraph with Send fan-out per source + safe aggregation
3. ⬜ Validation/normalization node + interrupt #1 (mapping, issues, conflicts)
4. ⬜ Analysis fan-out per region × indicator + aggregation
5. ⬜ Retrieval subgraph
6. ⬜ Drafting subgraph: draft → verify → bounded revise loop → safe failure
7. ⬜ Human review interrupt #2 (approve / reject / request changes) and routing
8. ⬜ Postgres checkpointer, segmented runner (time budget, per-thread lock, SSE events)
9. ⬜ Replay / fork from checkpoint, long-term preferences store, error-handling routes
10. ⬜ Graph tests: routing, revision limits, parallel aggregation, interrupt resume

### M8 — Web UI
1. ⬜ App shell, navigation, wizard stepper (matches the approved mockup)
2. ⬜ Step 1 Sources + Step 2 Upload & preview grid + column mapping
3. ⬜ Steps 3–4 Validate, issue groups, conflict resolution
4. ⬜ Step 5 Generate with live SSE progress timeline
5. ⬜ Step 6 Review: draft, charts, clickable citations / provenance, approve-reject-changes
6. ⬜ My reports (resume), History (checkpoints, fork), Diagnostics tab
7. ⬜ Preferences, Knowledge base page, About page (with fictional-sources note)

### M9 — Exports, data, deployment, docs
1. ⬜ PDF export (reportlab): cover, summary, sections + charts, data quality, grouped citations, calculation appendix
2. ⬜ Excel export (openpyxl, values only): Summary, Data, Metrics, Provenance, Conflicts, Citations
3. ⬜ Dummy datasets: IRS workbook, sector CSV, JSON, cryptic-column CSV (with planted issues and conflicts)
4. ⬜ Retention cleanup + Vercel Cron, "Delete my account" — must delete Blob objects before cascading DB deletes; purge abandoned chunked uploads
5. ⬜ Production deployment on Vercel + Neon + Blob, environment variables, smoke test of all 8 demo scenarios
6. ⬜ LaTeX concepts PDF (one page per concept, clickable TOC) — local only
7. ⬜ Final whole-branch review, QA report, Retro

## 8. Risk flags

| # | Risk | Mitigation | Verify at |
|---|---|---|---|
| R1 | Bundle > Vercel limit (pandas 3 + langchain + reportlab + psycopg) | no community extras beyond loaders, exclude tests/docs via `excludeFiles`, measure in M1 | M1 |
| R2 | SSE buffering on Vercel Python | explicit flush, `X-Accel-Buffering: no`; fallback to HTMX polling of `run_events` | M1 |
| R3 | PostgresSaver over Neon pooler (prepared statements) | `prepare_threshold=None`; or direct (unpooled) URL for the checkpointer | M1 |
| R4 | Long LLM call exceeds segment budget | per-call timeout (60 s), `max_retries=2`, segment budget 240 s < maxDuration 300 s | M6/M7 |
| R5 | Concurrent resume of the same thread from two tabs | `pg_try_advisory_lock(hash(report_id))`; second tab gets "already running" | M7 |
| R6 | Checkpoint bloat | state holds references; observations in tables | M7 |
| R7 | `with_structured_output` provider differences | provider-agnostic via JSON-schema method + Pydantic validation + `include_raw` | M6 |
| R8 | Eurostat API rate/latency, JSON-stat quirks | cache raw responses per report in DB (resume never refetches), backoff | M3 |
| R9 | Private Vercel Blob availability on Hobby | `BlobStore` abstraction, Postgres fallback | M2 |
| R10 | Cold starts compiling graph + importing pandas | lazy imports in routes, compile graph once per instance | M8 |
