# M9 — Exports, Sample Data, Retention, Deployment, Concepts PDF — Plan (lean, fast mode D-026)

> One developer + one review. Worktree `D:/AI_Finance_summary-m9`, branch `m9-finish` from main after M8. Tasks 1–3 may start in parallel with M8b (they don't touch UI files). Task 6 (concepts PDF) needs the M6/M7 concept tables from `.superpowers/sdd/m6-report.md`, `m7a-report.md` and `m7b-report.md`.

**Goal:** Approved reports export to PDF and Excel with full provenance. Realistic dummy datasets exercise every demo scenario. Retention and account deletion work, including Blob objects. Production is verified. A local LaTeX PDF explains every LangChain/LangGraph concept (AC 47–50, 52–62).

**Constraints:**
- `reportlab` moves from requirements-dev to runtime (pin it exactly in both requirements.txt and pyproject.toml); `openpyxl` is already present.
- Exports are built from the approved draft plus observations, never from LLM free text alone.
- Numbers are rounded only at presentation, with the claim text's precision.
- Exports are stored in Blob (private), served through an owner-checked download route, and recorded in `report_versions` (migration 006).
- Concepts PDF: LOCAL ONLY in `docs/concepts/` (`concepts.tex` + compiled `concepts.pdf`). It must not be deployed: `docs/**` is already excluded in vercel.json; verify this.
- Fictional issuers (ESMB/IRS) only for invented documents; never attribute invented content to INS, Eurostat or the World Bank.
- Docstrings; tunables in Settings; tests without network; commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Task 1 — PDF export (`export/pdf.py`)
- `build_pdf(bundle: ReportBundle) -> bytes` (reportlab platypus): cover (title, period, regions, generated date, "approved by" user email), executive summary, one section per indicator with paragraphs and a chart (reportlab graphics line chart from observations), data-quality notes (validation exclusions, gap fills, conflict resolutions), citations grouped *Uploaded files / Public APIs / Reference documents* (AC 47/59), and a calculation appendix (metric id, formula, input observation ids → source).
- `ReportBundle` is assembled by `export/bundle.py` from graph state + repos.
- Tests: the PDF opens with pypdf, contains the section titles and all three citation groups, the appendix lists every claim's metric, and it is deterministic apart from timestamps.

## Task 2 — Excel export (`export/xlsx.py`)
- `build_xlsx(bundle) -> bytes`, values only (no formulas, AC 48): sheets Summary, Data (observations used, with provenance columns), Metrics, Provenance, Conflicts, Citations. Strings starting with `= + - @` are written as text (formula-injection safe).
- Wire the M7 `export` node: build the requested formats → Blob → `report_versions` → `ExportStatus(done, links)`; download route `GET /reports/{id}/exports/{version}/{fmt}` (owner only).
- Tests: openpyxl reads back every sheet; no cell has a formula; injection strings are neutralised; another user gets 404.

## Task 3 — Sample data (`sample_data/`)
- `IRS_regional_monthly_2023-2024.xlsx`: two sheets (Romanian headers `luna, judet, indicator, valoare, um`, plus an undocumented `val_adj_x`), planted issues (a text in a numeric cell, a duplicate row, an unknown county, `15.03.2024`-style dates) and planted conflicts against Eurostat HICP for RO (2021=100 vs 2015=100 base effect, matching the IRS Technical Note §3.2).
- `sector_output_Q.csv` (quarterly levels, thousands separators), `regional_indicators.json`, and `cryptic_columns.csv` (column meanings unknown → guard demo).
- `sample_data/README.md` maps each file to the 8 demo scenarios (AC 52–59).
- Tests: each file passes sniff and detection with the expected roles, the planted issues are detected, and the planted conflicts are detected against a recorded Eurostat fixture.

## Task 4 — Retention + account deletion (`maintenance/retention.py`, `/cron/cleanup`, settings page)
- The rules exactly as AC 50: raw files 30 d after last activity on unfinished reports, 7 d after approval, immediately on delete; checkpoints +30 d; run_events/logs 14 d; abandoned chunked uploads purged after `upload_abandon_hours` (Setting, default 24).
- **Blob objects are deleted before the cascading DB deletes.** A Blob failure leaves the DB row for a retry (idempotent).
- `/cron/cleanup` requires the `CRON_SECRET` header; `vercel.json` gets a daily cron. "Delete my account" (with confirmation and password) removes Blob objects, LangGraph threads/checkpoints and store namespaces, then the user (cascade).
- Tests: each rule with a fake clock; Blob deleted first; a failure is retried; the cron is rejected without the secret.

## Task 5 — Production readiness
- Measure the bundle (< 500 MB). Check `/healthz` additions: migrations at head, LangGraph tables present, blob backend; no secrets in output. Confirm the function region fra1 is effective (`x-vercel-id` header).
- `docs/deploy.md`: environment variables (names only), cron, region, the knowledge-base indexing command, and the user-action checklist (from docs/PROGRESS.md).
- A smoke-test script `scripts/smoke.py` (run manually against a URL with credentials from env) that walks the 8 scenarios through the HTTP API with a fake-model toggle unavailable in production. It is only documented here; it runs live after the user actions.

## Task 6 — LaTeX concepts PDF (`docs/concepts/`)
- `concepts.tex`: a cover; a clickable TOC (`hyperref`); one page per concept with a fixed layout: *What it is (general)* · *How StatBrief uses it and why* · *Where in the code* (module:function) · *How to see it in the app* · *Tests* · *Core / optional (feature flag)*.
- Concepts, LangChain: chat model config, prompt templates, structured output, LCEL/Runnables (incl. RunnableParallel), tools/tool calling, callbacks, document loaders, text splitters, embeddings, vector store/retriever. LangGraph: StateGraph + typed state, reducers, nodes and edges, conditional edges, cycles/bounded retries, Send (map-reduce), subgraphs, interrupt + Command, checkpointing (Postgres), streaming modes, time travel (history/replay/fork), long-term memory store.
- Compile with a local TeX distribution if one is installed (`latexmk -pdf` or `pdflatex` twice). If none is installed, try `tectonic`, installed into the venv/scratch without admin rights. If compiling is impossible, commit the .tex and record that.
- Include the `docs/graph.mmd` diagram as a rendered figure if feasible (mermaid-cli via npx), else as a TikZ or verbatim listing.

## Task 7 — Final checks
- Whole-branch final review (≥ 8/10) of main after the merge, the QA verdict (coverage estimate, AC checklist 1–62 with pass/defer), triage of the open minors from `.superpowers/sdd/progress.md`. No retro (project decision).

**Done:** all tests green; production redeployed; the user-action checklist presented to the user once.
