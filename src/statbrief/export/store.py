"""Build the requested export formats, store them privately in Blob and record them as a report version.

Order: every file is built and uploaded first, then all are recorded in one transaction
(`ReportVersionRepo.add_version`). If an upload or the recording fails, the blobs already written are
deleted (best effort) so no orphan stays behind, and the error propagates. The M7 `export` node calls
`save_exports` and turns the result into `ExportStatus(done, ...)` (wired after M7 merges).
"""
import hashlib
import logging
from collections.abc import Callable, Sequence

from psycopg_pool import ConnectionPool

from statbrief.export.bundle import ReportBundle
from statbrief.export.format import ExportOptions
from statbrief.export.pdf import build_pdf
from statbrief.export.xlsx import build_xlsx
from statbrief.storage.blobs import BlobStore
from statbrief.storage.report_versions import ExportFile, ReportVersionRepo, export_blob_key

logger = logging.getLogger(__name__)

CONTENT_TYPES = {"pdf": "application/pdf",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def build_export(bundle: ReportBundle, fmt: str, options: ExportOptions) -> bytes:
    """The bytes of one export format (ValueError for an unknown format)."""
    builders: dict[str, Callable[[], bytes]] = {"pdf": lambda: build_pdf(bundle, options),
                                                "xlsx": lambda: build_xlsx(bundle)}
    if fmt not in builders:
        raise ValueError(f"unknown export format {fmt!r}")
    return builders[fmt]()


def save_exports(pool: ConnectionPool, blobs: BlobStore, user_id: str, bundle: ReportBundle,
                 formats: Sequence[str], options: ExportOptions) -> int:
    """Build, upload and record the formats as the report's next version; returns the version number.

    PermissionError (via the repo) unless the user owns `bundle.report_id`; ValueError for unknown or
    repeated formats (checked before anything is uploaded).
    """
    if not formats or len(set(formats)) != len(formats) or any(f not in CONTENT_TYPES for f in formats):
        raise ValueError("one entry per known export format is required")
    stored: list[ExportFile] = []
    try:
        for fmt in formats:
            data = build_export(bundle, fmt, options)
            key = blobs.put(export_blob_key(user_id, bundle.report_id, fmt), data, CONTENT_TYPES[fmt])
            stored.append(ExportFile(fmt=fmt, blob_key=key, size_bytes=len(data),
                                     sha256=hashlib.sha256(data).hexdigest()))
        return ReportVersionRepo(pool).add_version(user_id, bundle.report_id, stored)
    except BaseException:
        for f in stored:
            try:
                blobs.delete(f.blob_key)
            except Exception as exc:  # orphan left for manual cleanup; log the class only
                logger.warning("export blob cleanup failed: %s", type(exc).__name__)
        raise
