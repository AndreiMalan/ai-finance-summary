"""Structured output + LCEL chains (Task 3): context chain, draft/revise chains, parse-failure detection."""
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from statbrief.domain.report import Finding, ReportDraft
from statbrief.llm.chains import (
    DraftAttempt, PassageRetriever, build_context_chain, build_draft_chain, build_interpret_chain,
    build_review_revise_chain, build_revise_chain, check_parsed, document_to_passage, passage_to_document,
)
from statbrief.llm.prompts import render_context, revise_inputs, review_revise_inputs, source_infos
from tests.unit.llm import data
from tests.unit.llm.fakes import (
    ToolFakeChatModel, invalid_tool_call_message, make_settings, scripted, tool_call_message,
)

USAGE = {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}


def valid_draft() -> dict[str, Any]:
    """A schema-valid ReportDraft payload as a model would send it in a tool call."""
    return {
        "title": "Unemployment in Romania",
        "executive_summary": [{"headline": "Unemployment rose by 0.6 percentage points.", "claim_ids": ["c1"]}],
        "sections": [{"indicator_code": "UNEMP_RATE", "title": "Unemployment",
                      "paragraphs": ["The rate rose by 0.6 percentage points."], "claim_ids": ["c1"],
                      "citation_ids": ["file-1"]}],
        "claims": [{"claim_id": "c1", "text": "Unemployment rose by 0.6 percentage points.", "value": 0.6,
                    "unit": "pp", "metric_id": "pp_change:UNEMP_RATE:RO:2024-01:2023-01",
                    "observation_ids": ["u13", "u01"]}],
        "citations": [{"kind": "upload", "ref_id": "file-1", "label": "IRS_regional_monthly.xlsx"}],
        "caveats": ["Figures for the latest month are provisional."],
    }


def _context() -> dict[str, str]:
    """A rendered draft context."""
    metrics, sig = data.metrics()
    return render_context(regions=["RO"], period="2023-01 to 2024-01", metrics=metrics, significant=sig,
                          sources=source_infos(data.observations()), passages=data.passages(),
                          column_descriptions={}, undocumented_columns=["val_adj_x"], settings=make_settings())


class _Capture(BaseCallbackHandler):
    """Collects the messages every chat-model call received."""

    def __init__(self) -> None:
        """Start empty."""
        self.prompts: list[str] = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        """Keep the text of the prompt."""
        self.prompts.append("\n".join(str(m.content) for m in messages[0]))


def _run(chain_builder, reply: AIMessage, inputs: dict, **settings_kw) -> tuple[DraftAttempt, ToolFakeChatModel]:
    """Run a chain built around a fake model that answers `reply`."""
    model = ToolFakeChatModel(messages=scripted(reply))
    return chain_builder(model, make_settings(**settings_kw)).invoke(inputs), model


def test_valid_output_parsed_to_report_draft_with_usage():
    """A well-formed tool call becomes a ReportDraft; usage metadata is captured."""
    attempt, model = _run(build_draft_chain, tool_call_message("ReportDraft", valid_draft(), usage=USAGE), _context())
    assert isinstance(attempt.draft, ReportDraft) and attempt.parse_error is None
    assert attempt.draft.claims[0].observation_ids == ["u13", "u01"]
    assert attempt.usage == USAGE
    assert '"title": "Unemployment in Romania"' in attempt.raw_text
    assert model.structured_method == "function_calling"
    assert [t.__name__ for t in model.bound_tools] == ["ReportDraft"]


def test_structured_method_from_settings():
    """with_structured_output receives LLM_STRUCTURED_OUTPUT_METHOD."""
    _, model = _run(build_draft_chain, tool_call_message("ReportDraft", valid_draft()), _context(),
                    llm_structured_output_method="json_schema")
    assert model.structured_method == "json_schema"


def test_malformed_json_sets_parse_error():
    """Unparseable tool-call arguments never raise: parse_error is set and the raw text kept."""
    attempt, _ = _run(build_draft_chain, invalid_tool_call_message("ReportDraft", '{"title": "x", '), _context())
    assert attempt.draft is None and "malformed" in attempt.parse_error.lower()
    assert attempt.raw_text == '{"title": "x", '
    assert attempt.usage == {}


def test_missing_field_sets_parse_error():
    """A payload without a required field is a parse error naming the field."""
    payload = valid_draft()
    del payload["title"]
    attempt, _ = _run(build_draft_chain, tool_call_message("ReportDraft", payload), _context())
    assert attempt.draft is None and "title" in attempt.parse_error


def test_claim_without_observation_ids_sets_parse_error():
    """A claim with an empty observation_ids list fails validation (no unsupported numbers)."""
    payload = valid_draft()
    payload["claims"][0]["observation_ids"] = []
    attempt, _ = _run(build_draft_chain, tool_call_message("ReportDraft", payload), _context())
    assert attempt.draft is None and "observation_ids" in attempt.parse_error


def test_parse_error_does_not_echo_payload_values():
    """Validation feedback names locations and reasons, not the submitted values."""
    payload = valid_draft()
    payload["claims"][0]["value"] = "SECRET-VALUE"
    attempt, _ = _run(build_draft_chain, tool_call_message("ReportDraft", payload), _context())
    assert attempt.draft is None and "claims.0.value" in attempt.parse_error
    assert "SECRET-VALUE" not in attempt.parse_error


def test_plain_text_reply_is_parse_error():
    """A reply without any tool call is reported, with its text kept for diagnostics."""
    attempt, _ = _run(build_draft_chain, AIMessage(content="Here is your report: ..."), _context())
    assert attempt.draft is None and "no structured output" in attempt.parse_error
    assert attempt.raw_text == "Here is your report: ..."


def test_check_parsed_handles_parser_exception():
    """check_parsed turns a parsing_error into parse_error."""
    a = check_parsed({"raw": AIMessage(content="x"), "parsed": None, "parsing_error": ValueError("bad json")})
    assert a.draft is None and a.parse_error == "ValueError: bad json"


def test_revise_chain_sends_feedback_and_previous_draft():
    """The revise chain prompts with the rejected draft and the verifier feedback."""
    draft = ReportDraft.model_validate(valid_draft())
    cap = _Capture()
    model = ToolFakeChatModel(messages=scripted(tool_call_message("ReportDraft", valid_draft())))
    attempt = build_revise_chain(model, make_settings()).invoke(
        revise_inputs(_context(), draft, "claim c1: value should be 0.59"), config={"callbacks": [cap]})
    assert attempt.draft is not None
    assert "claim c1: value should be 0.59" in cap.prompts[0] and '"claim_id":"c1"' in cap.prompts[0]


def test_review_revise_chain_sends_comment():
    """The review-revise chain prompts with the human comment."""
    draft = ReportDraft(title="T", executive_summary=[Finding(headline="h")], sections=[], claims=[],
                        citations=[], caveats=[])
    cap = _Capture()
    model = ToolFakeChatModel(messages=scripted(tool_call_message("ReportDraft", valid_draft())))
    build_review_revise_chain(model, make_settings()).invoke(
        review_revise_inputs(_context(), draft, "Mention the HICP too"), config={"callbacks": [cap]})
    assert "Mention the HICP too" in cap.prompts[0]


def test_context_chain_runs_retrieval_and_formatting_in_parallel():
    """build_context_chain = RunnableParallel(retrieve ‖ format metrics) -> render; equals render_context."""
    seen = []
    retriever = PassageRetriever(search_fn=lambda q: seen.append(q) or data.passages())
    metrics, sig = data.metrics()
    settings = make_settings()
    request = {"query": "unemployment rate percentage points", "regions": ["RO"], "period": "2023-01 to 2024-01",
               "metrics": metrics, "significant": sig, "sources": source_infos(data.observations()),
               "column_descriptions": {}, "undocumented_columns": ["val_adj_x"]}
    out = build_context_chain(retriever, settings).invoke(request)
    assert seen == ["unemployment rate percentage points"]
    assert out == _context()


def test_passage_document_round_trip():
    """Passages survive conversion to LangChain Documents (score and method kept in metadata)."""
    p = data.passages()[1]
    assert document_to_passage(passage_to_document(p)) == p


def test_interpret_chain_returns_text():
    """The dataset-interpretation chain returns the model's text."""
    model = ToolFakeChatModel(messages=scripted(AIMessage(content="The file holds monthly rates.")))
    out = build_interpret_chain(model).invoke({"dataset_summary": "- rows: 3", "column_descriptions": "(none)",
                                               "undocumented_columns": "(none)", "style_rules": "-"})
    assert out == "The file holds monthly rates."


def test_chains_build_with_real_openai_model_offline():
    """The real ChatOpenAI accepts the structured-output configuration (no request is made)."""
    from statbrief.llm.models import get_chat_model

    for method in ("function_calling", "json_schema"):
        s = make_settings(openai_api_key="sk-dummy", llm_structured_output_method=method)
        assert build_draft_chain(get_chat_model(s, cap_guard=None), s) is not None
