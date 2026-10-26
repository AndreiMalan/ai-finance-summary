"""Tests for normalization into provenance-tracked observations."""
import pytest

from statbrief.domain.dataset import ColumnMapping, DatasetRef
from statbrief.domain.observation import make_obs_id
from statbrief.ingest.normalize import UnresolvedIssues, to_observations
from statbrief.ingest.readers import RawTable
from statbrief.ingest.validate import validate_table

MAP = ColumnMapping(date_col="luna", region_col="judet", indicator_col="ind", value_col="v", unit_col="um",
                    worksheet="Inflation", header_row=3)
REF = DatasetRef(file_id="f1", report_id="r1", user_id="u1", filename="IRS.xlsx", kind="xlsx", size_bytes=10,
                 sha256="x", blob_key="b")


def _t(rows):
    """Table with rows numbered from 4."""
    return RawTable(columns=["luna", "judet", "ind", "v", "um"], rows=rows,
                    row_numbers=list(range(4, 4 + len(rows))), worksheet="Inflation")


def _codes(excluded_row):
    """Reason codes of one excluded row, in order."""
    return [r.split(":")[0] for r in excluded_row.reasons]


def test_observations_carry_full_provenance():
    """Each observation points to file, sheet, row and column."""
    t = _t([["2024-01", "Cluj", "HICP", "7,4", "%"], ["2024-02", "Iași", "HICP", "n/a", "%"]])
    res = to_observations(t, MAP, REF, validate_table(t, MAP, "f1"))
    o = res.observations[0]
    assert (o.region_code, o.region_level, o.period, o.frequency, o.value, o.unit) == \
        ("RO113", "nuts3", "2024-01", "M", 7.4, "%")
    p = o.provenance
    assert (p.file_id, p.filename, p.worksheet, p.row_number, p.column) == ("f1", "IRS.xlsx", "Inflation", 4, "v")
    assert res.observations[1].value is None  # gap kept, not dropped
    assert res.excluded == []


def test_blocking_report_refuses():
    """Unresolved errors stop normalization."""
    t = _t([["2024-13", "Cluj", "HICP", "1", "%"]])
    with pytest.raises(UnresolvedIssues) as exc:
        to_observations(t, MAP, REF, validate_table(t, MAP, "f1"))
    assert exc.value.issue_ids == ["invalid_date:luna"]


def test_blocking_list_matches_report_blocking():
    """Every error not excluded/fixed is pending, whatever its resolution value."""
    t = _t([["2024-13", "Cluj", "HICP", "1", "%"], ["2024-02", "Atlantis", "HICP", "2", "%"]])
    report = validate_table(t, MAP, "f1").with_resolution("invalid_date:luna", "excluded")
    assert report.blocking
    with pytest.raises(UnresolvedIssues) as exc:
        to_observations(t, MAP, REF, report)
    assert exc.value.issue_ids == ["unknown_region:judet"]


def test_excluded_rows_listed_with_reasons_and_duplicates_dropped():
    """Explicitly excluded and duplicate rows are listed, never silently discarded."""
    t = _t([["2024-13", "Cluj", "HICP", "1", "%"], ["2024-02", "Cluj", "HICP", "2", "%"],
            ["2024-02", "Cluj", "HICP", "2", "%"]])
    report = validate_table(t, MAP, "f1").with_resolution("invalid_date:luna", "excluded")
    res = to_observations(t, MAP, REF, report)
    assert [o.provenance.row_number for o in res.observations] == [5]
    assert [(e.row_number, e.reasons[0].split(":")[0]) for e in res.excluded] == \
        [(4, "invalid_date"), (6, "duplicate_row")]
    assert _codes(res.excluded[0]) == ["invalid_date"]  # issue + row problem → one reason


def test_each_reason_listed_once_per_code():
    """A duplicate row also in an excluded warning lists both reasons, each exactly once."""
    t = _t([["2024-01", "Cluj", "HICP", "n/a", "%"], ["2024-01", "Cluj", "HICP", "n/a", "%"],
            ["2024-02", "Cluj", "HICP", "3", "%"]])
    report = validate_table(t, MAP, "f1").with_resolution("missing_value:v", "excluded")
    res = to_observations(t, MAP, REF, report)
    assert [o.provenance.row_number for o in res.observations] == [6]
    assert [(e.row_number, sorted(_codes(e))) for e in res.excluded] == \
        [(4, ["missing_value"]), (5, ["duplicate_row", "missing_value"])]


def test_warning_resolved_excluded_removes_its_rows():
    """A user may exclude the rows of a warning (here a unit-scale warning)."""
    t = _t([["2024-01", "Cluj", "GDP", "5", "EUR"], ["2024-02", "Cluj", "GDP", "6", "thousand EUR"]])
    report = validate_table(t, MAP, "f1")
    assert report.issues[0].issue_id == "inconsistent_unit:um:GDP" and report.issues[0].severity == "warning"
    res = to_observations(t, MAP, REF, report.with_resolution("inconsistent_unit:um:GDP", "excluded"))
    assert res.observations == []
    assert [(e.row_number, _codes(e)) for e in res.excluded] == \
        [(4, ["inconsistent_unit"]), (5, ["inconsistent_unit"])]


def test_fixed_error_still_excludes_rows_that_fail_parsing():
    """Marking an error "fixed" without fixing the data still excludes the failing row, with its reason."""
    t = _t([["2024-13", "Cluj", "HICP", "1", "%"], ["2024-02", "Cluj", "HICP", "2", "%"]])
    report = validate_table(t, MAP, "f1").with_resolution("invalid_date:luna", "fixed")
    res = to_observations(t, MAP, REF, report)
    assert [o.provenance.row_number for o in res.observations] == [5]
    assert len(res.excluded) == 1 and res.excluded[0].row_number == 4
    assert _codes(res.excluded[0]) == ["invalid_date"]


def test_unit_multiplier_carried_value_not_scaled():
    """The unit multiplier is stored on the observation; the value is left as written."""
    t = _t([["2024", "Cluj", "GDP", "5", "thousand EUR"]])
    o = to_observations(t, MAP, REF, validate_table(t, MAP, "f1")).observations[0]
    assert (o.value, o.unit, o.unit_multiplier) == (5.0, "EUR", 3)


def test_obs_id_uses_canonical_region_code():
    """obs_id is built from file, row, indicator, canonical region code and period."""
    t = _t([["2024-01", "Cluj", "HICP", "1", "%"]])
    o = to_observations(t, MAP, REF, validate_table(t, MAP, "f1")).observations[0]
    assert o.obs_id == make_obs_id("f1", 4, "HICP", "RO113", "2024-01")


def test_obs_ids_unique_and_stable():
    """Re-normalizing yields the same ids; ids differ per row."""
    t = _t([["2024-01", "Cluj", "HICP", "1", "%"], ["2024-02", "Cluj", "HICP", "2", "%"]])
    r = validate_table(t, MAP, "f1")
    a, b = to_observations(t, MAP, REF, r), to_observations(t, MAP, REF, r)
    assert [o.obs_id for o in a.observations] == [o.obs_id for o in b.observations]
    assert len({o.obs_id for o in a.observations}) == 2
