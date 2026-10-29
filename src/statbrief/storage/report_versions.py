"""User-scoped repository for stored report exports (`report_versions`, migration 006) and report approval.

A version groups the export files built together (e.g. one PDF and one XLSX); it is allocated atomically
when the files are recorded, with the report row locked, so concurrent exports never share a number.
Reads return None/empty for other users' rows; writes for reports the user does not own raise
PermissionError (AC 6). Malformed ids are treated as "not found".
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

from statbrief.storage.blobs import blob_key

EXPORT_FORMATS = ("pdf", "xlsx")  # mirrors the CHECK constraint of migration 006


def _uuid(value: str) -> uuid.UUID | None:
    """Parse a UUID; None when malformed."""
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class ExportFile:
    """One built export file stored in Blob, before it is recorded."""

    fmt: str
    blob_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ReportVersion:
    """One recorded export file of a report version."""

    report_id: str
    version: int
    fmt: str
    blob_key: str
    size_bytes: int
    sha256: str
    created_at: datetime


def _row(r: dict) -> ReportVersion:
    """Row -> ReportVersion."""
    return ReportVersion(report_id=str(r["report_id"]), version=r["version"], fmt=r["fmt"], blob_key=r["blob_key"],
                         size_bytes=r["size_bytes"], sha256=r["sha256"], created_at=r["created_at"])


def export_blob_key(user_id: str, report_id: str, fmt: str) -> str:
    """Owner-scoped blob key for a new export file (unique per call; ValueError for an unknown format)."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unknown export format {fmt!r}")
    return blob_key(user_id, report_id, f"export-{uuid.uuid4().hex}", f"report.{fmt}")


class ReportVersionRepo:
    """Export files per report version."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def add_version(self, user_id: str, report_id: str, files: list[ExportFile]) -> int:
        """Record the files as the report's next version (one transaction); returns the version number.

        PermissionError unless the user owns the report; ValueError for no files, an unknown format or a
        format listed twice.
        """
        fmts = [f.fmt for f in files]
        if not files or len(set(fmts)) != len(fmts) or any(f not in EXPORT_FORMATS for f in fmts):
            raise ValueError("one file per known export format is required")
        rid = _uuid(report_id)
        with self.pool.connection() as c, c.transaction():
            if rid is None or not c.execute("SELECT 1 FROM reports WHERE id = %s AND user_id = %s FOR UPDATE",
                                            (rid, user_id)).fetchone():
                raise PermissionError("report not owned by user")
            version = c.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM report_versions WHERE report_id = %s",
                                (rid,)).fetchone()["v"]
            with c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO report_versions (report_id, user_id, version, fmt, blob_key, size_bytes, sha256) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [(rid, user_id, version, f.fmt, f.blob_key, f.size_bytes, f.sha256) for f in files])
        return version

    def get(self, user_id: str, report_id: str, version: int, fmt: str) -> ReportVersion | None:
        """One export file of the user's report, or None."""
        rid = _uuid(report_id)
        if rid is None:
            return None
        with self.pool.connection() as c:
            row = c.execute("SELECT * FROM report_versions WHERE report_id = %s AND user_id = %s AND version = %s "
                            "AND fmt = %s", (rid, user_id, version, fmt)).fetchone()
        return _row(row) if row else None

    def list_for_report(self, user_id: str, report_id: str) -> list[ReportVersion]:
        """The user's export files of a report, newest version first (formats alphabetically)."""
        rid = _uuid(report_id)
        if rid is None:
            return []
        with self.pool.connection() as c:
            rows = c.execute("SELECT * FROM report_versions WHERE report_id = %s AND user_id = %s "
                             "ORDER BY version DESC, fmt", (rid, user_id)).fetchall()
        return [_row(r) for r in rows]


def mark_approved(pool: ConnectionPool, user_id: str, report_id: str, approved_at: datetime) -> bool:
    """Record the approval time of the user's report (starts the 7-day raw-file retention, D-009).

    The first approval time is kept (re-approving after a later revision does not extend retention).
    Returns False when the user does not own the report. ValueError for a naive datetime.
    """
    if approved_at.tzinfo is None or approved_at.tzinfo.utcoffset(approved_at) is None:
        raise ValueError("approved_at must be timezone-aware")
    rid = _uuid(report_id)
    if rid is None:
        return False
    with pool.connection() as c:
        row = c.execute("UPDATE reports SET approved_at = coalesce(approved_at, %s) WHERE id = %s AND user_id = %s "
                        "RETURNING id", (approved_at, rid, user_id)).fetchone()
    return row is not None
