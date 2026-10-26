"""User-scoped repositories for reports, files, uploads, mappings, validation reports and observations.

Every read takes `user_id` and returns None/empty for other users' rows; writes to rows the user does not
own raise PermissionError (spec AC 6). Malformed ids are treated as "not found" (never a SQL error).
"""
import uuid

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from statbrief.domain.dataset import ColumnMapping, DatasetRef, ValidationReport
from statbrief.domain.observation import Observation


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
        """Store one chunk (re-sending the same index replaces it); ValueError for an out-of-range index."""
        up = self._owned(user_id, upload_id)
        if not 0 <= idx < up["chunk_count"]:
            raise ValueError("chunk index out of range")
        with self.pool.connection() as c:
            c.execute("INSERT INTO upload_chunks (upload_id, idx, data) VALUES (%s, %s, %s) "
                      "ON CONFLICT (upload_id, idx) DO UPDATE SET data = EXCLUDED.data", (up["id"], idx, data))

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
        """Insert or replace the mapping of the user's file (PermissionError otherwise)."""
        with self.pool.connection() as c, c.transaction():
            if not _owns_file(c, user_id, file_id):
                raise PermissionError("file not owned by user")
            c.execute("INSERT INTO file_mappings (file_id, mapping) VALUES (%s, %s) ON CONFLICT (file_id) "
                      "DO UPDATE SET mapping = EXCLUDED.mapping, updated_at = now()",
                      (file_id, Jsonb(mapping.model_dump(mode="json"))))

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
            owned = c.execute("SELECT 1 FROM files WHERE id = %s AND user_id = %s AND report_id = %s",
                              (_uuid(file_id), user_id, _uuid(report_id))).fetchone()
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

    def list_for_report(self, user_id: str, report_id: str) -> list[Observation]:
        """The user's observations in a report (stable order)."""
        with self.pool.connection() as c:
            rows = c.execute("SELECT payload FROM observations WHERE report_id = %s AND user_id = %s "
                             "ORDER BY indicator_code, region_code, period, obs_id",
                             (_uuid(report_id), user_id)).fetchall()
        return [Observation.model_validate(r["payload"]) for r in rows]
