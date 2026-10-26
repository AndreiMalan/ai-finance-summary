"""Contracts for uploaded datasets: references, column mappings, detection results, validation reports."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from statbrief.domain.observation import Frequency

FileKind = Literal["csv", "xlsx", "xls", "json"]
ColumnRole = Literal["date", "region", "indicator", "value", "unit", "ignore"]
DateFormat = Literal["auto", "DMY", "MDY", "excel_serial"]
DecimalSep = Literal["auto", ".", ","]
IssueCode = Literal[
    "missing_required", "duplicate_row", "duplicate_key_conflict", "missing_value", "invalid_date",
    "ambiguous_date", "non_numeric", "inconsistent_unit", "unknown_region", "undocumented_column",
    "formula_without_value", "frequency_mismatch",
]
Resolution = Literal["pending", "excluded", "fixed", "accepted"]


class DatasetRef(BaseModel):
    """A stored uploaded file that belongs to one user's report."""

    model_config = ConfigDict(frozen=True)
    file_id: str
    report_id: str
    user_id: str
    filename: str
    kind: FileKind
    size_bytes: int = Field(ge=0)
    sha256: str
    blob_key: str


class ColumnMapping(BaseModel):
    """How the columns of one table map to observation fields, plus parsing options."""

    worksheet: str | None = None
    header_row: int = Field(1, ge=1)  # 1-based row number of the header line in the file/sheet
    date_col: str
    value_col: str
    region_col: str | None = None
    indicator_col: str | None = None
    unit_col: str | None = None
    constants: dict[Literal["region", "indicator", "unit"], str] = Field(default_factory=dict)
    date_format: DateFormat = "auto"
    decimal: DecimalSep = "auto"
    column_descriptions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _complete(self) -> "ColumnMapping":
        """Region/indicator/unit come from a column or a constant; no column serves two roles."""
        for role in ("region", "indicator", "unit"):
            if not getattr(self, f"{role}_col") and not self.constants.get(role, "").strip():
                raise ValueError(f"{role}: map a column or set a constant")
        used = [c for c in (self.date_col, self.value_col, self.region_col, self.indicator_col, self.unit_col) if c]
        if len(used) != len(set(used)):
            raise ValueError("a column is mapped to more than one role")
        return self

    def mapped_columns(self) -> set[str]:
        """Columns used by this mapping."""
        return {c for c in (self.date_col, self.value_col, self.region_col, self.indicator_col, self.unit_col) if c}


class ColumnGuess(BaseModel):
    """Detected role for one column, with confidence in [0, 1]."""

    column: str
    role: ColumnRole
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = None


class DetectedSchema(BaseModel):
    """Result of automatic detection for one table."""

    worksheet: str | None
    header_row: int = Field(ge=1)
    columns: list[ColumnGuess]
    frequency: Frequency | None = None
    day_order: Literal["DMY", "MDY"] | None = None
    excel_serial: bool = False
    decimal: Literal[".", ","] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    def _col(self, role: str) -> ColumnGuess | None:
        """The column guessed for `role`, if any."""
        return next((c for c in self.columns if c.role == role), None)

    def suggested_mapping(self) -> ColumnMapping | None:
        """Best-guess mapping; None when no date or value column was found (constants needed otherwise)."""
        date, value = self._col("date"), self._col("value")
        if not date or not value:
            return None
        region, indicator, unit = self._col("region"), self._col("indicator"), self._col("unit")
        date_format = "excel_serial" if self.excel_serial else (self.day_order or "auto")
        try:
            return ColumnMapping(
                worksheet=self.worksheet, header_row=self.header_row, date_col=date.column, value_col=value.column,
                region_col=region.column if region else None,
                indicator_col=indicator.column if indicator else None,
                unit_col=unit.column if unit else None,
                date_format=date_format, decimal=self.decimal or "auto",
            )
        except ValueError:
            return None  # missing region/indicator/unit: the user must supply constants

    def needs_review(self, threshold: float) -> bool:
        """True when any mapped role is below `threshold` or the suggestion is incomplete."""
        mapped = [c for c in self.columns if c.role != "ignore"]
        return self.suggested_mapping() is None or any(c.confidence < threshold for c in mapped)


class ValidationIssue(BaseModel):
    """One group of problems of the same kind in the same column."""

    issue_id: str
    code: IssueCode
    severity: Literal["error", "warning"]
    rows: list[int] = Field(default_factory=list)  # original 1-based row numbers
    column: str | None = None
    message: str
    resolution: Resolution = "pending"


class ValidationReport(BaseModel):
    """All issues found in one table under one mapping."""

    file_id: str
    row_count: int = Field(ge=0)
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """True while any error is unresolved."""
        return any(i.severity == "error" and i.resolution == "pending" for i in self.issues)

    def with_resolution(self, issue_id: str, resolution: Resolution) -> "ValidationReport":
        """Copy of the report with one issue's resolution changed (KeyError if unknown)."""
        if not any(i.issue_id == issue_id for i in self.issues):
            raise KeyError(issue_id)
        issues = [i.model_copy(update={"resolution": resolution}) if i.issue_id == issue_id else i
                  for i in self.issues]
        return self.model_copy(update={"issues": issues})
