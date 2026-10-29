"""Report and account deletion: Blob objects first, LangGraph threads and store namespace, cascade, retry."""
import pytest

from statbrief.export.bundle import ReportBundle
from statbrief.export.format import ExportOptions
from statbrief.export.store import save_exports
from statbrief.maintenance.deletion import DeletionIncomplete, delete_account, delete_report
from statbrief.storage.blobs import LocalBlobStore
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.report_versions import ReportVersionRepo
from tests.unit.maintenance_helpers import (RecordingBlobs, add_thread, file_with_blob, put_pref, report,
                                            store_prefixes, thread_rows, user)


@pytest.fixture
def ctx(settings, tmp_path):
    """Bootstrapped DB, pool, blob store; alice with two reports (files, an export, threads, prefs), bob."""
    bootstrap(settings)
    pool = create_pool(settings)
    blobs = LocalBlobStore(tmp_path)
    a, b = user(pool, "alice@example.com"), user(pool, "bob@example.com")
    r1, r2, rb = report(pool, a.id), report(pool, a.id), report(pool, b.id)
    files = [file_with_blob(pool, blobs, a.id, r) for r in (r1, r2)] + [file_with_blob(pool, blobs, b.id, rb)]
    bundle = ReportBundle.example().model_copy(update={"report_id": r1})
    save_exports(pool, blobs, a.id, bundle, ["pdf"], ExportOptions())
    for tid in (r1, r2, rb):
        add_thread(settings.database_url, tid)
    put_pref(settings.database_url, ("prefs", a.id))
    put_pref(settings.database_url, ("prefs", b.id))
    yield pool, blobs, settings.database_url, a, b, (r1, r2, rb), files
    pool.close()


def _stored(blobs) -> set[str]:
    """Keys of every stored blob."""
    return {p.relative_to(blobs.root).as_posix() for p in blobs.root.rglob("*") if p.is_file()}


def _count(pool, sql, *params) -> int:
    """count(*) of a query."""
    with pool.connection() as c:
        return c.execute(sql, params).fetchone()["n"]


def test_delete_account_removes_everything_of_the_user_only(ctx):
    """Blobs, threads, prefs namespace and rows of alice are gone; bob is untouched."""
    pool, blobs, url, a, b, (r1, r2, rb), files = ctx
    export_key = ReportVersionRepo(pool).list_for_report(a.id, r1)[0].blob_key
    assert delete_account(pool, blobs, a.id) is True
    assert _stored(blobs) == {files[2].blob_key}
    assert export_key not in _stored(blobs)
    assert thread_rows(url, r1) == thread_rows(url, r2) == 0 and thread_rows(url, rb) > 0
    assert store_prefixes(url) == {f"prefs.{b.id}"}
    assert _count(pool, "SELECT count(*) AS n FROM users WHERE id = %s", a.id) == 0
    for table in ("reports", "files", "observations", "report_versions"):
        assert _count(pool, f"SELECT count(*) AS n FROM {table} WHERE user_id = %s", a.id) == 0
    assert _count(pool, "SELECT count(*) AS n FROM reports WHERE user_id = %s", b.id) == 1
    assert delete_account(pool, blobs, a.id) is False


def test_store_namespace_prefix_is_exact(ctx):
    """Deleting ("prefs", id) never touches a namespace that merely starts with the same characters."""
    pool, blobs, url, a, b, _, _ = ctx
    put_pref(url, ("prefs", a.id + "0"))
    put_pref(url, ("prefs", a.id, "sub"))
    delete_account(pool, blobs, a.id)
    assert store_prefixes(url) == {f"prefs.{b.id}", f"prefs.{a.id}0"}


def test_blobs_are_deleted_before_any_row(ctx):
    """Every blob delete happens while the user row still exists."""
    pool, blobs, _, a, _, _, _ = ctx
    alive = []
    recording = RecordingBlobs(blobs, before_delete=lambda key: alive.append(
        _count(pool, "SELECT count(*) AS n FROM users WHERE id = %s", a.id)))
    delete_account(pool, recording, a.id)
    assert alive and set(alive) == {1} and len(recording.deleted) == 3  # two files + one export


def test_blob_failure_keeps_the_database_for_a_retry(ctx):
    """A failing blob delete raises DeletionIncomplete and removes no row; the retry completes."""
    pool, blobs, url, a, _, (r1, _, _), files = ctx
    with pytest.raises(DeletionIncomplete) as exc:
        delete_account(pool, RecordingBlobs(blobs, fail={files[0].blob_key}), a.id)
    assert exc.value.failed == 1
    assert _count(pool, "SELECT count(*) AS n FROM users WHERE id = %s", a.id) == 1
    assert thread_rows(url, r1) > 0
    assert delete_account(pool, blobs, a.id) is True
    assert _count(pool, "SELECT count(*) AS n FROM users WHERE id = %s", a.id) == 0


def test_delete_report_removes_only_that_report(ctx):
    """The report's blobs (file + export), thread and rows go; the user's other report stays."""
    pool, blobs, url, a, b, (r1, r2, rb), files = ctx
    assert delete_report(pool, blobs, a.id, r1) is True
    assert files[0].blob_key not in _stored(blobs) and files[1].blob_key in _stored(blobs)
    assert not [k for k in _stored(blobs) if k.endswith("report.pdf")]
    assert thread_rows(url, r1) == 0 and thread_rows(url, r2) > 0
    assert _count(pool, "SELECT count(*) AS n FROM reports WHERE id = %s", r1) == 0
    assert store_prefixes(url) == {f"prefs.{a.id}", f"prefs.{b.id}"}


def test_delete_report_of_another_user_does_nothing(ctx):
    """Bob cannot delete Alice's report: nothing matches and nothing is deleted."""
    pool, blobs, url, a, b, (r1, _, _), files = ctx
    before = _stored(blobs)
    assert delete_report(pool, blobs, b.id, r1) is False
    assert _stored(blobs) == before and thread_rows(url, r1) > 0


def test_purged_files_are_not_deleted_again(ctx):
    """Raw files already purged by retention have no blob left; deletion skips them."""
    pool, blobs, _, a, _, (r1, _, _), files = ctx
    blobs.delete(files[0].blob_key)
    with pool.connection() as c:
        c.execute("UPDATE files SET purged_at = now() WHERE id = %s", (files[0].file_id,))
    recording = RecordingBlobs(blobs)
    delete_report(pool, recording, a.id, r1)
    assert files[0].blob_key not in recording.deleted and len(recording.deleted) == 1  # only the export
