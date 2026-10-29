"""sample_data/: every file passes sniffing and detection, planted issues are found, planted conflicts are
detected against the recorded Eurostat HICP responses (offline)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from statbrief.analytics.combine import combine, detect_conflicts
from statbrief.config import Settings
from statbrief.domain.conflict import IndicatorLink
from statbrief.domain.dataset import DatasetRef
from statbrief.ingest.detect import detect_schema
from statbrief.ingest.normalize import to_observations
from statbrief.ingest.readers import list_worksheets, read_grid, to_table
from statbrief.ingest.sniff import validate_file_bytes
from statbrief.ingest.validate import validate_table
from statbrief.sources.catalog import CATALOG, advanced_spec
from statbrief.sources.eurostat import parse_jsonstat

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "sample_data"
EUROSTAT = ROOT / "tests" / "data" / "eurostat"
IRS = "IRS_regional_monthly_2023-2024.xlsx"
_S = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
_MB = 1024 * 1024
_ROLES = {"date", "region", "indicator", "value", "unit"}
T = datetime(2026, 10, 29, tzinfo=timezone.utc)


def _detect(name: str, kind: str, worksheet: str | None = None):
    """Grid and detected schema of a sample file."""
    grid = read_grid((SAMPLES / name).read_bytes(), kind, worksheet=worksheet)
    return grid, detect_schema(grid, sample_rows=_S.detect_sample_rows, scan_rows=_S.detect_header_scan_rows)


def _validated(worksheet: str):
    """Table, mapping and validation report of an IRS workbook sheet under the suggested mapping."""
    grid, schema = _detect(IRS, "xlsx", worksheet)
    mapping = schema.suggested_mapping()
    table = to_table(grid, mapping.header_row)
    return table, mapping, validate_table(table, mapping, "f1")


@pytest.mark.parametrize("name,kind", [(IRS, "xlsx"), ("sector_output_Q.csv", "csv"),
                                       ("regional_indicators.json", "json"), ("cryptic_columns.csv", "csv")])
def test_every_file_passes_sniffing(name, kind):
    """Each sample is accepted by content sniffing with its kind and within the default upload limits."""
    check = validate_file_bytes(name, (SAMPLES / name).read_bytes(), _S.max_upload_mb * _MB,
                                _S.max_uncompressed_ratio, _S.max_uncompressed_mb * _MB)
    assert check.kind == kind


def test_irs_workbook_has_two_visible_sheets():
    """Preturi and Somaj, both visible."""
    sheets = list_worksheets((SAMPLES / IRS).read_bytes(), "xlsx")
    assert [(s.name, s.hidden) for s in sheets] == [("Preturi", False), ("Somaj", False)]


@pytest.mark.parametrize("name,kind,worksheet,freq,expected", [
    (IRS, "xlsx", "Preturi", "M", {"luna": "date", "judet": "region", "indicator": "indicator",
                                   "valoare": "value", "um": "unit", "val_adj_x": "ignore"}),
    (IRS, "xlsx", "Somaj", "M", {"luna": "date", "judet": "region", "indicator": "indicator",
                                 "valoare": "value", "um": "unit", "val_adj_x": "ignore"}),
    ("sector_output_Q.csv", "csv", None, "Q", {"period": "date", "region": "region", "indicator": "indicator",
                                               "value": "value", "unit": "unit"}),
    ("regional_indicators.json", "json", None, "M", {"luna": "date", "judet": "region", "indicator": "indicator",
                                                     "valoare": "value", "um": "unit"}),
])
def test_detection_finds_expected_roles(name, kind, worksheet, freq, expected):
    """Clean headers are mapped confidently, with the right frequency, and need no review."""
    _, schema = _detect(name, kind, worksheet)
    assert {c.column: c.role for c in schema.columns} == expected
    assert schema.frequency == freq and not schema.needs_review(_S.mapping_min_confidence)


def test_undocumented_column_is_flagged():
    """val_adj_x is ignored and noted as undocumented (guard demo)."""
    _, schema = _detect(IRS, "xlsx", "Preturi")
    guess = next(c for c in schema.columns if c.column == "val_adj_x")
    assert guess.role == "ignore" and guess.note == "undocumented"


def test_thousands_separators_and_dmy_dates_are_detected():
    """The sector CSV uses '.' decimals with ',' grouping; the workbook's dd.mm.yyyy dates set DMY."""
    assert _detect("sector_output_Q.csv", "csv")[1].decimal == "."
    assert _detect(IRS, "xlsx", "Preturi")[1].day_order == "DMY"


def test_cryptic_columns_need_review_and_have_no_complete_mapping():
    """Meaningless names give low confidence and no unit: the user must confirm the mapping."""
    _, schema = _detect("cryptic_columns.csv", "csv")
    assert schema.needs_review(_S.mapping_min_confidence)
    assert schema.suggested_mapping() is None
    assert all(c.confidence < _S.mapping_min_confidence for c in schema.columns if c.role in _ROLES)
    assert {c.column for c in schema.columns if c.note == "undocumented"} == {"zz_flag", "adj2"}


def test_planted_issues_in_preturi_are_detected():
    """Text in a numeric cell, unknown county (errors), duplicate row and undocumented column (warnings)."""
    _, _, report = _validated("Preturi")
    found = {(i.code, i.severity, tuple(i.rows)) for i in report.issues}
    assert found == {("non_numeric", "error", (67,)), ("unknown_region", "error", (147,)),
                     ("duplicate_row", "warning", (88,)), ("undocumented_column", "warning", ())}
    assert "5,9 provizoriu" in next(i.message for i in report.issues if i.code == "non_numeric")
    assert "Cluj-Napoca" in next(i.message for i in report.issues if i.code == "unknown_region")


def test_planted_missing_value_in_somaj():
    """One empty value (warning) plus the undocumented column."""
    _, _, report = _validated("Somaj")
    assert {(i.code, tuple(i.rows)) for i in report.issues} == {("missing_value", (241,)),
                                                                 ("undocumented_column", ())}


def _irs_observations():
    """Observations of sheet Preturi after excluding its errors (as a user would)."""
    table, mapping, report = _validated("Preturi")
    for issue in report.issues:
        if issue.severity == "error":
            report = report.with_resolution(issue.issue_id, "excluded")
    ref = DatasetRef(file_id="f1", report_id="r1", user_id="u1", filename=IRS, kind="xlsx", size_bytes=1,
                     sha256="h", blob_key="k")
    return to_observations(table, mapping, ref, report)


def test_dmy_dates_normalise_to_months():
    """15.03.2024 and 15.04.2024 (Timis) become the monthly periods 2024-03 and 2024-04."""
    result = _irs_observations()
    timis = {o.provenance.row_number: o.period for o in result.observations if o.region_code == "RO424"}
    assert timis[113] == "2024-03" and timis[114] == "2024-04"
    assert sorted(e.row_number for e in result.excluded) == [67, 88, 147]


def _eurostat(name: str, spec):
    """Observations parsed from a recorded Eurostat response."""
    return parse_jsonstat(json.loads((EUROSTAT / name).read_text(encoding="utf-8")), spec,
                          "https://ec.europa.eu/eurostat/recorded", T).observations


def test_planted_conflicts_against_recorded_eurostat_hicp():
    """Rates conflict in exactly the three planted months; index levels are unit differences (§3.2)."""
    index_spec = advanced_spec("eurostat", "prc_hicp_minr", {"coicop18": "TOTAL", "unit": "I15"})
    api = (_eurostat("hicp_ro_rch_a_2023_2024.json", CATALOG["HICP_YOY"])
           + _eurostat("hicp_ro_i15_2021_2024.json", index_spec))
    links = [IndicatorLink(upload_code="IPC_VAR_AN", catalog_code="HICP_YOY"),
             IndicatorLink(upload_code="IPC", catalog_code=index_spec.code)]
    scan = detect_conflicts(combine(_irs_observations().observations, api, links), rel_tol=_S.conflict_rel_tol)
    assert sorted((c.indicator, c.region, c.period) for c in scan.conflicts) == [
        ("HICP_YOY", "RO", "2023-04"), ("HICP_YOY", "RO", "2023-11"), ("HICP_YOY", "RO", "2024-07")]
    assert len(scan.unit_mismatches) == 24
    assert {tuple(m.units) for m in scan.unit_mismatches} == {("I21", "I15")}
    assert {m.indicator for m in scan.unit_mismatches} == {index_spec.code}
