"""Chat-model and embeddings factory configured from the environment (AC 28).

`get_chat_model` uses LangChain's provider-agnostic `init_chat_model("<provider>:<model>", ...)`, so the
provider and model are switched with LLM_PROVIDER / LLM_MODEL only. Building the client makes no network
call; requests happen only when the model is invoked. Tests inject fake chat models instead.
"""
from collections.abc import Sequence

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from statbrief.auth.limits import LimitExceeded
from statbrief.config import Settings
from statbrief.llm.callbacks import DailyLLMCap, LLMCapCallbackHandler
from statbrief.rag.embeddings import make_embeddings

GENERIC_LLM_ERROR = "The language model could not complete this step. Please try again later."


class LLMNotConfigured(RuntimeError):
    """No API key is configured for the selected LLM provider."""


def get_chat_model(settings: Settings, *, cap_guard: DailyLLMCap | None,
                   callbacks: Sequence[BaseCallbackHandler] | None = None) -> BaseChatModel:
    """The configured chat model (temperature, timeout and retries from Settings), capped at model level.

    `cap_guard` is required: application code passes `daily_llm_cap_guard(pool, settings, user_id)`, which
    is attached to the model as an `LLMCapCallbackHandler`, so every call is counted against
    DAILY_LLM_CALL_CAP even when the caller passes no callbacks (each call once, see DailyLLMCap). Only
    tests and admin scripts may pass `cap_guard=None` (an uncapped model). Raises LLMNotConfigured when the
    provider's API key is missing.
    """
    if settings.openai_api_key is None:
        raise LLMNotConfigured(f"no API key configured for LLM provider {settings.llm_provider!r}")
    return init_chat_model(
        f"{settings.llm_provider}:{settings.llm_model}",
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        api_key=settings.openai_api_key,
        callbacks=([LLMCapCallbackHandler(cap_guard)] if cap_guard is not None else [])
        + list(callbacks or []) or None,
    )


def get_embeddings(settings: Settings) -> Embeddings | None:
    """The configured embeddings model, or None (no key / vector search off -> full-text retrieval).

    Delegates to the M5 factory so there is exactly one place that builds embeddings.
    """
    return make_embeddings(settings)


def friendly_llm_error(exc: BaseException) -> str:
    """A user-facing sentence for an LLM-layer failure; never echoes the exception text (may hold keys)."""
    if isinstance(exc, LLMNotConfigured):
        return "The language model is not configured (no API key). Ask the administrator to set it up."
    if isinstance(exc, LimitExceeded):
        return (f"The daily limit of {exc.limit} language-model calls has been reached. "
                "Please try again tomorrow.")
    return GENERIC_LLM_ERROR
