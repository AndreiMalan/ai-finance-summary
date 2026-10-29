"""Blob storage for uploaded files: local filesystem (dev/tests) or private Vercel Blob (production, OIDC).

Keys are owner-scoped (`users/{user_id}/reports/{report_id}/{file_id}/{name}`) and built only by `blob_key`,
which never lets user input contribute path segments, drive letters or Windows device names (D-021).
Both backends can list keys by prefix (`list`), which account deletion and the orphan sweep of the
retention job use to find blobs no database row references any more.
"""
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from statbrief.config import Settings

# Characters kept in a user-supplied filename; everything else becomes "_" (protocol, not a tunable).
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
# Server-generated ids (uuids) used as key segments.
_ID = re.compile(r"[A-Za-z0-9_-]+")
# Longest sanitised filename kept in a key (well below per-segment filesystem limits of 255).
_MAX_NAME_LEN = 100
_FALLBACK_NAME = "upload"
# Windows device names: reserved with any extension ("nul.csv") and in any case.
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})


USERS_PREFIX = "users/"  # every key starts here (blob_key)


@dataclass(frozen=True)
class StoredBlob:
    """One listed blob: its key and when it was stored (timezone-aware)."""

    key: str
    uploaded_at: datetime


def _aware(dt: datetime) -> datetime:
    """`dt` with UTC assumed when it carries no timezone."""
    return dt if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None else dt.replace(tzinfo=timezone.utc)


class BlobStore(Protocol):
    """Minimal private object store."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return the key to read them back."""

    def get(self, key: str) -> bytes:
        """Read stored bytes (FileNotFoundError when absent)."""

    def delete(self, key: str) -> None:
        """Delete stored bytes (no error when absent)."""

    def list(self, prefix: str) -> Iterator[StoredBlob]:
        """Every stored blob whose key starts with `prefix`."""


def _is_reserved(segment: str) -> bool:
    """True when a path segment is a Windows device name (CON, nul.csv, COM1.txt, ...)."""
    return segment.split(".", 1)[0].upper() in _RESERVED


def _safe_filename(filename: str) -> str:
    """Last path component of `filename`, reduced to safe characters, never a device name, never empty."""
    name = _SAFE.sub("_", filename.replace("\\", "/").rsplit("/", 1)[-1]).strip("._") or _FALLBACK_NAME
    if len(name) > _MAX_NAME_LEN:
        stem, dot, ext = name.rpartition(".")
        suffix = f".{ext}" if dot and stem and len(ext) < _MAX_NAME_LEN // 2 else ""
        name = name[:_MAX_NAME_LEN - len(suffix)] + suffix
    return f"_{name}" if _is_reserved(name) else name


def blob_key(user_id: str, report_id: str, file_id: str, filename: str) -> str:
    """Owner-scoped key with a sanitised filename (no path segments from user input).

    Raises ValueError when an id is not a plain identifier (ids become path segments verbatim).
    """
    for value in (user_id, report_id, file_id):
        if not _ID.fullmatch(str(value)):
            raise ValueError("blob key ids must be plain identifiers")
    return f"{owner_prefix(user_id, report_id)}{file_id}/{_safe_filename(filename)}"


def owner_prefix(user_id: str, report_id: str | None = None) -> str:
    """Key prefix of everything a user (or one of their reports) stores: "users/{u}/[reports/{r}/]".

    Raises ValueError when an id is not a plain identifier.
    """
    for value in (user_id, report_id) if report_id is not None else (user_id,):
        if not _ID.fullmatch(str(value)):
            raise ValueError("blob key ids must be plain identifiers")
    return f"{USERS_PREFIX}{user_id}/" + (f"reports/{report_id}/" if report_id is not None else "")


class LocalBlobStore:
    """Files under a root directory; keys may not escape it (checked lexically and after resolving)."""

    def __init__(self, root: Path):
        """Use (and create) `root`."""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Absolute path for a key, refusing traversal, absolute paths, drive letters and device names."""
        segments = key.split("/")
        if (not key or "\\" in key or ":" in key or "\0" in key
                or any(s in ("", ".", "..") or _is_reserved(s) for s in segments)):
            raise ValueError("unsafe blob key")
        p = self.root.joinpath(*segments).resolve()
        if self.root not in p.parents:
            raise ValueError("blob key escapes the storage root")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Write bytes to disk (content_type is not stored locally)."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        """Read bytes from disk (FileNotFoundError when absent)."""
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        """Remove the file if present."""
        self._path(key).unlink(missing_ok=True)

    def list(self, prefix: str) -> Iterator[StoredBlob]:
        """Stored files whose key starts with `prefix` (sorted), with their modification time."""
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield StoredBlob(key, datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))


def _vercel_sync():
    """The synchronous Vercel Blob SDK module (imported lazily so dev/tests never need it loaded)."""
    from vercel.blob import sync

    return sync


class VercelBlobStore:
    """Private Vercel Blob store; credentials resolved by the SDK (BLOB_STORE_ID + Vercel OIDC).

    The SDK's BlobNotFoundError subclasses FileNotFoundError, which satisfies the BlobStore contract.
    """

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Upload privately with a random suffix; return the stored pathname."""
        result = _vercel_sync().put_blob(key, data, access="private", content_type=content_type,
                                         add_random_suffix=True)
        return result.pathname

    def get(self, key: str) -> bytes:
        """Download a private blob."""
        return _vercel_sync().get_blob(key, access="private").body

    def delete(self, key: str) -> None:
        """Delete a blob (no error when absent: the SDK's BlobNotFoundError is a FileNotFoundError)."""
        try:
            _vercel_sync().delete_blob(key)
        except FileNotFoundError:
            pass

    def list(self, prefix: str) -> Iterator[StoredBlob]:
        """Blobs under a literal pathname prefix (the SDK follows pagination cursors)."""
        for b in _vercel_sync().query_blobs(prefix=prefix):
            yield StoredBlob(b.pathname, _aware(b.uploaded_at))


def get_blob_store(settings: Settings) -> BlobStore:
    """Blob store selected by BLOB_BACKEND."""
    if settings.blob_backend == "vercel":
        return VercelBlobStore()
    return LocalBlobStore(Path(settings.local_blob_dir))
