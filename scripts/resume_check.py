"""Print where work stands by comparing docs/PROGRESS.md with git, so a new session resumes exactly.

Usage: python scripts/resume_check.py [--tests]
Run automatically by the Claude Code SessionStart hook (.claude/settings.json). Never fails the hook:
problems are reported as text and the exit code is always 0.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROGRESS = ROOT / "docs" / "PROGRESS.md"
LEDGER = ROOT / ".superpowers" / "sdd" / "progress.md"
WORK_PHASES = {"implementing", "fixing"}


def git(*args: str) -> str:
    """Run a git command in the repo and return stripped stdout ('' on failure)."""
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def read_state() -> dict[str, str]:
    """Parse the ```state block of docs/PROGRESS.md into a key/value dict."""
    if not PROGRESS.exists():
        return {}
    block = re.search(r"```state\n(.*?)```", PROGRESS.read_text(encoding="utf-8"), re.S)
    if not block:
        return {}
    pairs = (line.split(":", 1) for line in block.group(1).splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in pairs}


BOOKKEEPING_PATHS = (":(exclude)docs", ":(exclude)CLAUDE.md", ":(exclude).claude")


def commits_since(base: str) -> list[str]:
    """Commits after `base` that touch code (docs/CLAUDE.md/.claude bookkeeping commits are ignored)."""
    out = git("log", "--oneline", f"{base}..HEAD", "--", ".", *BOOKKEEPING_PATHS) if base else ""
    return out.splitlines() if out else []


def run_tests() -> str:
    """Run the test suite quietly and return its summary line."""
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    py = py if py.exists() else Path(sys.executable)
    res = subprocess.run([str(py), "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else f"pytest exited {res.returncode}"


def diagnose(state: dict[str, str], branch: str, new_commits: list[str], dirty: list[str]) -> list[str]:
    """Turn the observed facts into explicit resume instructions."""
    phase, notes = state.get("phase", "?"), []
    if state.get("branch") and branch != state["branch"]:
        notes.append(f"WARNING: on branch '{branch}', PROGRESS.md expects '{state['branch']}'. Switch before continuing.")
    if phase in WORK_PHASES and dirty:
        notes.append("INTERRUPTED: uncommitted changes while a step was in progress. Inspect the diff and run tests; "
                     "finish+commit or `git stash` and re-dispatch the step from base_commit.")
    if phase in WORK_PHASES and new_commits and not dirty:
        notes.append("STEP FINISHED: new commits exist after base_commit — move to 'reviewing' "
                     "(review package base_commit..HEAD).")
    if phase in WORK_PHASES and not new_commits and not dirty:
        notes.append("NOT STARTED or LOST: no commits and no changes since base_commit — (re-)dispatch the step.")
    if phase == "reviewing":
        notes.append("Review was in progress: re-dispatch the reviewer on base_commit..HEAD (reviews are read-only, safe to repeat).")
    if phase not in WORK_PHASES and phase != "reviewing" and dirty:
        notes.append("NOTE: uncommitted changes outside a work phase — check whether they belong to the next step.")
    return notes or ["Consistent: follow next_action."]


def main() -> None:
    """Print the resume report."""
    state = read_state()
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    head = git("rev-parse", "--short", "HEAD")
    dirty = [ln for ln in git("status", "--porcelain").splitlines() if ln.strip()]
    base = state.get("base_commit", "")
    new_commits = commits_since(base)

    print("=== RESUME CHECK (docs/PROGRESS.md vs git) ===")
    if not state:
        print("docs/PROGRESS.md missing or has no ```state block — reconstruct state from git log and the plan files.")
    for key in ("milestone", "task", "task_title", "phase", "base_commit", "last_verified_commit", "plan"):
        print(f"{key:>21}: {state.get(key, '-')}")
    print(f"{'current branch':>21}: {branch}   HEAD: {head}")
    print(f"{'commits since base':>21}: {len(new_commits)}")
    for c in new_commits[:15]:
        print(f"{'':>23}{c}")
    print(f"{'uncommitted files':>21}: {len(dirty)}")
    for d in dirty[:20]:
        print(f"{'':>23}{d}")
    if "--tests" in sys.argv:
        print(f"{'tests':>21}: {run_tests()}")
    print("--- diagnosis ---")
    for note in diagnose(state, branch, new_commits, dirty):
        print(f"* {note}")
    print(f"--- next_action ---\n{state.get('next_action', '-')}")
    if LEDGER.exists():
        tail = LEDGER.read_text(encoding="utf-8").strip().splitlines()[-6:]
        print("--- review ledger (last lines) ---\n" + "\n".join(tail))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # the hook must never break session start
        print(f"resume_check failed: {exc!r} — fall back to `git log` and docs/PROGRESS.md")
