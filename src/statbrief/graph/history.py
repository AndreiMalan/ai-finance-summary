"""History, replay and fork (AC 42, ENABLE_REPLAY): checkpoints of one report thread.

- `list_checkpoints`: the thread's *forkable* checkpoints, newest first (`get_state_history`, at most
  HISTORY_LIMIT): id, step, the node due next, time, status, whether a draft exists, which interrupt (if any)
  was pending. Only the safe resume points (`FORK_POINTS`: before analysis, retrieval, drafting and the
  review) are listed and can be forked.
- `get_checkpoint_draft`: the draft stored in one checkpoint (prior drafts for the History tab).
- `fork_from`: `update_state(<old checkpoint config>, {})` creates a new checkpoint as a child of the chosen
  one; it becomes the thread's latest and the next run segment continues from it (e.g. re-drafting from the
  checkpoint before drafting). API responses come from the report's `api_cache` while it is kept, so a fork
  normally re-fetches nothing.

Why only single, edge-reached steps (D-039): `update_state` re-derives the next nodes from the graph's
edges; the `Send` packets of a fan-out step (`analyze_one` x K, even when K = 1) are not carried over, so a fork there would leave a
branch with nothing to run that reports "done" without metrics. A checkpoint before the fan-out
(`analyze_dispatch`) re-plans it instead. Lookups use `get_state(thread_id + checkpoint_id)` and check
that the returned checkpoint belongs to this thread; callers check report ownership first.
"""
from datetime import datetime

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StateSnapshot
from pydantic import BaseModel

from statbrief.config import Settings
from statbrief.domain.report import ReportDraft
from statbrief.graph.builder import graph_config


class UnknownCheckpoint(KeyError):
    """No checkpoint with this id in the report's thread."""


class NotForkable(ValueError):
    """The checkpoint sits inside a fan-out step (or at the end): it cannot start a branch."""


class CheckpointInfo(BaseModel):
    """One checkpoint as listed in the History tab."""

    checkpoint_id: str
    step: int | None
    next: list[str]
    created_at: datetime | None
    status: str | None
    has_draft: bool
    interrupt: str | None  # "resolve" / "review" when the run was waiting there


PULL = "__pregel_pull"  # LangGraph's path marker of a task reached by an edge (Send tasks are "push")
# The safe resume points (D-039): before analysis, before retrieval, before drafting, before the review. Each
# is reached from a single static node, so `update_state(old, {})` re-derives exactly that next step.
# Elsewhere a fork can dead-end: inside a Send fan-out (packets are not carried over) or at the fan-in
# (`aggregate_metrics`, last written by Send tasks), and `__start__` has no values to fork from.
FORK_POINTS: frozenset[str] = frozenset({"analyze_dispatch", "retrieval", "drafting", "human_review"})


def forkable(snap: StateSnapshot) -> bool:
    """True when the checkpoint's next step is exactly one allow-listed resume point reached by an edge."""
    return (len(snap.next) == 1 and snap.next[0] in FORK_POINTS and bool(snap.values)
            and all(t.path[:1] == (PULL,) for t in snap.tasks))


def _info(snap: StateSnapshot) -> CheckpointInfo:
    """Listing entry of a snapshot."""
    created = datetime.fromisoformat(snap.created_at) if snap.created_at else None
    kinds = [i.value.get("kind") for t in snap.tasks for i in t.interrupts if isinstance(i.value, dict)]
    return CheckpointInfo(checkpoint_id=snap.config["configurable"]["checkpoint_id"],
                          step=snap.metadata.get("step") if snap.metadata else None, next=list(snap.next),
                          created_at=created, status=snap.values.get("status"),
                          has_draft=snap.values.get("draft") is not None, interrupt=kinds[0] if kinds else None)


def list_checkpoints(graph: CompiledStateGraph, report_id: str, settings: Settings) -> list[CheckpointInfo]:
    """The thread's forkable checkpoints, newest first, at most HISTORY_LIMIT of them."""
    out: list[CheckpointInfo] = []
    for snap in graph.get_state_history(graph_config(report_id, settings)):
        if forkable(snap):
            out.append(_info(snap))
            if len(out) >= settings.history_limit:
                break
    return out


def _snapshot(graph: CompiledStateGraph, report_id: str, checkpoint_id: str, settings: Settings) -> StateSnapshot:
    """The snapshot of one checkpoint of this report's thread; UnknownCheckpoint otherwise."""
    config = graph_config(report_id, settings)
    config["configurable"]["checkpoint_id"] = checkpoint_id
    try:
        snap = graph.get_state(config)
    except Exception:  # malformed id
        raise UnknownCheckpoint(checkpoint_id) from None
    found = snap.config.get("configurable", {}) if snap.config else {}
    if not snap.values or found.get("thread_id") != report_id or found.get("checkpoint_id") != checkpoint_id:
        raise UnknownCheckpoint(checkpoint_id)
    return snap


def get_checkpoint_draft(graph: CompiledStateGraph, report_id: str, checkpoint_id: str,
                         settings: Settings) -> ReportDraft | None:
    """The draft stored in a checkpoint (None when it has none); UnknownCheckpoint for a foreign id."""
    return _snapshot(graph, report_id, checkpoint_id, settings).values.get("draft")


def validate_fork(graph: CompiledStateGraph, report_id: str, checkpoint_id: str, settings: Settings) -> StateSnapshot:
    """The checkpoint a fork would start from; UnknownCheckpoint / NotForkable (nothing is written)."""
    snap = _snapshot(graph, report_id, checkpoint_id, settings)
    if not forkable(snap):
        raise NotForkable(checkpoint_id)
    return snap


def fork_from(graph: CompiledStateGraph, report_id: str, checkpoint_id: str, settings: Settings) -> str:
    """Make the chosen checkpoint the start of a new branch; returns the new (latest) checkpoint id.

    UnknownCheckpoint / NotForkable (see `validate_fork`) before anything is written.
    """
    snap = validate_fork(graph, report_id, checkpoint_id, settings)
    new_config = graph.update_state(snap.config, {})
    return new_config["configurable"]["checkpoint_id"]
