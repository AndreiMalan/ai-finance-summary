"""Tests for user-scoped repositories (AC 6)."""
import pytest

from statbrief.auth.users import create_user
from statbrief.domain.dataset import ColumnMapping, DatasetRef, ValidationIssue, ValidationReport
from statbrief.domain.observation import Observation, UploadProvenance
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.repos import (FileLimitReached, FileRepo, MappingRepo, ObservationRepo, ReportRepo,
                                     UploadGone, UploadRepo, ValidationRepo)


@pytest.fixture
def ctx(settings):
    """Pool + two users (alice, bob) and alice's report."""
    bootstrap(settings)
    pool = create_pool(settings)
    a = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    b = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    rep = ReportRepo(pool).create(a.id, "Inflation Q1")
    yield pool, a, b, rep
    pool.close()


def _ref(user, rep, fid="00000000-0000-0000-0000-000000000001"):
    """A DatasetRef for tests."""
    return DatasetRef(file_id=fid, report_id=str(rep["id"]), user_id=user.id, filename="f.csv", kind="csv",
                      size_bytes=3, sha256="h", blob_key="k")


def _obs(ref, n):
    """`n` monthly observations of one uploaded file."""
    prov = UploadProvenance(file_id=ref.file_id, filename="f.csv", row_number=2, column="v")
    return [Observation(obs_id=f"obs_{i}", indicator_code="I", region_code="RO", region_level="country",
                        period=f"2024-0{i}", frequency="M", value=float(i), unit="%", provenance=prov)
            for i in range(1, n + 1)]


def test_reports_are_user_scoped(ctx):
    """Bob can neither see nor touch Alice's report."""
    pool, a, b, rep = ctx
    repos = ReportRepo(pool)
    assert repos.get(a.id, str(rep["id"]))["title"] == "Inflation Q1"
    assert repos.get(b.id, str(rep["id"])) is None
    assert [r["title"] for r in repos.list_for_user(b.id)] == []


def test_report_touch_and_list_order(ctx):
    """touch() bumps only the owner's report; listing is most-recent first; malformed ids are not found."""
    pool, a, b, rep = ctx
    repos = ReportRepo(pool)
    second = repos.create(a.id, "GDP")
    with pool.connection() as c:  # make timestamps distinct regardless of clock resolution
        c.execute("UPDATE reports SET last_activity_at = now() - interval '1 hour' WHERE id = %s", (rep["id"],))
    assert [r["title"] for r in repos.list_for_user(a.id)] == ["GDP", "Inflation Q1"]
    before = repos.get(a.id, str(rep["id"]))["last_activity_at"]
    repos.touch(b.id, str(rep["id"]))
    assert repos.get(a.id, str(rep["id"]))["last_activity_at"] == before
    repos.touch(a.id, str(rep["id"]))
    assert repos.get(a.id, str(rep["id"]))["last_activity_at"] > before
    assert [r["title"] for r in repos.list_for_user(a.id)] == ["Inflation Q1", "GDP"]
    assert repos.get(a.id, "not-a-uuid") is None
    repos.touch(a.id, "not-a-uuid")  # no error
    assert second["status"] == "draft"


def test_files_mapping_validation_scoped(ctx):
    """File, mapping and validation rows are only visible to their owner."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    assert FileRepo(pool).get(a.id, ref.file_id) == ref and FileRepo(pool).get(b.id, ref.file_id) is None
    assert FileRepo(pool).count_for_report(a.id, ref.report_id) == 1
    m = ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "I", "unit": "%"})
    MappingRepo(pool).save(a.id, ref.file_id, m)
    assert MappingRepo(pool).get(a.id, ref.file_id) == m and MappingRepo(pool).get(b.id, ref.file_id) is None
    vr = ValidationReport(file_id=ref.file_id, row_count=1, issues=[ValidationIssue(
        issue_id="x:-", code="missing_value", severity="warning", rows=[2], message="m")])
    ValidationRepo(pool).save(a.id, vr)
    assert ValidationRepo(pool).get(a.id, ref.file_id) == vr and ValidationRepo(pool).get(b.id, ref.file_id) is None
    with pytest.raises(PermissionError):
        MappingRepo(pool).save(b.id, ref.file_id, m)
    with pytest.raises(PermissionError):
        ValidationRepo(pool).save(b.id, vr)


def test_mapping_and_validation_upsert(ctx):
    """Saving again replaces the stored mapping / validation report; malformed ids are not found."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    m1 = ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "I", "unit": "%"})
    m2 = m1.model_copy(update={"date_format": "DMY"})
    MappingRepo(pool).save(a.id, ref.file_id, m1)
    MappingRepo(pool).save(a.id, ref.file_id, m2)
    assert MappingRepo(pool).get(a.id, ref.file_id) == m2
    v1 = ValidationReport(file_id=ref.file_id, row_count=1, issues=[])
    v2 = ValidationReport(file_id=ref.file_id, row_count=7, issues=[])
    ValidationRepo(pool).save(a.id, v1)
    ValidationRepo(pool).save(a.id, v2)
    assert ValidationRepo(pool).get(a.id, ref.file_id) == v2
    assert MappingRepo(pool).get(a.id, "garbage") is None and ValidationRepo(pool).get(a.id, "garbage") is None
    with pytest.raises(PermissionError):
        MappingRepo(pool).save(a.id, "garbage", m1)


def test_file_add_requires_owned_report(ctx):
    """Bob cannot attach a file to Alice's report."""
    pool, a, b, rep = ctx
    with pytest.raises(PermissionError):
        FileRepo(pool).add(_ref(b, rep))
    assert FileRepo(pool).count_for_report(a.id, str(rep["id"])) == 0


def test_file_list_status_delete_scoped(ctx):
    """Listing, status updates and deletion only affect the owner's files."""
    pool, a, b, rep = ctx
    files = FileRepo(pool)
    r1, r2 = _ref(a, rep), _ref(a, rep, fid="00000000-0000-0000-0000-000000000002")
    files.add(r1)
    files.add(r2)
    assert files.list_for_report(a.id, r1.report_id) == [r1, r2]
    assert files.list_for_report(b.id, r1.report_id) == [] and files.count_for_report(b.id, r1.report_id) == 0
    files.set_status(b.id, r1.file_id, "hacked")
    files.set_status(a.id, r1.file_id, "validated")
    with pool.connection() as c:
        assert c.execute("SELECT status FROM files WHERE id = %s", (r1.file_id,)).fetchone()["status"] == "validated"
    assert files.delete(b.id, r1.file_id) is None
    assert files.delete(a.id, r1.file_id) == r1
    assert files.get(a.id, r1.file_id) is None and files.count_for_report(a.id, r1.report_id) == 1
    assert files.delete(a.id, "garbage") is None and files.get(a.id, "garbage") is None
    assert files.list_for_report(a.id, "garbage") == [] and files.count_for_report(a.id, "garbage") == 0


def test_chunked_upload_assembly(ctx):
    """Chunks arrive in any order, re-sends are idempotent, missing chunks are detected."""
    pool, a, b, rep = ctx
    up = UploadRepo(pool)
    uid = up.start(a.id, str(rep["id"]), "f.csv", total_bytes=6, chunk_count=3)
    up.put_chunk(a.id, uid, 2, b"ef")
    up.put_chunk(a.id, uid, 0, b"ab")
    with pytest.raises(ValueError, match="missing"):
        up.assemble(a.id, uid)
    up.put_chunk(a.id, uid, 1, b"cd")
    up.put_chunk(a.id, uid, 1, b"cd")
    assert up.assemble(a.id, uid) == b"abcdef"
    assert up.get(b.id, uid) is None
    with pytest.raises(PermissionError):
        up.put_chunk(b.id, uid, 0, b"zz")


def test_upload_scoping_bounds_and_delete(ctx):
    """Uploads need an owned report, reject out-of-range chunks, and delete only for the owner."""
    pool, a, b, rep = ctx
    up = UploadRepo(pool)
    with pytest.raises(PermissionError):
        up.start(b.id, str(rep["id"]), "f.csv", total_bytes=1, chunk_count=1)
    with pytest.raises(PermissionError):
        up.start(a.id, "garbage", "f.csv", total_bytes=1, chunk_count=1)
    uid = up.start(a.id, str(rep["id"]), "f.csv", total_bytes=2, chunk_count=1)
    for bad in (-1, 1):
        with pytest.raises(ValueError, match="range"):
            up.put_chunk(a.id, uid, bad, b"x")
    with pytest.raises(PermissionError):
        up.assemble(b.id, uid)
    up.delete(b.id, uid)
    assert up.get(a.id, uid) is not None
    up.delete(a.id, uid)
    assert up.get(a.id, uid) is None and up.get(a.id, "garbage") is None
    up.delete(a.id, "garbage")  # no error


def test_upload_size_mismatch_detected(ctx):
    """All chunks present but fewer/more bytes than declared is refused."""
    pool, a, b, rep = ctx
    up = UploadRepo(pool)
    uid = up.start(a.id, str(rep["id"]), "f.csv", total_bytes=5, chunk_count=2)
    up.put_chunk(a.id, uid, 0, b"ab")
    up.put_chunk(a.id, uid, 1, b"c")
    with pytest.raises(ValueError, match="size"):
        up.assemble(a.id, uid)


def test_observations_replace_and_list(ctx):
    """Re-normalizing a file replaces its observations; listing is user-scoped."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    obs = _obs(ref, 2)
    repo = ObservationRepo(pool)
    assert repo.replace_for_file(a.id, ref.report_id, ref.file_id, obs) == 2
    assert repo.replace_for_file(a.id, ref.report_id, ref.file_id, obs[:1]) == 1
    assert [o.obs_id for o in repo.list_for_report(a.id, ref.report_id)] == ["obs_1"]
    assert repo.list_for_report(b.id, ref.report_id) == []
    assert repo.list_for_report(a.id, ref.report_id)[0] == obs[0]
    assert repo.replace_for_file(a.id, ref.report_id, ref.file_id, []) == 0
    assert repo.list_for_report(a.id, ref.report_id) == [] and repo.list_for_report(a.id, "garbage") == []


def test_observations_replace_rejects_foreign_or_mismatched(ctx):
    """Bob cannot write Alice's file; a report_id that is not the file's report is refused."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    other = ReportRepo(pool).create(a.id, "Other")
    repo = ObservationRepo(pool)
    repo.replace_for_file(a.id, ref.report_id, ref.file_id, _obs(ref, 1))
    with pytest.raises(PermissionError):
        repo.replace_for_file(b.id, ref.report_id, ref.file_id, _obs(ref, 2))
    with pytest.raises(PermissionError):
        repo.replace_for_file(a.id, str(other["id"]), ref.file_id, _obs(ref, 2))
    with pytest.raises(PermissionError):
        repo.replace_for_file(a.id, ref.report_id, "garbage", _obs(ref, 2))
    assert len(repo.list_for_report(a.id, ref.report_id)) == 1
    assert repo.list_for_report(a.id, str(other["id"])) == []


def test_observations_replace_is_atomic(ctx):
    """A failing insert leaves the previous observations in place."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    repo = ObservationRepo(pool)
    repo.replace_for_file(a.id, ref.report_id, ref.file_id, _obs(ref, 2))
    dup = _obs(ref, 1) * 2  # duplicate primary key -> insert fails mid-way
    with pytest.raises(Exception):
        repo.replace_for_file(a.id, ref.report_id, ref.file_id, dup)
    assert [o.obs_id for o in repo.list_for_report(a.id, ref.report_id)] == ["obs_1", "obs_2"]


def test_upload_bounds_enforced(ctx):
    """Non-positive sizes are refused (repo and DB CHECK); a chunk larger than the whole upload is refused."""
    import psycopg

    pool, a, b, rep = ctx
    up = UploadRepo(pool)
    for total, count in ((0, 1), (1, 0), (-5, 1)):
        with pytest.raises(ValueError):
            up.start(a.id, str(rep["id"]), "f.csv", total_bytes=total, chunk_count=count)
    with pool.connection() as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO uploads (user_id, report_id, filename, total_bytes, chunk_count) "
                  "VALUES (%s, %s, 'f', 0, 1)", (a.id, rep["id"]))
    uid = up.start(a.id, str(rep["id"]), "f.csv", total_bytes=3, chunk_count=2)
    with pytest.raises(ValueError, match="larger"):
        up.put_chunk(a.id, uid, 0, b"abcd")
    up.put_chunk(a.id, uid, 0, b"abc")  # exactly total_bytes is fine


def test_put_chunk_racing_delete_is_permission_error(ctx, settings):
    """A chunk racing a concurrent upload delete is PermissionError, never a foreign-key error."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from statbrief.storage.db import connect

    pool, a, b, rep = ctx
    up = UploadRepo(pool)
    uid = up.start(a.id, str(rep["id"]), "f.csv", total_bytes=2, chunk_count=1)
    with connect(settings.database_url) as deleter, ThreadPoolExecutor(max_workers=1) as ex:
        with deleter.transaction():
            deleter.execute("DELETE FROM uploads WHERE id = %s", (uid,))  # row locked, not yet committed
            started = threading.Event()

            def _put():
                """Send a chunk while the delete is in flight."""
                started.set()
                up.put_chunk(a.id, uid, 0, b"ab")

            fut = ex.submit(_put)
            started.wait()
            with pytest.raises(TimeoutError):
                fut.result(timeout=0.5)  # blocked on the upload row lock
        with pytest.raises(PermissionError):
            fut.result(timeout=10)
    with pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM upload_chunks").fetchone()["n"] == 0


def test_concurrent_replace_for_same_file_serialises(ctx):
    """Two overlapping re-normalisations of one file both succeed; the later one wins entirely (no merge)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    repo = ObservationRepo(pool)
    set1 = _obs(ref, 3)
    prov = set1[0].provenance
    set2 = [Observation(obs_id=f"other_{i}", indicator_code="J", region_code="RO", region_level="country",
                        period=f"2024-0{i}", frequency="M", value=float(i), unit="%", provenance=prov)
            for i in range(1, 5)]
    release, inside = threading.Event(), threading.Event()

    class _Paused(list):
        """Observation list whose iteration (i.e. the INSERT) waits until released, mid-transaction."""

        def __iter__(self):
            """Signal that the first replace is inside its transaction, then wait for the release."""
            inside.set()
            assert release.wait(10)
            return super().__iter__()

    with ThreadPoolExecutor(max_workers=2) as ex:
        first = ex.submit(repo.replace_for_file, a.id, ref.report_id, ref.file_id, _Paused(set1))
        assert inside.wait(10)
        second = ex.submit(repo.replace_for_file, a.id, ref.report_id, ref.file_id, set2)
        with pytest.raises(TimeoutError):
            second.result(timeout=0.5)  # waits on the file row lock held by the first replace
        release.set()
        assert (first.result(timeout=10), second.result(timeout=10)) == (3, 4)
    assert sorted(o.obs_id for o in repo.list_for_report(a.id, ref.report_id)) == sorted(o.obs_id for o in set2)


def _staged(pool, user, rep, n=1):
    """Open `n` uploads for the report; return their ids."""
    return [UploadRepo(pool).start(user.id, str(rep["id"]), "f.csv", total_bytes=3, chunk_count=1)
            for _ in range(n)]


def test_count_open_uploads(ctx):
    """Open (staged) uploads are counted per user and report."""
    pool, a, b, rep = ctx
    _staged(pool, a, rep, 2)
    assert UploadRepo(pool).count_open(a.id, str(rep["id"])) == 2
    assert UploadRepo(pool).count_open(b.id, str(rep["id"])) == 0
    assert UploadRepo(pool).count_open(a.id, "garbage") == 0


def test_add_from_upload_consumes_upload_and_enforces_limit(ctx):
    """The file row replaces the upload atomically; the limit, a gone upload and foreign reports are refused."""
    pool, a, b, rep = ctx
    files = FileRepo(pool)
    u1, u2 = _staged(pool, a, rep, 2)
    files.add_from_upload(_ref(a, rep), u1, max_files=1)
    assert UploadRepo(pool).get(a.id, u1) is None and files.count_for_report(a.id, str(rep["id"])) == 1
    with pytest.raises(UploadGone):
        files.add_from_upload(_ref(a, rep, fid="00000000-0000-0000-0000-000000000002"), u1, max_files=5)
    with pytest.raises(FileLimitReached):
        files.add_from_upload(_ref(a, rep, fid="00000000-0000-0000-0000-000000000003"), u2, max_files=1)
    assert UploadRepo(pool).get(a.id, u2) is not None  # rolled back: still staged
    with pytest.raises(PermissionError):
        files.add_from_upload(_ref(b, rep, fid="00000000-0000-0000-0000-000000000004"), u2, max_files=5)


def _race(pool, settings, rep, jobs):
    """Run `jobs` in threads while the report row is locked, then release; return results/exceptions."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from statbrief.storage.db import connect

    with connect(settings.database_url) as locker, ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        with locker.transaction():
            locker.execute("SELECT 1 FROM reports WHERE id = %s FOR UPDATE", (rep["id"],))
            futs = [ex.submit(j) for j in jobs]
            time.sleep(0.3)  # both completions are now waiting on the report lock
            assert not any(f.done() for f in futs)
        out = []
        for f in futs:
            try:
                out.append(f.result(timeout=10))
            except Exception as exc:  # noqa: BLE001 - collected for assertions
                out.append(exc)
    return out


def test_concurrent_completes_respect_file_limit(ctx, settings):
    """Two uploads completed at the same time with a limit of 1: exactly one file is stored."""
    pool, a, b, rep = ctx
    u1, u2 = _staged(pool, a, rep, 2)
    files = FileRepo(pool)
    out = _race(pool, settings, rep, [
        lambda: files.add_from_upload(_ref(a, rep), u1, max_files=1),
        lambda: files.add_from_upload(_ref(a, rep, fid="00000000-0000-0000-0000-000000000002"), u2, max_files=1)])
    assert sum(r is None for r in out) == 1 and sum(isinstance(r, FileLimitReached) for r in out) == 1
    assert files.count_for_report(a.id, str(rep["id"])) == 1


def test_same_upload_completed_twice_stores_one_file(ctx, settings):
    """Completing one upload twice concurrently stores one file; the other completion finds it gone."""
    pool, a, b, rep = ctx
    (u1,) = _staged(pool, a, rep)
    files = FileRepo(pool)
    out = _race(pool, settings, rep, [
        lambda: files.add_from_upload(_ref(a, rep), u1, max_files=5),
        lambda: files.add_from_upload(_ref(a, rep, fid="00000000-0000-0000-0000-000000000002"), u1, max_files=5)])
    assert sum(r is None for r in out) == 1 and sum(isinstance(r, UploadGone) for r in out) == 1
    assert files.count_for_report(a.id, str(rep["id"])) == 1


def test_saving_mapping_invalidates_validation_and_observations(ctx):
    """A new mapping drops the stored validation report and the file's observations; status back to mapped."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    m = ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "I", "unit": "%"})
    MappingRepo(pool).save(a.id, ref.file_id, m)
    ValidationRepo(pool).save(a.id, ValidationReport(file_id=ref.file_id, row_count=1))
    ObservationRepo(pool).replace_for_file(a.id, ref.report_id, ref.file_id, _obs(ref, 2))
    FileRepo(pool).set_status(a.id, ref.file_id, "normalized")
    MappingRepo(pool).save(a.id, ref.file_id, m.model_copy(update={"date_format": "DMY"}))
    assert ValidationRepo(pool).get(a.id, ref.file_id) is None
    assert ObservationRepo(pool).list_for_report(a.id, ref.report_id) == []
    with pool.connection() as c:
        assert c.execute("SELECT status FROM files WHERE id = %s", (ref.file_id,)).fetchone()["status"] == "mapped"


def _api_obs(oid, value, region="RO", ind="HICP"):
    """One API observation (file-less row)."""
    from datetime import datetime, timezone

    from statbrief.domain.observation import ApiProvenance
    prov = ApiProvenance(api_name="eurostat", series_id="prc?x=1", query_url="https://ec.europa.eu/x",
                         retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return Observation(obs_id=oid, indicator_code=ind, region_code=region, region_level="country", period="2024-01",
                       frequency="M", value=value, unit="%", provenance=prov)


def test_api_observations_upsert_scoped_and_never_overwrite_uploads(ctx):
    """API rows are stored without a file, replaced on re-store, never overwrite an upload row, owner only."""
    pool, a, b, rep = ctx
    repo, rid = ObservationRepo(pool), str(rep["id"])
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    upload = _obs(ref, 1)
    repo.replace_for_file(a.id, rid, ref.file_id, upload)
    assert repo.upsert_api_observations(a.id, rid, [_api_obs("api_1", 2.0), _api_obs("api_2", 3.0, region="HU")]) == 2
    repo.upsert_api_observations(a.id, rid, [_api_obs("api_1", 2.5)])
    repo.upsert_api_observations(a.id, rid, [_api_obs(upload[0].obs_id, 99.0)])  # same id as the uploaded row
    by_id = {o.obs_id: o for o in repo.list_for_report(a.id, rid)}
    assert by_id["api_1"].value == 2.5 and by_id[upload[0].obs_id].value == upload[0].value
    assert [o.obs_id for o in repo.list_for_series(a.id, rid, ["HICP"], "HU")] == ["api_2"]
    assert repo.list_for_series(b.id, rid, ["HICP"], "HU") == []
    with pytest.raises(PermissionError):
        repo.upsert_api_observations(b.id, rid, [_api_obs("api_3", 1.0)])
    with pytest.raises(ValueError):
        repo.upsert_api_observations(a.id, rid, upload)
