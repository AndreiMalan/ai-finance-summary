"""Tests for blob stores (no network: the Vercel SDK is replaced by a fake)."""
from types import SimpleNamespace

import pytest

from statbrief.storage import blobs
from statbrief.storage.blobs import LocalBlobStore, VercelBlobStore, blob_key, get_blob_store


def test_local_roundtrip(tmp_path):
    """Local store writes, reads and deletes; keys cannot escape the root."""
    s = LocalBlobStore(tmp_path)
    k = s.put("users/u/reports/r/f/data.csv", b"abc", "text/csv")
    assert s.get(k) == b"abc"
    s.delete(k)
    with pytest.raises(FileNotFoundError):
        s.get(k)
    s.delete(k)  # absent: no error
    with pytest.raises(ValueError):
        s.put("../escape.txt", b"x", "text/plain")


@pytest.mark.parametrize("key", ["C:/x", "C:x", "..\\x", "/abs", "\\abs", "a/../../x", "a/./b", "a//b", "",
                                 "users/u/../../x", "\\\\server\\share\\x", "a/b/", "users/CON", "a/nul.csv"])
def test_local_refuses_unsafe_keys(tmp_path, key):
    """Absolute paths, drive letters, backslashes and dot/empty segments are refused on every OS."""
    s = LocalBlobStore(tmp_path / "root")
    for op in (lambda: s.put(key, b"x", "text/plain"), lambda: s.get(key), lambda: s.delete(key)):
        with pytest.raises(ValueError):
            op()
    assert not (tmp_path / "x").exists()


def test_blob_key_sanitises_filename():
    """User filenames cannot inject path segments."""
    assert blob_key("u", "r", "f", "../../evil name.csv") == "users/u/reports/r/f/evil_name.csv"


@pytest.mark.parametrize("filename,expected", [
    ("C:\\Users\\me\\data.csv", "data.csv"),
    ("a:b?c*.csv", "a_b_c_.csv"),
    ("CON", "_CON"),
    ("nul.csv", "_nul.csv"),
    ("com1.txt.csv", "_com1.txt.csv"),
    ("LPT9", "_LPT9"),
    ("console.csv", "console.csv"),
    ("...", "upload"),
    ("", "upload"),
    ("data.csv. ", "data.csv"),
])
def test_blob_key_windows_safe(filename, expected):
    """Windows-reserved device names and characters never reach the key."""
    assert blob_key("u", "r", "f", filename) == f"users/u/reports/r/f/{expected}"


def test_blob_key_caps_long_names_keeping_extension():
    """Very long filenames are truncated but keep their extension."""
    name = blob_key("u", "r", "f", "a" * 500 + ".xlsx").rsplit("/", 1)[-1]
    assert len(name) == blobs._MAX_NAME_LEN and name.endswith(".xlsx")


@pytest.mark.parametrize("bad",["../x", "a/b", "", "a\\b", "c:", ".."])
def test_blob_key_rejects_unsafe_ids(bad):
    """Owner/report/file ids must be plain identifiers (they become path segments)."""
    for args in ((bad, "r", "f"), ("u", bad, "f"), ("u", "r", bad)):
        with pytest.raises(ValueError):
            blob_key(*args, "x.csv")


def test_vercel_store_uses_private_access(monkeypatch):
    """The Vercel store always uploads/downloads with access='private'."""
    calls = []
    fake = SimpleNamespace(
        put_blob=lambda key, data, **kw: calls.append(("put", key, kw)) or SimpleNamespace(pathname=key + "-x1"),
        get_blob=lambda key, **kw: calls.append(("get", key, kw)) or SimpleNamespace(body=b"data"),
        delete_blob=lambda key, **kw: calls.append(("delete", key, kw)),
    )
    monkeypatch.setattr(blobs, "_vercel_sync", lambda: fake)
    s = VercelBlobStore()
    assert s.put("users/u/x.csv", b"d", "text/csv") == "users/u/x.csv-x1"
    assert s.get("users/u/x.csv-x1") == b"data"
    s.delete("users/u/x.csv-x1")
    assert calls[0][2]["access"] == "private" and calls[0][2]["add_random_suffix"] is True
    assert calls[0][2]["content_type"] == "text/csv"
    assert calls[1][2]["access"] == "private"
    assert calls[2][:2] == ("delete", "users/u/x.csv-x1")


def test_vercel_missing_blob_is_file_not_found(monkeypatch):
    """The SDK's not-found error satisfies the BlobStore contract (FileNotFoundError)."""
    from vercel.blob import BlobNotFoundError

    def _missing(key, **kw):
        """Simulate a 404 from the Blob API."""
        raise BlobNotFoundError()

    monkeypatch.setattr(blobs, "_vercel_sync", lambda: SimpleNamespace(get_blob=_missing))
    with pytest.raises(FileNotFoundError):
        VercelBlobStore().get("users/u/nope")


def test_vercel_sync_module_is_the_sdk():
    """The lazy import resolves the installed SDK's sync API (no network)."""
    sync = blobs._vercel_sync()
    assert all(hasattr(sync, n) for n in ("put_blob", "get_blob", "delete_blob"))


def test_get_blob_store_by_setting(settings, tmp_path):
    """The backend setting selects the implementation."""
    assert isinstance(get_blob_store(settings.model_copy(update={"local_blob_dir": str(tmp_path)})), LocalBlobStore)
    assert isinstance(get_blob_store(settings.model_copy(update={"blob_backend": "vercel"})), VercelBlobStore)
    assert settings.blob_backend == "local" and settings.local_blob_dir == ".local_blobs"


def test_vercel_delete_absent_is_silent(monkeypatch):
    """Deleting a missing blob is not an error (BlobStore contract)."""
    from vercel.blob import BlobNotFoundError

    def _missing(key, **kw):
        """Simulate a 404 from the Blob API."""
        raise BlobNotFoundError()

    monkeypatch.setattr(blobs, "_vercel_sync", lambda: SimpleNamespace(delete_blob=_missing))
    VercelBlobStore().delete("users/u/nope")


def test_local_list_by_prefix_with_upload_time(tmp_path):
    """list(prefix) yields every key under the prefix with a timezone-aware upload time."""
    from datetime import timezone

    store = LocalBlobStore(tmp_path)
    store.put("users/u1/reports/r1/f1/a.csv", b"1", "text/csv")
    store.put("users/u1/reports/r2/f2/b.csv", b"2", "text/csv")
    store.put("users/u10/reports/r3/f3/c.csv", b"3", "text/csv")
    listed = list(store.list("users/u1/"))
    assert sorted(b.key for b in listed) == ["users/u1/reports/r1/f1/a.csv", "users/u1/reports/r2/f2/b.csv"]
    assert all(b.uploaded_at.tzinfo is not None for b in listed)
    assert [b.key for b in store.list("users/u1/reports/r2/")] == ["users/u1/reports/r2/f2/b.csv"]
    assert list(store.list("users/none/")) == []


def test_vercel_list_uses_sdk_query_with_prefix(monkeypatch):
    """VercelBlobStore.list follows the SDK's paginated query_blobs with the literal prefix."""
    from datetime import datetime, timezone

    seen = {}

    def query_blobs(prefix=None, page_size=None, cursor=None):
        """Fake SDK listing."""
        seen["prefix"] = prefix
        return iter([SimpleNamespace(pathname="users/u1/x-abc.csv", uploaded_at=datetime(2026, 1, 1)),
                     SimpleNamespace(pathname="users/u1/y.csv", uploaded_at=datetime(2026, 1, 2, tzinfo=timezone.utc))])

    monkeypatch.setattr(blobs, "_vercel_sync", lambda: SimpleNamespace(query_blobs=query_blobs))
    listed = list(VercelBlobStore().list("users/u1/"))
    assert seen["prefix"] == "users/u1/" and [b.key for b in listed] == ["users/u1/x-abc.csv", "users/u1/y.csv"]
    assert all(b.uploaded_at.tzinfo is not None for b in listed)  # naive SDK times are read as UTC


def test_owner_prefix():
    """owner_prefix scopes to a user or one report and validates ids like blob_key."""
    from statbrief.storage.blobs import owner_prefix

    assert owner_prefix("u1") == "users/u1/"
    assert owner_prefix("u1", "r1") == "users/u1/reports/r1/"
    with pytest.raises(ValueError):
        owner_prefix("../x")
