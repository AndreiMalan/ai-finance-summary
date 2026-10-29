"""Shared builders for retention and deletion tests: users, reports, files with blobs, LangGraph rows."""
import uuid
from datetime import datetime

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore

from statbrief.auth.users import create_user
from statbrief.domain.dataset import DatasetRef
from statbrief.domain.observation import Observation, UploadProvenance
from statbrief.storage.blobs import blob_key
from statbrief.storage.db import connect
from statbrief.storage.repos import FileRepo, ObservationRepo, ReportRepo

PW = "long-enough-pw"


class RecordingBlobs:
    """Wraps a blob store: records deletes, can fail chosen keys, and runs a hook before each delete."""

    def __init__(self, inner, fail: set[str] | None = None, before_delete=None):
        """Wrap `inner`; keys in `fail` raise on delete; `before_delete(key)` runs first."""
        self.inner, self.fail, self.before_delete, self.deleted = inner, set(fail or ()), before_delete, []

    def put(self, key, data, content_type):
        """Delegate."""
        return self.inner.put(key, data, content_type)

    def get(self, key):
        """Delegate."""
        return self.inner.get(key)

    def delete(self, key):
        """Fail for chosen keys, else record and delegate."""
        if self.before_delete:
            self.before_delete(key)
        if key in self.fail:
            raise RuntimeError("blob store down")
        self.deleted.append(key)
        self.inner.delete(key)


def user(pool, email: str):
    """Create a user."""
    return create_user(pool, email, PW, 10, 128)


def report(pool, user_id: str, last_activity: datetime | None = None, approved: datetime | None = None) -> str:
    """Create a report with the given activity / approval times; returns its id."""
    rid = str(ReportRepo(pool).create(user_id, "R")["id"])
    with pool.connection() as c:
        if last_activity is not None:
            c.execute("UPDATE reports SET last_activity_at = %s WHERE id = %s", (last_activity, rid))
        if approved is not None:
            c.execute("UPDATE reports SET approved_at = %s WHERE id = %s", (approved, rid))
    return rid


def file_with_blob(pool, blobs, user_id: str, report_id: str) -> DatasetRef:
    """A stored file (blob + row) with one observation; returns its ref."""
    fid = str(uuid.uuid4())
    key = blobs.put(blob_key(user_id, report_id, fid, "d.csv"), b"a,b\n1,2\n", "text/csv")
    ref = DatasetRef(file_id=fid, report_id=report_id, user_id=user_id, filename="d.csv", kind="csv",
                     size_bytes=8, sha256="h", blob_key=key)
    FileRepo(pool).add(ref)
    obs = Observation(obs_id=f"obs_{fid[:8]}", indicator_code="I", region_code="RO", region_level="country",
                      period="2024-01", frequency="M", value=1.0, unit="%",
                      provenance=UploadProvenance(file_id=fid, filename="d.csv", row_number=2, column="b"))
    ObservationRepo(pool).replace_for_file(user_id, report_id, fid, [obs])
    return ref


def add_thread(db_url: str, thread_id: str) -> None:
    """Write one real LangGraph checkpoint (with blobs) and a pending write for a thread."""
    with connect(db_url) as conn:
        saver = PostgresSaver(conn)
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        cp = empty_checkpoint()
        cp["channel_values"] = {"status": "collecting"}
        cp["channel_versions"] = {"status": 1}
        saved = saver.put(config, cp, {"source": "input", "step": 0}, {"status": 1})
        saver.put_writes(saved, [("status", "drafting")], task_id="task-1")


def thread_rows(db_url: str, thread_id: str) -> int:
    """Rows of a thread across the three checkpoint tables."""
    with connect(db_url) as conn:
        return sum(conn.execute(f"SELECT count(*) AS n FROM {t} WHERE thread_id = %s", (thread_id,)).fetchone()["n"]
                   for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"))


def put_pref(db_url: str, namespace: tuple[str, ...], key: str = "last") -> None:
    """Store one long-term memory item through LangGraph's PostgresStore."""
    with connect(db_url) as conn:
        PostgresStore(conn).put(namespace, key, {"regions": ["RO"]})


def store_prefixes(db_url: str) -> set[str]:
    """Every namespace prefix in the store table."""
    with connect(db_url) as conn:
        return {r["prefix"] for r in conn.execute("SELECT prefix FROM store")}
