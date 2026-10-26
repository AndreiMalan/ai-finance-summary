"""Tests for reading uploads into grids and tables without executing anything."""
import json
import struct
from datetime import datetime
from unittest import mock

import pytest
import xlrd

from statbrief.ingest import readers
from statbrief.ingest.readers import FORMULA_NO_VALUE, list_worksheets, read_grid, to_table
from statbrief.ingest.sniff import UploadRejected
from tests.fixtures import (ENCRYPTED_XLS, SAMPLE_XLS, XLM_MACRO_XLS, csv_bytes, xlsx_bytes,
                            xlsx_with_formula_no_cache)


def test_csv_semicolon_decimal_comma_cp1250_and_formula_text():
    """Semicolon CSV in cp1250 keeps strings verbatim, incl. formula-looking cells."""
    # cp1250 has the cedilla forms ţ/ş (not the comma-below ț/ș), which legacy Romanian exports use.
    data = csv_bytes([["luna", "judeţ", "valoare"], ["2024-01", "Iaşi", "7,4"], ["2024-02", "Cluj", "=1+1"]],
                     delimiter=";", encoding="cp1250")
    g = read_grid(data, "csv")
    assert g.meta["delimiter"] == ";" and g.meta["encoding"] in ("cp1250", "latin-1")
    assert g.rows[1] == ["2024-01", "Iaşi", "7,4"] and g.rows[2][2] == "=1+1"
    assert g.row_numbers == [1, 2, 3]


@pytest.mark.parametrize("cell", ["=SUM(A1:A2)", "+1", "-2", "@cmd"])
def test_csv_formula_looking_cells_stay_text(cell):
    """Cells starting with = + - @ are returned verbatim as strings."""
    g = read_grid(csv_bytes([["a", "b"], ["x", cell]]), "csv")
    assert g.rows[1][1] == cell and isinstance(g.rows[1][1], str)


def test_csv_utf8_bom():
    """A UTF-8 BOM is removed from the first header."""
    g = read_grid("﻿a,b\n1,2\n".encode("utf-8"), "csv")
    assert g.rows[0] == ["a", "b"] and g.meta["encoding"] == "utf-8-sig"
    assert "nul_removed" not in g.meta


def test_csv_nul_bytes_after_probe_window_are_stripped():
    """Sniffing only probes the first 8 KB; later NUL bytes are removed and flagged, not fatal."""
    head = csv_bytes([["a", "b"]] + [[f"r{i}", i] for i in range(2000)])
    assert len(head) > 8192
    g = read_grid(head + b"x\x00y,1\x00\n", "csv")
    assert g.rows[-1] == ["xy", "1"] and g.meta["nul_removed"] is True


def test_csv_blank_rows_skipped_with_original_numbers():
    """Empty lines are not grid rows but numbering follows the source file."""
    g = read_grid(b"title\n\nh1,h2\n1,2\n", "csv")
    assert g.row_numbers == [1, 3, 4] and to_table(g, header_row=3).columns == ["h1", "h2"]


def test_xlsx_header_not_in_row_1_hidden_sheet_and_dates():
    """Title rows above the header are kept in the grid with real row numbers; hidden sheets listed."""
    data = xlsx_bytes({"Notes": [["n"]], "Inflation": [["IRS regional"], [None], ["luna", "valoare"],
                                                       [datetime(2024, 1, 1), 7.4], [datetime(2024, 2, 1), 7.1]]},
                      hidden=frozenset({"Notes"}))
    sheets = list_worksheets(data, "xlsx")
    assert [(s.name, s.hidden) for s in sheets] == [("Notes", True), ("Inflation", False)]
    g = read_grid(data, "xlsx")  # default: first visible sheet
    assert g.worksheet == "Inflation" and g.row_numbers == [1, 3, 4, 5]
    t = to_table(g, header_row=3)
    assert t.columns == ["luna", "valoare"] and t.row_numbers == [4, 5]
    assert t.rows[0] == [datetime(2024, 1, 1), 7.4]


def test_xlsx_explicit_worksheet_and_missing_worksheet():
    """A named sheet is read even if hidden; an unknown name is rejected."""
    data = xlsx_bytes({"A": [["a"]], "B": [["b"]]}, hidden=frozenset({"B"}))
    assert read_grid(data, "xlsx", worksheet="B").rows == [["b"]]
    with pytest.raises(UploadRejected) as exc:
        read_grid(data, "xlsx", worksheet="Nope")
    assert exc.value.code == "malformed"


def test_xlsx_formula_without_cached_value_is_flagged_not_computed():
    """openpyxl-saved formulas have no cached value → FORMULA_NO_VALUE, never '2'."""
    g = read_grid(xlsx_with_formula_no_cache(), "xlsx")
    assert g.rows[1][1] is FORMULA_NO_VALUE and g.rows[2][1] == 7.1


def test_xlsx_corrupt_is_malformed():
    """Unreadable workbook bytes become a malformed rejection."""
    with pytest.raises(UploadRejected) as exc:
        read_grid(b"PK\x03\x04garbage", "xlsx")
    assert exc.value.code == "malformed"


def test_to_table_blank_and_duplicate_headers_and_ragged_rows():
    """Blank headers get names, duplicates get suffixes, short rows are padded."""
    data = csv_bytes([["a", "", "a"], ["1"], ["2", "3", "4"]])
    t = to_table(read_grid(data, "csv"), header_row=1)
    assert t.columns == ["a", "column_2", "a_2"]
    assert t.rows == [["1", None, None], ["2", "3", "4"]] and t.column_index("a_2") == 2


def test_to_table_strips_header_names_and_extends_for_wide_rows():
    """Header names are stripped (matching ColumnMapping); extra data columns get names."""
    t = to_table(read_grid(csv_bytes([[" a ", "b"], ["1", "2", "3"]]), "csv"), header_row=1)
    assert t.columns == ["a", "b", "column_3"]


def test_to_table_unknown_header_row():
    """A header row that is empty or outside the grid is a ValueError."""
    with pytest.raises(ValueError):
        to_table(read_grid(csv_bytes([["a"], ["1"]]), "csv"), header_row=5)


def test_json_records_and_wrapped():
    """List of objects and {data: [...]} both become tables; key union preserves order."""
    for payload in ([{"d": "2024", "v": 1}, {"d": "2025", "v": 2, "u": "%"}],
                    {"data": [{"d": "2024", "v": 1}, {"d": "2025", "v": 2, "u": "%"}]}):
        t = to_table(read_grid(json.dumps(payload).encode(), "json"), header_row=1)
        assert t.columns == ["d", "v", "u"] and t.rows[0] == ["2024", 1, None] and t.row_numbers == [2, 3]


def test_json_nested_values_kept_as_json_text():
    """Nested objects/lists are serialised, not flattened or interpreted."""
    g = read_grid(json.dumps([{"a": {"b": 1}, "c": [1, 2]}]).encode(), "json")
    assert g.rows[1] == ['{"b": 1}', "[1, 2]"]


@pytest.mark.parametrize("raw", [b'{"a": {"b": 1}}', b"[]", b"[1, 2]", b'"x"'])
def test_json_not_tabular_rejected(raw):
    """Non-tabular JSON is rejected with guidance."""
    with pytest.raises(UploadRejected) as exc:
        read_grid(raw, "json")
    assert exc.value.code == "malformed"


def test_max_rows_limit():
    """Reading stops with a clear rejection when a file exceeds the row limit."""
    data = csv_bytes([["a"]] + [[i] for i in range(20)])
    with pytest.raises(UploadRejected) as exc:
        read_grid(data, "csv", max_rows=10)
    assert exc.value.code == "too_large"
    assert len(read_grid(data, "csv", max_rows=21).rows) == 21


def test_csv_and_json_have_no_worksheets():
    """Only Excel files have worksheets."""
    assert list_worksheets(b"a,b\n", "csv") == [] and list_worksheets(b"[]", "json") == []


@pytest.mark.skipif(not SAMPLE_XLS.exists(), reason="tests/data/sample.xls not available")
def test_xls_reads_cached_values():
    """Legacy .xls is read via xlrd: cached values only, dates as datetime, first visible sheet."""
    data = SAMPLE_XLS.read_bytes()
    assert [(s.name, s.hidden) for s in list_worksheets(data, "xls")] == [("Ascuns", True), ("Date", False)]
    g = read_grid(data, "xls")
    assert g.worksheet == "Date"
    t = to_table(g, header_row=1)
    assert t.columns == ["luna", "valoare", "calc"]
    assert t.rows == [[datetime(2024, 1, 1), 7.4, 2.0], [datetime(2024, 2, 1), 7.1, "text"]]


@pytest.mark.skipif(not ENCRYPTED_XLS.exists(), reason="tests/data/encrypted.xls not available")
def test_xls_encrypted_is_password_protected():
    """A FILEPASS-encrypted .xls is reported as password-protected, not merely malformed."""
    for call in (lambda d: read_grid(d, "xls"), lambda d: list_worksheets(d, "xls")):
        with pytest.raises(UploadRejected) as exc:
            call(ENCRYPTED_XLS.read_bytes())
        assert exc.value.code == "password_protected"


def test_xls_encrypted_error_mapping_without_sample():
    """The exact xlrd 2.0.2 FILEPASS error maps to password_protected; other xlrd errors to malformed."""
    with mock.patch.object(xlrd, "open_workbook", side_effect=xlrd.XLRDError("Workbook is encrypted")):
        with pytest.raises(UploadRejected) as exc:
            read_grid(b"x", "xls")
    assert exc.value.code == "password_protected"
    with pytest.raises(UploadRejected) as exc:
        read_grid(b"not an xls", "xls")
    assert exc.value.code == "malformed"


@pytest.mark.skipif(not XLM_MACRO_XLS.exists(), reason="tests/data/xlm_macro_sheet.xls not available")
def test_xls_with_xlm_macro_sheet_rejected():
    """An Excel 4.0 macro sheet (BOUNDSHEET type 1) is rejected even though xlrd silently skips it."""
    for call in (lambda d: read_grid(d, "xls"), lambda d: list_worksheets(d, "xls")):
        with pytest.raises(UploadRejected) as exc:
            call(XLM_MACRO_XLS.read_bytes())
        assert exc.value.code == "macro_enabled"


def _record(rc: int, body: bytes) -> bytes:
    """One BIFF record: type, length, body."""
    return struct.pack("<HH", rc, len(body)) + body


def _boundsheet(sheet_type: int) -> bytes:
    """A BIFF8 BOUNDSHEET record body for a sheet named 'S' with the given type byte."""
    return _record(0x85, struct.pack("<iBB", 0, 0, sheet_type) + b"\x01\x00S")


@pytest.mark.parametrize("sheet_type,expected", [(0, False), (2, False), (1, True), (6, True)])
def test_boundsheet_scan(sheet_type, expected):
    """Macro (1) and VB module (6) sheet types are detected; worksheets and charts are not."""
    stream = b"\xff" * 3 + _record(0x809, b"\x00" * 16) + _boundsheet(0) + _boundsheet(sheet_type) \
        + _record(0x0A, b"") + _boundsheet(1)  # records after the globals EOF are ignored
    assert readers._xls_has_macro_sheets(stream, 3, len(stream)) is expected


def test_boundsheet_scan_truncated_stream():
    """A record header claiming more bytes than exist does not crash the scan."""
    stream = _boundsheet(0) + struct.pack("<HH", 0x85, 500)
    assert readers._xls_has_macro_sheets(stream, 0, len(stream)) is False


def test_to_table_duplicate_suffix_never_collides_with_existing_name():
    """A generated a_2 skips a name the header already uses, so all columns stay unique."""
    t = to_table(read_grid(csv_bytes([["a", "a", "a_2", "", "column_4"], ["1", "2", "3", "4", "5"]]), "csv"),
                 header_row=1)
    assert len(set(t.columns)) == len(t.columns)
    assert t.columns[:3] == ["a", "a_3", "a_2"]
