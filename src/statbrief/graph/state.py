"""Graph state (`ReportState`), its reducers, the sub-graph input/output schemas and the Send payloads.

State-serialisation decision (M7a): the state holds typed objects — Pydantic models from `domain/` and this
module, plus the frozen dataclasses `DraftAttempt` (M6) and `RetrievedPassage` (M5). LangGraph's
JsonPlusSerializer round-trips both kinds through msgpack, and `statbrief.graph.serde.make_serializer`
allow-lists exactly these classes, so a checkpoint can never instantiate any other class and an
unregistered type is caught by the tests instead of silently coming back as a plain dict. Interrupt
payloads, by contrast, are plain JSON-ready dicts (`model_dump(mode="json")`): they are sent to the browser.

The state carries references, never rows (R6): uploaded files are `DatasetRef`s (blob keys), observations
live in the `observations` table and raw API responses in `api_cache`; `normalized_ref` names the report
whose observations are current.

Reducers:
- `operator.add` for lists filled by parallel branches (Send fan-in) or appended over time:
  `source_metadata`, `metrics`, `metric_skips`, `draft_history`, `errors`. Nodes that need to replace such a
  list return `langgraph.types.Overwrite(...)`.
- `merge_dicts` for per-source dictionaries (`mappings`, `validation`): keys are merged and, for a key
  written twice, the later write wins (parallel writers use distinct keys: one file id each); writing None
  for a key deletes it.
Every other key is last-value: exactly one node may write it in a super-step.
"""
import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from statbrief.domain.conflict import Conflict, ConflictResolution, IndicatorLink, UnitMismatch
from statbrief.domain.dataset import ColumnMapping, DatasetRef, DetectedSchema, ValidationIssue, ValidationReport
from statbrief.domain.metrics import MetricResult, MetricSkip, SignificantChange
from statbrief.domain.report import ExportStatus, ReportDraft, ReportParams
from statbrief.domain.review import ReviewDecision, VerificationResult
from statbrief.llm.chains import DraftAttempt
from statbrief.rag.retriever import RetrievedPassage

Status = Literal["collecting", "needs_input", "analyzing", "drafting", "awaiting_review", "approved",
                 "exported", "rejected", "failed"]


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer for per-source dicts: union of both; for a key in both, the later (right) value wins; a right
    value of None deletes the key (how a node drops a stale per-file entry)."""
    merged = {**(left or {}), **(right or {})}
    return {k: v for k, v in merged.items() if v is not None}


class ApiRequest(BaseModel):
    """One public-API series to fetch for the report: a catalog indicator for some regions and a period window."""

    model_config = ConfigDict(frozen=True)
    indicator: str = Field(min_length=1)  # catalog code (statbrief.sources.catalog.CATALOG)
    regions: list[str] = Field(min_length=1)
    period_from: str | None = None
    period_to: str | None = None

    @property
    def request_id(self) -> str:
        """Stable id of the request (shown to the user, used to drop a failed source)."""
        return f"api:{self.indicator}:{','.join(self.regions)}:{self.period_from or ''}:{self.period_to or ''}"


class SourceMeta(BaseModel):
    """What ingestion learned about one source (no rows): status, counts, mapping and API metadata.

    `source_id` is the file id or the API request id; `source_key` is the identity
    `statbrief.analytics.combine.source_key` gives its observations ("upload:<file_id>" or
    "api:<api>:<series_id>"), so a dropped source can be filtered out of the observations.
    """

    model_config = ConfigDict(frozen=True)
    source_id: str
    kind: Literal["upload", "api"]
    label: str
    status: Literal["ok", "failed"]
    source_key: str | None = None
    message: str | None = None  # failure reason (never a response body or an exception message from a provider)
    observation_count: int = 0
    skipped_count: int = 0
    from_cache: bool = False
    # uploads
    mapping_source: Literal["stored", "detected", "none"] | None = None
    mapping_confidence: float | None = None
    needs_review: bool = False
    detected: DetectedSchema | None = None
    # public APIs
    indicator: str | None = None  # catalog code of the request
    query_url: str | None = None
    series_label: str | None = None
    unit_label: str | None = None
    last_update: str | None = None


class WorkflowError(BaseModel):
    """A problem recorded by a node (shown in the timeline); `retryable` says whether trying again may help."""

    model_config = ConfigDict(frozen=True)
    node: str
    kind: str
    message: str
    retryable: bool = False


class PendingMapping(BaseModel):
    """A file whose column mapping the user must confirm or supply (low confidence, none found, or unusable)."""

    model_config = ConfigDict(frozen=True)
    file_id: str
    filename: str
    mapping: ColumnMapping | None = None
    confidence: float | None = None
    reason: str


class BlockingIssues(BaseModel):
    """Validation errors of one file that block normalization until excluded."""

    model_config = ConfigDict(frozen=True)
    file_id: str
    filename: str
    issues: list[ValidationIssue]


class FailedSource(BaseModel):
    """A public-API source that could not be fetched; the user may continue without it (AC 44)."""

    model_config = ConfigDict(frozen=True)
    source_id: str
    label: str
    message: str


class ResolveRequest(BaseModel):
    """Everything the user must resolve before analysis (interrupt #1); empty means nothing blocks."""

    model_config = ConfigDict(frozen=True)
    mappings: list[PendingMapping] = Field(default_factory=list)
    issues: list[BlockingIssues] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    unit_mismatches: list[UnitMismatch] = Field(default_factory=list)
    failed_sources: list[FailedSource] = Field(default_factory=list)
    no_data: bool = False  # nothing usable at all: not resolvable by the user, routes to the safe failure

    @property
    def empty(self) -> bool:
        """True when nothing needs the user's input."""
        return not (self.mappings or self.issues or self.conflicts or self.unit_mismatches or self.failed_sources)


class ObservationView(BaseModel):
    """How to read a report's effective observations from the database (see `graph.observations`).

    Built from the state by `view_from_state`: the owner and report, the user's indicator links and gap-fill
    consent, the source keys to leave out (dropped sources), the period window, and the conflicts with their
    resolutions.
    """

    model_config = ConfigDict(frozen=True)
    user_id: str
    report_id: str
    links: list[IndicatorLink] = Field(default_factory=list)
    consent_gap_fill: bool = False
    dropped_keys: list[str] = Field(default_factory=list)
    period_from: str | None = None
    period_to: str | None = None
    conflicts: list[Conflict] = Field(default_factory=list)
    resolutions: dict[str, ConflictResolution] = Field(default_factory=dict)


class ReportState(TypedDict, total=False):
    """The checkpointed state of one report run (thread_id = report_id); see the module docstring."""

    # parameters
    report_id: str
    user_id: str
    params: ReportParams
    # sources and ingestion
    file_refs: list[DatasetRef]
    api_requests: list[ApiRequest]
    source_metadata: Annotated[list[SourceMeta], operator.add]
    mappings: Annotated[dict[str, ColumnMapping], merge_dicts]  # file_id -> mapping in use (survives resume)
    confirmed_mappings: list[str]  # file ids whose mapping the user confirmed or supplied
    validation: Annotated[dict[str, ValidationReport], merge_dicts]  # file_id -> latest validation report
    normalized_ref: str | None  # report id whose observations (DB) are current; None until normalized
    conflicts: list[Conflict]
    unit_mismatches: list[UnitMismatch]
    resolutions: dict[str, ConflictResolution]  # conflict_id -> the user's resolution
    dropped_sources: list[str]  # source ids the user chose to continue without
    acknowledged_mismatches: list[str]  # keys (validate.mismatch_key) of unit mismatches the user accepted
    corroborations: dict[str, list[str]]  # kept obs_id -> agreeing obs ids collapsed into it (D-036)
    needs_input: ResolveRequest | None
    gate_rounds: int  # resolve rounds answered so far (bounded by MAX_REVISIONS)
    # analysis
    metrics: Annotated[list[MetricResult], operator.add]
    metric_skips: Annotated[list[MetricSkip], operator.add]
    significant: list[SignificantChange]
    # retrieval and drafting
    retrieved: list[RetrievedPassage]
    draft: ReportDraft | None
    draft_history: Annotated[list[DraftAttempt], operator.add]
    verification: VerificationResult | None
    revision_count: int
    parse_retry_count: int
    model_retry_count: int  # consecutive failed model calls in drafting (MAX_MODEL_RETRIES)
    last_generation: Literal["ok", "parse_error", "model_error"]  # outcome of the latest draft/revise call
    # review and export
    review: ReviewDecision | None
    review_round: int
    export: ExportStatus | None
    errors: Annotated[list[WorkflowError], operator.add]
    status: Status


# --- sub-graph schemas ------------------------------------------------------------------------------------
# A compiled sub-graph used as a node reads only its input keys from the parent and returns only its output
# keys. Accumulating (operator.add) keys are kept OUT of the input schemas, so they start empty inside the
# sub-graph and the parent's reducer receives only the new items (otherwise every item would be duplicated).


class IngestionInput(TypedDict, total=False):
    """What the ingestion sub-graph reads from the parent."""

    report_id: str
    user_id: str
    params: ReportParams


class IngestionOutput(TypedDict, total=False):
    """What the ingestion sub-graph returns to the parent."""

    file_refs: list[DatasetRef]
    api_requests: list[ApiRequest]
    source_metadata: Annotated[list[SourceMeta], operator.add]
    mappings: Annotated[dict[str, ColumnMapping], merge_dicts]
    errors: Annotated[list[WorkflowError], operator.add]
    status: Status


class RetrievalState(ReportState, total=False):
    """Retrieval sub-graph state: the report state plus its private search queries."""

    queries: list[str]


class RetrievalInput(TypedDict, total=False):
    """What the retrieval sub-graph reads from the parent."""

    user_id: str
    params: ReportParams
    source_metadata: list[SourceMeta]
    dropped_sources: list[str]
    significant: list[SignificantChange]


class RetrievalOutput(TypedDict, total=False):
    """What the retrieval sub-graph returns to the parent."""

    retrieved: list[RetrievedPassage]
    errors: Annotated[list[WorkflowError], operator.add]


class DraftingInput(TypedDict, total=False):
    """What the drafting sub-graph reads from the parent (counters start at zero on every entry)."""

    report_id: str
    user_id: str
    params: ReportParams
    source_metadata: list[SourceMeta]
    mappings: dict[str, ColumnMapping]
    validation: dict[str, ValidationReport]
    conflicts: list[Conflict]
    resolutions: dict[str, ConflictResolution]
    dropped_sources: list[str]
    metrics: list[MetricResult]
    metric_skips: list[MetricSkip]
    significant: list[SignificantChange]
    retrieved: list[RetrievedPassage]
    draft: ReportDraft | None
    review: ReviewDecision | None


class DraftingOutput(TypedDict, total=False):
    """What the drafting sub-graph returns to the parent."""

    draft: ReportDraft | None
    draft_history: Annotated[list[DraftAttempt], operator.add]
    verification: VerificationResult | None
    revision_count: int
    parse_retry_count: int
    model_retry_count: int
    last_generation: Literal["ok", "parse_error", "model_error"]
    review: ReviewDecision | None
    errors: Annotated[list[WorkflowError], operator.add]
    status: Status


# --- Send payloads (the input of one fanned-out task) -------------------------------------------------------


class FetchTask(TypedDict):
    """Input of one `fetch_api` task."""

    report_id: str
    user_id: str
    request: ApiRequest


class ParseTask(TypedDict):
    """Input of one `parse_file` task."""

    report_id: str
    user_id: str
    ref: DatasetRef


class AnalyzeTask(TypedDict):
    """Input of one `analyze_one` task: one (indicator, region) pair and how to read its observations."""

    indicator: str
    region: str
    view: ObservationView
    period_from: str | None  # the report window (the view's window includes the look-back year)
    period_to: str | None
