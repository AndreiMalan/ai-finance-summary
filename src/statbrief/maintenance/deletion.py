"""Delete a report or a whole account, Blob objects first (D-009, AC 50).

Order:
1. Delete every Blob object: the keys the database references (raw files not yet purged, export files)
   plus every key listed under the owner prefix (`users/{u}/` for an account, `users/{u}/reports/{r}/`
   for a report), which also removes blobs orphaned by earlier best-effort cleanups. If listing or any
   delete fails, stop with `DeletionIncomplete` and leave the database untouched so the user can retry
   (deleting an absent blob is not an error, so a retry is idempotent).
2. In one transaction: lock the user / report row first, then collect blob keys that appeared since step 1
   (an upload completing meanwhile; once the row is locked no new child row can commit), delete the
   LangGraph checkpoint threads (thread_id = report id) and, for an account, the long-term memory
   namespace ("prefs", user_id), then the report / user row; everything else cascades.
3. Delete those late blobs best-effort after the commit (any leftover is swept by the retention job).
"""
import logging
import uuid
from collections.abc import Callable

from psycopg import Connection
from psycopg_pool import ConnectionPool

from statbrief.maintenance.langgraph_data import PREFS_NAMESPACE, delete_store_namespace, delete_threads
from statbrief.storage.blobs import BlobStore, owner_prefix

logger = logging.getLogger(__name__)

_USER_BLOBS = ("SELECT blob_key FROM files WHERE user_id = %(u)s AND purged_at IS NULL "
               "UNION SELECT blob_key FROM report_versions WHERE user_id = %(u)s")
_REPORT_BLOBS = ("SELECT blob_key FROM files WHERE user_id = %(u)s AND report_id = %(r)s AND purged_at IS NULL "
                 "UNION SELECT blob_key FROM report_versions WHERE user_id = %(u)s AND report_id = %(r)s")


class DeletionIncomplete(Exception):
    """Some Blob objects could not be listed or deleted; nothing was removed from the database (retry later)."""

    def __init__(self, failed: int):
        """Remember how many blob operations failed."""
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


def _listed(blobs: BlobStore, prefix: str) -> set[str]:
    """Every key under the owner prefix; DeletionIncomplete when the store cannot be listed."""
    try:
        return {b.key for b in blobs.list(prefix)}
    except Exception as exc:
        logger.warning("blob listing failed during deletion: %s", type(exc).__name__)
        raise DeletionIncomplete(1) from None


def _delete(pool: ConnectionPool, blobs: BlobStore, sql: str, params: dict, prefix: str,
            lock: Callable[[Connection], bool], delete_rows: Callable[[Connection], None]) -> bool:
    """Steps 1-3 of the module doc; False when `lock` finds no row to delete."""
    with pool.connection() as c:
        keys = _keys(c, sql, params)
    keys |= _listed(blobs, prefix)
    failed = _delete_blobs(blobs, keys)
    if failed:
        raise DeletionIncomplete(failed)
    with pool.connection() as c, c.transaction():
        if not lock(c):
            return False
        late = _keys(c, sql, params) - keys
        delete_rows(c)
    if late:
        _delete_blobs(blobs, late)
    return True


def delete_report(pool: ConnectionPool, blobs: BlobStore, user_id: str, report_id: str) -> bool:
    """Delete the user's report with its blobs and checkpoints; False when the user has no such report.

    `report_id` may be any UUID spelling; it is canonicalised (the thread id and blob prefix use the
    canonical text). ValueError for a malformed id (callers map it to "not found").
    """
    report_id = str(uuid.UUID(report_id))
    params = {"u": user_id, "r": report_id}

    def lock(c: Connection) -> bool:
        """Lock the user's report row; False when it does not exist."""
        return c.execute("SELECT 1 FROM reports WHERE id = %(r)s AND user_id = %(u)s FOR UPDATE",
                         params).fetchone() is not None

    def rows(c: Connection) -> None:
        """Delete the thread and the report row (cascades files, observations, versions, events...)."""
        delete_threads(c, [report_id])
        c.execute("DELETE FROM reports WHERE id = %(r)s AND user_id = %(u)s", params)

    return _delete(pool, blobs, _REPORT_BLOBS, params, owner_prefix(user_id, report_id), lock, rows)


def delete_account(pool: ConnectionPool, blobs: BlobStore, user_id: str) -> bool:
    """Delete the user and everything they own: blobs, checkpoint threads, preference namespace, rows.

    Returns False when the user does not exist.
    """
    params = {"u": user_id}

    def lock(c: Connection) -> bool:
        """Lock the user row; False when it does not exist."""
        return c.execute("SELECT 1 FROM users WHERE id = %(u)s FOR UPDATE", params).fetchone() is not None

    def rows(c: Connection) -> None:
        """Delete threads of every report, the prefs namespace and the user row (cascade)."""
        reports = [str(r["id"]) for r in c.execute("SELECT id FROM reports WHERE user_id = %(u)s", params)]
        delete_threads(c, reports)
        delete_store_namespace(c, (PREFS_NAMESPACE, user_id))
        c.execute("DELETE FROM users WHERE id = %(u)s", params)

    return _delete(pool, blobs, _USER_BLOBS, params, owner_prefix(user_id), lock, rows)
