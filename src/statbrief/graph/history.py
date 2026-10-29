"""History, replay and fork (AC 42, ENABLE_REPLAY): checkpoints of one report thread.

- `list_checkpoints`: the top-level checkpoints of the thread, newest first (`get_state_history`): id, step,
  the nodes due next, time, status, whether a draft exists and which interrupt (if any) was pending.
- `get_checkpoint_draft`: the draft stored in one checkpoint (prior drafts for the History tab).
- `fork_from`: `update_state(<old checkpoint config>, {})` creates a new checkpoint as a child of the chosen one,
  which becomes the thread's latest; the next run segment continues from it (e.g. re-drafting from the
  checkpoint before drafting). API responses come from `api_cache`, so nothing is re-fetched.
Only the report's own thread (thread_id = report_id) is ever read; callers check ownership first.
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


class CheckpointInfo(BaseModel):
    """One checkpoint as listed in the History tab."""

    checkpoint_id: str
    step: int | None
    next: list[str]
    created_at: datetime | None
    status: str | None
    has_draft: bool
    interrupt: str | None  # "resolve" / "review" when the run was waiting there


def _info(snap: StateSnapshot) -> CheckpointInfo:
    """Listing entry of a snapshot."""
    created = datetime.fromisoformat(snap.created_at) if snap.created_at else None
    kinds = [i.value.get("kind") for t in snap.tasks for i in t.interrupts if isinstance(i.value, dict)]
    return CheckpointInfo(checkpoint_id=snap.config["configurable"]["checkpoint_id"],
                          step=snap.metadata.get("step") if snap.metadata else None, next=list(snap.next),
                          created_at=created, status=snap.values.get("status"),
                          has_draft=snap.values.get("draft") is not None, interrupt=kinds[0] if kinds else None)


def list_checkpoints(graph: CompiledStateGraph, report_id: str, settings: Settings) -> list[CheckpointInfo]:
    """The thread's checkpoints, newest first."""
    return [_info(s) for s in graph.get_state_history(graph_config(report_id, settings))]


def _snapshot(graph: CompiledStateGraph, report_id: str, checkpoint_id: str, settings: Settings) -> StateSnapshot:
    """The snapshot of one checkpoint of the thread; UnknownCheckpoint otherwise."""
    for snap in graph.get_state_history(graph_config(report_id, settings)):
        if snap.config["configurable"]["checkpoint_id"] == checkpoint_id:
            return snap
    raise UnknownCheckpoint(checkpoint_id)


def get_checkpoint_draft(graph: CompiledStateGraph, report_id: str, checkpoint_id: str,
                         settings: Settings) -> ReportDraft | None:
    """The draft stored in a checkpoint (None when it has none); UnknownCheckpoint for a foreign id."""
    return _snapshot(graph, report_id, checkpoint_id, settings).values.get("draft")


def fork_from(graph: CompiledStateGraph, report_id: str, checkpoint_id: str, settings: Settings) -> str:
    """Make the chosen checkpoint the start of a new branch; returns the new (latest) checkpoint id."""
    snap = _snapshot(graph, report_id, checkpoint_id, settings)
    new_config = graph.update_state(snap.config, {})
    return new_config["configurable"]["checkpoint_id"]
