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
