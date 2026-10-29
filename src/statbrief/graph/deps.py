"""Dependencies injected into the report graph (`GraphDeps`) and the helper that binds them to node functions.

Every node is a plain function ``fn(state, deps, config) -> dict``; `node(fn, deps)` turns it into the
``(state, config)`` callable LangGraph expects, so nodes never reach for globals and tests swap in fakes
(fake chat models, stub HTTP transports, repos on the test database, in-memory checkpointers).

`GraphDeps` is built per run (M7b): the chat model carries that user's daily-cap guard
(`get_chat_model(settings, cap_guard=cap)`, with the same `DailyLLMCap` given to the run's
`RunEventsCallbackHandler`), and `retriever` is scoped to that user's documents.
"""
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from psycopg_pool import ConnectionPool

from statbrief.config import Settings
from statbrief.rag.retriever import RetrievedPassage
from statbrief.sources.catalog import CATALOG, IndicatorSpec
from statbrief.sources.eurostat import fetch_eurostat
from statbrief.sources.http import utc_now
from statbrief.sources.results import FetchResult
from statbrief.sources.worldbank import fetch_worldbank
from statbrief.storage.blobs import BlobStore
from statbrief.storage.repos import ApiCacheRepo, FileRepo, MappingRepo, ObservationRepo, ValidationRepo


class Fetcher(Protocol):
    """A public-API series fetcher (`fetch_eurostat` / `fetch_worldbank` signature)."""

    def __call__(self, client: httpx.Client, spec: IndicatorSpec, geos: Sequence[str], **kwargs: Any) -> FetchResult:
        """Fetch one series (cache-first when given a cache scope)."""


DEFAULT_FETCHERS: Mapping[str, Fetcher] = {"eurostat": fetch_eurostat, "worldbank": fetch_worldbank}


@dataclass(frozen=True)
class GraphDeps:
    """Everything the nodes need besides the state: settings, repos, blob store, model, retriever, HTTP."""

    settings: Settings
    files: FileRepo
    mappings: MappingRepo
    validations: ValidationRepo
    observations: ObservationRepo
    api_cache: ApiCacheRepo
    blobs: BlobStore
    chat_model: BaseChatModel | None  # None: drafting is unavailable (the drafting nodes record an error)
    retriever: Callable[[str], Sequence[RetrievedPassage]]  # user-scoped passage search (M5 `retrieve`)
    http_client: Callable[[], httpx.Client] = httpx.Client
    fetchers: Mapping[str, Fetcher] = field(default_factory=lambda: dict(DEFAULT_FETCHERS))
    catalog: Mapping[str, IndicatorSpec] = field(default_factory=lambda: dict(CATALOG))
    now: Callable[[], datetime] = utc_now
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_pool(cls, pool: ConnectionPool, settings: Settings, *, blobs: BlobStore,
                  chat_model: BaseChatModel | None, retriever: Callable[[str], Sequence[RetrievedPassage]],
                  **kwargs: Any) -> "GraphDeps":
        """Deps with every repository on `pool`; other fields (http_client, fetchers, ...) via kwargs."""
        return cls(settings=settings, files=FileRepo(pool), mappings=MappingRepo(pool),
                   validations=ValidationRepo(pool), observations=ObservationRepo(pool),
                   api_cache=ApiCacheRepo(pool), blobs=blobs, chat_model=chat_model, retriever=retriever, **kwargs)


NodeFn = Callable[[Any, GraphDeps, RunnableConfig], Any]


def node(fn: NodeFn, deps: GraphDeps) -> Callable[[Any, RunnableConfig], Any]:
    """Bind `deps` to a node function ``fn(state, deps, config)``; the result is a LangGraph node."""
    def bound(state: Any, config: RunnableConfig) -> Any:
        """Run the node with the injected dependencies."""
        return fn(state, deps, config)

    bound.__name__ = bound.__qualname__ = fn.__name__
    bound.__doc__ = fn.__doc__
    return bound


def emit_progress(node_name: str, message: str) -> None:
    """Send a custom progress event to the stream (`stream_mode="custom"`, shown in the run timeline).

    A no-op when the graph is not streamed or the code runs outside a graph (unit tests of a node).
    `message` must hold no data values (it reaches the browser and logs).
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:  # not inside a graph run
        return
    writer({"node": node_name, "message": message})
