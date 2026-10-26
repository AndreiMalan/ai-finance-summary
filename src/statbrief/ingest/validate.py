"""Validate a table under a mapping: row problems + table-level checks → grouped, explicit issues."""
from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from statbrief.domain.dataset import ColumnMapping, IssueCode, ValidationIssue, ValidationReport
from statbrief.ingest.readers import RawTable
from statbrief.ingest.rows import ParsedRow, parse_rows


class _Collector:
    """Groups problems into one issue per (issue_id)."""

    def __init__(self) -> None:
        """Start empty."""
        self.issues: dict[str, dict] = {}

    def add(self, issue_id: str, code: IssueCode, severity: Literal["error", "warning"], column: str | None,
            message: str, rows: Iterable[int] = ()) -> None:
        """Add rows to an issue, creating it on first use (the first message is kept)."""
        it = self.issues.setdefault(issue_id, {"code": code, "severity": severity, "column": column,
                                               "message": message, "rows": set()})
        it["rows"].update(rows)

    def report(self, file_id: str, row_count: int) -> ValidationReport:
        """Freeze into a ValidationReport (rows sorted)."""
        return ValidationReport(file_id=file_id, row_count=row_count, issues=[
            ValidationIssue(issue_id=iid, code=d["code"], severity=d["severity"], column=d["column"],
                            message=d["message"], rows=sorted(d["rows"]))
            for iid, d in self.issues.items()])


def _row_issues(rows: list[ParsedRow], col: _Collector) -> None:
    """One issue per (code, column) from row problems."""
    for r in rows:
        for p in r.problems:
            col.add(f"{p.code}:{p.column or '-'}", p.code, p.severity, p.column, p.message, [r.row_number])


def _duplicate_issues(rows: list[ParsedRow], col: _Collector) -> None:
    """Exact duplicates (warning) and same-key/different-value conflicts (error)."""
    by_key: dict[tuple, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.ok and r.period and r.region and r.indicator:
            by_key[(r.indicator, r.region.code, r.period.period)].append(r)
    for group in by_key.values():
        if len(group) < 2:
            continue
        values = {(g.value, g.unit.code if g.unit else None) for g in group}
        if len(values) == 1:
            col.add("duplicate_row:-", "duplicate_row", "warning", None,
                    "Identical rows: only the first occurrence will be used.", [g.row_number for g in group[1:]])
        else:
            col.add("duplicate_key_conflict:-", "duplicate_key_conflict", "error", None,
                    "Different values for the same indicator, region and period.", [g.row_number for g in group])


def _per_indicator_issues(rows: list[ParsedRow], mapping: ColumnMapping, col: _Collector) -> None:
    """Unit and frequency consistency within each indicator."""
    groups: dict[str, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.indicator:
            groups[r.indicator].append(r)
    for ind, group in groups.items():
        with_unit = [r for r in group if r.unit]
        codes = {r.unit.code for r in with_unit}
        spellings = {r.unit.raw for r in with_unit}
        if len(codes) > 1:
            col.add(f"inconsistent_unit:{mapping.unit_col or '-'}:{ind}", "inconsistent_unit", "error",
                    mapping.unit_col, f"{ind} uses different units: {', '.join(sorted(codes))}.",
                    [r.row_number for r in with_unit])
        elif len(spellings) > 1:
            col.add(f"inconsistent_unit:{mapping.unit_col or '-'}:{ind}", "inconsistent_unit", "warning",
                    mapping.unit_col, f"{ind}: spellings {', '.join(sorted(spellings))} mean the same unit; "
                    "confirm to treat them as one.", [r.row_number for r in with_unit])
        freqs = {r.period.frequency for r in group if r.period}
        if len(freqs) > 1:
            col.add(f"frequency_mismatch:{mapping.date_col}:{ind}", "frequency_mismatch", "error", mapping.date_col,
                    f"{ind} mixes frequencies ({', '.join(sorted(freqs))}).",
                    [r.row_number for r in group if r.period])


def validate_table(table: RawTable, mapping: ColumnMapping, file_id: str,
                   known_columns: frozenset[str] = frozenset()) -> ValidationReport:
    """All issues for a table under a mapping; rows are referenced by original row number."""
    rows = parse_rows(table, mapping)
    col = _Collector()
    _row_issues(rows, col)
    _duplicate_issues(rows, col)
    _per_indicator_issues(rows, mapping, col)
    documented = mapping.mapped_columns() | set(mapping.column_descriptions) | set(known_columns)
    for c in table.columns:
        if c not in documented:
            col.add(f"undocumented_column:{c}", "undocumented_column", "warning", c,
                    f"Column {c!r} has no documented meaning; it will be ignored unless you describe it.")
    return col.report(file_id, len(table.rows))
