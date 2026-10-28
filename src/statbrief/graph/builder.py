"""Wiring of the report graph (AC 36-39): one checkpointed StateGraph with three sub-graphs.

    START -> ingestion* -> validate_normalize --> resolve_gate (interrupt #1) --> validate_normalize (cycle)
                                    |--> fail_safe -> END
                                    '--> analyze_dispatch --Send x K--> analyze_one --> aggregate_metrics
                                                         '-----------> analyze_all --'        |
               flag_significant <---------------------------------------------------------------'
                     '--> retrieval* -> drafting* --> human_review (interrupt #2) --> export -> END
                                            |  ^              |  '--> END (rejected)
                                            |  '--------------'  (request_changes)
                                            '--> fail_safe -> END
    (* = compiled sub-graph)

`build_graph(checkpointer, store, deps)` compiles it; every checkpointer must use
`statbrief.graph.serde.make_serializer()` (allow-listed state classes). `graph_config` gives the run config
(thread_id = report_id, recursion limit). `mermaid_diagram` renders the structure (with sub-graphs expanded)
for docs/graph.mmd.
"""
from collections.abc import Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from statbrief.config import Settings
from statbrief.graph.deps import GraphDeps, node
from statbrief.graph.drafting import build_drafting_graph, route_after_drafting
from statbrief.graph.failure import fail_safe
from statbrief.graph.ingestion import build_ingestion_graph
from statbrief.graph.nodes_analyze import (aggregate_metrics, analyze_all, analyze_dispatch, analyze_one,
                                           flag_significant)
from statbrief.graph.nodes_review import export, human_review, route_after_review
from statbrief.graph.nodes_validate import resolve_gate, route_after_validation, validate_normalize
from statbrief.graph.retrieval import build_retrieval_graph
from statbrief.graph.state import ReportState

GRAPH_NAME = "statbrief_report"


def build_graph(checkpointer: BaseCheckpointSaver | None, store: BaseStore | None,
                deps: GraphDeps) -> CompiledStateGraph:
    """The compiled report graph (interrupts need a checkpointer; `store` is LangGraph's long-term store)."""
    s = deps.settings
    g = StateGraph(ReportState)
    g.add_node("ingestion", build_ingestion_graph(deps))
    g.add_node("validate_normalize", node(validate_normalize, deps))
    g.add_node("resolve_gate", node(resolve_gate, deps))
    g.add_node("analyze_dispatch", node(analyze_dispatch, deps), destinations=("analyze_one", "analyze_all"))
    g.add_node("analyze_one", node(analyze_one, deps))
    g.add_node("analyze_all", node(analyze_all, deps))
    g.add_node("aggregate_metrics", node(aggregate_metrics, deps))
    g.add_node("flag_significant", node(flag_significant, deps))
    g.add_node("retrieval", build_retrieval_graph(deps))
    g.add_node("drafting", build_drafting_graph(deps))
    g.add_node("human_review", node(human_review, deps))
    g.add_node("export", node(export, deps))
    g.add_node("fail_safe", node(fail_safe, deps))

    g.add_edge(START, "ingestion")
    g.add_edge("ingestion", "validate_normalize")
    g.add_conditional_edges("validate_normalize", lambda st: route_after_validation(st, s),
                            ["resolve_gate", "analyze_dispatch", "fail_safe"])
    g.add_edge("resolve_gate", "validate_normalize")
    g.add_edge("analyze_one", "aggregate_metrics")
    g.add_edge("analyze_all", "aggregate_metrics")
    g.add_edge("aggregate_metrics", "flag_significant")
    g.add_edge("flag_significant", "retrieval")
    g.add_edge("retrieval", "drafting")
    g.add_conditional_edges("drafting", lambda st: route_after_drafting(st, s), ["human_review", "fail_safe"])
    g.add_conditional_edges("human_review", route_after_review, ["drafting", "export", END])
    g.add_edge("export", END)
    g.add_edge("fail_safe", END)
    return g.compile(checkpointer=checkpointer, store=store, name=GRAPH_NAME)


def graph_config(report_id: str, settings: Settings,
                 callbacks: Sequence[BaseCallbackHandler] | None = None) -> RunnableConfig:
    """Run config of one report: thread_id = report_id, GRAPH_RECURSION_LIMIT, optional callbacks."""
    config: RunnableConfig = {"configurable": {"thread_id": report_id}, "recursion_limit": settings.graph_recursion_limit}
    if callbacks:
        config["callbacks"] = list(callbacks)
    return config


def mermaid_diagram(deps: GraphDeps) -> str:
    """Mermaid source of the graph with its sub-graphs expanded (xray); building it runs no node."""
    return build_graph(None, None, deps).get_graph(xray=True).draw_mermaid()
