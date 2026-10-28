# M7 — LangGraph Workflow — Plan (lean, fast mode D-026)

> Large milestone → two batches, each one developer + one review (D-026). **M7a** (graph core, tasks 1–6) then **M7b** (persistence + runtime, tasks 7–10). Worktree `D:/AI_Finance_summary-m7`, branch `m7-graph` from main after M6 is merged. M7b continues on the same branch after M7a's review fixes.

**Goal:** One checkpointed `StateGraph` that turns report parameters + uploaded files + API requests into a verified, human-approved draft, demonstrating every LangGraph concept the brief requires as a working feature: typed state with reducers, nodes, conditional edges, cycles with bounded retries, `Send` fan-out/fan-in, subgraphs, `interrupt()` + `Command(resume=…)`, Postgres checkpointing with `thread_id = report_id`, streaming (`updates` + `custom`), replay/fork from checkpoints, long-term memory (`PostgresStore`), error-handling routes (AC 36–46; architecture §3–§5; risks R4–R6).

**Constraints:** Settings (+ `.env.example`): `enable_parallel=True`, `enable_replay=True`, `segment_budget_s=240` (must be < Vercel `maxDuration` 300), `graph_recursion_limit=60`, `max_revisions` / `max_parse_retries` (exist, M4), `api_fetch_attempts` reuse M3 retry settings. The core path must work with `ENABLE_PARALLEL`, `ENABLE_VECTOR_SEARCH`, `ENABLE_REPLAY` all off (AC 46). State carries **references** (file ids, report id), never observations or file bytes (R6) — observations live in `observations`, API payloads in `api_cache`. All dependencies injected via `GraphDeps` (dataclass): chat model, retriever fn, repos, blob store, HTTP client factory, clock, settings — tests use `GenericFakeChatModel`/`FakeMessagesListChatModel`, `InMemorySaver`, `InMemoryStore`, stub API clients, the pgserver DB fixture. No network, no API key. Docstrings; commit per task; trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. No new dependencies expected (langgraph 1.2.17 + checkpoint-postgres 3.2.0 already pinned).

**Existing APIs to use (read them first):** `domain/` (Observation, DatasetRef, ColumnMapping, ValidationReport, Conflict, ConflictResolution, MetricResult, MetricSkip, SignificantChange, ReportDraft, VerificationResult, review contracts), `ingest/` (readers, detect_schema, validate_table, normalize.to_observations + StaleReport/UnresolvedIssues), `sources/` (CATALOG, fetch_eurostat, fetch_worldbank — sync httpx — SourceError), `analytics/` (combine, detect_conflicts, apply_resolutions, fill_gaps, compute_metrics, flag_significant, ObservationStore, verify_draft), `rag/retriever.retrieve` + `rag/api_metadata.metadata_documents`, `llm/` (get_chat_model, prompts, chains build_context_chain/build_draft_chain/build_revise_chain/build_review_revise_chain → DraftAttempt, tools build_tools/run_tool_loop, callbacks RunEventsCallbackHandler + daily_llm_cap_guard, guards.check_unknown_columns), `storage/repos.py` (ReportRepo, FileRepo, MappingRepo, ValidationRepo, ObservationRepo, ApiCacheRepo, RunEventRepo), `storage/db.py` (pool, unpooled bootstrap, LangGraph setup with advisory lock — already done in M1).

---

## M7a — graph core

### Task 1 — State (`graph/state.py`)
- `ReportState` exactly as architecture §3 (TypedDict, `total=False`); reducers: `operator.add` for fan-in lists, `merge_dicts` for per-source dicts (later key wins, documented). `ReportParams` (Pydantic: indicators, regions, period_from/to, mode, style, formats, consent_gap_fill, links: list[IndicatorLink]). `DraftAttempt` from M6. `WorkflowError(node, kind, message, retryable)`. `ReviewDecision(action: approve|reject|request_changes, comment)`.
- Tests: reducers merge Send results without loss; state JSON-serialisable through the checkpointer serde (round-trip with `InMemorySaver`).

### Task 2 — Ingestion subgraph (`graph/ingestion.py`)
- Nodes `select_sources` → fan-out: `Send("fetch_api", req)` per ApiRequest ‖ `Send("parse_file", ref)` per DatasetRef (sequential loop when `enable_parallel=False`, same results).
- `fetch_api`: cache-first via `ApiCacheRepo` (resume never re-fetches, AC 40); on `SourceError` after M3's retries → `WorkflowError(retryable=False)` + `source_metadata` entry status `failed` (no exception escapes).
- `parse_file`: blob → reader → stored mapping (MappingRepo) or `detect_schema`; returns `SourceMeta` only (no rows in state).
- Tests: N APIs + M files fan-in count; cache hit makes zero client calls; one failing API does not lose the others; parallel on/off identical output.

### Task 3 — Validation, conflicts, interrupt #1 (`graph/nodes_validate.py`)
- `validate_normalize`: per file validate + normalize into `ObservationRepo.replace_for_file`; API observations persisted likewise (source key); `combine` + `detect_conflicts`.
- `route_after_validation(state) -> "resolve_gate" | "analyze_dispatch" | "fail_safe"`: gate when any mapping confidence < `mapping_min_confidence`, blocking validation issues, unresolved conflicts, unit mismatches, or a failed API source (AC 44 "continue without World Bank?").
- `resolve_gate`: `interrupt({"kind": "resolve", mappings, issues, conflicts, failed_sources})`; resume payload validated with Pydantic (`ResolvePayload`: mapping overrides, issue fixes/exclusions, conflict resolutions, `drop_sources`, `consent_gap_fill`) → saves mappings, loops back to `validate_normalize` (cycle, bounded by `max_revisions` gate rounds → fail_safe).
- Tests: clean data skips the interrupt; low-confidence mapping interrupts; resume with fixes reaches analysis; invalid resume payload → interrupt again with error message (never crash); failed API → interrupt offering drop.

### Task 4 — Analysis fan-out (`graph/nodes_analyze.py`)
- `analyze_dispatch` → `Send("analyze_one", {indicator, region})` per pair (or loop) → `compute_metrics` against `ObservationStore` rebuilt from `ObservationRepo` (not from state) → fan-in `metrics`/`metric_skips` → `aggregate_metrics` (deterministic sort) → `flag_significant`.
- Tests: K pairs aggregate deterministically regardless of completion order; skips preserved; parallel on/off identical.

### Task 5 — Retrieval + drafting subgraphs (`graph/retrieval.py`, `graph/drafting.py`)
- Retrieval subgraph: `build_queries` (indicator labels + significant changes + "commentary guidelines") → `retrieve` (vector or FTS per M5) + `metadata_documents` for API series → dedupe, cap `prompt_max_passages` → `retrieved`.
- Drafting subgraph: `draft` (M6 draft chain, tools bound via `run_tool_loop` when the model supports tools) → if `parse_error`: retry ≤ `max_parse_retries` then `fail_safe`; else `verify_claims` (M4 `verify_draft` + M6 `check_unknown_columns`) → `route_after_verify(state) -> "revise" | "human_review" | "fail_safe"`; `revise` uses the revise chain with the verifier feedback; `revision_count` bounded by `max_revisions` (AC 27/38).
- `fail_safe`: status `failed`, keeps the last draft + verification feedback for the UI ("needs manual attention"), never raises.
- Tests (fake models emitting scripted JSON): verified first time → human_review; wrong number → revise → verified; always wrong → fail_safe after exactly `max_revisions`; malformed output → retry then fail_safe; undocumented column interpreted → revise.

### Task 6 — Human review interrupt #2 + export hand-off (`graph/nodes_review.py`, `graph/builder.py`)
- `human_review`: `interrupt({"kind": "review", draft, verification, citations})`; resume `ReviewDecision`: approve → `export` (stub node in M7 that records `ExportStatus(pending)`; real exporters M9); reject → END status `rejected`; request_changes → drafting subgraph via review-revise chain with the comment, `review_round` incremented (bounded by `max_revisions`); edited drafts from the UI are re-verified before export.
- `build_graph(checkpointer, store, deps) -> CompiledStateGraph`: wires everything; `route_after_review`.
- Tests: full happy path to export with `InMemorySaver` and two `Command(resume=…)`; reject ends; request_changes loops; graph diagram renders (`get_graph().draw_mermaid()`) — saved to `docs/graph.mmd` for the concepts PDF.

**M7a done criteria:** all tests green, no network; report `.superpowers/sdd/m7a-report.md`.

---

## M7b — persistence and runtime

### Task 7 — Postgres checkpointer + store wiring (`graph/runtime.py`)
- `PostgresSaver`/`PostgresStore` on the existing psycopg pool (setup already done in M1 bootstrap); `thread_id = report_id`; config carries `callbacks=[RunEventsCallbackHandler(...)]`, `recursion_limit`.
- Long-term preferences: `PreferencesRepo` over `PostgresStore`, namespace `("prefs", user_id)`; saved on approve (regions, indicators, style, formats); read only to pre-fill the new-report form — never as data (AC 43).
- Tests on pgserver: interrupt → new process-equivalent (fresh graph object, same checkpointer) → resume continues without re-calling stub API client and with mappings intact (AC 40); prefs namespaced per user.

### Task 8 — Segmented runner + SSE (`graph/runner.py`, `web/routes/run.py`)
- `run_segment(report_id, user, command | None, budget_s) -> Iterator[SseEvent]`: `pg_try_advisory_lock(hashtext(report_id))` (second caller → event `already_running`, R5); streams `graph.stream(..., stream_mode=["updates","custom"])` → `SseEvent(type, node, message, progress)`; nodes emit custom progress via `get_stream_writer()`; stops cleanly when an interrupt is reached, the graph ends, or `budget_s` elapses between super-steps (event `continue` — the browser reconnects and the next segment resumes from the checkpoint; AC 45).
- Routes: `GET /reports/{id}/run/stream` (SSE), `POST /reports/{id}/resume` (validated payload → `Command(resume=…)`), user-scoped (404 for other users' reports), CSRF per M1 conventions, daily cap errors → friendly event.
- Tests: SSE event order; lock contention; budget split resumes to completion across segments; other-user 404.

### Task 9 — History, replay, fork (`graph/history.py`, routes)
- `list_checkpoints(report_id, user)` (get_state_history: step, node, created_at, has_draft); `get_checkpoint_draft`; `fork_from(report_id, checkpoint_id, user)` → `update_state` on that checkpoint config → new branch continues on next run (AC 42). Disabled (404) when `enable_replay=False`.
- Tests: history lists interrupts and drafts; fork re-runs drafting from an earlier checkpoint without refetching APIs.

### Task 10 — Error routes + end-to-end graph tests
- Model errors/timeouts in draft → bounded retry then fail_safe with WorkflowError; daily cap `LimitExceeded` → status `failed` with friendly message (no retry); API failure → resolve interrupt (Task 3).
- End-to-end on pgserver with fakes: sample upload (IRS-style CSV with `val_adj_x`) + stubbed Eurostat HICP → conflict interrupt → resolve → draft verified → review approve → export pending; assert run_events written, no cell values in `detail`.

**M7b done criteria:** all tests green, no network; bundle measured; report `.superpowers/sdd/m7b-report.md` incl. the LangGraph concept → module table (for the M9 concepts PDF).
