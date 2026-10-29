"""Retention rules with a fake clock (D-009, AC 50): raw files, checkpoints, logs, abandoned uploads."""
from datetime import datetime, timedelta, timezone

import pytest

from statbrief.maintenance import retention
from statbrief.maintenance.retention import cleanup_expired
from statbrief.storage.blobs import LocalBlobStore
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.repos import ObservationRepo
from tests.unit.maintenance_helpers import (RecordingBlobs, add_thread, file_with_blob, report, thread_rows,
                                            user)

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


def days(n: float) -> datetime:
    """NOW minus n days."""
    return NOW - timedelta(days=n)


@pytest.fixture
def ctx(settings, tmp_path):
    """Bootstrapped DB (migrations + LangGraph tables), pool, local blob store and a user."""
    bootstrap(settings)
    pool = create_pool(settings)
    yield pool, LocalBlobStore(tmp_path), settings, user(pool, "a@example.com")
    pool.close()


def _purged(pool, file_id: str) -> bool:
    """Whether the file row is marked purged."""
    with pool.connection() as c:
        return c.execute("SELECT purged_at IS NOT NULL AS p FROM files WHERE id = %s", (file_id,)).fetchone()["p"]


def _exists(blobs, key: str) -> bool:
    """Whether the blob is still stored."""
    try:
        blobs.get(key)
        return True
    except FileNotFoundError:
        return False


def test_raw_files_of_unfinished_reports_expire_30_days_after_last_activity(ctx):
    """31 days inactive → blob deleted and row marked purged; 29 days → kept; observations always kept."""
    pool, blobs, s, a = ctx
    old = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(31)))
    recent = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(29)))
    result = cleanup_expired(pool, blobs, s, NOW)
    assert (result.raw_files_purged, result.raw_files_failed) == (1, 0)
    assert not _exists(blobs, old.blob_key) and _purged(pool, old.file_id)
    assert _exists(blobs, recent.blob_key) and not _purged(pool, recent.file_id)
    assert len(ObservationRepo(pool).list_for_report(a.id, old.report_id)) == 1  # provenance outlives the file


def test_raw_files_of_approved_reports_expire_7_days_after_approval(ctx):
    """Approval starts a 7-day clock that replaces the inactivity rule."""
    pool, blobs, s, a = ctx
    expired = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(8), approved=days(8)))
    # long inactive but approved recently: the approval rule applies, so the file is kept
    kept = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(40), approved=days(6)))
    cleanup_expired(pool, blobs, s, NOW)
    assert _purged(pool, expired.file_id) and not _exists(blobs, expired.blob_key)
    assert not _purged(pool, kept.file_id) and _exists(blobs, kept.blob_key)


def test_retention_periods_come_from_settings(ctx):
    """RETENTION_UNFINISHED_DAYS is honoured."""
    pool, blobs, s, a = ctx
    ref = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(3)))
    cleanup_expired(pool, blobs, s.model_copy(update={"retention_unfinished_days": 2}), NOW)
    assert _purged(pool, ref.file_id)


def test_blob_is_deleted_before_the_row_is_marked(ctx):
    """At the moment the blob is deleted, the row is still unmarked (Blob first, then DB)."""
    pool, blobs, s, a = ctx
    ref = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(31)))
    seen = []
    recording = RecordingBlobs(blobs, before_delete=lambda key: seen.append(_purged(pool, ref.file_id)))
    cleanup_expired(pool, recording, s, NOW)
    assert seen == [False] and _purged(pool, ref.file_id)


def test_blob_failure_leaves_the_row_for_a_retry(ctx):
    """A failing blob delete keeps the row unmarked; the next run purges it."""
    pool, blobs, s, a = ctx
    ref = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(31)))
    first = cleanup_expired(pool, RecordingBlobs(blobs, fail={ref.blob_key}), s, NOW)
    assert (first.raw_files_purged, first.raw_files_failed) == (0, 1) and not _purged(pool, ref.file_id)
    second = cleanup_expired(pool, blobs, s, NOW)
    assert second.raw_files_purged == 1 and _purged(pool, ref.file_id)
    assert cleanup_expired(pool, blobs, s, NOW).raw_files_purged == 0  # idempotent


def test_checkpoints_expire_30_days_after_last_activity_and_with_their_report(ctx, db_url):
    """Threads of reports inactive > 30 d and threads without a report are deleted; others kept."""
    pool, blobs, s, a = ctx
    stale, active = report(pool, a.id, last_activity=days(31)), report(pool, a.id, last_activity=days(29))
    orphan = "00000000-0000-0000-0000-00000000dead"
    for tid in (stale, active, orphan):
        add_thread(db_url, tid)
    assert thread_rows(db_url, stale) >= 2  # checkpoint + pending write (primitives may be stored inline)
    result = cleanup_expired(pool, blobs, s, NOW)
    assert result.threads_deleted == 2
    assert thread_rows(db_url, stale) == 0 and thread_rows(db_url, orphan) == 0 and thread_rows(db_url, active) >= 2


def test_logs_expire_after_14_days(ctx):
    """run_events, login_attempts and usage_events older than 14 days are deleted; newer ones kept."""
    pool, blobs, s, a = ctx
    rid = report(pool, a.id)
    with pool.connection() as c:
        for ts in (days(15), days(13)):
            c.execute("INSERT INTO run_events (report_id, user_id, ts, kind) VALUES (%s, %s, %s, 'node')",
                      (rid, a.id, ts))
            c.execute("INSERT INTO login_attempts (email, ip, succeeded, at) VALUES ('a@example.com', '1.1.1.1', "
                      "false, %s)", (ts,))
            c.execute("INSERT INTO usage_events (user_id, kind, at) VALUES (%s, 'llm_call', %s)", (a.id, ts))
    result = cleanup_expired(pool, blobs, s, NOW)
    assert (result.run_events_deleted, result.login_attempts_deleted, result.usage_events_deleted) == (1, 1, 1)
    with pool.connection() as c:
        for table in ("run_events", "login_attempts", "usage_events"):
            assert c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 1


def test_abandoned_uploads_are_purged_after_upload_abandon_hours(ctx):
    """Staged uploads older than UPLOAD_ABANDON_HOURS (24) are deleted with their chunks."""
    pool, blobs, s, a = ctx
    rid = report(pool, a.id)
    with pool.connection() as c:
        for hours in (25, 23):
            up = c.execute("INSERT INTO uploads (user_id, report_id, filename, total_bytes, chunk_count, created_at) "
                           "VALUES (%s, %s, 'f.csv', 3, 1, %s) RETURNING id",
                           (a.id, rid, NOW - timedelta(hours=hours))).fetchone()["id"]
            c.execute("INSERT INTO upload_chunks (upload_id, idx, data) VALUES (%s, 0, 'abc')", (up,))
    assert cleanup_expired(pool, blobs, s, NOW).uploads_deleted == 1
    with pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM uploads").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM upload_chunks").fetchone()["n"] == 1


def test_batch_size_bounds_each_run(ctx):
    """At most RETENTION_BATCH_SIZE files per run; the rest are handled by later runs."""
    pool, blobs, s, a = ctx
    rid = report(pool, a.id, last_activity=days(31))
    for _ in range(3):
        file_with_blob(pool, blobs, a.id, rid)
    small = s.model_copy(update={"retention_batch_size": 2})
    assert cleanup_expired(pool, blobs, small, NOW).raw_files_purged == 2
    assert cleanup_expired(pool, blobs, small, NOW).raw_files_purged == 1


def test_a_failing_rule_does_not_stop_the_others(ctx, monkeypatch):
    """A rule that raises is reported in failed_rules; the remaining rules still run."""
    pool, blobs, s, a = ctx
    ref = file_with_blob(pool, blobs, a.id, report(pool, a.id, last_activity=days(31)))
    monkeypatch.setattr(retention, "purge_logs", lambda *args: (_ for _ in ()).throw(RuntimeError("db")))
    result = cleanup_expired(pool, blobs, s, NOW)
    assert result.failed_rules == ["logs"] and result.raw_files_purged == 1 and _purged(pool, ref.file_id)


def test_naive_clock_rejected(ctx):
    """`now` must be timezone-aware."""
    pool, blobs, s, _ = ctx
    with pytest.raises(ValueError):
        cleanup_expired(pool, blobs, s, datetime(2026, 1, 1))
