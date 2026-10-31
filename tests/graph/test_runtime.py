"""Postgres checkpointer + store wiring (M7b Task 7): resume in a fresh graph, no re-fetch, preferences."""
from langgraph.types import Command

from statbrief.analytics.metrics import compute_metrics
from statbrief.domain.preferences import UserPreferences
from statbrief.domain.report import ReportParams
from statbrief.graph.preferences import PreferencesRepo
from statbrief.graph.runtime import RunResources, make_checkpointer, make_store, prepare_run
from statbrief.storage.repos import MappingRepo, ObservationRepo, RunEventRepo
from tests.graph.conftest import FIXED_NOW
from tests.graph.helpers import draft_for, long_csv, monthly, report_call, unemp_rows
from tests.unit.llm.fakes import ToolFakeChatModel

CRYPTIC = ("c1", "c2", "c3", "c4", "c5")
PARAMS = ReportParams(mode="combined", indicators=["HICP_YOY", "IRS_UNEMP"], regions=["RO"],
                      period_from="2023-01", period_to="2024-01", style="detailed", formats=["xlsx"])


class Script:
    """Replies appended while the test runs."""

    def __init__(self) -> None:
        """Start empty."""
        self.replies: list = []

    def stream(self):
        """Hand out queued replies in order."""
        while True:
            yield self.replies.pop(0)


def resources(world, script: Script) -> RunResources:
    """Run resources over the test world with a scripted model and the API stub."""
    model = ToolFakeChatModel(messages=script.stream())
    return RunResources(pool=world.pool, settings=world.settings, blobs=world.blobs,
                        model_factory=lambda settings, cap: model, http_client=world.api.client,
                        embeddings_factory=lambda settings: None, now=lambda: FIXED_NOW, sleep=lambda s: None)


def _metric(world):
    """The uploaded pp change 2024-01 vs 2023-01."""
    obs = [o for o in ObservationRepo(world.pool).list_for_report(world.user_id, world.report_id)
           if o.indicator_code == "IRS_UNEMP"]
    return next(m for m in compute_metrics(obs, indicator="IRS_UNEMP", region="RO")[0]
                if m.kind == "pp_change" and m.base_period == "2023-01")


def test_resume_in_a_fresh_graph_neither_refetches_nor_loses_mappings(world):
    """AC 40: interrupt, then a new graph object on the same Postgres checkpointer resumes from the checkpoint."""
    world.api.serve("HICP_YOY", {"RO": monthly(2022, [9.0 - i * 0.2 for i in range(25)])})
    ref = world.add_file("cryptic.csv", long_csv(unemp_rows(), header=CRYPTIC))
    script = Script()
    first = prepare_run(resources(world, script), world.user_id, world.report_id)
    out = first.graph.invoke(world.base_state(PARAMS), first.config)
    assert out["__interrupt__"][0].value["kind"] == "resolve" and len(world.api.requests) == 1

    second = prepare_run(resources(world, script), world.user_id, world.report_id)  # "another process"
    assert second.graph is not first.graph
    saved = second.graph.get_state(second.config).values
    assert set(saved["mappings"]) == {ref.file_id} and saved["needs_input"].mappings[0].file_id == ref.file_id
    script.replies.append(report_call(draft_for(_metric(world), "0.6")))
    review = second.graph.invoke(Command(resume={"confirm_mappings": [ref.file_id]}), second.config)
    assert review["__interrupt__"][0].value["kind"] == "review"
    assert len(world.api.requests) == 1  # nothing re-fetched
    assert MappingRepo(world.pool).get(world.user_id, ref.file_id) is None  # confirmed, not rewritten
    kinds = {e["kind"] for e in RunEventRepo(world.pool).list_for_report(world.user_id, world.report_id)}
    assert {"llm_start", "llm_end"} <= kinds  # the handler (sharing the cap) logged the model call


def test_approve_saves_preferences_per_user(world):
    """AC 43: approval stores the report defaults under ("prefs", user_id); other users see nothing."""
    world.add_file("irs.csv", long_csv(unemp_rows()))
    script = Script()
    run = prepare_run(resources(world, script), world.user_id, world.report_id)
    params = ReportParams(mode="upload", indicators=["IRS_UNEMP"], regions=["RO"], style="detailed",
                          formats=["xlsx"])
    # The draft can cite real ids only after normalization: script it when analysis is done.
    for chunk in run.graph.stream(world.base_state(params), run.config, stream_mode="updates"):
        if "flag_significant" in chunk:
            script.replies.append(report_call(draft_for(_metric(world), "0.6")))
    assert run.graph.get_state(run.config).next == ("human_review",)
    run.graph.invoke(Command(resume={"action": "approve"}), run.config)
    prefs = PreferencesRepo(make_store(world.pool))
    assert prefs.get(world.user_id) == UserPreferences(regions=["RO"], indicators=["IRS_UNEMP"], style="detailed",
                                                       formats=["xlsx"])
    assert prefs.get(world.other_user_id) is None


def test_preferences_repo_is_namespaced_and_tolerates_bad_values(world):
    """Two users' defaults never mix; a malformed stored value reads as none."""
    store = make_store(world.pool)
    repo = PreferencesRepo(store)
    repo.save(world.user_id, UserPreferences(regions=["RO"]))
    repo.save(world.other_user_id, UserPreferences(regions=["HU"]))
    assert repo.get(world.user_id).regions == ["RO"] and repo.get(world.other_user_id).regions == ["HU"]
    store.put(("prefs", world.user_id), "report_defaults", {"formats": ["docx"]})
    assert repo.get(world.user_id) is None


def test_checkpointer_uses_the_allow_listed_serde(world):
    """Every Postgres checkpointer of the graph carries make_serializer()'s allow-list."""
    saver = make_checkpointer(world.pool)
    assert ("statbrief.graph.state", "SourceMeta") in saver.serde._allowed_msgpack_modules
