"""Owner-checked download of stored report exports: `GET /reports/{id}/exports/{version}/{fmt}`.

Login required (anonymous users are redirected to login); another user's report, an unknown version or
format, and malformed ids all answer 404 (AC 6). The file is streamed from the private Blob store with
`Content-Disposition: attachment`, `nosniff` and `no-store`; a blob that is gone answers 410 and a failing
store 503 (`{code, message}`, via the handlers installed by `uploads.install_error_handlers`).
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from statbrief.auth.users import User
from statbrief.export.store import CONTENT_TYPES
from statbrief.storage.report_versions import ReportVersionRepo
from statbrief.web.deps import current_user, get_runtime
from statbrief.web.routes.uploads import StoredFileError
from statbrief.web.runtime import Runtime

logger = logging.getLogger(__name__)
router = APIRouter()

UserDep = Annotated[User, Depends(current_user)]
RtDep = Annotated[Runtime, Depends(get_runtime)]
_FILENAME = "statbrief-report-{short_id}-v{version}.{fmt}"
_MAX_VERSION_DIGITS = 9  # report_versions.version is a Postgres integer (< 2^31)


@router.get("/reports/{report_id}/exports/{version}/{fmt}")
def download_export(report_id: str, version: str, fmt: str, user: UserDep, rt: RtDep) -> Response:
    """The stored export file of the user's report (404 for anything the user does not own)."""
    if (fmt not in CONTENT_TYPES or not version.isascii() or not version.isdigit()
            or len(version) > _MAX_VERSION_DIGITS):
        raise HTTPException(status_code=404, detail="export not found")
    row = ReportVersionRepo(rt.pool).get(user.id, report_id, int(version), fmt)
    if row is None:
        raise HTTPException(status_code=404, detail="export not found")
    try:
        data = rt.blobs.get(row.blob_key)
    except FileNotFoundError:
        raise StoredFileError(410, "file_gone", "This export is no longer stored.") from None
    except Exception as exc:  # Blob SDK / network errors: never a bare 500
        logger.warning("export blob read failed: %s", type(exc).__name__)
        raise StoredFileError(503, "storage_unavailable", "File storage is unavailable; try again shortly.") \
            from None
    name = _FILENAME.format(short_id=row.report_id[:8], version=row.version, fmt=fmt)
    return Response(data, media_type=CONTENT_TYPES[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
