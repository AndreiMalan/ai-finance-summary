"""Error routes (M7b Task 10): model/provider/timeout failures retried then fail_safe; the daily cap never retries."""
import uuid
from typing import Any

from statbrief.auth.limits import LimitExceeded
from statbrief.domain.report import ReportParams
from statbrief.graph.runner import run_segment
from statbrief.graph.runtime import state_graph
from statbrief.llm.callbacks import daily_llm_cap_guard
from tests.graph.helpers import draft_for, long_csv, report_call, unemp_rows
from tests.graph.runkit import Script, pp_metric, resources
from tests.unit.llm.fakes import ToolFakeChatModel

PARAMS = ReportParams(mode="upload", indicators=["IRS_UNEMP"], regions=["RO"]).model_dump(mode="json")


class FlakyModel(ToolFakeChatModel):
    """Fake model that raises the queued exceptions first, then answers the script."""

    failures: list = []

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        """Raise the next queued failure, else answer normally."""
        if self.failures:
            raise self.failures.pop(0)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _run(world, failures, replies, **settings):
    """One run segment (upload mode, clean file) with a model failing as queued; returns (events, state)."""
    world.add_file("irs.csv", long_csv(unemp_rows()))
    script = Script()
    script.replies.extend(replies)
    model = FlakyModel(messages=script.stream(), failures=list(failures))
    res = resources(world, script, **settings)
    res.model_factory = lambda s, cap: model
    events = list(run_segment(res, world.user_id, world.report_id, params=PARAMS, take_resume=lambda: None,
                              budget_s=240))
    state = state_graph(res).get_state({"configurable": {"thread_id": world.report_id}}).values
    return events, state


def test_timeout_is_retried_then_the_draft_succeeds(world):
    """A provider timeout costs one node-level retry; the next call drafts normally."""
    events, state = _run(world, [TimeoutError("provider timed out: sk-secret")],
                         [lambda: report_call(draft_for(pp_metric(world, "IRS_UNEMP"), "0.6"))])
    assert events[-1].type == "interrupt" and events[-1].data["kind"] == "review"
    model_errors = [e for e in state["errors"] if e.kind == "model_error"]
    assert len(model_errors) == 1 and model_errors[0].retryable and "secret" not in model_errors[0].message


def test_persistent_provider_errors_fail_safely_with_a_friendly_message(world):
    """MAX_MODEL_RETRIES=1: two failed calls, then the safe failure; the run ends 'done' with status failed."""
    events, state = _run(world, [ConnectionError("x"), ConnectionError("y"), ConnectionError("z")], [])
    assert events[-1].type == "done" and events[-1].data == {"status": "failed"}
    assert [e.kind for e in state["errors"]].count("model_error") == 2
    final = state["errors"][-1]
    assert final.kind == "needs_manual_attention" and "language model could not complete" in final.message


def test_daily_cap_fails_at_once_without_retry(world):
    """LimitExceeded is final: no retry, a friendly 'daily limit' message, status failed."""
    events, state = _run(world, [LimitExceeded("llm_call", 500, "global")], [])
    assert events[-1].data == {"status": "failed"}
    assert [e.kind for e in state["errors"] if e.kind.startswith(("llm", "model"))] == ["llm_limit"]
    assert "daily limit of 500" in state["errors"][-1].message


def test_real_cap_guard_blocks_the_call_through_the_run_handler(world):
    """With DAILY_LLM_CALL_CAP used up, the run's handler (sharing the cap) blocks the first model call."""
    cap = daily_llm_cap_guard(world.pool, world.settings.model_copy(update={"daily_llm_call_cap": 1}),
                              world.user_id)
    cap.check(uuid.uuid4())  # spend the only call of the day
    events, state = _run(world, [], [lambda: report_call(draft_for(pp_metric(world, "IRS_UNEMP"), "0.6"))],
                         daily_llm_call_cap=1)
    assert events[-1].data == {"status": "failed"} and state["errors"][-1].message.startswith("The daily limit")


def test_missing_model_is_not_retried(world):
    """A run without a chat model (wiring error) ends in fail_safe at once."""
    world.add_file("irs.csv", long_csv(unemp_rows()))
    res = resources(world, Script())
    res.model_factory = lambda s, cap: None
    events = list(run_segment(res, world.user_id, world.report_id, params=PARAMS, take_resume=lambda: None,
                              budget_s=240))
    assert events[-1].data == {"status": "failed"}
