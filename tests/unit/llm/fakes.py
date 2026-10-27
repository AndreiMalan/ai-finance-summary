"""Offline chat-model fakes and settings for the LLM-layer tests (no network, no API key)."""
import json
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage

from statbrief.config import Settings


def make_settings(**overrides: Any) -> Settings:
    """Settings without a database connection or environment (the DB URL is never used by these tests)."""
    base: dict[str, Any] = {"database_url": "postgresql://x/y", "session_secret": "s" * 32, "_env_file": None}
    return Settings(**{**base, **overrides})


class ToolFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel that accepts `bind_tools` (tools are recorded, replies stay scripted).

    `with_structured_output` records the `method` it was asked for and then uses LangChain's generic
    tool-calling implementation, which is what OpenAI's `function_calling` method does as well.
    """

    bound_tools: list = []
    structured_method: str | None = None

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any):
        """Record the tools and return self (the scripted messages already contain any tool calls)."""
        self.bound_tools = list(tools)
        return self

    def with_structured_output(self, schema: Any, *, include_raw: bool = False, method: str | None = None,
                               **kwargs: Any):
        """Record the method, then delegate to BaseChatModel's tool-calling structured output."""
        self.structured_method = method
        return super().with_structured_output(schema, include_raw=include_raw, **kwargs)


class ToolFakeListChatModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel (cycles through `responses`) that accepts `bind_tools`."""

    bound_tools: list = []

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any):
        """Record the tools and return self."""
        self.bound_tools = list(tools)
        return self


def tool_call_message(name: str, args: dict, *, call_id: str = "call_1", usage: dict | None = None) -> AIMessage:
    """An AIMessage carrying one well-formed tool call (and optional usage metadata)."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
                     usage_metadata=usage)


def invalid_tool_call_message(name: str, raw_args: str, *, call_id: str = "call_1") -> AIMessage:
    """An AIMessage whose tool-call arguments are not valid JSON (as a provider returns them)."""
    return AIMessage(content="", invalid_tool_calls=[
        {"name": name, "args": raw_args, "id": call_id, "error": "malformed JSON", "type": "invalid_tool_call"}])


def scripted(*messages: AIMessage) -> Iterator[AIMessage]:
    """Iterator of replies for GenericFakeChatModel."""
    return iter(messages)


def as_json(obj: Any) -> str:
    """Compact JSON for assertions."""
    return json.dumps(obj, sort_keys=True)
