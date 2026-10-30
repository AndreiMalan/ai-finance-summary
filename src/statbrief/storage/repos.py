"""User-scoped repositories for reports, files, uploads, mappings, validation reports, observations and the
per-report API response cache.

Every read takes `user_id` and returns None/empty for other users' rows; writes to rows the user does not
own raise PermissionError (spec AC 6). Malformed ids are treated as "not found" (never a SQL error).
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from statbrief.domain.dataset import ColumnMapping, DatasetRef, ValidationReport
from statbrief.domain.observation import ApiProvenance, Observation

# File status after a mapping is (re)saved: validation and normalization must be redone.
MAPPED_STATUS = "mapped"


class FileLimitReached(Exception):
    """The report already holds the maximum number of files."""


class UploadGone(Exception):
    """The staged upload no longer exists (already completed, or deleted)."""


def _uuid(value: str) -> uuid.UUID | None:
    """Parse a UUID; None when malformed (treated as not found)."""
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _owns_report(c: Connection, user_id: str, report_id: str) -> bool:
    """True when the user owns the report."""
    return bool(c.execute("SELECT 1 FROM reports WHERE id = %s AND user_id = %s",
                          (_uuid(report_id), user_id)).fetchone())


def _owns_file(c: Connection, user_id: str, file_id: str) -> bool:
    """True when the user owns the file."""
    return bool(c.execute("SELECT 1 FROM files WHERE id = %s AND user_id = %s",
                          (_uuid(file_id), user_id)).fetchone())


class ReportRepo:
    """Reports owned by users."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def create(self, user_id: str, title: str) -> dict:
        """Create a draft report."""
        with self.pool.connection() as c:
            return c.execute("INSERT INTO reports (user_id, title) VALUES (%s, %s) RETURNING *",
                             (user_id, title)).fetchone()

    def get(self, user_id: str, report_id: str) -> dict | None:
        """A report if it belongs to the user."""
        rid = _uuid(report_id)
        if rid is None:
            return None
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM reports WHERE id = %s AND user_id = %s", (rid, user_id)).fetchone()

    def list_for_user(self, user_id: str) -> list[dict]:
        """The user's reports, most recently active first."""
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM reports WHERE user_id = %s ORDER BY last_activity_at DESC, id",
                             (user_id,)).fetchall()

    def set_params(self, user_id: str, report_id: str, params: dict[str, Any]) -> None:
        """Store the run parameters (JSON) of the user's report; PermissionError for other users' reports."""
        with self.pool.connection() as c:
            row = c.execute("UPDATE reports SET params = %s, last_activity_at = now() WHERE id = %s AND user_id = %s "
                            "RETURNING id", (Jsonb(params), _uuid(report_id), user_id)).fetchone()
        if row is None:
            raise PermissionError("report not owned by user")

    def get_params(self, user_id: str, report_id: str) -> dict[str, Any] | None:
        """The stored run parameters of the user's report, or None."""
        row = self.get(user_id, report_id)
        return row["params"] if row else None

    def set_pending_resume(self, user_id: str, report_id: str, payload: Any) -> None:
        """Queue a validated resume answer for the next run segment (replaces an earlier one)."""
        with self.pool.connection() as c:
            row = c.execute("UPDATE reports SET pending_resume = %s WHERE id = %s AND user_id = %s RETURNING id",
                            (Jsonb(payload), _uuid(report_id), user_id)).fetchone()
        if row is None:
            raise PermissionError("report not owned by user")

    def take_pending_resume(self, user_id: str, report_id: str) -> Any | None:
        """Return and clear the queued resume answer atomically (None when there is none)."""
        rid = _uuid(report_id)
        if rid is None:
            return None
        with self.pool.connection() as c:
            row = c.execute(
                "UPDATE reports r SET pending_resume = NULL FROM (SELECT id, pending_resume FROM reports "
                "WHERE id = %s AND user_id = %s FOR UPDATE) old WHERE r.id = old.id RETURNING old.pending_resume",
                (rid, user_id)).fetchone()
        return row["pending_resume"] if row else None

    def acquire_lease(self, user_id: str, report_id: str, token: str, seconds: float) -> bool:
        """Take the report's run lease for `seconds` when it is free or expired; True when this caller won.

        One atomic UPDATE (safe through a transaction-mode pooler): two concurrent callers cannot both win.
        """
        with self.pool.connection() as c:
            row = c.execute(
                "UPDATE reports SET run_lease_token = %s, run_lease_until = now() + make_interval(secs => %s) "
                "WHERE id = %s AND user_id = %s AND (run_lease_until IS NULL OR run_lease_until < now()) "
                "RETURNING id", (token, seconds, _uuid(report_id), user_id)).fetchone()
        return row is not None

    def renew_lease(self, report_id: str, token: str, seconds: float) -> bool:
        """Extend the lease held with `token`; False when it was lost (expired and taken by someone else)."""
        with self.pool.connection() as c:
            row = c.execute("UPDATE reports SET run_lease_until = now() + make_interval(secs => %s) "
                            "WHERE id = %s AND run_lease_token = %s RETURNING id",
                            (seconds, _uuid(report_id), token)).fetchone()
        return row is not None

    def release_lease(self, report_id: str, token: str) -> None:
        """Release the lease if `token` still holds it (a no-op for anyone else)."""
        with self.pool.connection() as c:
            c.execute("UPDATE reports SET run_lease_token = NULL, run_lease_until = NULL "
                      "WHERE id = %s AND run_lease_token = %s", (_uuid(report_id), token))

    def touch(self, user_id: str, report_id: str) -> None:
        """Record activity (drives retention, D-009); no-op for reports the user does not own."""
        with self.pool.connection() as c:
            c.execute("UPDATE reports SET last_activity_at = now() WHERE id = %s AND user_id = %s",
                      (_uuid(report_id), user_id))


class FileRepo:
    """Uploaded files (metadata + blob key)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    @staticmethod
    def _ref(row: dict) -> DatasetRef:
        """Row -> DatasetRef."""
        return DatasetRef(file_id=str(row["id"]), report_id=str(row["report_id"]), user_id=str(row["user_id"]),
                          filename=row["filename"], kind=row["kind"], size_bytes=row["size_bytes"],
                          sha256=row["sha256"], blob_key=row["blob_key"])

    def add(self, ref: DatasetRef) -> None:
        """Insert a file row (PermissionError unless the report belongs to ref.user_id)."""
        with self.pool.connection() as c, c.transaction():
            if not _owns_report(c, ref.user_id, ref.report_id):
                raise PermissionError("report not owned by user")
            c.execute("INSERT INTO files (id, report_id, user_id, filename, kind, size_bytes, sha256, blob_key) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                      (ref.file_id, ref.report_id, ref.user_id, ref.filename, ref.kind, ref.size_bytes,
                       ref.sha256, ref.blob_key))

    def add_from_upload(self, ref: DatasetRef, upload_id: str, max_files: int) -> None:
        """Turn a staged upload into a file row atomically (one transaction).

        The report row is locked FOR UPDATE, so concurrent completions for one report run one after the
        other: the staged upload is consumed (deleted; UploadGone when it is already gone), the file count
        is checked against `max_files` (FileLimitReached) and the file row is inserted. Any failure rolls
        everything back, leaving the upload staged. PermissionError unless ref.user_id owns the report.
        """
        with self.pool.connection() as c, c.transaction():
            if not c.execute("SELECT 1 FROM reports WHERE id = %s AND user_id = %s FOR UPDATE",
                             (_uuid(ref.report_id), ref.user_id)).fetchone():
                raise PermissionError("report not owned by user")
            uid = _uuid(upload_id)
            gone = uid is None or not c.execute(
                "DELETE FROM uploads WHERE id = %s AND user_id = %s AND report_id = %s RETURNING id",
                (uid, ref.user_id, _uuid(ref.report_id))).fetchone()
            if gone:
                raise UploadGone(upload_id)
            n = c.execute("SELECT count(*) AS n FROM files WHERE report_id = %s",
                          (_uuid(ref.report_id),)).fetchone()["n"]
            if n >= max_files:
                raise FileLimitReached(ref.report_id)
            c.execute("INSERT INTO files (id, report_id, user_id, filename, kind, size_bytes, sha256, blob_key) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                      (ref.file_id, ref.report_id, ref.user_id, ref.filename, ref.kind, ref.size_bytes,
                       ref.sha256, ref.blob_key))

    def get(self, user_id: str, file_id: str) -> DatasetRef | None:
        """A file if it belongs to the user."""
        fid = _uuid(file_id)
        if fid is None:
            return None
        with self.pool.connection() as c:
            row = c.execute("SELECT * FROM files WHERE id = %s AND user_id = %s", (fid, user_id)).fetchone()
        return self._ref(row) if row else None

    def list_for_report(self, user_id: str, report_id: str) -> list[DatasetRef]:
        """The user's files in a report, oldest first."""
        with self.pool.connection() as c:
            rows = c.execute("SELECT * FROM files WHERE report_id = %s AND user_id = %s ORDER BY created_at, id",
                             (_uuid(report_id), user_id)).fetchall()
        return [self._ref(r) for r in rows]

    def count_for_report(self, user_id: str, report_id: str) -> int:
        """Number of files in the user's report (MAX_FILES_PER_REPORT check)."""
        with self.pool.connection() as c:
            return c.execute("SELECT count(*) AS n FROM files WHERE report_id = %s AND user_id = %s",
                             (_uuid(report_id), user_id)).fetchone()["n"]

    def set_status(self, user_id: str, file_id: str, status: str) -> None:
        """Update the processing status of the user's file (no-op for other users' files)."""
        with self.pool.connection() as c:
            c.execute("UPDATE files SET status = %s WHERE id = %s AND user_id = %s",
                      (status, _uuid(file_id), user_id))

    def delete(self, user_id: str, file_id: str) -> DatasetRef | None:
        """Delete the user's file row (cascades); returns the ref so the caller can delete the blob."""
        with self.pool.connection() as c:
            row = c.execute("DELETE FROM files WHERE id = %s AND user_id = %s RETURNING *",
                            (_uuid(file_id), user_id)).fetchone()
        return self._ref(row) if row else None


class UploadRepo:
    """Chunked uploads staged in Postgres until complete (D-021)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def start(self, user_id: str, report_id: str, filename: str, total_bytes: int, chunk_count: int) -> str:
        """Open an upload for the user's report (PermissionError for other users' reports)."""
        if total_bytes <= 0 or chunk_count <= 0:
            raise ValueError("total_bytes and chunk_count must be positive")
        with self.pool.connection() as c, c.transaction():
            if not _owns_report(c, user_id, report_id):
                raise PermissionError("report not owned by user")
            return str(c.execute("INSERT INTO uploads (user_id, report_id, filename, total_bytes, chunk_count) "
                                 "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                 (user_id, report_id, filename, total_bytes, chunk_count)).fetchone()["id"])

    def get(self, user_id: str, upload_id: str) -> dict | None:
        """Upload metadata if owned by the user."""
        uid = _uuid(upload_id)
        if uid is None:
            return None
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM uploads WHERE id = %s AND user_id = %s", (uid, user_id)).fetchone()

    def _owned(self, user_id: str, upload_id: str) -> dict:
        """Upload metadata, or PermissionError when the user does not own it."""
        up = self.get(user_id, upload_id)
        if up is None:
            raise PermissionError("upload not owned by user")
        return up

    def put_chunk(self, user_id: str, upload_id: str, idx: int, data: bytes) -> None:
        """Store one chunk (re-sending the same index replaces it).

        Ownership check and insert share one transaction; the upload row is locked FOR SHARE so a concurrent
        delete cannot turn the insert into a foreign-key error. PermissionError when the user does not own
        the upload (or it is gone); ValueError for an out-of-range index or a chunk larger than the upload.
        """
        uid = _uuid(upload_id)
        with self.pool.connection() as c, c.transaction():
            up = None if uid is None else c.execute(
                "SELECT id, chunk_count, total_bytes FROM uploads WHERE id = %s AND user_id = %s FOR SHARE",
                (uid, user_id)).fetchone()
            if up is None:
                raise PermissionError("upload not owned by user")
            if not 0 <= idx < up["chunk_count"]:
                raise ValueError("chunk index out of range")
            if len(data) > up["total_bytes"]:
                raise ValueError("chunk is larger than the declared upload size")
            c.execute("INSERT INTO upload_chunks (upload_id, idx, data) VALUES (%s, %s, %s) "
                      "ON CONFLICT (upload_id, idx) DO UPDATE SET data = EXCLUDED.data", (up["id"], idx, data))

    def count_open(self, user_id: str, report_id: str) -> int:
        """Number of the user's staged (not yet completed) uploads for a report."""
        rid = _uuid(report_id)
        if rid is None:
            return 0
        with self.pool.connection() as c:
            return c.execute("SELECT count(*) AS n FROM uploads WHERE report_id = %s AND user_id = %s",
                             (rid, user_id)).fetchone()["n"]

    def assemble(self, user_id: str, upload_id: str) -> bytes:
        """Concatenate all chunks in order (ValueError when any is missing or the size is wrong)."""
        up = self._owned(user_id, upload_id)
        with self.pool.connection() as c:
            rows = c.execute("SELECT idx, data FROM upload_chunks WHERE upload_id = %s ORDER BY idx",
                             (up["id"],)).fetchall()
        if [r["idx"] for r in rows] != list(range(up["chunk_count"])):
            raise ValueError("upload has missing chunks")
        data = b"".join(bytes(r["data"]) for r in rows)
        if len(data) != up["total_bytes"]:
            raise ValueError("assembled upload size does not match the declared total")
        return data

    def delete(self, user_id: str, upload_id: str) -> None:
        """Remove the user's upload and its chunks (no-op otherwise)."""
        with self.pool.connection() as c:
            c.execute("DELETE FROM uploads WHERE id = %s AND user_id = %s", (_uuid(upload_id), user_id))


class MappingRepo:
    """Column mappings per file (survive resume, AC 58)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def save(self, user_id: str, file_id: str, mapping: ColumnMapping) -> None:
        """Insert or replace the mapping of the user's file (PermissionError otherwise).

        In the same transaction the file's validation report and observations are deleted and its status
        goes back to "mapped": both were derived under the previous mapping and must be redone.
        """
        with self.pool.connection() as c, c.transaction():
            if not _owns_file(c, user_id, file_id):
                raise PermissionError("file not owned by user")
            c.execute("INSERT INTO file_mappings (file_id, mapping) VALUES (%s, %s) ON CONFLICT (file_id) "
                      "DO UPDATE SET mapping = EXCLUDED.mapping, updated_at = now()",
                      (file_id, Jsonb(mapping.model_dump(mode="json"))))
            c.execute("DELETE FROM validation_reports WHERE file_id = %s", (file_id,))
            c.execute("DELETE FROM observations WHERE file_id = %s AND user_id = %s", (file_id, user_id))
            c.execute("UPDATE files SET status = %s WHERE id = %s", (MAPPED_STATUS, file_id))

    def get(self, user_id: str, file_id: str) -> ColumnMapping | None:
        """The mapping of the user's file."""
        with self.pool.connection() as c:
            row = c.execute("SELECT m.mapping FROM file_mappings m JOIN files f ON f.id = m.file_id "
                            "WHERE m.file_id = %s AND f.user_id = %s", (_uuid(file_id), user_id)).fetchone()
        return ColumnMapping.model_validate(row["mapping"]) if row else None


class ValidationRepo:
    """Latest validation report per file."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def save(self, user_id: str, report: ValidationReport) -> None:
        """Insert or replace the report of the user's file (PermissionError otherwise)."""
        with self.pool.connection() as c, c.transaction():
            if not _owns_file(c, user_id, report.file_id):
                raise PermissionError("file not owned by user")
            c.execute("INSERT INTO validation_reports (file_id, report) VALUES (%s, %s) ON CONFLICT (file_id) "
                      "DO UPDATE SET report = EXCLUDED.report, updated_at = now()",
                      (report.file_id, Jsonb(report.model_dump(mode="json"))))

    def get(self, user_id: str, file_id: str) -> ValidationReport | None:
        """The latest report of the user's file."""
        with self.pool.connection() as c:
            row = c.execute("SELECT v.report FROM validation_reports v JOIN files f ON f.id = v.file_id "
                            "WHERE v.file_id = %s AND f.user_id = %s", (_uuid(file_id), user_id)).fetchone()
        return ValidationReport.model_validate(row["report"]) if row else None


class ObservationRepo:
    """Normalized observations per report."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def replace_for_file(self, user_id: str, report_id: str, file_id: str, observations: list[Observation]) -> int:
        """Atomically replace one file's observations; returns how many were stored.

        PermissionError unless the user owns the file and the file belongs to `report_id`.
        """
        with self.pool.connection() as c, c.transaction():
            # FOR UPDATE serialises concurrent re-normalisations of the same file: the second waits for the
            # first to commit, then replaces its rows, instead of both writing and merging two result sets.
            owned = c.execute("SELECT 1 FROM files WHERE id = %s AND user_id = %s AND report_id = %s "
                              "FOR UPDATE", (_uuid(file_id), user_id, _uuid(report_id))).fetchone()
            if not owned:
                raise PermissionError("file not owned by user or not in this report")
            c.execute("DELETE FROM observations WHERE file_id = %s AND user_id = %s", (file_id, user_id))
            if observations:
                with c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO observations (report_id, obs_id, file_id, user_id, indicator_code, "
                        "region_code, period, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(report_id, o.obs_id, file_id, user_id, o.indicator_code, o.region_code, o.period,
                          Jsonb(o.model_dump(mode="json"))) for o in observations])
        return len(observations)

    def upsert_api_observations(self, user_id: str, report_id: str, observations: list[Observation]) -> int:
        """Store public-API observations of the user's report (rows without a file); returns how many.

        An API row with the same obs_id is replaced (the per-report API cache makes a re-fetch identical); a row
        of an uploaded file is never overwritten. PermissionError unless the user owns the report; ValueError
        for an observation that does not come from an API.
        """
        if any(not isinstance(o.provenance, ApiProvenance) for o in observations):
            raise ValueError("only API observations can be stored without a file")
        with self.pool.connection() as c, c.transaction():
            if _uuid(report_id) is None or not _owns_report(c, user_id, report_id):
                raise PermissionError("report not owned by user")
            if observations:
                with c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO observations (report_id, obs_id, file_id, user_id, indicator_code, region_code, "
                        "period, payload) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (report_id, obs_id) DO UPDATE SET indicator_code = EXCLUDED.indicator_code, "
                        "region_code = EXCLUDED.region_code, period = EXCLUDED.period, payload = EXCLUDED.payload "
                        "WHERE observations.file_id IS NULL",
                        [(report_id, o.obs_id, user_id, o.indicator_code, o.region_code, o.period,
                          Jsonb(o.model_dump(mode="json"))) for o in observations])
        return len(observations)

    def list_for_report(self, user_id: str, report_id: str) -> list[Observation]:
        """The user's observations in a report (stable order)."""
        with self.pool.connection() as c:
            rows = c.execute("SELECT payload FROM observations WHERE report_id = %s AND user_id = %s "
                             "ORDER BY indicator_code, region_code, period, obs_id",
                             (_uuid(report_id), user_id)).fetchall()
        return [Observation.model_validate(r["payload"]) for r in rows]

    def list_for_series(self, user_id: str, report_id: str, indicator_codes: list[str],
                        region: str) -> list[Observation]:
        """The user's observations of some indicator codes in one region of a report (stable order)."""
        with self.pool.connection() as c:
            rows = c.execute("SELECT payload FROM observations WHERE report_id = %s AND user_id = %s "
                             "AND indicator_code = ANY(%s) AND region_code = %s "
                             "ORDER BY indicator_code, region_code, period, obs_id",
                             (_uuid(report_id), user_id, list(indicator_codes), region)).fetchall()
        return [Observation.model_validate(r["payload"]) for r in rows]


@dataclass(frozen=True)
class CachedApiResponse:
    """One cached raw API response of a report."""

    source: str
    url: str
    payload: Any
    retrieved_at: datetime


class ApiCacheRepo:
    """Raw public-API responses cached per report (resume never re-fetches)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def get(self, user_id: str, report_id: str, request_hash: str) -> CachedApiResponse | None:
        """The cached response of the user's report for this request, or None."""
        rid = _uuid(report_id)
        if rid is None:
            return None
        with self.pool.connection() as c:
            row = c.execute("SELECT source, url, payload, retrieved_at FROM api_cache "
                            "WHERE report_id = %s AND user_id = %s AND request_hash = %s",
                            (rid, user_id, request_hash)).fetchone()
        return CachedApiResponse(row["source"], row["url"], row["payload"], row["retrieved_at"]) if row else None

    def put(self, user_id: str, report_id: str, request_hash: str, source: str, url: str, payload: Any,
            retrieved_at: datetime) -> None:
        """Cache a response for the user's report; an existing entry is kept (first response wins).

        PermissionError unless the user owns the report.
        """
        with self.pool.connection() as c, c.transaction():
            if _uuid(report_id) is None or not _owns_report(c, user_id, report_id):
                raise PermissionError("report not owned by user")
            c.execute("INSERT INTO api_cache (report_id, user_id, request_hash, source, url, payload, retrieved_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (report_id, request_hash) DO NOTHING",
                      (report_id, user_id, request_hash, source, url, Jsonb(payload), retrieved_at))


RUN_EVENT_KINDS: tuple[str, ...] = ("llm_start", "llm_end", "llm_error", "tool_start", "tool_end", "tool_error",
                                    "retrieval", "node")  # mirrors the CHECK constraint of migration 005


@dataclass(frozen=True)
class RunEvent:
    """One observability event of a report run (model call, tool call, retrieval or graph node).

    `detail` must hold no raw data values, prompt text or keys (the callback handler redacts them).
    `ts` defaults to the database time when None.
    """

    kind: str
    name: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    ts: datetime | None = None


class RunEventRepo:
    """run_events rows, scoped to the owner of the report (Diagnostics tab, AC 51)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def add(self, user_id: str, report_id: str, event: RunEvent) -> None:
        """Insert one event; ValueError for an unknown kind, PermissionError unless the user owns the report."""
        if event.kind not in RUN_EVENT_KINDS:
            raise ValueError(f"unknown run event kind {event.kind!r}")
        rid = _uuid(report_id)
        if rid is None:
            raise PermissionError("report not owned by user")
        with self.pool.connection() as c:
            row = c.execute(
                "INSERT INTO run_events (report_id, user_id, ts, kind, name, model, latency_ms, prompt_tokens, "
                "completion_tokens, detail) "
                "SELECT r.id, r.user_id, coalesce(%s, now()), %s, %s, %s, %s, %s, %s, %s FROM reports r "
                "WHERE r.id = %s AND r.user_id = %s RETURNING id",
                (event.ts, event.kind, event.name, event.model, event.latency_ms, event.prompt_tokens,
                 event.completion_tokens, Jsonb(event.detail), rid, user_id)).fetchone()
        if row is None:
            raise PermissionError("report not owned by user")

    def list_for_report(self, user_id: str, report_id: str) -> list[dict]:
        """The report's events in insertion order; empty for other users and malformed ids."""
        rid = _uuid(report_id)
        if rid is None:
            return []
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM run_events WHERE report_id = %s AND user_id = %s ORDER BY id",
                             (rid, user_id)).fetchall()
