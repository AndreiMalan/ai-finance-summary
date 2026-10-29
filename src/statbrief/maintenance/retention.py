"""Daily retention cleanup (D-009, AC 50), run by Vercel Cron through `/cron/cleanup`.

Rules (periods from Settings, all env-configurable):
- Raw uploaded files: RETENTION_UNFINISHED_DAYS after the last activity of a report that is not approved,
  RETENTION_APPROVED_DAYS after approval (`reports.approved_at`). The Blob object is deleted first; only
  then is the file row marked `purged_at` (status "purged"). The row itself stays: observations reference
  it and normalized data with provenance lives for the report's lifetime. A Blob failure leaves the row
  unmarked, so the next run retries (deleting an absent blob is not an error, so retries are idempotent).
  Deleting a file or a report deletes its blobs immediately (routes / `maintenance.deletion`).
- LangGraph checkpoints (thread = report id): CHECKPOINT_RETENTION_DAYS after the report's last activity,
  and at once for threads whose report no longer exists.
- Logs: run_events, login_attempts and usage_events older than LOG_RETENTION_DAYS.
- Abandoned chunked uploads (staged in Postgres, never completed) older than UPLOAD_ABANDON_HOURS.
- Orphaned blobs: stored objects under "users/" that no unpurged `files` row and no `report_versions` row
  references, older than BLOB_ORPHAN_GRACE_HOURS (the grace covers the window between a blob upload and
  the insert of its row). They come from best-effort cleanups that failed (file deletion, a failed export
  or upload completion). Staged uploads live in Postgres, so the `uploads` table references no blob.
The cheap SQL rules run first, so a large Blob backlog cannot starve them within one request.
Each rule handles at most RETENTION_BATCH_SIZE rows per run (bounded work per request). A rule that fails
is logged (exception class only) and reported; the other rules still run.
"""
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from psycopg_pool import ConnectionPool

from statbrief.config import Settings
from statbrief.maintenance.langgraph_data import delete_threads, stale_thread_ids
from statbrief.storage.blobs import USERS_PREFIX, BlobStore

logger = logging.getLogger(__name__)

PURGED_STATUS = "purged"
# Log tables and their timestamp/id columns (constants, never user input).
_LOG_TABLES = (("run_events", "ts"), ("login_attempts", "at"), ("usage_events", "at"))


@dataclass
class CleanupResult:
    """What one cleanup run did; `failed_rules` names rules that raised (others still ran)."""

    raw_files_purged: int = 0
    raw_files_failed: int = 0
    threads_deleted: int = 0
    run_events_deleted: int = 0
    login_attempts_deleted: int = 0
    usage_events_deleted: int = 0
    uploads_deleted: int = 0
    orphan_blobs_deleted: int = 0
    orphan_blobs_failed: int = 0
    failed_rules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """JSON-safe summary (counts only; no ids, keys or user data)."""
        return asdict(self)


def _require_aware(now: datetime) -> None:
    """ValueError for a naive `now` (it would be read as server-local time)."""
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")


def purge_raw_files(pool: ConnectionPool, blobs: BlobStore, settings: Settings, now: datetime) -> tuple[int, int]:
    """Delete expired raw-file blobs, then mark their rows purged; returns (purged, failed)."""
    with pool.connection() as c:
        rows = c.execute(
            "SELECT f.id, f.blob_key FROM files f JOIN reports r ON r.id = f.report_id "
            "WHERE f.purged_at IS NULL AND ((r.approved_at IS NULL AND r.last_activity_at < %(unfinished)s) "
            "OR (r.approved_at IS NOT NULL AND r.approved_at < %(approved)s AND f.created_at < %(approved)s)) "
            "ORDER BY f.created_at, f.id LIMIT %(n)s",
            {"unfinished": now - timedelta(days=settings.retention_unfinished_days),
             "approved": now - timedelta(days=settings.retention_approved_days),
             "n": settings.retention_batch_size}).fetchall()
    purged = failed = 0
    for row in rows:
        try:
            blobs.delete(row["blob_key"])
        except Exception as exc:  # keep the row unmarked: the next run retries
            logger.warning("retention: raw file blob delete failed: %s", type(exc).__name__)
            failed += 1
            continue
        with pool.connection() as c:
            c.execute("UPDATE files SET purged_at = %s, status = %s WHERE id = %s AND purged_at IS NULL",
                      (now, PURGED_STATUS, row["id"]))
        purged += 1
    return purged, failed


def purge_stale_threads(pool: ConnectionPool, settings: Settings, now: datetime) -> int:
    """Delete checkpoint threads of long-inactive or deleted reports; returns the threads deleted."""
    with pool.connection() as c, c.transaction():
        threads = stale_thread_ids(c, now - timedelta(days=settings.checkpoint_retention_days),
                                   settings.retention_batch_size)
        delete_threads(c, threads)
    return len(threads)


def purge_logs(pool: ConnectionPool, settings: Settings, now: datetime) -> dict[str, int]:
    """Delete log rows older than LOG_RETENTION_DAYS; returns rows deleted per table."""
    cutoff = now - timedelta(days=settings.log_retention_days)
    out: dict[str, int] = {}
    with pool.connection() as c:
        for table, ts in _LOG_TABLES:
            out[table] = c.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {ts} < %s LIMIT %s)",
                (cutoff, settings.retention_batch_size)).rowcount
    return out


def purge_abandoned_uploads(pool: ConnectionPool, settings: Settings, now: datetime) -> int:
    """Delete staged uploads (and their chunks, by cascade) older than UPLOAD_ABANDON_HOURS."""
    with pool.connection() as c:
        return c.execute("DELETE FROM uploads WHERE id IN (SELECT id FROM uploads WHERE created_at < %s LIMIT %s)",
                         (now - timedelta(hours=settings.upload_abandon_hours),
                          settings.retention_batch_size)).rowcount


def _referenced(pool: ConnectionPool, keys: list[str]) -> set[str]:
    """The keys an unpurged file row or an export row still references."""
    with pool.connection() as c:
        rows = c.execute("SELECT blob_key FROM files WHERE purged_at IS NULL AND blob_key = ANY(%(k)s) "
                         "UNION SELECT blob_key FROM report_versions WHERE blob_key = ANY(%(k)s)",
                         {"k": keys}).fetchall()
    return {r["blob_key"] for r in rows}


def sweep_orphan_blobs(pool: ConnectionPool, blobs: BlobStore, settings: Settings, now: datetime) -> tuple[int, int]:
    """Delete unreferenced blobs older than BLOB_ORPHAN_GRACE_HOURS (at most a batch); returns (deleted, failed)."""
    cutoff = now - timedelta(hours=settings.blob_orphan_grace_hours)
    batch_size = settings.retention_batch_size
    deleted = failed = 0
    pending: list[str] = []

    def flush() -> None:
        """Delete the unreferenced keys among `pending`."""
        nonlocal deleted, failed
        referenced = _referenced(pool, pending)
        for key in pending:
            if key in referenced or deleted + failed >= batch_size:
                continue
            try:
                blobs.delete(key)
                deleted += 1
            except Exception as exc:
                logger.warning("retention: orphan blob delete failed: %s", type(exc).__name__)
                failed += 1
        pending.clear()

    for blob in blobs.list(USERS_PREFIX):
        if blob.uploaded_at >= cutoff:
            continue
        pending.append(blob.key)
        if len(pending) >= batch_size:
            flush()
            if deleted + failed >= batch_size:
                return deleted, failed
    if pending:
        flush()
    return deleted, failed


def cleanup_expired(pool: ConnectionPool, blobs: BlobStore, settings: Settings, now: datetime) -> CleanupResult:
    """Run every retention rule once at time `now` (timezone-aware; injectable for tests)."""
    _require_aware(now)
    result = CleanupResult()

    def raw() -> None:
        """Raw files rule."""
        result.raw_files_purged, result.raw_files_failed = purge_raw_files(pool, blobs, settings, now)

    def threads() -> None:
        """Checkpoint threads rule."""
        result.threads_deleted = purge_stale_threads(pool, settings, now)

    def logs() -> None:
        """Log tables rule."""
        counts = purge_logs(pool, settings, now)
        result.run_events_deleted = counts["run_events"]
        result.login_attempts_deleted = counts["login_attempts"]
        result.usage_events_deleted = counts["usage_events"]

    def uploads() -> None:
        """Abandoned uploads rule."""
        result.uploads_deleted = purge_abandoned_uploads(pool, settings, now)

    def orphans() -> None:
        """Orphan blob sweep."""
        result.orphan_blobs_deleted, result.orphan_blobs_failed = sweep_orphan_blobs(pool, blobs, settings, now)

    # Cheap SQL rules first; the Blob-bound rules last (M8 of the review).
    rules: dict[str, Callable[[], None]] = {"logs": logs, "uploads": uploads, "threads": threads, "raw_files": raw,
                                            "orphan_blobs": orphans}
    for name, rule in rules.items():
        try:
            rule()
        except Exception as exc:
            logger.error("retention rule %s failed: %s", name, type(exc).__name__)
            result.failed_rules.append(name)
    return result
