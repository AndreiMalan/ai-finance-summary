"""Ingestion sub-graph (AC 37): select sources, then fetch every API series and parse every uploaded file.

    START -> select_sources -+-> Send("fetch_api", FetchTask) x N  -+-> END     (ENABLE_PARALLEL on)
                             +-> Send("parse_file", ParseTask) x M -+
                             +-> ingest_all (same work, one node) ----> END     (ENABLE_PARALLEL off)

Each task returns only `SourceMeta` (plus a detected mapping for files and a `WorkflowError` on failure);
rows go to the database: API observations are stored by `fetch_api` (file-less rows), uploaded files stay
in the blob store until validation normalizes them. Nothing raises out of a task: an API that still fails
after M3's retries, a missing blob or an unreadable file becomes a `failed` source that the resolve gate
offers to drop (AC 44). API responses are read cache-first from `api_cache` (M3 `cached_fetch`), so a
resumed or replayed run never re-fetches (AC 40).
"""
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from statbrief.domain.dataset import DatasetRef, DetectedSchema
from statbrief.graph.deps import GraphDeps, emit_progress, node
from statbrief.graph.observations import lookback_start
from statbrief.graph.state import (ApiRequest, FetchTask, IngestionInput, IngestionOutput, ParseTask, ReportState,
                                   SourceMeta, WorkflowError)
from statbrief.ingest.detect import detect_schema
from statbrief.ingest.readers import read_grid
from statbrief.ingest.sniff import UploadRejected
from statbrief.sources.http import CacheScope, SourceError

_STORAGE_GONE = "This file is no longer stored; delete it and upload it again."
_STORAGE_DOWN = "File storage is unavailable."


def select_sources(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """The report's uploaded files (upload/combined mode) and one API request per catalog indicator (api/combined).

    In "api" mode an indicator that is not in the catalog is recorded as an error (nothing to fetch); in
    "combined" mode non-catalog codes are taken to be uploaded indicator codes.
    """
    params = state["params"]
    refs = deps.files.list_for_report(state["user_id"], state["report_id"]) if params.mode != "api" else []
    requests: list[ApiRequest] = []
    errors: list[WorkflowError] = []
    if params.mode != "upload":
        for code in params.indicators:
            if code in deps.catalog:
                # one year of look-back before the window, so year-ago bases exist (D-035)
                requests.append(ApiRequest(indicator=code, regions=params.regions,
                                           period_from=lookback_start(params.period_from),
                                           period_to=params.period_to))
            elif params.mode == "api":
                errors.append(WorkflowError(node="select_sources", kind="unknown_indicator",
                                            message=f"indicator {code!r} is not in the catalog"))
    return {"file_refs": refs, "api_requests": requests, "errors": errors, "status": "collecting"}


def fan_out_sources(state: ReportState, deps: GraphDeps) -> list[Send] | str:
    """One Send per API request and per file (parallel), or the sequential `ingest_all` node."""
    if not deps.settings.enable_parallel:
        return "ingest_all"
    base = {"report_id": state["report_id"], "user_id": state["user_id"]}
    sends = [Send("fetch_api", FetchTask(**base, request=r)) for r in state.get("api_requests") or []]
    sends += [Send("parse_file", ParseTask(**base, ref=ref)) for ref in state.get("file_refs") or []]
    return sends or END


class _RecordingCache:
    """Wraps the API cache repo to tell whether a fetch was answered from the cache (no `put`)."""

    def __init__(self, repo: Any) -> None:
        """Wrap `repo`."""
        self.repo, self.stored = repo, False

    def get(self, *args: Any) -> Any:
        """Delegate a cache read."""
        return self.repo.get(*args)

    def put(self, *args: Any) -> None:
        """Delegate a cache write and remember that the network was used."""
        self.stored = True
        self.repo.put(*args)


def _year(period: str | None) -> int | None:
    """Calendar year of a canonical period."""
    return int(period[:4]) if period else None


def _time_kwargs(source: str, req: ApiRequest) -> dict[str, Any]:
    """Time bounds in the form each client takes: Eurostat periods (either may be open); World Bank years only
    when both bounds are set (the client takes both or neither; the report's period window trims the rest)."""
    if source == "eurostat":
        return {"since": req.period_from, "until": req.period_to}
    if req.period_from is None or req.period_to is None:
        return {}
    return {"since": _year(req.period_from), "until": _year(req.period_to)}


def _failed_api(req: ApiRequest, label: str, message: str) -> dict[str, Any]:
    """State update for an API source that could not be fetched."""
    meta = SourceMeta(source_id=req.request_id, kind="api", label=label, status="failed", indicator=req.indicator,
                      message=message)
    return {"source_metadata": [meta], "errors": [WorkflowError(node="fetch_api", kind="api_failed",
                                                                message=f"{label}: {message}")]}


def fetch_api(task: FetchTask, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Fetch one API series cache-first, store its observations, and return its SourceMeta (never raises)."""
    req = task["request"]
    spec = deps.catalog.get(req.indicator)
    if spec is None or spec.source not in deps.fetchers:
        return _failed_api(req, req.indicator, "unknown indicator or source")
    label = f"{spec.label} ({spec.source})"
    emit_progress("fetch_api", f"Fetching {label}")
    cache = _RecordingCache(deps.api_cache)
    try:
        with deps.http_client() as client:
            result = deps.fetchers[spec.source](
                client, spec, req.regions, settings=deps.settings, cache=CacheScope(cache, task["user_id"],
                                                                                    task["report_id"]),
                sleep=deps.sleep, now=deps.now, **_time_kwargs(spec.source, req))
        deps.observations.upsert_api_observations(task["user_id"], task["report_id"], result.observations)
    except SourceError as exc:  # message is body-free by construction (M3)
        return _failed_api(req, label, str(exc))
    except ValueError as exc:  # invalid request arguments (our own messages)
        return _failed_api(req, label, f"invalid request: {exc}")
    except Exception as exc:  # network stack / database: the type only (messages may echo URLs or data)
        return _failed_api(req, label, f"unexpected error ({type(exc).__name__})")
    first = result.observations[0].provenance if result.observations else None
    meta = SourceMeta(
        source_id=req.request_id, kind="api", label=label, status="ok", indicator=req.indicator,
        source_key=f"api:{first.api_name}:{first.series_id}" if first is not None else None,  # type: ignore[union-attr]
        observation_count=len(result.observations), skipped_count=len(result.skipped), from_cache=not cache.stored,
        query_url=first.query_url if first is not None else None,  # type: ignore[union-attr]
        series_label=result.meta.label, unit_label=result.meta.unit_label,
        last_update=str(result.meta.last_update) if result.meta.last_update is not None else None)
    if not result.observations:  # visible in the timeline: an indicator must never vanish silently (review M7)
        return {"source_metadata": [meta], "errors": [WorkflowError(
            node="fetch_api", kind="api_no_data", message=f"{label}: no observations for the requested regions "
                                                          "and period")]}
    return {"source_metadata": [meta]}


def _confidence(detected: DetectedSchema) -> float:
    """Lowest confidence among the header row and the mapped (non-ignored) columns."""
    scores = [c.confidence for c in detected.columns if c.role != "ignore"]
    return min([detected.header_confidence, *scores])


def _failed_file(ref: DatasetRef, message: str) -> dict[str, Any]:
    """State update for an uploaded file that cannot be read."""
    meta = SourceMeta(source_id=ref.file_id, kind="upload", label=ref.filename, status="failed",
                      source_key=f"upload:{ref.file_id}", message=message)
    return {"source_metadata": [meta], "errors": [WorkflowError(node="parse_file", kind="file_failed",
                                                                message=f"{ref.filename}: {message}")]}


def parse_file(task: ParseTask, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Read one uploaded file: its saved mapping, else a detected one with confidence (never raises, no rows)."""
    ref, s = task["ref"], deps.settings
    emit_progress("parse_file", f"Reading {ref.filename}")
    stored = deps.mappings.get(task["user_id"], ref.file_id)
    if stored is not None:  # the user mapped it in the wizard: confirmed by definition
        meta = SourceMeta(source_id=ref.file_id, kind="upload", label=ref.filename, status="ok",
                          source_key=f"upload:{ref.file_id}", mapping_source="stored", mapping_confidence=1.0)
        return {"source_metadata": [meta], "mappings": {ref.file_id: stored}}
    try:
        data = deps.blobs.get(ref.blob_key)
    except FileNotFoundError:
        return _failed_file(ref, _STORAGE_GONE)
    except Exception as exc:  # blob SDK / network: the type only
        return _failed_file(ref, f"{_STORAGE_DOWN} ({type(exc).__name__})")
    try:
        grid = read_grid(data, ref.kind, None, max_rows=s.max_rows_per_file, max_scan_rows=s.max_scan_rows_per_file,
                         max_columns=s.max_columns_per_file)
        detected = detect_schema(grid, sample_rows=s.detect_sample_rows, scan_rows=s.detect_header_scan_rows)
    except UploadRejected as exc:
        return _failed_file(ref, exc.message)
    except ValueError as exc:  # no non-empty rows
        return _failed_file(ref, str(exc))
    except Exception as exc:  # a reader bug must not crash the run
        return _failed_file(ref, f"the file could not be read ({type(exc).__name__})")
    mapping = detected.suggested_mapping()
    meta = SourceMeta(source_id=ref.file_id, kind="upload", label=ref.filename, status="ok",
                      source_key=f"upload:{ref.file_id}", mapping_source="detected" if mapping else "none",
                      mapping_confidence=_confidence(detected), detected=detected,
                      needs_review=detected.needs_review(s.mapping_min_confidence))
    return {"source_metadata": [meta], "mappings": {ref.file_id: mapping} if mapping else {}}


def ingest_all(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Sequential path (ENABLE_PARALLEL off): the same tasks one after another, merged like the reducers do."""
    base = {"report_id": state["report_id"], "user_id": state["user_id"]}
    outputs = [fetch_api(FetchTask(**base, request=r), deps, config) for r in state.get("api_requests") or []]
    outputs += [parse_file(ParseTask(**base, ref=ref), deps, config) for ref in state.get("file_refs") or []]
    merged: dict[str, Any] = {"source_metadata": [], "mappings": {}, "errors": []}
    for out in outputs:
        merged["source_metadata"] += out.get("source_metadata", [])
        merged["mappings"].update(out.get("mappings", {}))
        merged["errors"] += out.get("errors", [])
    return merged


def build_ingestion_graph(deps: GraphDeps) -> CompiledStateGraph:
    """The compiled ingestion sub-graph (reads report_id/user_id/params, returns sources and mappings)."""
    g = StateGraph(ReportState, input_schema=IngestionInput, output_schema=IngestionOutput)
    g.add_node("select_sources", node(select_sources, deps))
    g.add_node("fetch_api", node(fetch_api, deps))
    g.add_node("parse_file", node(parse_file, deps))
    g.add_node("ingest_all", node(ingest_all, deps))
    g.add_edge(START, "select_sources")
    g.add_conditional_edges("select_sources", lambda s: fan_out_sources(s, deps),
                            ["fetch_api", "parse_file", "ingest_all", END])
    for name in ("fetch_api", "parse_file", "ingest_all"):
        g.add_edge(name, END)
    return g.compile(name="ingestion")
