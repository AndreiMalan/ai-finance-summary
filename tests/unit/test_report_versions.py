"""report_versions repository, approval marker and the export storage service (pgserver + local blobs)."""
from datetime import datetime, timezone

import pytest

from statbrief.auth.users import create_user
from statbrief.export.bundle import ReportBundle
from statbrief.export.format import ExportOptions
from statbrief.export.store import save_exports
from statbrief.storage.blobs import LocalBlobStore
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.report_versions import ExportFile, ReportVersionRepo, export_blob_key, mark_approved
from statbrief.storage.repos import ReportRepo

T = datetime(2025, 1, 20, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def ctx(settings, tmp_path):
    """Pool, local blob store, two users (alice, bob) and alice's report."""
    bootstrap(settings)
    pool = create_pool(settings)
    a = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    b = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    rep = ReportRepo(pool).create(a.id, "Inflation Q1")
    yield pool, LocalBlobStore(tmp_path), a, b, str(rep["id"])
    pool.close()


def _bundle(report_id: str) -> ReportBundle:
    """The example bundle for a given report id."""
    return ReportBundle.example().model_copy(update={"report_id": report_id})


def _file(fmt: str, key: str = "k") -> ExportFile:
    """An ExportFile record."""
    return ExportFile(fmt=fmt, blob_key=f"{key}.{fmt}", size_bytes=3, sha256="h")


def test_versions_are_allocated_per_report_and_user_scoped(ctx):
    """Versions count up per report; formats share a version; other users see nothing and cannot write."""
    pool, _, a, b, rid = ctx
    repo = ReportVersionRepo(pool)
    assert repo.add_version(a.id, rid, [_file("pdf"), _file("xlsx")]) == 1
    assert repo.add_version(a.id, rid, [_file("pdf", "k2")]) == 2
    assert [(v.version, v.fmt) for v in repo.list_for_report(a.id, rid)] == [(2, "pdf"), (1, "pdf"), (1, "xlsx")]
    assert repo.get(a.id, rid, 1, "xlsx").blob_key == "k.xlsx"
    assert repo.get(b.id, rid, 1, "xlsx") is None and repo.list_for_report(b.id, rid) == []
    assert repo.get(a.id, "not-a-uuid", 1, "pdf") is None
    with pytest.raises(PermissionError):
        repo.add_version(b.id, rid, [_file("pdf")])


@pytest.mark.parametrize("files", [[], ["pdf", "pdf"], ["docx"]])
def test_add_version_rejects_bad_format_lists(ctx, files):
    """No files, a repeated format or an unknown format are refused before writing."""
    pool, _, a, _, rid = ctx
    with pytest.raises(ValueError):
        ReportVersionRepo(pool).add_version(a.id, rid, [_file(f) for f in files])


def test_export_blob_key_is_owner_scoped_and_unique():
    """Export keys live under the owner's report prefix and never repeat."""
    k1, k2 = (export_blob_key("u1", "r1", "pdf") for _ in range(2))
    assert k1.startswith("users/u1/reports/r1/export-") and k1.endswith("/report.pdf") and k1 != k2
    with pytest.raises(ValueError):
        export_blob_key("u1", "r1", "exe")


def test_mark_approved_keeps_first_time_and_is_owner_only(ctx):
    """Approval time is set once (re-approval does not extend retention); other users cannot set it."""
    pool, _, a, b, rid = ctx
    assert mark_approved(pool, b.id, rid, T) is False
    assert mark_approved(pool, a.id, rid, T) is True
    assert mark_approved(pool, a.id, rid, datetime(2026, 1, 1, tzinfo=timezone.utc)) is True
    assert ReportRepo(pool).get(a.id, rid)["approved_at"] == T
    with pytest.raises(ValueError):
        mark_approved(pool, a.id, rid, datetime(2025, 1, 1))


def test_save_exports_stores_blobs_then_rows(ctx):
    """Both formats are built, stored privately and recorded as one version."""
    pool, blobs, a, _, rid = ctx
    version = save_exports(pool, blobs, a.id, _bundle(rid), ["pdf", "xlsx"], ExportOptions())
    rows = ReportVersionRepo(pool).list_for_report(a.id, rid)
    assert version == 1 and {r.fmt for r in rows} == {"pdf", "xlsx"}
    pdf = next(r for r in rows if r.fmt == "pdf")
    assert blobs.get(pdf.blob_key).startswith(b"%PDF") and pdf.size_bytes == len(blobs.get(pdf.blob_key))


def test_save_exports_cleans_up_blobs_when_recording_fails(ctx):
    """A user who does not own the report gets PermissionError and no blob is left behind."""
    pool, blobs, _, b, rid = ctx
    with pytest.raises(PermissionError):
        save_exports(pool, blobs, b.id, _bundle(rid), ["pdf", "xlsx"], ExportOptions())
    assert not [p for p in blobs.root.rglob("*") if p.is_file()]


def test_save_exports_rejects_unknown_format_before_upload(ctx):
    """Unknown formats fail fast."""
    pool, blobs, a, _, rid = ctx
    with pytest.raises(ValueError):
        save_exports(pool, blobs, a.id, _bundle(rid), ["pdf", "docx"], ExportOptions())
    assert not [p for p in blobs.root.rglob("*") if p.is_file()]
