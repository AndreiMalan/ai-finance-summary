"""Tests for the downloadable template."""
from statbrief.domain.dataset import ColumnMapping
from statbrief.ingest.readers import list_worksheets, read_grid, to_table
from statbrief.ingest.sniff import validate_file_bytes
from statbrief.ingest.template import TEMPLATE_COLUMNS, build_template_csv, build_template_xlsx
from statbrief.ingest.validate import validate_table

MAP = ColumnMapping(date_col="date", region_col="region", indicator_col="indicator", value_col="value",
                    unit_col="unit")


def test_template_files_validate_cleanly():
    """Both template formats parse and validate without issues under the canonical mapping."""
    for kind, data in (("csv", build_template_csv()), ("xlsx", build_template_xlsx())):
        t = to_table(read_grid(data, kind, worksheet="Data" if kind == "xlsx" else None), header_row=1)
        assert t.columns == TEMPLATE_COLUMNS
        assert validate_table(t, MAP, "tmpl").issues == []


def test_template_xlsx_passes_upload_checks_and_has_readme():
    """The XLSX template is accepted by the upload sniffer and has a visible Data sheet first plus a README."""
    data = build_template_xlsx()
    assert validate_file_bytes("t.xlsx", data, 10 * 1024 * 1024, 100, 200 * 1024 * 1024).kind == "xlsx"
    assert [s.name for s in list_worksheets(data, "xlsx")] == ["Data", "README"]
    readme = read_grid(data, "xlsx", worksheet="README")
    text = " ".join(str(c) for r in readme.rows for c in r)
    for col in TEMPLATE_COLUMNS:
        assert col in text
