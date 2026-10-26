"""Tests for row parsing and table validation."""
from datetime import datetime

from statbrief.domain.dataset import ColumnMapping
from statbrief.ingest.readers import FORMULA_NO_VALUE, RawTable
from statbrief.ingest.rows import parse_rows
from statbrief.ingest.validate import validate_table

COLS = ["luna", "judet", "indicator", "valoare", "um", "val_adj_x"]
MAP = ColumnMapping(date_col="luna", region_col="judet", indicator_col="indicator", value_col="valoare",
                    unit_col="um", header_row=3)


def _table(rows):
    """Table with row numbers starting at 4 (header on row 3)."""
    return RawTable(columns=COLS, rows=rows, row_numbers=list(range(4, 4 + len(rows))), worksheet="Inflation")


def _issues(report):
    """{issue_id: (severity, rows)}."""
    return {i.issue_id: (i.severity, i.rows) for i in report.issues}


def test_clean_table_only_undocumented_column():
    """A clean table yields just the undocumented-column warning."""
    t = _table([[datetime(2024, 1, 1), "Cluj", "HICP", 7.4, "%", 1.0],
                [datetime(2024, 2, 1), "Cluj", "HICP", 7.1, "%", 1.0]])
    r = validate_table(t, MAP, "f1")
    assert _issues(r) == {"undocumented_column:val_adj_x": ("warning", [])}
    assert not r.blocking and r.row_count == 2
    rows = parse_rows(t, MAP)
    assert rows[0].ok and rows[0].period.period == "2024-01" and rows[0].region.code == "RO113"


def test_every_problem_type_is_reported_with_row_numbers():
    """Each documented issue code is produced with original row numbers; nothing dropped silently."""
    t = _table([
        ["2024-13", "Cluj", "HICP", "7,4", "%", 1],          # 4 invalid date
        ["01/02/2024", "Cluj", "HICP", "7,1", "%", 1],       # 5 ambiguous date
        ["2024-03", "Atlantis", "HICP", "6,6", "%", 1],      # 6 unknown region
        ["2024-03", "Cluj", "HICP", "abc", "%", 1],          # 7 non numeric
        ["2024-04", "Cluj", "HICP", "n/a", "%", 1],          # 8 missing value (warning)
        ["2024-05", "Cluj", "HICP", FORMULA_NO_VALUE, "%", 1],  # 9 formula without value
        ["2024-06", "Cluj", "", "6,0", "%", 1],              # 10 missing indicator
        ["2024-07", "Cluj", "HICP", "5,9", "%", 1],          # 11
        ["2024-07", "Cluj", "HICP", "5,9", "%", 1],          # 12 exact duplicate of 11
        ["2024-08", "Cluj", "HICP", "5,8", "%", 1],          # 13
        ["2024-08", "Cluj", "HICP", "5,1", "%", 1],          # 14 conflicting value for same key
        ["2024-09", "Cluj", "HICP", "5,7", "percent", 1],    # 15 spelling variant of %
        ["2024-10", "Cluj", "HICP", "5,6", "EUR", 1],        # 16 different unit
        ["2024", "Cluj", "HICP", "5,5", "%", 1],             # 17 annual inside monthly indicator
    ])
    issues = _issues(validate_table(t, MAP, "f1"))
    assert issues["invalid_date:luna"] == ("error", [4])
    assert issues["ambiguous_date:luna"] == ("error", [5])
    assert issues["unknown_region:judet"] == ("error", [6])
    assert issues["non_numeric:valoare"] == ("error", [7])
    assert issues["missing_value:valoare"] == ("warning", [8])
    assert issues["formula_without_value:valoare"] == ("error", [9])
    assert issues["missing_required:indicator"] == ("error", [10])
    assert issues["duplicate_row:-"] == ("warning", [12])
    assert issues["duplicate_key_conflict:-"] == ("error", [13, 14])
    assert issues["inconsistent_unit:um:HICP"][0] == "error"
    assert issues["frequency_mismatch:luna:HICP"][0] == "error"
    assert "undocumented_column:val_adj_x" in issues


def test_constants_and_date_format_options():
    """Constants replace missing columns; DMY resolves ambiguity; descriptions document columns."""
    m = ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "UNEMP", "unit": "%"},
                      date_format="DMY", decimal=",", column_descriptions={"note": "free text"})
    t = RawTable(columns=["d", "v", "note"], rows=[["01/02/2024", "5,4", "x"]], row_numbers=[2])
    r = validate_table(t, m, "f1")
    assert r.issues == [] and parse_rows(t, m)[0].period.period == "2024-02"


def test_spelling_only_unit_variation_is_warning():
    """'%' vs 'percent' within one indicator is a warning, not an error."""
    t = _table([["2024-01", "Cluj", "HICP", "1", "%", 1], ["2024-02", "Cluj", "HICP", "2", "percent", 1]])
    issues = _issues(validate_table(t, MAP, "f1", known_columns=frozenset({"val_adj_x"})))
    assert issues == {"inconsistent_unit:um:HICP": ("warning", [4, 5])}


def _value_table(values, decimal="auto"):
    """One-indicator table with the given value cells and decimal option."""
    m = MAP.model_copy(update={"decimal": decimal})
    rows = [[f"2024-{i + 1:02d}", "Cluj", "HICP", v, "%", 1] for i, v in enumerate(values)]
    return _table(rows), m


def test_thousands_lookalikes_without_evidence_are_ambiguous():
    """'1,234' with no decimal evidence is never guessed: it is flagged as ambiguous_number."""
    t, m = _value_table(["1,234", "2,345"])
    rows = parse_rows(t, m)
    assert [r.value for r in rows] == [None, None]
    assert all(p.code == "ambiguous_number" and p.severity == "error" for r in rows for p in r.problems)
    issues = _issues(validate_table(t, m, "f1", known_columns=frozenset({"val_adj_x"})))
    assert issues == {"ambiguous_number:valoare": ("error", [4, 5])}


def test_explicit_decimal_resolves_lookalikes():
    """With decimal ',' chosen in the mapping, '1,234' is the decimal 1.234; with '.' it is 1234."""
    t, m = _value_table(["1,234", "2,345"], decimal=",")
    assert [r.value for r in parse_rows(t, m)] == [1.234, 2.345]
    t, m = _value_table(["1,234", "2,345"], decimal=".")
    assert [r.value for r in parse_rows(t, m)] == [1234.0, 2345.0]


def test_decimal_inferred_once_per_table():
    """A plain decimal elsewhere in the column decides the separator for look-alikes too."""
    t, m = _value_table(["1,234", "7,4"])
    assert [r.value for r in parse_rows(t, m)] == [1.234, 7.4]
    t, m = _value_table(["1,234", "7.4"])
    assert [r.value for r in parse_rows(t, m)] == [1234.0, 7.4]
    t, m = _value_table(["7,4", "7,1"])
    assert [r.value for r in parse_rows(t, m)] == [7.4, 7.1]


def test_formula_in_region_and_missing_region_value():
    """Formula-without-value in a non-value column and an empty region are explicit errors."""
    t = _table([["2024-01", FORMULA_NO_VALUE, "HICP", "1", "%", 1], ["2024-02", " ", "HICP", "2", "%", 1]])
    issues = _issues(validate_table(t, MAP, "f1", known_columns=frozenset({"val_adj_x"})))
    assert issues == {"formula_without_value:judet": ("error", [4]), "missing_required:judet": ("error", [5])}


def test_mapped_column_absent_from_table_is_reported():
    """A mapping naming a column the table lacks yields an explicit error for every row, not a crash."""
    t = RawTable(columns=["luna", "valoare"], rows=[["2024-01", "1"]], row_numbers=[2])
    m = ColumnMapping(date_col="luna", value_col="valoare", region_col="judet",
                      constants={"indicator": "HICP", "unit": "%"})
    issues = _issues(validate_table(t, m, "f1"))
    assert issues == {"missing_required:judet": ("error", [2])}


def test_percent_suffix_does_not_bypass_lookalike_check():
    """'1,234%' is as ambiguous as '1,234'; '%' values also count as decimal evidence."""
    t, m = _value_table(["1,234%"])
    assert [p.code for p in parse_rows(t, m)[0].problems] == ["ambiguous_number"]
    t, m = _value_table(["7.4%", "1,234%"])
    assert [r.value for r in parse_rows(t, m)] == [7.4, 1234.0]


def test_zero_head_is_not_a_thousands_lookalike():
    """'0,123' / '-0.123' cannot be thousands, so they parse as decimals without an ambiguity error."""
    t, m = _value_table(["0,123", "-0.123"])
    rows = parse_rows(t, m)
    assert [r.value for r in rows] == [0.123, -0.123] and all(r.ok for r in rows)


def test_mixed_decimal_separators_warn():
    """Equal evidence for ',' and '.' yields an explicit warning on the value column."""
    t, m = _value_table(["7,4", "7.4"])
    issues = _issues(validate_table(t, m, "f1", known_columns=frozenset({"val_adj_x"})))
    assert issues == {"ambiguous_number:valoare:mixed": ("warning", [4, 5])}
    report = validate_table(t, m, "f1", known_columns=frozenset({"val_adj_x"}))
    assert report.issues[0].message == ("Both ',' and '.' are used as decimal separators; "
                                        "choose one in the mapping.")


def test_unit_scale_differences():
    """'5 EUR' vs '5 thousand EUR' for one key is a conflict; across periods it is a scale warning."""
    t = _table([["2024-01", "Cluj", "GDP", 5, "EUR", 1], ["2024-01", "Cluj", "GDP", 5, "thousand EUR", 1]])
    issues = _issues(validate_table(t, MAP, "f1", known_columns=frozenset({"val_adj_x"})))
    assert issues["duplicate_key_conflict:-"] == ("error", [4, 5]) and "duplicate_row:-" not in issues
    t = _table([["2024-01", "Cluj", "GDP", 5, "EUR", 1], ["2024-02", "Cluj", "GDP", 5, "thousand EUR", 1]])
    report = validate_table(t, MAP, "f1", known_columns=frozenset({"val_adj_x"}))
    [issue] = report.issues
    assert issue.issue_id == "inconsistent_unit:um:GDP" and issue.severity == "warning"
    assert "different scales" in issue.message and "EUR" in issue.message and "thousand EUR" in issue.message


def test_grouped_message_lists_distinct_values():
    """A grouped issue names every distinct offending value (capped), not only the first."""
    t = _table([["2024-01", "Atlantis", "X", 1, "%", 1], ["2024-02", "Mordor", "X", 1, "%", 1],
                ["2024-03", "Atlantis", "X", 1, "%", 1]])
    [issue] = [i for i in validate_table(t, MAP, "f1").issues if i.code == "unknown_region"]
    assert "'Atlantis'" in issue.message and "'Mordor'" in issue.message and issue.rows == [4, 5, 6]


def test_grouped_message_caps_value_list():
    """More distinct values than the cap are summarised as '(+N more)'."""
    from statbrief.ingest.validate import _MAX_VALUES_IN_MESSAGE
    n = _MAX_VALUES_IN_MESSAGE + 2
    t = _table([[f"2024-{i + 1:02d}", f"Nowhere{i}", "X", 1, "%", 1] for i in range(n)])
    [issue] = [i for i in validate_table(t, MAP, "f1").issues if i.code == "unknown_region"]
    assert "(+2 more)" in issue.message and f"'Nowhere{n - 1}'" not in issue.message


def test_yyyymm_periods_parse_in_date_named_column_only():
    """202403 is March 2024 under a date-named header ("luna"), but an invalid date under "cod"."""
    ok = parse_rows(_table([[202403, "Cluj", "HICP", 7.4, "%", 1.0]]), MAP)[0]
    assert ok.period is not None and ok.period.period == "2024-03" and ok.ok
    cols = ["cod", "judet", "indicator", "valoare", "um", "val_adj_x"]
    t = RawTable(columns=cols, rows=[[202403, "Cluj", "HICP", 7.4, "%", 1.0]], row_numbers=[4])
    bad = parse_rows(t, MAP.model_copy(update={"date_col": "cod"}))[0]
    assert [p.code for p in bad.problems] == ["invalid_date"]
