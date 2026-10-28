"""Validation and normalization, conflict detection, and the resolve interrupt (#1) with its cycle.

    ingestion -> validate_normalize --route_after_validation--> analyze_dispatch     (nothing blocks)
                     ^                         |-------------> resolve_gate          (needs the user)
                     |                         '-------------> fail_safe             (rounds exhausted / no data)
                     '---------- resolve_gate <- Command(resume=ResolvePayload)

`validate_normalize` validates every file under its mapping (fresh validation, the user's stored issue
resolutions carried over by issue id, D-027), stores its observations (or clears them while errors block),
then detects conflicts and unit mismatches across all effective observations. The user is asked to resolve
(`ResolveRequest`) when a mapping has low confidence or none was found, errors block a file, conflicts are
unresolved, units mismatch (until acknowledged), or a source failed (AC 44: "continue without World Bank?").

`resolve_gate` pauses with `interrupt(...)` and validates the resume value with `ResolvePayload` plus checks
against the state; an invalid value interrupts again with the error, never raising. A valid one saves
mappings and issue resolutions, records the rest in the state and loops back to `validate_normalize`. The
number of answered rounds is bounded by MAX_REVISIONS; after that the run ends in the safe failure.
"""
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from statbrief.analytics.combine import detect_conflicts
from statbrief.config import Settings
from statbrief.domain.conflict import ConflictResolution
from statbrief.domain.dataset import ColumnMapping, DatasetRef, Resolution, ValidationReport
from statbrief.graph.deps import GraphDeps
from statbrief.graph.observations import load_effective, view_from_state
from statbrief.graph.state import BlockingIssues, FailedSource, PendingMapping, ReportState, ResolveRequest
from statbrief.ingest.normalize import UnresolvedIssues, to_observations
from statbrief.ingest.readers import RawTable, read_grid, to_table
from statbrief.ingest.sniff import UploadRejected
from statbrief.ingest.validate import validate_table

NO_MAPPING = "No usable column mapping was detected: map the columns."
LOW_CONFIDENCE = "The detected column mapping has low confidence: confirm or correct it."


class _MappingProblem(Exception):
    """The file cannot be read under its mapping (worksheet, header row or columns)."""


class _SourceProblem(Exception):
    """The stored file cannot be read at all."""


def _read_table(ref: DatasetRef, mapping: ColumnMapping, deps: GraphDeps) -> RawTable:
    """The file's table under its mapping; _SourceProblem / _MappingProblem with a user-facing message."""
    s = deps.settings
    try:
        data = deps.blobs.get(ref.blob_key)
    except FileNotFoundError:
        raise _SourceProblem("This file is no longer stored; delete it and upload it again.") from None
    except Exception as exc:  # blob SDK / network: the type only
        raise _SourceProblem(f"File storage is unavailable ({type(exc).__name__}).") from None
    try:
        grid = read_grid(data, ref.kind, mapping.worksheet, max_rows=s.max_rows_per_file,
                         max_scan_rows=s.max_scan_rows_per_file, max_columns=s.max_columns_per_file)
        table = to_table(grid, mapping.header_row)
    except UploadRejected as exc:
        raise _MappingProblem(exc.message) from None
    except ValueError as exc:  # unknown worksheet, empty header row
        raise _MappingProblem(str(exc)) from None
    missing = sorted(mapping.mapped_columns() - set(table.columns))
    if missing:
        raise _MappingProblem(f"Mapped columns not found: {', '.join(missing)}.")
    return table


def _with_stored_resolutions(fresh: ValidationReport, stored: ValidationReport | None) -> ValidationReport:
    """Fresh issues carrying the user's stored resolutions by issue id (others "pending")."""
    chosen = {i.issue_id: i.resolution for i in stored.issues} if stored else {}
    return fresh.model_copy(update={"issues": [i.model_copy(update={"resolution": chosen.get(i.issue_id, "pending")})
                                               for i in fresh.issues]})


def _validate_file(ref: DatasetRef, mapping: ColumnMapping, state: ReportState,
                   deps: GraphDeps) -> tuple[ValidationReport, list[str]]:
    """Validate and normalize one file; returns its report and the ids of errors that still block it.

    Observations are stored when nothing blocks and cleared otherwise (stale rows are never analysed).
    """
    uid, rid = state["user_id"], state["report_id"]
    table = _read_table(ref, mapping, deps)
    fresh = validate_table(table, mapping, ref.file_id, known_columns=frozenset(mapping.column_descriptions))
    report = _with_stored_resolutions(fresh, deps.validations.get(uid, ref.file_id))
    deps.validations.save(uid, report)
    try:
        result = to_observations(table, mapping, ref, report)
    except UnresolvedIssues as exc:
        deps.observations.replace_for_file(uid, rid, ref.file_id, [])
        return report, exc.issue_ids
    deps.observations.replace_for_file(uid, rid, ref.file_id, result.observations)
    return report, []


def validate_normalize(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Validate/normalize every file, detect conflicts, and say what (if anything) needs the user."""
    metas = {m.source_id: m for m in state.get("source_metadata") or []}
    dropped = set(state.get("dropped_sources") or [])
    confirmed = set(state.get("confirmed_mappings") or [])
    mappings = state.get("mappings") or {}
    pending: list[PendingMapping] = []
    blocking: list[BlockingIssues] = []
    failed = [FailedSource(source_id=m.source_id, label=m.label, message=m.message or "failed")
              for m in metas.values() if m.status == "failed" and m.source_id not in dropped]
    reports: dict[str, ValidationReport] = {}
    for ref in state.get("file_refs") or []:
        meta = metas.get(ref.file_id)
        if ref.file_id in dropped or meta is None or meta.status == "failed":
            continue
        mapping = mappings.get(ref.file_id)
        if mapping is None:
            deps.observations.replace_for_file(state["user_id"], state["report_id"], ref.file_id, [])
            pending.append(PendingMapping(file_id=ref.file_id, filename=ref.filename,
                                          confidence=meta.mapping_confidence, reason=NO_MAPPING))
            continue
        if meta.needs_review and ref.file_id not in confirmed:
            pending.append(PendingMapping(file_id=ref.file_id, filename=ref.filename, mapping=mapping,
                                          confidence=meta.mapping_confidence, reason=LOW_CONFIDENCE))
        try:
            report, blocked = _validate_file(ref, mapping, state, deps)
        except _MappingProblem as exc:
            pending.append(PendingMapping(file_id=ref.file_id, filename=ref.filename, mapping=mapping,
                                          confidence=meta.mapping_confidence, reason=str(exc)))
            continue
        except _SourceProblem as exc:
            failed.append(FailedSource(source_id=ref.file_id, label=ref.filename, message=str(exc)))
            continue
        reports[ref.file_id] = report
        if blocked:
            blocking.append(BlockingIssues(file_id=ref.file_id, filename=ref.filename,
                                           issues=[i for i in report.issues if i.issue_id in set(blocked)]))
    observations = load_effective(deps, view_from_state(state), resolve=False)
    scan = detect_conflicts(observations, rel_tol=deps.settings.conflict_rel_tol)
    resolutions = state.get("resolutions") or {}
    request = ResolveRequest(
        mappings=pending, issues=blocking, conflicts=[c for c in scan.conflicts if c.conflict_id not in resolutions],
        unit_mismatches=[] if state.get("unit_mismatches_acknowledged") else scan.unit_mismatches,
        failed_sources=failed, no_data=not observations)
    ready = request.empty and not request.no_data
    return {"validation": reports, "conflicts": scan.conflicts, "unit_mismatches": scan.unit_mismatches,
            "needs_input": request, "normalized_ref": state["report_id"] if ready else None,
            "status": "analyzing" if ready else "needs_input"}


def route_after_validation(state: ReportState,
                           settings: Settings) -> Literal["resolve_gate", "analyze_dispatch", "fail_safe"]:
    """Ask the user while rounds remain; analyse when nothing blocks; otherwise fail safely."""
    request = state.get("needs_input")
    if request is None:
        return "fail_safe"
    if not request.empty:
        return "resolve_gate" if (state.get("gate_rounds") or 0) < settings.max_revisions else "fail_safe"
    return "fail_safe" if request.no_data else "analyze_dispatch"


class ResolvePayload(BaseModel):
    """The user's answer to the resolve interrupt (every field optional; unknown fields rejected)."""

    model_config = ConfigDict(extra="forbid")
    mappings: dict[str, ColumnMapping] = Field(default_factory=dict)  # file_id -> corrected mapping (confirms it)
    confirm_mappings: list[str] = Field(default_factory=list)  # file ids whose shown mapping is accepted as is
    issue_resolutions: dict[str, dict[str, Resolution]] = Field(default_factory=dict)  # file_id -> issue -> choice
    conflict_resolutions: list[ConflictResolution] = Field(default_factory=list)
    drop_sources: list[str] = Field(default_factory=list)  # continue without these sources
    accept_unit_mismatches: bool = False
    consent_gap_fill: bool | None = None  # None keeps the current choice


def _check(payload: ResolvePayload, state: ReportState) -> dict[str, ValidationReport]:
    """ValueError (user-facing) unless the payload fits the state; returns the updated validation reports."""
    files = {r.file_id for r in state.get("file_refs") or []}
    for fid in [*payload.mappings, *payload.confirm_mappings]:
        if fid not in files:
            raise ValueError(f"unknown file {fid!r}")
    for fid in payload.confirm_mappings:
        if fid not in (state.get("mappings") or {}) and fid not in payload.mappings:
            raise ValueError(f"file {fid!r} has no mapping to confirm: supply one")
    updated: dict[str, ValidationReport] = {}
    for fid, choices in payload.issue_resolutions.items():
        if fid in payload.mappings:
            raise ValueError(f"file {fid!r}: resolve its issues after the new mapping has been validated")
        report = (state.get("validation") or {}).get(fid)
        if report is None:
            raise ValueError(f"file {fid!r} has no validation report")
        for issue_id, resolution in choices.items():
            report = report.with_resolution(issue_id, resolution)  # KeyError / ValueError
        updated[fid] = report
    conflicts = {c.conflict_id: c for c in state.get("conflicts") or []}
    seen: set[str] = set()
    for r in payload.conflict_resolutions:
        if r.conflict_id not in conflicts or r.conflict_id in seen:
            raise ValueError(f"unknown or repeated conflict {r.conflict_id!r}")
        if r.preferred_obs_id is not None and r.preferred_obs_id not in conflicts[r.conflict_id].candidates:
            raise ValueError(f"{r.preferred_obs_id!r} is not a candidate of conflict {r.conflict_id!r}")
        seen.add(r.conflict_id)
    sources = {m.source_id for m in state.get("source_metadata") or []}
    unknown = sorted(set(payload.drop_sources) - sources)
    if unknown:
        raise ValueError(f"unknown sources: {', '.join(unknown)}")
    return updated


def _error_text(exc: Exception) -> str:
    """A resume error for the user: validation locations and reasons, or the check's message."""
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors())
    if isinstance(exc, KeyError):
        return f"unknown issue {exc.args[0]!r}"
    return str(exc)


def _gate_value(state: ReportState, deps: GraphDeps, error: str | None) -> dict[str, Any]:
    """The JSON-ready interrupt value shown to the user."""
    request = state.get("needs_input") or ResolveRequest()
    return {"kind": "resolve", "round": (state.get("gate_rounds") or 0) + 1, "max_rounds": deps.settings.max_revisions,
            "error": error, **request.model_dump(mode="json", exclude={"no_data"})}


def resolve_gate(state: ReportState, deps: GraphDeps, config: RunnableConfig) -> dict[str, Any]:
    """Interrupt #1: ask for mappings, issue fixes, conflict resolutions and source drops; apply a valid answer."""
    error: str | None = None
    while True:
        answer = interrupt(_gate_value(state, deps, error))
        try:
            payload = ResolvePayload.model_validate(answer)
            reports = _check(payload, state)
        except (ValidationError, ValueError, KeyError) as exc:
            error = _error_text(exc)
            continue
        break
    uid = state["user_id"]
    for fid, mapping in payload.mappings.items():
        deps.mappings.save(uid, fid, mapping)  # also drops the file's old validation report and observations
    for report in reports.values():
        deps.validations.save(uid, report)
    params = state["params"]
    if payload.consent_gap_fill is not None:
        params = params.model_copy(update={"consent_gap_fill": payload.consent_gap_fill})
    return {
        "mappings": payload.mappings,
        "confirmed_mappings": sorted(set(state.get("confirmed_mappings") or []) | set(payload.confirm_mappings)
                                     | set(payload.mappings)),
        "validation": reports,
        "resolutions": {**(state.get("resolutions") or {}),
                        **{r.conflict_id: r for r in payload.conflict_resolutions}},
        "dropped_sources": sorted(set(state.get("dropped_sources") or []) | set(payload.drop_sources)),
        "unit_mismatches_acknowledged": bool(state.get("unit_mismatches_acknowledged"))
        or payload.accept_unit_mismatches,
        "params": params, "gate_rounds": (state.get("gate_rounds") or 0) + 1, "status": "collecting",
    }
