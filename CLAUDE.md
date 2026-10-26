# StatBrief — project instructions

AI statistical report generator: FastAPI + HTMX + Tailwind/DaisyUI UI, LangChain/LangGraph workflow, Neon Postgres (pgvector, LangGraph checkpointer), deployed on Vercel (fra1). Python 3.12, venv at `.venv`.

## Resume protocol (mandatory)
- At session start a hook runs `scripts/resume_check.py`; follow its diagnosis before doing anything else. If the hook output is missing, run it yourself: `.venv/Scripts/python scripts/resume_check.py`.
- `docs/PROGRESS.md` is the single source of truth for where work stands. Update its ```state block (phase, base_commit, last_verified_commit, next_action) at **every** step transition, and commit it with the step.
- Never re-dispatch a task listed as completed in `docs/PROGRESS.md`; trust git + that file over conversation memory.

## Process override for this project (fast mode, user decision 2026-10-26, D-026)
Overrides the global pipeline's per-task cadence for THIS project only (global ~/.claude/CLAUDE.md unchanged):
- One developer subagent + one reviewer subagent per milestone batch (a whole milestone, or half of a large one) — not per task. Both on Opus.
- One review pass per batch, scored /10 with the global rubric. Critical/Important findings are fixed by the developer, who reports test evidence; no re-review unless a Critical was found or the score was < 6. Minor findings are logged in `.superpowers/sdd/progress.md` and handled at the milestone's final check.
- Independent milestones may run in parallel in separate git worktrees (e.g. M3 API clients ∥ M5 RAG).
- Plans list interfaces, rules and test cases rather than full code for every function.
- Reviews probe security and silent-wrong-value risks first; exhaustive edge-case fuzzing only where those risks exist.
- Each milestone still ends with: final review (≥ 8/10), QA verdict, retro, merge.

## Conventions
- Docstring on every function/class; no hardcoded tunables (use `statbrief.config.Settings`); tests need no network or API keys.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Docs
- Spec: `docs/spec.md` · Decisions: `docs/decisions.md` · Architecture + milestone task lists: `docs/architecture.md` · Plans: `docs/superpowers/plans/` · Risk spikes: `docs/spikes.md`
