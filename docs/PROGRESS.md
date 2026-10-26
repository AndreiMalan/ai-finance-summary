# Progress — single source of truth for resuming work

This file is updated at **every** step transition (task dispatched, implemented, reviewed, fixed, approved, merged).
`scripts/resume_check.py` compares it against git and prints where to resume; a Claude Code SessionStart hook runs that check automatically.

## Current state

```state
milestone: M2
task: 9
task_title: Upload & ingestion JSON API + template download
phase: reviewing
branch: m2-ingestion
base_commit: 163a834
last_verified_commit: f8264af
plan: docs/superpowers/plans/2026-10-26-m2-ingestion.md
next_action: Combined T9 review + M2 final whole-branch review + QA running (diff main..b9e3d17). Then developer fixes Critical/Important, preview deploy + live upload test, merge to main. Then M3 ∥ M5 in parallel worktrees.
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
| M1 Skeleton, auth, limits, Vercel | ✅ 7 tasks + hardening (final review 9.5/10, QA PASS); retro docs/retros/m1.md |
| M2 Schema + ingestion | 🔄 implementing (plan: 9 tasks) |
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
| M1 hardening (final review fixes) | 6cc6f90..4bd343b | 9.5/10 |
| M2-T1 Deps + domain contracts | a0a39fa..cb4e6ef | 10/10 |
| M2-T2 Upload sniffing | b40e7b3..63d6976 | 9/10 |
| M2-T3 Readers | cbbf1e2..b3410f6 | 9/10 (after fix round from 7/10) |
| M2-T4 Cell parsers | e65e9e9..087602f | 10/10 (after 2 fix rounds) |
| M2-T5 Schema detection | 018c355..8933e08 | 9/10 (after fix round from 7/10) |
| M2-T6 Row parsing + validation | 85d0e8d..45054fd | 9/10 (after fix round from 7/10) |
| M2-T7 Normalization | a51ffd2..813e6e2 | 10/10 (after 3 review cycles) |
| M2-T8 Storage, repos, blobs | 308aea6..f8264af | 10/10 (after hardening) |

## External setup status

| Item | Status |
|---|---|
| GitHub repo AndreiMalan/ai-finance-summary | ✅ |
| Vercel project ai-finance-summary (https://ai-finance-summary.vercel.app) | ✅ linked locally (`vercel link`) |
| Neon Postgres (fra1) with DATABASE_URL / DATABASE_URL_UNPOOLED | ✅ |
| Vercel env: SESSION_SECRET, APP_ENV, DEMO_USER_EMAIL, DEMO_USER_PASSWORD | ✅ (Production; SESSION_SECRET also Preview) |
| Vercel Blob store (private, OIDC: BLOB_STORE_ID) | ✅ Production + Preview |
| BLOB_BACKEND=vercel (Production + Preview) | ✅ set 2026-10-26 (required by prod guard from M2-T8) |
| Production deploy | ✅ https://ai-finance-summary.vercel.app (M1) |
| DEMO_USER_PASSWORD ≥ 10 chars | ⬜ user to fix in Vercel |
| OpenAI API key | ⬜ needed at M6 |
