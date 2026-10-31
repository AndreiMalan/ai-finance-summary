"""Checkpoint serializer for the report graph: msgpack with an explicit allow-list of state classes.

LangGraph rebuilds Pydantic models and dataclasses from a checkpoint by importing the class named in the
stored bytes. `make_serializer` restricts that to the classes the report state actually holds (plus
LangGraph's own safe types), so a tampered or foreign checkpoint cannot instantiate anything else, and the
"unregistered type" deprecation path is never used. A class missing from the list would come back as a
plain dict: `tests/graph/test_state.py` walks the state annotations and fails if any class is not listed.

Every checkpointer of the report graph (InMemorySaver in tests, PostgresSaver in M7b) must be created with
`serde=make_serializer()`.
"""
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from statbrief.domain.conflict import Conflict, ConflictResolution, IndicatorLink, UnitMismatch
from statbrief.domain.dataset import (ColumnGuess, ColumnMapping, DatasetRef, DetectedSchema, ValidationIssue,
                                      ValidationReport)
from statbrief.domain.metrics import MetricResult, MetricSkip, SignificantChange
from statbrief.domain.report import (Citation, ExportStatus, Finding, NumericalClaim, ReportDraft, ReportParams,
                                     Section)
from statbrief.domain.review import ClaimCheck, ReviewDecision, VerificationResult
from statbrief.graph.state import (ApiRequest, BlockingIssues, FailedSource, ObservationView, PendingMapping,
                                   ResolveRequest, SourceMeta, WorkflowError)
from statbrief.llm.chains import DraftAttempt
from statbrief.rag.retriever import RetrievedPassage

# Every class that can appear (at any depth) in ReportState, a sub-graph state or a Send payload.
STATE_CLASSES: tuple[type, ...] = (
    ReportParams, IndicatorLink, ExportStatus,
    DatasetRef, ColumnMapping, ColumnGuess, DetectedSchema, ValidationIssue, ValidationReport,
    Conflict, ConflictResolution, UnitMismatch,
    MetricResult, MetricSkip, SignificantChange,
    ReportDraft, Finding, Section, NumericalClaim, Citation,
    VerificationResult, ClaimCheck, ReviewDecision,
    ApiRequest, SourceMeta, WorkflowError, PendingMapping, BlockingIssues, FailedSource, ResolveRequest,
    ObservationView,
    DraftAttempt, RetrievedPassage,
)


def allowed_modules() -> list[tuple[str, str]]:
    """(module, class name) of every allow-listed state class."""
    return [(c.__module__, c.__name__) for c in STATE_CLASSES]


def make_serializer() -> JsonPlusSerializer:
    """The checkpoint serializer for the report graph (strict allow-list, no pickle fallback)."""
    return JsonPlusSerializer(allowed_msgpack_modules=allowed_modules())
