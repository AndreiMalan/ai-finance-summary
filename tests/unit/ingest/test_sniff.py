"""Tests for upload content sniffing: extension + magic bytes + structure, never execution."""
import hashlib
import io
import json
import zipfile

import pytest

from statbrief.ingest.sniff import UploadRejected, validate_file_bytes
from tests.fixtures import add_zip_member, csv_bytes, xlsx_bytes

MAX = 10 * 1024 * 1024
RATIO = 100
ABS = 200 * 1024 * 1024
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _check(name, data):
    """Call the validator with test limits."""
    return validate_file_bytes(name, data, max_bytes=MAX, max_uncompressed_ratio=RATIO,
                               max_uncompressed_bytes=ABS)


def test_accepts_csv_json_xlsx_and_hashes():
    """Valid files pass with kind, size and sha256."""
    c = csv_bytes([["a", "b"], [1, 2]])
    r = _check("data.CSV", c)
    assert (r.kind, r.size_bytes, r.sha256) == ("csv", len(c), hashlib.sha256(c).hexdigest())
    assert _check("d.json", json.dumps([{"a": 1}]).encode()).kind == "json"
    assert _check("w.xlsx", xlsx_bytes({"S": [["a"], [1]]})).kind == "xlsx"


@pytest.mark.parametrize("name,data,code", [
    ("e.csv", b"", "empty"),
    ("x.xlsm", b"PK\x03\x04", "macro_enabled"),
    ("x.xlsb", b"PK\x03\x04", "macro_enabled"),
    ("x.exe", b"MZ", "unsupported_extension"),
    ("x.txt", b"a,b", "unsupported_extension"),
    ("x.xlsx", b"not a zip at all", "malformed"),
    ("x.xlsx", OLE, "password_protected"),
    ("x.xls", b"PK\x03\x04garbage", "malformed"),
    ("x.csv", b"a,b\x00\x01\x02", "not_text"),
    ("x.json", b"{not json", "bad_json"),
])
def test_rejections(name, data, code):
    """Each unsafe or malformed upload is rejected with a specific code."""
    with pytest.raises(UploadRejected) as exc:
        _check(name, data)
    assert exc.value.code == code and exc.value.message


def test_too_large():
    """Files above the limit are rejected before parsing."""
    with pytest.raises(UploadRejected) as exc:
        validate_file_bytes("a.csv", b"a" * 11, max_bytes=10, max_uncompressed_ratio=RATIO,
                            max_uncompressed_bytes=ABS)
    assert exc.value.code == "too_large"


def test_xlsx_with_vba_project_rejected():
    """An .xlsx renamed from a macro workbook (contains vbaProject.bin) is rejected."""
    data = add_zip_member(xlsx_bytes({"S": [["a"]]}), "xl/vbaProject.bin", b"\x00" * 10)
    with pytest.raises(UploadRejected) as exc:
        _check("ok.xlsx", data)
    assert exc.value.code == "macro_enabled"


def test_zip_bomb_rejected():
    """A workbook whose uncompressed size exceeds ratio × compressed size is rejected."""
    data = add_zip_member(xlsx_bytes({"S": [["a"]]}), "xl/bomb.xml", b"0" * 5_000_000)
    with pytest.raises(UploadRejected) as exc:
        validate_file_bytes("b.xlsx", data, max_bytes=MAX, max_uncompressed_ratio=50,
                            max_uncompressed_bytes=ABS)
    assert exc.value.code == "malformed"


def test_xls_magic_accepted():
    """A legacy .xls must start with the OLE2 signature (content parsed later)."""
    assert _check("old.xls", OLE).kind == "xls"


# --- Hardening regressions (review fix round) ---------------------------------------------------

def _rewrite_zip(data, transform):
    """Return a copy of a zip whose member bytes are passed through transform(name, bytes)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            z.writestr(item, transform(item.filename, src.read(item.filename)))
    return out.getvalue()


def _ole_with_entries(*names):
    """Minimal synthetic OLE2 file: header sector then 128-byte directory entries with the given names."""
    out = bytearray(OLE)
    for name in names:
        encoded = (name + "\x00").encode("utf-16-le")
        entry = bytearray(128)
        entry[: len(encoded)] = encoded
        entry[64:66] = len(encoded).to_bytes(2, "little")
        out += entry
    return bytes(out)


def _rejected(name, data, **limits):
    """Return the UploadRejected raised for the given upload (test limits unless overridden)."""
    kwargs = {"max_bytes": MAX, "max_uncompressed_ratio": RATIO, "max_uncompressed_bytes": ABS, **limits}
    with pytest.raises(UploadRejected) as exc:
        validate_file_bytes(name, data, **kwargs)
    return exc.value


def test_forged_zip_version_is_malformed():
    """A central directory demanding an unsupported zip version is rejected, not a crash."""
    data = bytearray(xlsx_bytes({"S": [["a"]]}))
    pos = data.find(b"PK\x01\x02")
    assert pos >= 0
    while pos >= 0:
        data[pos + 6: pos + 8] = (99).to_bytes(2, "little")
        pos = data.find(b"PK\x01\x02", pos + 4)
    assert _rejected("v.xlsx", bytes(data)).code == "malformed"


def test_deeply_nested_json_is_bad_json():
    """Pathologically nested JSON (RecursionError) is reported as bad_json."""
    assert _rejected("d.json", b"[" * 200_000 + b"]" * 200_000).code == "bad_json"


@pytest.mark.parametrize("payload", [b"[NaN]", b"[Infinity]", b'{"a": -Infinity}'])
def test_json_non_finite_constants_are_bad_json(payload):
    """JSON's non-standard NaN/Infinity constants are rejected."""
    assert _rejected("n.json", payload).code == "bad_json"


@pytest.mark.parametrize("member", [
    "xl/macrosheets/sheet1.xml", "xl/dialogsheets/sheet1.xml", "xl/activeX/activeX1.bin",
    "XL/MacroSheets/Sheet1.xml",
])
def test_xlsx_macro_parts_without_vba_rejected(member):
    """XLM macro sheets, dialog sheets and ActiveX controls count as macro content."""
    data = add_zip_member(xlsx_bytes({"S": [["a"]]}), member, b"<x/>")
    assert _rejected("m.xlsx", data).code == "macro_enabled"


@pytest.mark.parametrize("marker", [b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                                    b"application/vnd.ms-office.vbaProject"])
def test_xlsx_macro_content_type_rejected(marker):
    """A [Content_Types].xml declaring macro-enabled parts is rejected."""
    def patch(name, content):
        """Inject the macro content type into [Content_Types].xml."""
        if name == "[Content_Types].xml":
            return content.replace(b"</Types>", b'<Default Extension="bin" ContentType="' + marker + b'"/></Types>')
        return content
    data = _rewrite_zip(xlsx_bytes({"S": [["a"]]}), patch)
    assert _rejected("c.xlsx", data).code == "macro_enabled"


@pytest.mark.parametrize("entry,code", [
    ("_VBA_PROJECT_CUR", "macro_enabled"),
    ("VBA", "macro_enabled"),
    ("EncryptionInfo", "password_protected"),
    ("EncryptedPackage", "password_protected"),
])
def test_xls_ole_directory_scan(entry, code):
    """Legacy .xls with VBA storages or an encrypted-OOXML payload is rejected by directory name."""
    assert _rejected("x.xls", _ole_with_entries("Root Entry", "Workbook", entry)).code == code


def test_xls_plain_ole_entries_accepted():
    """An OLE file with only ordinary workbook streams passes the scan."""
    assert _check("ok.xls", _ole_with_entries("Root Entry", "Workbook", "VBAish")).kind == "xls"


def test_absolute_expansion_cap():
    """A workbook within the ratio but above the absolute uncompressed cap is rejected."""
    data = add_zip_member(xlsx_bytes({"S": [["a"]]}), "xl/big.xml", b"0" * 2_000_000)
    exc = _rejected("b.xlsx", data, max_uncompressed_ratio=100_000, max_uncompressed_bytes=1_000_000)
    assert exc.code == "malformed" and "unexpectedly large" in exc.message


@pytest.mark.parametrize("name,data", [
    ("u.csv", "a,b\n1,2\n".encode("utf-16")),
    ("u.csv", b"\xfe\xff" + "a,b\n".encode("utf-16-be")),
    ("u.json", "[1]".encode("utf-16")),
])
def test_utf16_text_rejected_with_hint(name, data):
    """UTF-16 CSV/JSON is rejected with a save-as-UTF-8 hint."""
    exc = _rejected(name, data)
    assert exc.code == "not_text" and "UTF-16" in exc.message and "UTF-8" in exc.message


def test_too_large_message_sub_megabyte():
    """Limits under 1 MB are shown with one decimal."""
    exc = _rejected("a.csv", b"a" * (512 * 1024 + 1), max_bytes=512 * 1024)
    assert exc.code == "too_large" and "0.5 MB" in exc.message
