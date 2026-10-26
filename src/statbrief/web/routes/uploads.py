"""JSON API for chunked uploads and file ingestion (preview, mapping, validation, normalization).

Every route requires login and is scoped to the current user (other users' ids answer 404, AC 6).
State-changing routes also require the `X-StatBrief-Client: 1` header: a custom header forces a CORS
preflight, so a cross-site form cannot trigger them (D-018). Uploads arrive in chunks of at most
UPLOAD_CHUNK_MB so each request stays under Vercel's 4.5 MB body limit (D-021); files are sniffed by
content and never executed (AC 10-11).
"""
import math
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from statbrief.auth.users import User
from statbrief.config import Settings
from statbrief.domain.dataset import ColumnMapping, DatasetRef
from statbrief.ingest.detect import detect_schema
from statbrief.ingest.normalize import UnresolvedIssues, to_observations
from statbrief.ingest.readers import FORMULA_NO_VALUE, Grid, RawTable, list_worksheets, read_grid, to_table
from statbrief.ingest.sniff import UploadRejected, validate_file_bytes
from statbrief.ingest.template import build_template_csv, build_template_xlsx
from statbrief.ingest.validate import validate_table
from statbrief.storage.blobs import blob_key
from statbrief.storage.repos import FileRepo, MappingRepo, ObservationRepo, ReportRepo, UploadRepo, ValidationRepo
from statbrief.web.deps import current_user, get_runtime
from statbrief.web.runtime import Runtime

router = APIRouter(prefix="/api")

CLIENT_HEADER = "X-StatBrief-Client"
_CLIENT_HEADER_VALUE = "1"
_MIB = 1024 * 1024
# Early, extension-only check; the real format check is content sniffing at completion (AC 10).
_ALLOWED_EXTS = frozenset({".csv", ".xlsx", ".xls", ".json"})
_CONTENT_TYPES = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  "xls": "application/vnd.ms-excel", "json": "application/json"}
_TEMPLATE_BASENAME = "statbrief_template"
# File status values recorded on the files table as the user moves through the ingestion steps.
_STATUS_MAPPED, _STATUS_VALIDATED, _STATUS_NORMALIZED = "mapped", "validated", "normalized"

UserDep = Annotated[User, Depends(current_user)]
RtDep = Annotated[Runtime, Depends(get_runtime)]


def require_client_header(request: Request) -> None:
    """CSRF guard for state-changing API calls: 403 unless `X-StatBrief-Client: 1` is present (D-018)."""
    if request.headers.get(CLIENT_HEADER) != _CLIENT_HEADER_VALUE:
        raise HTTPException(status_code=403, detail="missing client header")


Guard = Depends(require_client_header)


class NewReport(BaseModel):
    """Request body: create a report."""

    title: str = Field(min_length=1, max_length=200)


class NewUpload(BaseModel):
    """Request body: start a chunked upload."""

    filename: str = Field(min_length=1, max_length=255)
    total_bytes: int = Field(gt=0)


class ResolutionBody(BaseModel):
    """Request body: resolve a validation issue (`accepted` is allowed for warnings only)."""

    resolution: Literal["excluded", "accepted"]


def rejected_response(exc: UploadRejected) -> JSONResponse:
    """422 body for a refused file: `{code, message}` (message is user-safe)."""
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)


def chunk_bytes(settings: Settings) -> int:
    """Maximum size of one upload chunk in bytes (at least 1)."""
    return max(1, int(settings.upload_chunk_mb * _MIB))


def _json_cell(v: Any) -> Any:
    """Make a raw cell JSON-safe for previews (ISO dates, formula marker, no NaN/inf)."""
    if v is FORMULA_NO_VALUE:
        return {"formula_without_value": True}
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if v is None or isinstance(v, str | int | float | bool):
        return v
    return str(v)


def _file_or_404(rt: Runtime, user: User, file_id: str) -> DatasetRef:
    """The user's file, or 404 (also for other users' files)."""
    ref = FileRepo(rt.pool).get(user.id, file_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="file not found")
    return ref


def _grid(rt: Runtime, ref: DatasetRef, worksheet: str | None) -> Grid:
    """Read the stored file into a grid under every reader limit (UploadRejected on breach)."""
    s = rt.settings
    return read_grid(rt.blobs.get(ref.blob_key), ref.kind, worksheet=worksheet,
                     max_rows=s.max_rows_per_file, max_scan_rows=s.max_scan_rows_per_file,
                     max_columns=s.max_columns_per_file)


def _table(grid: Grid, header_row: int) -> RawTable:
    """Table under `header_row`, or 422 when that row is empty or outside the table."""
    try:
        return to_table(grid, header_row)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/reports", status_code=201, dependencies=[Guard])
def create_report(body: NewReport, user: UserDep, rt: RtDep) -> dict:
    """Create a draft report owned by the current user."""
    row = ReportRepo(rt.pool).create(user.id, body.title)
    return {"id": str(row["id"]), "title": row["title"]}


@router.post("/reports/{report_id}/uploads", status_code=201, dependencies=[Guard])
def start_upload(report_id: str, body: NewUpload, user: UserDep, rt: RtDep) -> dict:
    """Open a chunked upload after cheap checks (ownership, extension, size, file count).

    The chunk count is derived here from the declared size; the client never chooses it.
    """
    s = rt.settings
    if ReportRepo(rt.pool).get(user.id, report_id) is None:
        raise HTTPException(status_code=404, detail="report not found")
    if PurePosixPath(body.filename.replace("\\", "/")).suffix.lower() not in _ALLOWED_EXTS:
        raise HTTPException(status_code=422, detail="Only CSV, Excel (.xlsx, .xls) and JSON files are accepted.")
    if body.total_bytes > s.max_upload_mb * _MIB:
        raise HTTPException(status_code=413, detail=f"Files are limited to {s.max_upload_mb} MB.")
    if FileRepo(rt.pool).count_for_report(user.id, report_id) >= s.max_files_per_report:
        raise HTTPException(status_code=409, detail=f"A report can have at most {s.max_files_per_report} files.")
    size = chunk_bytes(s)
    count = math.ceil(body.total_bytes / size)
    try:
        upload_id = UploadRepo(rt.pool).start(user.id, report_id, body.filename, body.total_bytes, count)
    except PermissionError:  # report deleted since the ownership check
        raise HTTPException(status_code=404, detail="report not found") from None
    return {"upload_id": upload_id, "chunk_bytes": size, "chunk_count": count}


async def _read_capped_body(request: Request, limit: int) -> bytes:
    """Request body, or 413 as soon as it exceeds `limit` bytes (never buffers more than limit + one part)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="chunk too large")
    data = bytearray()
    async for part in request.stream():
        data += part
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="chunk too large")
    return bytes(data)


@router.put("/uploads/{upload_id}/chunks/{idx}", status_code=204, dependencies=[Guard])
async def put_chunk(upload_id: str, idx: int, request: Request, user: UserDep, rt: RtDep) -> Response:
    """Store one raw chunk (≤ chunk size; 413 when larger, 404 for unknown/foreign uploads)."""
    data = await _read_capped_body(request, chunk_bytes(rt.settings))
    try:
        await run_in_threadpool(UploadRepo(rt.pool).put_chunk, user.id, upload_id, idx, data)
    except PermissionError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    except ValueError as exc:  # index out of range or chunk larger than the whole upload
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return Response(status_code=204)


@router.post("/uploads/{upload_id}/complete", status_code=201, dependencies=[Guard])
def complete_upload(upload_id: str, user: UserDep, rt: RtDep) -> JSONResponse:
    """Reassemble, sniff by content, store privately, register the file and drop the staged chunks.

    400 when chunks are missing or do not add up (the client may resend them); 409 when the report
    already holds MAX_FILES_PER_REPORT files; 422 `{code, message}` when the content is refused. Refused
    and over-limit uploads are discarded.
    """
    s = rt.settings
    uploads = UploadRepo(rt.pool)
    up = uploads.get(user.id, upload_id)
    if up is None:
        raise HTTPException(status_code=404, detail="upload not found")
    report_id = str(up["report_id"])
    try:
        data = uploads.assemble(user.id, upload_id)
    except PermissionError:  # deleted concurrently
        raise HTTPException(status_code=404, detail="upload not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    files = FileRepo(rt.pool)
    if files.count_for_report(user.id, report_id) >= s.max_files_per_report:  # uploads opened in parallel
        uploads.delete(user.id, upload_id)
        raise HTTPException(status_code=409, detail=f"A report can have at most {s.max_files_per_report} files.")
    try:
        check = validate_file_bytes(up["filename"], data, s.max_upload_mb * _MIB, s.max_uncompressed_ratio,
                                    s.max_uncompressed_mb * _MIB)
        sheets = list_worksheets(data, check.kind)  # also refuses .xls macro sheets and encryption
    except UploadRejected as exc:
        uploads.delete(user.id, upload_id)
        return rejected_response(exc)
    file_id = str(uuid.uuid4())
    key = rt.blobs.put(blob_key(user.id, report_id, file_id, up["filename"]), data, _CONTENT_TYPES[check.kind])
    try:
        files.add(DatasetRef(file_id=file_id, report_id=report_id, user_id=user.id, filename=up["filename"],
                             kind=check.kind, size_bytes=check.size_bytes, sha256=check.sha256, blob_key=key))
    except BaseException as exc:
        rt.blobs.delete(key)  # never leave an orphaned blob behind
        if isinstance(exc, PermissionError):  # report deleted meanwhile
            raise HTTPException(status_code=404, detail="report not found") from None
        raise
    uploads.delete(user.id, upload_id)
    ReportRepo(rt.pool).touch(user.id, report_id)
    return JSONResponse({"file_id": file_id, "kind": check.kind, "size_bytes": check.size_bytes,
                         "worksheets": [{"name": w.name, "hidden": w.hidden} for w in sheets]}, status_code=201)


@router.get("/files/{file_id}/preview", response_model=None)
def preview(file_id: str, user: UserDep, rt: RtDep, worksheet: str | None = None,
            header_row: Annotated[int | None, Query(ge=1)] = None) -> dict | JSONResponse:
    """First PREVIEW_ROWS data rows plus detection results (JSON-safe cells).

    The header defaults to the detected one; 422 when the file breaks a reader limit, the worksheet
    does not exist or `header_row` is empty.
    """
    ref = _file_or_404(rt, user, file_id)
    s = rt.settings
    try:
        grid = _grid(rt, ref, worksheet)
    except UploadRejected as exc:
        return rejected_response(exc)
    try:
        detected = detect_schema(grid, sample_rows=s.detect_sample_rows, scan_rows=s.detect_header_scan_rows)
    except ValueError as exc:  # no non-empty rows
        raise HTTPException(status_code=422, detail=str(exc)) from None
    table = _table(grid, header_row or detected.header_row)
    return {"worksheet": grid.worksheet, "columns": table.columns,
            "rows": [[_json_cell(v) for v in r] for r in table.rows[:s.preview_rows]],
            "row_numbers": table.row_numbers[:s.preview_rows],
            "detected": detected.model_dump(mode="json"),
            "needs_review": detected.needs_review(s.mapping_min_confidence)}


@router.put("/files/{file_id}/mapping", dependencies=[Guard], response_model=None)
def save_mapping(file_id: str, mapping: ColumnMapping, user: UserDep, rt: RtDep) -> dict | JSONResponse:
    """Store a mapping after checking that its worksheet, header row and every mapped column exist."""
    ref = _file_or_404(rt, user, file_id)
    try:
        grid = _grid(rt, ref, mapping.worksheet)
    except UploadRejected as exc:
        return rejected_response(exc)
    table = _table(grid, mapping.header_row)
    missing = sorted(mapping.mapped_columns() - set(table.columns))
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown columns: {', '.join(missing)}")
    MappingRepo(rt.pool).save(user.id, file_id, mapping)
    FileRepo(rt.pool).set_status(user.id, file_id, _STATUS_MAPPED)
    return {"ok": True}


def _table_and_mapping(rt: Runtime, user: User, file_id: str) -> tuple[DatasetRef, ColumnMapping, RawTable]:
    """The user's file, its saved mapping (409 when absent) and the parsed table (UploadRejected on limits)."""
    ref = _file_or_404(rt, user, file_id)
    mapping = MappingRepo(rt.pool).get(user.id, file_id)
    if mapping is None:
        raise HTTPException(status_code=409, detail="map the columns first")
    return ref, mapping, _table(_grid(rt, ref, mapping.worksheet), mapping.header_row)


@router.post("/files/{file_id}/validate", dependencies=[Guard], response_model=None)
def validate(file_id: str, user: UserDep, rt: RtDep) -> dict | JSONResponse:
    """Validate the file under its mapping and store the report (described columns count as known)."""
    try:
        ref, mapping, table = _table_and_mapping(rt, user, file_id)
    except UploadRejected as exc:
        return rejected_response(exc)
    report = validate_table(table, mapping, ref.file_id, known_columns=frozenset(mapping.column_descriptions))
    ValidationRepo(rt.pool).save(user.id, report)
    FileRepo(rt.pool).set_status(user.id, file_id, _STATUS_VALIDATED)
    return report.model_dump(mode="json")


@router.post("/files/{file_id}/issues/{issue_id}", dependencies=[Guard])
def resolve_issue(file_id: str, issue_id: str, body: ResolutionBody, user: UserDep, rt: RtDep) -> dict:
    """Mark an issue excluded (errors or warnings) or accepted (warnings only); returns the updated report.

    Each issue in the response lists the original row numbers it affects (`rows`), so the UI can show
    what an exclusion removes.
    """
    _file_or_404(rt, user, file_id)
    report = ValidationRepo(rt.pool).get(user.id, file_id)
    if report is None:
        raise HTTPException(status_code=409, detail="validate first")
    try:
        updated = report.with_resolution(issue_id, body.resolution)
    except KeyError:
        raise HTTPException(status_code=404, detail="issue not found") from None
    except ValidationError as exc:  # before ValueError: pydantic's ValidationError subclasses it
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ValueError as exc:  # an error cannot be accepted
        raise HTTPException(status_code=422, detail=str(exc)) from None
    ValidationRepo(rt.pool).save(user.id, updated)
    return updated.model_dump(mode="json")


@router.post("/files/{file_id}/normalize", dependencies=[Guard], response_model=None)
def normalize(file_id: str, user: UserDep, rt: RtDep) -> dict | JSONResponse:
    """Store the file's observations; 409 lists unresolved errors or asks to re-validate a stale report."""
    try:
        ref, mapping, table = _table_and_mapping(rt, user, file_id)
    except UploadRejected as exc:
        return rejected_response(exc)
    report = ValidationRepo(rt.pool).get(user.id, file_id)
    if report is None:
        raise HTTPException(status_code=409, detail="validate first")
    try:
        result = to_observations(table, mapping, ref, report)
    except UnresolvedIssues as exc:
        return JSONResponse({"unresolved": exc.issue_ids}, status_code=409)
    except ValueError:  # report produced for another table (mapping changed since validation)
        return JSONResponse({"detail": "re-validate first"}, status_code=409)
    n = ObservationRepo(rt.pool).replace_for_file(user.id, ref.report_id, ref.file_id, result.observations)
    FileRepo(rt.pool).set_status(user.id, file_id, _STATUS_NORMALIZED)
    ReportRepo(rt.pool).touch(user.id, ref.report_id)
    return {"observations": n,
            "excluded": [{"row_number": e.row_number, "reasons": e.reasons} for e in result.excluded]}


def _attachment(data: bytes, kind: str) -> Response:
    """Template download response for `kind` ("csv" or "xlsx")."""
    return Response(data, media_type=_CONTENT_TYPES[kind],
                    headers={"Content-Disposition": f'attachment; filename="{_TEMPLATE_BASENAME}.{kind}"'})


@router.get("/template.csv")
def template_csv(user: UserDep) -> Response:
    """Download the CSV template (login required; read-only, so no client header)."""
    return _attachment(build_template_csv(), "csv")


@router.get("/template.xlsx")
def template_xlsx(user: UserDep) -> Response:
    """Download the XLSX template (login required; read-only, so no client header)."""
    return _attachment(build_template_xlsx(), "xlsx")
