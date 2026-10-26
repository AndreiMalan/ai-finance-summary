"""Validate uploaded bytes by extension AND content; never execute or render anything."""
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from statbrief.domain.dataset import FileKind

_KIND_BY_EXT: dict[str, FileKind] = {".csv": "csv", ".xlsx": "xlsx", ".xls": "xls", ".json": "json"}
_MACRO_EXTS = {".xlsm", ".xlsb", ".xltm", ".xlam"}
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls and encrypted OOXML containers
_TEXT_PROBE_BYTES = 8192  # bytes inspected for binary content in text formats


class UploadRejected(Exception):
    """The upload is unsafe or unreadable; `code` is machine-readable, `message` user-facing."""

    def __init__(self, code: str, message: str):
        """Store the rejection reason."""
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class FileCheck:
    """Accepted file facts."""

    kind: FileKind
    size_bytes: int
    sha256: str


def _check_xlsx(data: bytes, max_uncompressed_ratio: int) -> None:
    """OOXML must be a zip with workbook parts, no VBA project, and a sane uncompressed size."""
    if data.startswith(_OLE_MAGIC):
        raise UploadRejected("password_protected", "This workbook is password-protected or encrypted. "
                             "Remove the password in Excel and upload it again.")
    if not data.startswith(_ZIP_MAGIC):
        raise UploadRejected("malformed", "This is not a valid .xlsx file.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            total = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as exc:
        raise UploadRejected("malformed", "This .xlsx file is damaged.") from exc
    if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
        raise UploadRejected("malformed", "This zip file is not an Excel workbook.")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise UploadRejected("macro_enabled", "Workbooks containing macros are not accepted. "
                             "Save it as a plain .xlsx without macros.")
    if total > max_uncompressed_ratio * max(len(data), 1):
        raise UploadRejected("malformed", "This workbook expands to an unexpectedly large size.")


def _check_text(data: bytes, kind: FileKind) -> None:
    """CSV/JSON must be text; JSON must parse."""
    if b"\x00" in data[:_TEXT_PROBE_BYTES]:
        raise UploadRejected("not_text", "This file contains binary data, not text.")
    if kind == "json":
        try:
            json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadRejected("bad_json", "This JSON file could not be parsed.") from exc


def validate_file_bytes(filename: str, data: bytes, max_bytes: int, max_uncompressed_ratio: int) -> FileCheck:
    """Return the file kind and hash, or raise UploadRejected with a specific reason."""
    ext = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if ext in _MACRO_EXTS:
        raise UploadRejected("macro_enabled", "Macro-enabled Excel files are not accepted. Save as .xlsx.")
    kind = _KIND_BY_EXT.get(ext)
    if kind is None:
        raise UploadRejected("unsupported_extension", "Only CSV, Excel (.xlsx, .xls) and JSON files are accepted.")
    if not data:
        raise UploadRejected("empty", "The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejected("too_large", f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    if kind == "xlsx":
        _check_xlsx(data, max_uncompressed_ratio)
    elif kind == "xls":
        if not data.startswith(_OLE_MAGIC):
            raise UploadRejected("malformed", "This is not a valid .xls file.")
    else:
        _check_text(data, kind)
    return FileCheck(kind=kind, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
