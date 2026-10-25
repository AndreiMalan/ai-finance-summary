# StatBrief — project instructions

AI statistical report generator: FastAPI + HTMX + Tailwind/DaisyUI UI, LangChain/LangGraph workflow, Neon Postgres (pgvector, LangGraph checkpointer), deployed on Vercel (fra1). Python 3.12, venv at `.venv`.

## Resume protocol (mandatory)
- At session start a hook runs `scripts/resume_check.py`; follow its diagnosis before doing anything else. If the hook output is missing, run it yourself: `.venv/Scripts/python scripts/resume_check.py`.
- `docs/PROGRESS.md` is the single source of truth for where work stands. Update its ```state block (phase, base_commit, last_verified_commit, next_action) at **every** step transition, and commit it with the step.
- Never re-dispatch a task listed as completed in `docs/PROGRESS.md`; trust git + that file over conversation memory.

## Conventions
- Docstring on every function/class; no hardcoded tunables (use `statbrief.config.Settings`); tests need no network or API keys.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Docs
- Spec: `docs/spec.md` · Decisions: `docs/decisions.md` · Architecture + milestone task lists: `docs/architecture.md` · Plans: `docs/superpowers/plans/` · Risk spikes: `docs/spikes.md`
