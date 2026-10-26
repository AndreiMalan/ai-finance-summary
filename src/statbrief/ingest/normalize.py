"""Turn validated rows into Observations with upload provenance; every exclusion is listed with reasons.

The report must belong to this file and table (same file_id and row count) or ValueError is raised.
Normalization refuses to run (UnresolvedIssues) while the report is blocking (an error neither excluded
nor fixed) or while re-validating the table still finds an error that the report does not resolve as
"excluded": "fixed" only counts once re-validation no longer finds the error. A row is excluded — never silently dropped — when any of these holds, and each applicable reason is listed
once (deduplicated by issue/problem code):
- it is a later occurrence named in the `duplicate_row` issue: only the first occurrence is kept,
  whatever the issue's resolution — "accepting" the duplicate_row warning still keeps only the first;
- it belongs to an issue (error or warning) resolved as "excluded";
- it still has a row-level error after parsing (a safety net; such errors are normally excluded issues).
"""
from dataclasses import dataclass, field

# Shared with ValidationReport.blocking so the two definitions cannot drift.
from statbrief.domain.dataset import ERROR_CLEARING_RESOLUTIONS, ColumnMapping, DatasetRef, ValidationReport
from statbrief.domain.observation import Observation, UploadProvenance, make_obs_id
from statbrief.ingest.readers import RawTable
from statbrief.ingest.rows import ParsedRow, parse_rows
from statbrief.ingest.validate import validate_table

# Issue codes whose rows are always excluded (the rows listed are the later, redundant occurrences).
_ALWAYS_EXCLUDED_CODES: frozenset[str] = frozenset({"duplicate_row"})
_EXCLUDED = "excluded"


class UnresolvedIssues(Exception):
    """Normalization was attempted while errors are still unresolved (the report is blocking)."""

    def __init__(self, issue_ids: list[str]):
        """Store the blocking error issue ids."""
        super().__init__(f"unresolved issues: {', '.join(issue_ids)}")
        self.issue_ids = issue_ids


@dataclass(frozen=True)
class ExcludedRow:
    """A source row not turned into an observation, with every reason (one per code, "code: message")."""

    row_number: int
    reasons: list[str]


@dataclass
class NormalizationResult:
    """Observations plus the explicit list of excluded rows (sorted by row number)."""

    observations: list[Observation] = field(default_factory=list)
    excluded: list[ExcludedRow] = field(default_factory=list)


def _blocking_ids(report: ValidationReport) -> list[str]:
    """Ids of errors whose resolution does not clear them (same rule as `report.blocking`)."""
    return [i.issue_id for i in report.issues if i.severity == "error" and i.resolution not in ERROR_CLEARING_RESOLUTIONS]


def _revalidated_ids(table: RawTable, mapping: ColumnMapping, ref: DatasetRef,
                     report: ValidationReport) -> list[str]:
    """Ids of errors that re-validation still finds and that the passed report does not resolve as excluded."""
    excluded = {i.issue_id for i in report.issues if i.resolution == _EXCLUDED}
    fresh = validate_table(table, mapping, ref.file_id)
    return [i.issue_id for i in fresh.issues if i.severity == "error" and i.issue_id not in excluded]


def _check_report_matches(table: RawTable, ref: DatasetRef, report: ValidationReport) -> None:
    """Raise ValueError when the report was produced for another file or another table."""
    if report.file_id != ref.file_id:
        raise ValueError(f"report is for file {report.file_id!r}, not {ref.file_id!r}")
    if report.row_count != len(table.rows):
        raise ValueError(f"report covers {report.row_count} rows, table has {len(table.rows)}")


def _issue_reasons(report: ValidationReport) -> dict[int, dict[str, str]]:
    """Row number → {code: reason} from issues whose rows must be excluded (first reason per code kept)."""
    reasons: dict[int, dict[str, str]] = {}
    for issue in report.issues:
        if issue.code in _ALWAYS_EXCLUDED_CODES or issue.resolution == _EXCLUDED:
            for rn in issue.rows:
                reasons.setdefault(rn, {}).setdefault(issue.code, f"{issue.code}: {issue.message}")
    return reasons


def _row_reasons(row: ParsedRow, from_issues: dict[str, str]) -> list[str]:
    """All reasons to exclude one row: issue reasons, then its own error problems (one per code)."""
    reasons = dict(from_issues)
    for p in row.problems:
        if p.severity == "error":
            reasons.setdefault(p.code, f"{p.code}: {p.message}")
    return list(reasons.values())


def _observation(row: ParsedRow, table: RawTable, mapping: ColumnMapping, ref: DatasetRef) -> Observation:
    """Observation for a clean row, pointing to file, worksheet, row and value column.

    The unit multiplier (power of ten, e.g. 3 for "thousand EUR") is stored as `unit_multiplier`; the
    value is kept exactly as written and is NOT scaled — downstream analytics decide how to combine scales.
    """
    return Observation(
        obs_id=make_obs_id(ref.file_id, row.row_number, row.indicator, row.region.code, row.period.period),
        indicator_code=row.indicator, region_code=row.region.code, region_level=row.region.level,
        period=row.period.period, frequency=row.period.frequency, value=row.value,
        unit=row.unit.code, unit_multiplier=row.unit.multiplier,
        provenance=UploadProvenance(file_id=ref.file_id, filename=ref.filename, worksheet=table.worksheet,
                                    row_number=row.row_number, column=mapping.value_col),
    )


def to_observations(table: RawTable, mapping: ColumnMapping, ref: DatasetRef,
                    report: ValidationReport) -> NormalizationResult:
    """Normalize a table whose errors have all been resolved; every excluded row is listed with reasons.

    Raises ValueError when the report does not match `ref`/`table`, and UnresolvedIssues with the
    blocking ids of the report followed by errors re-validation still finds (de-duplicated, stable order).
    Exclusions come from the passed report's resolutions.
    """
    _check_report_matches(table, ref, report)
    blocking = list(dict.fromkeys(_blocking_ids(report) + _revalidated_ids(table, mapping, ref, report)))
    if blocking:
        raise UnresolvedIssues(blocking)
    issue_reasons = _issue_reasons(report)
    result = NormalizationResult()
    for row in parse_rows(table, mapping):
        reasons = _row_reasons(row, issue_reasons.get(row.row_number, {}))
        if reasons:
            result.excluded.append(ExcludedRow(row.row_number, reasons))
        else:
            result.observations.append(_observation(row, table, mapping, ref))
    result.excluded.sort(key=lambda e: e.row_number)
    return result
