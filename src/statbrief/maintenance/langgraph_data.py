"""Delete LangGraph data with plain SQL: checkpoint threads and long-term memory store namespaces.

The tables are created by `PostgresSaver.setup()` / `PostgresStore.setup()` in the M1 bootstrap
(`storage/db.py`): `checkpoints`, `checkpoint_blobs` and `checkpoint_writes` are keyed by `thread_id`
(= the report id, M7), and `store` by `prefix` (the namespace tuple joined with "."; M7 keeps user
preferences under ("prefs", user_id)). Deleting rows directly avoids instantiating the saver/store in a
cron or a request and works inside the caller's transaction. Tables that do not exist (a database that
was migrated without LangGraph setup) are skipped.
"""
from datetime import datetime

from psycopg import Connection

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
STORE_TABLE = "store"
PREFS_NAMESPACE = "prefs"  # M7 PreferencesRepo: namespace (PREFS_NAMESPACE, user_id)
_NS_SEP = "."  # LangGraph's PostgresStore joins namespace parts with "."


def _existing(conn: Connection, tables: tuple[str, ...]) -> list[str]:
    """The tables (of `tables`, in order) that exist in the current schema search path."""
    return [t for t in tables
            if conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (t,)).fetchone()["ok"]]


def delete_threads(conn: Connection, thread_ids: list[str]) -> int:
    """Delete every checkpoint, blob and pending write of the threads; returns the checkpoint rows deleted."""
    if not thread_ids:
        return 0
    deleted = 0
    for table in _existing(conn, CHECKPOINT_TABLES):  # table names come from the constant above, never input
        n = conn.execute(f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (list(thread_ids),)).rowcount
        if table == "checkpoints":
            deleted = n
    return deleted


def _like_literal(text: str) -> str:
    """Escape LIKE metacharacters so `text` matches literally (with ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def delete_store_namespace(conn: Connection, namespace: tuple[str, ...]) -> int:
    """Delete the store items of a namespace and of every namespace below it; returns the rows deleted.

    ("prefs", "u1") matches the prefixes "prefs.u1" and "prefs.u1.<anything>", never "prefs.u10".
    """
    if not namespace or not _existing(conn, (STORE_TABLE,)):
        return 0
    prefix = _NS_SEP.join(namespace)
    return conn.execute("DELETE FROM store WHERE prefix = %s OR prefix LIKE %s ESCAPE '\\'",
                        (prefix, _like_literal(prefix + _NS_SEP) + "%")).rowcount


def stale_thread_ids(conn: Connection, inactive_before: datetime, limit: int) -> list[str]:
    """Threads whose report was last active before `inactive_before`, or whose report no longer exists."""
    tables = _existing(conn, CHECKPOINT_TABLES)
    if not tables:
        return []
    threads = " UNION ".join(f"SELECT DISTINCT thread_id FROM {t}" for t in tables)
    rows = conn.execute(
        f"SELECT t.thread_id FROM ({threads}) t LEFT JOIN reports r ON r.id::text = t.thread_id "
        "WHERE r.id IS NULL OR r.last_activity_at < %s ORDER BY t.thread_id LIMIT %s",
        (inactive_before, limit)).fetchall()
    return [r["thread_id"] for r in rows]
