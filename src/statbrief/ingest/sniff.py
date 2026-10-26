"""Validate uploaded bytes by extension AND content; never execute or render anything."""
import hashlib
import io
import json
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from statbrief.domain.dataset import FileKind

_KIND_BY_EXT: dict[str, FileKind] = {".csv": "csv", ".xlsx": "xlsx", ".xls": "xls", ".json": "json"}
_MACRO_EXTS = {".xlsm", ".xlsb", ".xltm", ".xlam"}
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls and encrypted OOXML containers
_TEXT_PROBE_BYTES = 8192  # bytes inspected for binary content in text formats
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_MIB = 1024 * 1024
# Anything the zip/zlib stack can raise on hostile input (forged versions, bad CRCs, truncation...).
_ZIP_ERRORS = (zipfile.BadZipFile, NotImplementedError, ValueError, OSError, EOFError, RuntimeError,
               zlib.error)
# OOXML parts that carry executable content even without a vbaProject.bin (lower-cased prefixes).
_MACRO_PART_PREFIXES = ("xl/macrosheets/", "xl/dialogsheets/", "xl/activex/")
_MACRO_CONTENT_TYPE_MARKERS = (b"macroenabled", b"vbaproject")
# OLE2 compound-file layout: the directory lives in 512/4096-byte sectors after the 512-byte header,
# made of 128-byte entries whose first 64 bytes are a UTF-16LE name and bytes 64-65 its byte length.
_OLE_HEADER_BYTES = 512
_OLE_DIR_ENTRY_BYTES = 128
_OLE_NAME_FIELD_BYTES = 64
_OLE_MACRO_ENTRIES = {"_VBA_PROJECT_CUR", "VBA"}
_OLE_ENCRYPTED_ENTRIES = {"ENCRYPTIONINFO", "ENCRYPTEDPACKAGE"}  # encrypted OOXML wrapped in OLE


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


def _too_large_message(max_bytes: int) -> str:
    """Describe the size limit in MB, with one decimal for sub-megabyte limits."""
    limit = f"{max_bytes / _MIB:.1f}" if max_bytes < _MIB else str(max_bytes // _MIB)
    return f"The file is larger than {limit} MB."


def _check_xlsx(data: bytes, max_uncompressed_ratio: int, max_uncompressed_bytes: int) -> None:
    """OOXML must be a zip with workbook parts, no macro content, and a sane uncompressed size."""
    if data.startswith(_OLE_MAGIC):
        raise UploadRejected("password_protected", "This workbook is password-protected or encrypted. "
                             "Remove the password in Excel and upload it again.")
    if not data.startswith(_ZIP_MAGIC):
        raise UploadRejected("malformed", "This is not a valid .xlsx file.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            total = sum(i.file_size for i in z.infolist())
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise UploadRejected("malformed", "This zip file is not an Excel workbook.")
            lowered = [n.lower() for n in names]
            if any(n.endswith("vbaproject.bin") or n.startswith(_MACRO_PART_PREFIXES) for n in lowered):
                raise UploadRejected("macro_enabled", "Workbooks containing macros are not accepted. "
                                     "Save it as a plain .xlsx without macros.")
            if total > max_uncompressed_ratio * max(len(data), 1) or total > max_uncompressed_bytes:
                raise UploadRejected("malformed", "This workbook expands to an unexpectedly large size.")
            # Safe to inflate: the declared total size was bounded just above.
            content_types = z.read("[Content_Types].xml").lower()
    except _ZIP_ERRORS as exc:
        raise UploadRejected("malformed", "This .xlsx file is damaged.") from exc
    if any(marker in content_types for marker in _MACRO_CONTENT_TYPE_MARKERS):
        raise UploadRejected("macro_enabled", "Workbooks containing macros are not accepted. "
                             "Save it as a plain .xlsx without macros.")


def _ole_entry_names(data: bytes) -> set[str]:
    """Upper-cased names of everything shaped like an OLE2 directory entry.

    This is a byte-level name scan of the compound-file directory (every 128-byte slot after the
    header whose name-length field is consistent); nothing is parsed further or executed.
    """
    found = set()
    for off in range(_OLE_HEADER_BYTES, len(data) - _OLE_DIR_ENTRY_BYTES + 1, _OLE_DIR_ENTRY_BYTES):
        name_len = int.from_bytes(data[off + _OLE_NAME_FIELD_BYTES: off + _OLE_NAME_FIELD_BYTES + 2], "little")
        if not 2 <= name_len <= _OLE_NAME_FIELD_BYTES or name_len % 2:
            continue
        raw = data[off: off + name_len]
        if raw[-2:] != b"\x00\x00":
            continue
        found.add(raw[:-2].decode("utf-16-le", errors="replace").upper())
    return found


def _check_xls(data: bytes) -> None:
    """Legacy .xls must be OLE2 without VBA storages or an encrypted-OOXML payload.

    FILEPASS-encrypted BIFF workbooks are not detectable here; the reader maps them to
    `password_protected` when it opens the workbook.
    """
    if not data.startswith(_OLE_MAGIC):
        raise UploadRejected("malformed", "This is not a valid .xls file.")
    names = _ole_entry_names(data)
    if names & _OLE_ENCRYPTED_ENTRIES:
        raise UploadRejected("password_protected", "This workbook is password-protected or encrypted. "
                             "Remove the password in Excel and upload it again.")
    if names & _OLE_MACRO_ENTRIES:
        raise UploadRejected("macro_enabled", "Workbooks containing macros are not accepted. "
                             "Save it as a plain .xlsx without macros.")


def _reject_constant(name: str) -> None:
    """Refuse JSON's non-standard NaN/Infinity constants."""
    raise ValueError(f"non-finite JSON constant {name}")


def _check_text(data: bytes, kind: FileKind) -> None:
    """CSV/JSON must be UTF-8-compatible text; JSON must parse with finite numbers only."""
    if data.startswith(_UTF16_BOMS):
        raise UploadRejected("not_text", "This file is UTF-16 encoded; save it as UTF-8 (CSV UTF-8) "
                             "and upload again.")
    if b"\x00" in data[:_TEXT_PROBE_BYTES]:
        raise UploadRejected("not_text", "This file contains binary data, not text.")
    if kind == "json":
        try:
            json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:  # includes UnicodeDecodeError and JSONDecodeError
            raise UploadRejected("bad_json", "This JSON file could not be parsed.") from exc


def validate_file_bytes(filename: str, data: bytes, max_bytes: int, max_uncompressed_ratio: int,
                        max_uncompressed_bytes: int) -> FileCheck:
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
        raise UploadRejected("too_large", _too_large_message(max_bytes))
    if kind == "xlsx":
        _check_xlsx(data, max_uncompressed_ratio, max_uncompressed_bytes)
    elif kind == "xls":
        _check_xls(data)
    else:
        _check_text(data, kind)
    return FileCheck(kind=kind, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
