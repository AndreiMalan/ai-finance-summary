# Progress — single source of truth for resuming work

This file is updated at **every** step transition (task dispatched, implemented, reviewed, fixed, approved, merged).
`scripts/resume_check.py` compares it against git and prints where to resume; a Claude Code SessionStart hook runs that check automatically.

## Current state

```state
milestone: M2
task: 0
task_title: Write M2 implementation plan (schema + file ingestion + chunked uploads to private Blob)
phase: planning
branch: main
base_commit: 52c5234
last_verified_commit: 52c5234
plan: (to be written) docs/superpowers/plans/2026-10-26-m2-ingestion.md
next_action: Write the M2 plan from docs/architecture.md §7 (M2 tasks), spec AC 9-19, decisions D-020/D-021; then create branch m2-ingestion and execute with subagent-driven-development. Pending in parallel: final M1 whole-branch review + QA (fix findings on main); user to lengthen DEMO_USER_PASSWORD in Vercel (healthz shows demo_user misconfigured).
updated: 2026-10-26
```

Phases: `implementing` → `reviewing` → `fixing` → `approved` → (next task) … → `merging` → `done`.
- `base_commit`: HEAD before the current task started (review diffs start here).
- `last_verified_commit`: newest commit whose tests passed **and** whose review is approved.

## How to resume (rules)

1. Run `.venv/Scripts/python scripts/resume_check.py` (the hook does this automatically).
2. If it reports **uncommitted changes while phase is `implementing`/`fixing`**, the step was interrupted: inspect the diff, run the tests, then either finish and commit it or discard it (`git stash`) and re-dispatch the step from `base_commit`.
3. If it reports **new commits after `base_commit` while phase is `implementing`/`fixing`**, the implementer finished: move to `reviewing`.
4. Never re-run a task listed as complete below; trust git + this file over conversation memory.

## Verified vs assumed (keep honest — the next session trusts this file)

- **Verified** (by test run or command output): M1 Tasks 1–6 — full suite green at f6c861e (87 tests); Vercel env var names present (`vercel env ls`); Neon region fra1 (user-confirmed).
- **Verified on Vercel preview (6a4ec94)**: R1 bundle 373 MB < 500 MB; R2 SSE incremental; R3 setup on Neon + healthz db=true.
- **Assumed / not yet verified**: R3 interrupt/resume through Neon pooler (M7); demo login on production (needs merge).

## Suggested skills for the next session

- `superpowers:subagent-driven-development` — continue the current plan task-by-task (implementer → reviewer → fixes).
- `superpowers:writing-plans` — when a milestone has no plan file yet (M2+).
- `superpowers:verification-before-completion` — before claiming a task, deploy or milestone is done.
- `review` / `qa` / `retro` — the global CLAUDE.md pipeline stages at milestone end.
- `handoff` — only if the work must travel (different tool, folder or person); it writes to temp, so this file stays the durable record.

## Milestone status

| Milestone | Status |
|---|---|
| M1 Skeleton, auth, limits, Vercel | ✅ all 7 tasks; merged to main |
| M2 Schema + ingestion | 🔄 planning |
| M3–M9 | ⬜ not started |

## Completed tasks (append-only)

| Task | Commits | Review |
|---|---|---|
| M1-T1 Scaffold, Settings, test harness | 5e17dbb..ff0fd53 | 9/10 |
| M1-T2 DB pool, migrations, LangGraph setup | 411ed58..d85f4c1 | 9/10 |
| M1-T3 Passwords, users, demo user | efaff14..0b9e83c | 10/10 |
| M1-T4 Login throttling, usage limits | a7b339a..563b07d | 10/10 |
| M1-T5 Signed session cookies | f5e81f2..f3004c7 | 10/10 |
| M1-T6 Web app, auth UI, asset build | 53319f0..f6c861e | 9/10 |
| M1-T7 Vercel entrypoint, spikes, deploy fixes | 71c71d4..ad86e83 | 9/10 |

## External setup status

| Item | Status |
|---|---|
| GitHub repo AndreiMalan/ai-finance-summary | ✅ |
| Vercel project ai-finance-summary (https://ai-finance-summary.vercel.app) | ✅ linked locally (`vercel link`) |
| Neon Postgres (fra1) with DATABASE_URL / DATABASE_URL_UNPOOLED | ✅ |
| Vercel env: SESSION_SECRET, APP_ENV, DEMO_USER_EMAIL, DEMO_USER_PASSWORD | ✅ (Production; SESSION_SECRET also Preview) |
| Vercel Blob store (private, OIDC: BLOB_STORE_ID) | ✅ Production + Preview |
| Production deploy | ✅ https://ai-finance-summary.vercel.app (M1) |
| DEMO_USER_PASSWORD ≥ 10 chars | ⬜ user to fix in Vercel |
| OpenAI API key | ⬜ needed at M6 |
