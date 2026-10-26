"""Tests for dataset contracts: mappings, detection results, validation reports."""
import pytest
from pydantic import ValidationError

from statbrief.domain.dataset import ColumnGuess, ColumnMapping, DetectedSchema, ValidationIssue, ValidationReport


def test_mapping_requires_region_indicator_unit_from_column_or_constant():
    """Each of region/indicator/unit must come from a column or a constant."""
    ColumnMapping(date_col="luna", value_col="v", region_col="judet", indicator_col="ind", unit_col="um")
    ColumnMapping(date_col="luna", value_col="v", constants={"region": "RO", "indicator": "HICP", "unit": "%"})
    with pytest.raises(ValidationError, match="region"):
        ColumnMapping(date_col="luna", value_col="v", indicator_col="ind", unit_col="um")


def test_mapping_rejects_unknown_options_and_role_reuse():
    """Unknown parse options and one column used for two roles are rejected."""
    with pytest.raises(ValidationError):
        ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "I", "unit": "%"},
                      date_format="YYYY")
    with pytest.raises(ValidationError, match="more than one role"):
        ColumnMapping(date_col="d", value_col="d", constants={"region": "RO", "indicator": "I", "unit": "%"})


def test_detected_schema_suggested_mapping_and_review():
    """A schema with date+value suggests a mapping; low confidence needs review."""
    cols = [ColumnGuess(column="luna", role="date", confidence=0.95),
            ColumnGuess(column="judet", role="region", confidence=0.9),
            ColumnGuess(column="indicator", role="indicator", confidence=0.85),
            ColumnGuess(column="valoare", role="value", confidence=0.97),
            ColumnGuess(column="um", role="unit", confidence=0.6),
            ColumnGuess(column="val_adj_x", role="ignore", confidence=0.0, note="undocumented")]
    s = DetectedSchema(worksheet="Inflation", header_row=3, columns=cols, frequency="M")
    m = s.suggested_mapping()
    assert m is not None and (m.date_col, m.region_col, m.unit_col, m.header_row) == ("luna", "judet", "um", 3)
    assert s.needs_review(0.8) is True
    assert DetectedSchema(worksheet=None, header_row=1, columns=cols[:1]).suggested_mapping() is None


def test_report_blocking_and_resolution():
    """Pending errors block; resolving them (or only warnings) unblocks; resolution returns a copy."""
    err = ValidationIssue(issue_id="invalid_date:luna", code="invalid_date", severity="error", rows=[18],
                          column="luna", message="bad")
    warn = ValidationIssue(issue_id="missing_value:v", code="missing_value", severity="warning", rows=[9],
                           column="v", message="gap")
    r = ValidationReport(file_id="f1", row_count=10, issues=[err, warn])
    assert r.blocking
    r2 = r.with_resolution("invalid_date:luna", "excluded")
    assert not r2.blocking and r.blocking
    with pytest.raises(KeyError):
        r.with_resolution("nope", "excluded")
