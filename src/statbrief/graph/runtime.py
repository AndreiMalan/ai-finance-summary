"""Persistence and per-run wiring of the report graph (AC 40, 43): Postgres checkpointer + store, run setup.

- `make_checkpointer(pool)`: `PostgresSaver` on the app's pool (autocommit, no prepared statements, dict
  rows; tables created at bootstrap), with the allow-listed serializer (D-037). `thread_id = report_id`.
- `make_store(pool)`: `PostgresStore` for long-term memory (`graph.preferences.PreferencesRepo`: one
  user's report defaults under ("prefs", user_id); they pre-fill the new-report form, never data, AC 43).
- `prepare_run(resources, user_id, report_id)`: everything one run segment needs, rebuilt per segment
  (building the graph takes ~20 ms): a `DailyLLMCap` for the user, the chat model capped by it
  (`get_chat_model(settings, cap_guard=cap)`), the run-events handler sharing the SAME cap (D-034, each call
  counted once), the user-scoped `PassageRetriever`, `GraphDeps`, the compiled graph and its run config.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.postgres import PostgresStore
from psycopg_pool import ConnectionPool

from statbrief.config import Settings
from statbrief.graph.builder import build_graph, graph_config
from statbrief.graph.deps import GraphDeps
from statbrief.graph.serde import make_serializer
from statbrief.llm.callbacks import DailyLLMCap, RunEventsCallbackHandler, daily_llm_cap_guard
from statbrief.llm.chains import PassageRetriever
from statbrief.llm.models import get_chat_model, get_embeddings
from statbrief.sources.http import utc_now
from statbrief.storage.blobs import BlobStore
from statbrief.storage.repos import RunEventRepo

def make_checkpointer(pool: ConnectionPool) -> PostgresSaver:
    """The report graph's checkpointer on the app pool (allow-listed serde)."""
    return PostgresSaver(pool, serde=make_serializer())


def make_store(pool: ConnectionPool) -> PostgresStore:
    """LangGraph's long-term store on the app pool."""
    return PostgresStore(pool)


def _default_model(settings: Settings, cap: DailyLLMCap) -> BaseChatModel:
    """The configured chat model, capped at model level by the user's daily-cap guard."""
    return get_chat_model(settings, cap_guard=cap)


@dataclass
class RunResources:
    """App-level resources and the factories a run is built from (tests replace the factories)."""

    pool: ConnectionPool
    settings: Settings
    blobs: BlobStore
    model_factory: Callable[[Settings, DailyLLMCap], BaseChatModel] = _default_model
    http_client: Callable[[], httpx.Client] = httpx.Client
    embeddings_factory: Callable[[Settings], Embeddings | None] = get_embeddings
    now: Callable[[], datetime] = utc_now
    sleep: Callable[[float], None] = field(default=time.sleep)


@dataclass
class PreparedRun:
    """One segment's graph, run config, run-events handler and deps."""

    graph: CompiledStateGraph
    config: RunnableConfig
    handler: RunEventsCallbackHandler
    deps: GraphDeps


def prepare_run(res: RunResources, user_id: str, report_id: str) -> PreparedRun:
    """Build the per-segment run: capped model, handler sharing the cap, user-scoped retriever, graph."""
    s = res.settings
    cap = daily_llm_cap_guard(res.pool, s, user_id, now=res.now)
    handler = RunEventsCallbackHandler(RunEventRepo(res.pool), user_id=user_id, report_id=report_id, settings=s,
                                       cap_guard=cap, now=res.now)
    retriever = PassageRetriever.for_user(res.pool, user_id, s, res.embeddings_factory(s))
    deps = GraphDeps.from_pool(res.pool, s, blobs=res.blobs, chat_model=res.model_factory(s, cap),
                               retriever=retriever, http_client=res.http_client, now=res.now, sleep=res.sleep)
    graph = build_graph(make_checkpointer(res.pool), make_store(res.pool), deps)
    return PreparedRun(graph=graph, config=graph_config(report_id, s, callbacks=[handler]), handler=handler,
                       deps=deps)


def state_graph(res: RunResources) -> CompiledStateGraph:
    """The report graph for reading or updating checkpoints only (status, resume validation, history, fork).

    It has no model and no retriever, so it never needs an API key; it must never be streamed.
    """
    deps = GraphDeps.from_pool(res.pool, res.settings, blobs=res.blobs, chat_model=None, retriever=lambda q: [],
                               http_client=res.http_client, now=res.now, sleep=res.sleep)
    return build_graph(make_checkpointer(res.pool), make_store(res.pool), deps)
