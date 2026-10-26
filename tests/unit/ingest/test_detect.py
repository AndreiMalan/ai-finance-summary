"""Tests for automatic header/role detection."""
from datetime import datetime

import pytest

from statbrief.ingest.detect import detect_header_row, detect_schema
from statbrief.ingest.readers import FORMULA_NO_VALUE, Grid, read_grid
from tests.fixtures import csv_bytes, xlsx_bytes

KW = dict(sample_rows=200, scan_rows=20)


def _irs_grid():
    """IRS-like sheet: title rows, Romanian headers, an undocumented column."""
    rows = [["Institutul Român de Statistică — Inflație regională"], [None],
            ["luna", "judet", "indicator", "valoare", "um", "val_adj_x"]]
    for m in (1, 2, 3):
        for county, v in (("Cluj", 7.4), ("Timiș", 7.0), ("Iași", 7.9)):
            rows.append([datetime(2024, m, 1), county, "HICP_YOY", v - m / 10, "%", 1.01])
    return read_grid(xlsx_bytes({"Inflation": rows}), "xlsx")


def test_header_row_skips_title_rows():
    """The title and blank rows above the header are skipped."""
    assert detect_header_row(_irs_grid(), scan_rows=20) == 3


def test_roles_detected_with_confidence_and_undocumented_flag():
    """All five roles are found on Romanian headers; unknown columns are flagged."""
    s = detect_schema(_irs_grid(), **KW)
    roles = {c.column: c.role for c in s.columns}
    assert roles == {"luna": "date", "judet": "region", "indicator": "indicator", "valoare": "value",
                     "um": "unit", "val_adj_x": "ignore"}
    assert next(c for c in s.columns if c.column == "val_adj_x").note == "undocumented"
    assert s.frequency == "M" and s.header_row == 3 and s.worksheet == "Inflation"
    m = s.suggested_mapping()
    assert m is not None and m.value_col == "valoare"
    assert not s.needs_review(0.5)


def test_known_columns_not_flagged():
    """A column described in a data dictionary is not flagged as undocumented."""
    s = detect_schema(_irs_grid(), known_columns=frozenset({"val_adj_x"}), **KW)
    assert next(c for c in s.columns if c.column == "val_adj_x").note is None


def test_csv_decimal_comma_quarterly_english_headers():
    """English headers, quarterly periods and decimal commas are detected."""
    rows = [["Period", "Country", "Value", "Unit"]] + [[f"2023-Q{q}", "RO", f"{5 + q},5", "%"] for q in (1, 2, 3, 4)]
    s = detect_schema(read_grid(csv_bytes(rows, delimiter=";"), "csv"), **KW)
    assert {c.column: c.role for c in s.columns} == {"Period": "date", "Country": "region", "Value": "value",
                                                     "Unit": "unit"}
    assert s.frequency == "Q" and s.decimal == "," and s.meta["delimiter"] == ";"
    assert s.suggested_mapping() is None  # no indicator column → user must set a constant


def test_ambiguous_day_order_left_undecided():
    """dd/mm vs mm/dd that never disambiguates leaves day_order None (user decides)."""
    rows = [["date", "value"], ["01/02/2024", "1"], ["03/04/2024", "2"]]
    s = detect_schema(read_grid(csv_bytes(rows), "csv"), **KW)
    assert s.day_order is None


def test_empty_grid_raises_clear_error():
    """A grid without any rows cannot be detected and says so."""
    with pytest.raises(ValueError, match="no non-empty rows"):
        detect_schema(Grid(rows=[], row_numbers=[]), **KW)
    with pytest.raises(ValueError, match="no non-empty rows"):
        detect_header_row(Grid(rows=[], row_numbers=[]), scan_rows=20)


def test_header_only_grid_ignores_every_column():
    """A header without data rows yields no roles and no frequency (nothing to infer from)."""
    s = detect_schema(read_grid(csv_bytes([["date", "region", "value"]]), "csv"), **KW)
    assert s.header_row == 1
    assert [(c.column, c.role, c.confidence) for c in s.columns] == [
        ("date", "ignore", 0.0), ("region", "ignore", 0.0), ("value", "ignore", 0.0)]
    assert s.frequency is None and s.day_order is None and s.decimal is None
    assert s.needs_review(0.8)


def test_formula_without_value_column_is_not_mapped_from_content():
    """A column whose cells are all formulas without cached values gets no content evidence."""
    grid = Grid(rows=[["date", "value", "calc"]] + [[f"2024-0{m}", str(m), FORMULA_NO_VALUE] for m in (1, 2, 3)],
                row_numbers=[1, 2, 3, 4], meta={})
    s = detect_schema(grid, **KW)
    roles = {c.column: c.role for c in s.columns}
    assert roles == {"date": "date", "value": "value", "calc": "ignore"}
    assert next(c for c in s.columns if c.column == "calc").note == "undocumented"
