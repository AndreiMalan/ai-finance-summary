"""Tests for upload content sniffing: extension + magic bytes + structure, never execution."""
import hashlib
import json

import pytest

from statbrief.ingest.sniff import UploadRejected, validate_file_bytes
from tests.fixtures import add_zip_member, csv_bytes, xlsx_bytes

MAX = 10 * 1024 * 1024
RATIO = 100
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _check(name, data):
    """Call the validator with test limits."""
    return validate_file_bytes(name, data, max_bytes=MAX, max_uncompressed_ratio=RATIO)


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
        validate_file_bytes("a.csv", b"a" * 11, max_bytes=10, max_uncompressed_ratio=RATIO)
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
        validate_file_bytes("b.xlsx", data, max_bytes=MAX, max_uncompressed_ratio=50)
    assert exc.value.code == "malformed"


def test_xls_magic_accepted():
    """A legacy .xls must start with the OLE2 signature (content parsed later)."""
    assert _check("old.xls", OLE).kind == "xls"
