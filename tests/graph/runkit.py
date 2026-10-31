"""Helpers for runner, route and end-to-end tests: a scripted fake model and run resources on the test world."""
from statbrief.analytics.metrics import compute_metrics
from statbrief.graph.runtime import RunResources, state_graph
from statbrief.storage.repos import ObservationRepo
from tests.graph.conftest import FIXED_NOW
from tests.unit.llm.fakes import ToolFakeChatModel


class Script:
    """Model replies appended while a test runs (drafts cite ids known only after normalization)."""

    def __init__(self) -> None:
        """Start empty."""
        self.replies: list = []

    def stream(self):
        """Hand out queued replies in order; a callable reply is built when the model is called."""
        while True:
            reply = self.replies.pop(0)
            yield reply() if callable(reply) else reply

    def model(self) -> ToolFakeChatModel:
        """A tool-capable fake chat model reading this script."""
        return ToolFakeChatModel(messages=self.stream())


def resources(world, script: Script, **settings_overrides) -> RunResources:
    """Run resources over the test world: scripted model, API stub, no embeddings, fixed clock, no sleeping."""
    settings = world.settings.model_copy(update=settings_overrides) if settings_overrides else world.settings
    model = script.model()
    return RunResources(pool=world.pool, settings=settings, blobs=world.blobs,
                        model_factory=lambda s, cap: model, http_client=world.api.client,
                        embeddings_factory=lambda s: None, now=lambda: FIXED_NOW, sleep=lambda s: None)


def pp_metric(world, indicator: str, base: str = "2023-01", region: str = "RO"):
    """The stored pp change of `indicator` from `base` to the latest period (read from the database)."""
    obs = [o for o in ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)
           if o.indicator_code == indicator and o.region_code == region]
    return next(m for m in compute_metrics(obs, indicator=indicator, region=region)[0]
                if m.kind == "pp_change" and m.base_period == base)


def quoted(world, payload):
    """An answer as POST /resume queues it: the payload with the id of the interrupt pending right now."""
    snap = state_graph(resources(world, Script())).get_state({"configurable": {"thread_id": world.report_id}})
    return {"interrupt_id": snap.interrupts[0].id if snap.interrupts else "none", "payload": payload}


def queue_taker(world, queue: list):
    """take_resume for run_segment: pops a raw payload and quotes the currently pending interrupt."""
    return lambda: quoted(world, queue.pop()) if queue else None
