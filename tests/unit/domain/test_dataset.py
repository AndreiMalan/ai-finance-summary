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


def _issue(severity="error", **kw):
    """Build a validation issue, overriding fields."""
    base = dict(issue_id="i1", code="invalid_date", severity=severity, rows=[2], column="d", message="m")
    base.update(kw)
    return ValidationIssue(**base)


def test_with_resolution_rejects_invalid_value():
    """An unknown resolution value is rejected by validation, not silently stored."""
    r = ValidationReport(file_id="f1", row_count=3, issues=[_issue()])
    with pytest.raises(ValidationError):
        r.with_resolution("i1", "exclude")  # type: ignore[arg-type]


def test_with_resolution_rejects_accepted_on_error():
    """Errors can only be excluded or fixed; warnings may be accepted."""
    r = ValidationReport(file_id="f1", row_count=3, issues=[_issue(), _issue("warning", issue_id="w1")])
    with pytest.raises(ValueError, match="excluded or fixed"):
        r.with_resolution("i1", "accepted")
    assert r.with_resolution("w1", "accepted").issues[1].resolution == "accepted"


@pytest.mark.parametrize("resolution,blocking", [("pending", True), ("excluded", False), ("fixed", False)])
def test_blocking_semantics(resolution, blocking):
    """Only an error that is excluded or fixed stops blocking; warnings never block."""
    r = ValidationReport(file_id="f1", row_count=3, issues=[_issue(resolution=resolution), _issue("warning", issue_id="w")])
    assert r.blocking is blocking


def test_mapping_rejects_empty_required_columns():
    """date_col/value_col must be non-empty names."""
    consts = {"region": "RO", "indicator": "I", "unit": "%"}
    with pytest.raises(ValidationError):
        ColumnMapping(date_col="", value_col="v", constants=consts)
    with pytest.raises(ValidationError):
        ColumnMapping(date_col="d", value_col="   ", constants=consts)


def test_mapping_blank_optional_column_is_none_and_names_stripped():
    """A whitespace column name counts as unmapped; names and constants are stripped."""
    with pytest.raises(ValidationError, match="region"):
        ColumnMapping(date_col="d", value_col="v", region_col="  ", indicator_col="i", unit_col="u")
    with pytest.raises(ValidationError, match="region"):
        ColumnMapping(date_col="d", value_col="v", indicator_col="i", unit_col="u", constants={"region": "  "})
    m = ColumnMapping(date_col="d", value_col="v", region_col=" judet ", indicator_col="i", unit_col="u",
                      constants={"region": " "})
    assert m.region_col == "judet" and m.constants == {}
    m2 = ColumnMapping(date_col="d", value_col="v", constants={"region": " RO ", "indicator": "I", "unit": "%"})
    assert m2.constants["region"] == "RO"


@pytest.mark.parametrize("role", ["region", "indicator", "unit"])
def test_mapping_rejects_column_and_constant_for_same_role(role):
    """A role comes from a column or a constant, not both."""
    cols = {"region_col": "r", "indicator_col": "i", "unit_col": "u"}
    with pytest.raises(ValidationError, match=f"{role}: use either a column or a constant"):
        ColumnMapping(date_col="d", value_col="v", constants={role: "X"}, **cols)
