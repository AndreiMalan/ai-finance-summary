"""Model factory (Task 1): chat model and embeddings configured from Settings, no network."""
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from statbrief.auth.limits import LimitExceeded
from statbrief.llm import models
from statbrief.llm.models import LLMNotConfigured, friendly_llm_error, get_chat_model, get_embeddings
from tests.unit.llm.fakes import ToolFakeChatModel, make_settings, scripted


def test_llm_settings_defaults():
    """LLM tunables have the planned defaults (D-010 / plan M6)."""
    s = make_settings()
    assert (s.llm_provider, s.llm_model) == ("openai", "gpt-5-mini")
    assert (s.llm_temperature, s.llm_timeout_s, s.llm_max_retries) == (0.2, 60, 2)
    assert s.max_tool_rows == 200 and s.log_prompts is False
    assert s.llm_structured_output_method == "function_calling"


def test_missing_key_raises_not_configured():
    """Without OPENAI_API_KEY the factory refuses (callers show a friendly message)."""
    with pytest.raises(LLMNotConfigured):
        get_chat_model(make_settings(), cap_guard=None)


def test_provider_and_model_read_from_settings(monkeypatch):
    """init_chat_model gets '<provider>:<model>' and every tunable from Settings."""
    seen = {}

    def fake_init(model, **kwargs):
        seen["model"], seen["kwargs"] = model, kwargs
        return ToolFakeChatModel(messages=scripted(AIMessage(content="hi")))

    monkeypatch.setattr(models, "init_chat_model", fake_init)
    s = make_settings(openai_api_key="sk-test", llm_model="gpt-x-mini", llm_temperature=0.5,
                      llm_timeout_s=12, llm_max_retries=1)
    marker = object()
    m = get_chat_model(s, cap_guard=None, callbacks=[marker])
    assert isinstance(m, BaseChatModel)
    assert seen["model"] == "openai:gpt-x-mini"
    kw = seen["kwargs"]
    assert (kw["temperature"], kw["timeout"], kw["max_retries"]) == (0.5, 12, 1)
    assert kw["api_key"].get_secret_value() == "sk-test"
    assert kw["callbacks"] == [marker]


def test_real_openai_model_constructed_offline():
    """With a (dummy) key the real ChatOpenAI is built without any network call."""
    m = get_chat_model(make_settings(openai_api_key="sk-dummy"), cap_guard=None)
    assert type(m).__name__ == "ChatOpenAI"
    assert m.model_name == "gpt-5-mini"
    assert "sk-dummy" not in repr(m)


def test_fake_model_injectable():
    """Downstream code takes any BaseChatModel: a fake answers without a key."""
    fake = ToolFakeChatModel(messages=scripted(AIMessage(content="ok")))
    assert fake.invoke("x").content == "ok"


def test_embeddings_none_without_key_and_openai_with_key():
    """get_embeddings reuses the M5 factory: None without a key, OpenAIEmbeddings (offline) with one."""
    assert get_embeddings(make_settings()) is None
    e = get_embeddings(make_settings(openai_api_key="sk-dummy"))
    assert type(e).__name__ == "OpenAIEmbeddings" and e.check_embedding_ctx_length is False


def test_friendly_errors():
    """Configuration and cap errors become user-facing sentences without internals."""
    assert "not configured" in friendly_llm_error(LLMNotConfigured("x")).lower()
    msg = friendly_llm_error(LimitExceeded("llm_call", 500, "global"))
    assert "daily" in msg.lower() and "500" in msg
    assert friendly_llm_error(RuntimeError("sk-secret leaked")) == models.GENERIC_LLM_ERROR


def test_cap_guard_is_a_required_keyword():
    """I2: a call site must pass a cap guard (or an explicit None, tests/admin only)."""
    with pytest.raises(TypeError):
        get_chat_model(make_settings(openai_api_key="sk-dummy"))  # type: ignore[call-arg]


def test_cap_guard_attached_at_model_level(monkeypatch):
    """I2: the factory attaches the cap callback to the model itself, before any caller callbacks."""
    from statbrief.llm.callbacks import DailyLLMCap, LLMCapCallbackHandler

    seen = {}
    monkeypatch.setattr(models, "init_chat_model", lambda model, **kw: seen.update(kw) or
                        ToolFakeChatModel(messages=scripted(AIMessage(content="x"))))
    cap = DailyLLMCap(lambda: None)
    marker = object()
    get_chat_model(make_settings(openai_api_key="sk-test"), cap_guard=cap, callbacks=[marker])
    first, second = seen["callbacks"]
    assert isinstance(first, LLMCapCallbackHandler) and first.cap is cap and second is marker


def test_temperature_drop_logged_for_reasoning_models(caplog):
    """Review M5: a non-default temperature that gpt-5 models ignore is logged at model build."""
    import logging

    with caplog.at_level(logging.INFO, logger="statbrief.llm.models"):
        get_chat_model(make_settings(openai_api_key="sk-dummy"), cap_guard=None)
    assert "LLM_TEMPERATURE" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="statbrief.llm.models"):
        get_chat_model(make_settings(openai_api_key="sk-dummy", llm_model="gpt-5-chat-latest"), cap_guard=None)
    assert "LLM_TEMPERATURE" not in caplog.text
