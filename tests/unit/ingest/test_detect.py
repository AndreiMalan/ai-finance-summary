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


def _grid(rows):
    """Grid from literal rows numbered from 1."""
    return Grid(rows=rows, row_numbers=list(range(1, len(rows) + 1)), meta={})


def _by_col(s):
    """Column name -> ColumnGuess."""
    return {c.column: c for c in s.columns}


def test_two_date_columns_are_ambiguous():
    """A report-date column next to the period column caps the date confidence and needs review."""
    rows = [["Data raportare", "Perioada", "Judet", "Indicator", "Valoare", "UM"]] + [
        ["2025-01-15", f"2024-0{m}", j, "PIB", 1.0 * m, "%"] for m in range(1, 7) for j in ("Cluj", "Iasi")]
    s = detect_schema(_grid(rows), **KW)
    date = next(c for c in s.columns if c.role == "date")
    assert date.confidence <= 0.7 and date.note.startswith("ambiguous: also ")
    assert s.needs_review(0.8)


def test_two_value_columns_are_ambiguous():
    """An adjusted value column next to the value column makes the value guess ambiguous."""
    rows = [["Perioada", "Judet", "Indicator", "Valoare ajustata", "Valoare", "UM"]] + [
        [f"2024-0{m}", j, "PIB", 2.0 * m, 1.0 * m, "%"] for m in range(1, 7) for j in ("Cluj", "Iasi")]
    s = detect_schema(_grid(rows), **KW)
    value = next(c for c in s.columns if c.role == "value")
    assert value.confidence <= 0.7 and "ambiguous" in value.note
    assert s.needs_review(0.8)


def test_two_region_columns_are_ambiguous():
    """Partner and reporter country codes both look like regions; the guess is flagged."""
    rows = [["time", "partner country", "country", "indicator", "value", "unit"]] + [
        [f"2024-0{m}", "DE", g, "EXP", 1.0, "EUR"] for m in range(1, 7) for g in ("RO", "HU")]
    s = detect_schema(_grid(rows), **KW)
    region = next(c for c in s.columns if c.role == "region")
    other = "country" if region.column == "partner country" else "partner country"
    assert region.confidence <= 0.7 and region.note == f"ambiguous: also {other}/region"
    assert s.needs_review(0.8)


def test_unit_of_measure_header_is_unit_not_indicator():
    """'Unit of measure' with % values is the unit column."""
    rows = [["Time", "Geo", "Indicator", "Obs value", "Unit of measure"]] + [
        [f"2024-0{m}", j, "HICP", 1.0 * m, "%"] for m in range(1, 4) for j in ("RO", "HU")]
    c = _by_col(detect_schema(_grid(rows), **KW))
    assert c["Unit of measure"].role == "unit" and c["Indicator"].role == "indicator"


def test_mostly_unit_values_are_unit_not_indicator():
    """An unnamed column of mostly % values is a unit, not a categorical indicator."""
    rows = [["date", "value", "u"]] + [[f"2024-0{m}", 1.0, "%" if m < 8 else "lei"] for m in range(1, 10)]
    assert _by_col(detect_schema(_grid(rows), **KW))["u"].role == "unit"


def test_serial_range_counts_are_values_next_to_real_date():
    """Integer counts in the Excel serial range are values when the column has no date name."""
    rows = [["perioada", "a", "judet"]] + [[f"2024-0{m}", 30000 + m * 7, "Cluj"] for m in range(1, 10)]
    s = detect_schema(_grid(rows), **KW)
    c = _by_col(s)
    assert c["perioada"].role == "date" and c["a"].role == "value" and s.excel_serial is False


def test_serial_range_counts_without_date_column_are_not_dates():
    """Population counts are never read as Excel dates when nothing names them a date."""
    rows = [["cod_siruta", "populatie"]] + [[s, p] for s, p in ((54975, 30000), (9262, 41000), (95060, 25000))] * 3
    s = detect_schema(_grid(rows), **KW)
    assert _by_col(s)["populatie"].role != "date" and s.excel_serial is False


def test_date_named_serial_column_detected_as_excel_dates():
    """A 'data' column holding Excel serials is a date column with excel_serial set."""
    rows = [["data", "valoare"]] + [[serial, 1.5] for serial in (45292, 45323, 45352, 45383)]
    s = detect_schema(_grid(rows), **KW)
    assert _by_col(s)["data"].role == "date" and s.excel_serial is True and s.frequency == "M"


def test_header_without_role_hints_has_low_header_confidence():
    """A title row picked as header (no role name in it) gets header confidence 0.5 and needs review."""
    rows = [["Raport INS", "Inflatie 2024"], ["Sursa: INS", "generat 2024"], ["luna", "judet", "valoare"]] + [
        [f"2024-0{m}", "Cluj", 1.0] for m in range(1, 4)]
    s = detect_schema(_grid(rows), **KW)
    assert s.header_confidence == 0.5 and s.needs_review(0.8)


def test_header_with_role_hints_has_full_header_confidence():
    """A header naming known roles keeps header confidence 1.0."""
    assert detect_schema(_irs_grid(), **KW).header_confidence == 1.0
