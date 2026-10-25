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
