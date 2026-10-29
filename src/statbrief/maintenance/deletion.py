"""Delete a report or a whole account, Blob objects first (D-009, AC 50).

Order: (1) delete every Blob object (raw files not yet purged, export files); if any delete fails, stop
with `DeletionIncomplete` and leave the database untouched so the user can retry (deleting an absent blob
is not an error, so a retry is idempotent). (2) In one transaction: delete the LangGraph checkpoint
threads (thread_id = report id) and, for an account, the long-term memory namespace ("prefs", user_id),
then the report / user row; everything else cascades. (3) Blob objects that appeared concurrently between
(1) and (2) (an upload completing meanwhile) are deleted best-effort after the commit.
"""
import logging

from psycopg import Connection
from psycopg_pool import ConnectionPool

from statbrief.maintenance.langgraph_data import PREFS_NAMESPACE, delete_store_namespace, delete_threads
from statbrief.storage.blobs import BlobStore

logger = logging.getLogger(__name__)

_USER_BLOBS = ("SELECT blob_key FROM files WHERE user_id = %(u)s AND purged_at IS NULL "
               "UNION SELECT blob_key FROM report_versions WHERE user_id = %(u)s")
_REPORT_BLOBS = ("SELECT blob_key FROM files WHERE user_id = %(u)s AND report_id = %(r)s AND purged_at IS NULL "
                 "UNION SELECT blob_key FROM report_versions WHERE user_id = %(u)s AND report_id = %(r)s")


class DeletionIncomplete(Exception):
    """Some Blob objects could not be deleted; nothing was removed from the database (retry later)."""

    def __init__(self, failed: int):
        """Remember how many blob deletes failed."""
        super().__init__(f"{failed} stored files could not be deleted")
        self.failed = failed


def _keys(conn: Connection, sql: str, params: dict) -> set[str]:
    """Blob keys selected by `sql`."""
    return {r["blob_key"] for r in conn.execute(sql, params).fetchall()}


def _delete_blobs(blobs: BlobStore, keys: set[str]) -> int:
    """Delete each blob; returns how many deletes failed (exception class logged only)."""
    failed = 0
    for key in sorted(keys):
        try:
            blobs.delete(key)
        except Exception as exc:
            logger.warning("blob delete failed during deletion: %s", type(exc).__name__)
            failed += 1
    return failed


def _delete(pool: ConnectionPool, blobs: BlobStore, sql: str, params: dict, delete_rows) -> bool:
    """Blobs first, then rows in one transaction, then late blobs best-effort; False when nothing matched."""
    with pool.connection() as c:
        keys = _keys(c, sql, params)
    failed = _delete_blobs(blobs, keys)
    if failed:
        raise DeletionIncomplete(failed)
    with pool.connection() as c, c.transaction():
        late = _keys(c, sql, params) - keys
        found = delete_rows(c)
    if late:
        _delete_blobs(blobs, late)
    return found


def delete_report(pool: ConnectionPool, blobs: BlobStore, user_id: str, report_id: str) -> bool:
    """Delete the user's report with its blobs and checkpoints; False when the user has no such report.

    `report_id` must be a well-formed UUID (callers map malformed ids to "not found").
    """
    params = {"u": user_id, "r": report_id}

    def rows(c: Connection) -> bool:
        """Delete the thread and the report row (cascades files, observations, versions, events...)."""
        if not c.execute("SELECT 1 FROM reports WHERE id = %(r)s AND user_id = %(u)s FOR UPDATE", params).fetchone():
            return False
        delete_threads(c, [report_id])
        c.execute("DELETE FROM reports WHERE id = %(r)s AND user_id = %(u)s", params)
        return True

    return _delete(pool, blobs, _REPORT_BLOBS, params, rows)


def delete_account(pool: ConnectionPool, blobs: BlobStore, user_id: str) -> bool:
    """Delete the user and everything they own: blobs, checkpoint threads, preference namespace, rows.

    Returns False when the user does not exist.
    """
    params = {"u": user_id}

    def rows(c: Connection) -> bool:
        """Delete threads of every report, the prefs namespace and the user row (cascade)."""
        if not c.execute("SELECT 1 FROM users WHERE id = %(u)s FOR UPDATE", params).fetchone():
            return False
        reports = [str(r["id"]) for r in c.execute("SELECT id FROM reports WHERE user_id = %(u)s", params)]
        delete_threads(c, reports)
        delete_store_namespace(c, (PREFS_NAMESPACE, user_id))
        c.execute("DELETE FROM users WHERE id = %(u)s", params)
        return True

    return _delete(pool, blobs, _USER_BLOBS, params, rows)
