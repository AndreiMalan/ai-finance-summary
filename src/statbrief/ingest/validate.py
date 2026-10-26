"""Validate a table under a mapping: row problems + table-level checks → grouped, explicit issues.

Duplicate and conflict detection only considers rows without errors: a conflict involving a row
that has an error surfaces once that error is fixed (the error itself is already reported). A gap
(missing value, None) and a value for the same indicator, region and period is a conflict, and so
is the same number in units of a different scale ("5 EUR" vs "5 thousand EUR").
"""
from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from statbrief.domain.dataset import ColumnMapping, IssueCode, ValidationIssue, ValidationReport
from statbrief.ingest.numbers import decimal_evidence
from statbrief.ingest.readers import RawTable
from statbrief.ingest.rows import ParsedRow, parse_rows

# Distinct offending values named in one grouped message; the rest are counted as "(+N more)".
_MAX_VALUES_IN_MESSAGE = 5
# Codes whose grouped message lists the offending values: code → (label, hint appended after the list).
_VALUE_MESSAGES: dict[str, tuple[str, str]] = {
    "unknown_region": ("Unknown regions", "Use a NUTS code or a known county/region name."),
    "non_numeric": ("Not numbers", ""),
    "invalid_date": ("Unrecognised or impossible dates", ""),
    "ambiguous_date": ("Ambiguous dates", "Choose DD/MM or MM/DD in the mapping."),
    "ambiguous_number": ("Ambiguous numbers (decimal or thousands separator?)",
                         "Choose the decimal separator in the mapping."),
}
_MIXED_DECIMAL_MESSAGE = "Both ',' and '.' are used as decimal separators; choose one in the mapping."


class _Collector:
    """Groups problems into one issue per issue_id, collecting rows and distinct offending values."""

    def __init__(self) -> None:
        """Start empty."""
        self.issues: dict[str, dict] = {}

    def add(self, issue_id: str, code: IssueCode, severity: Literal["error", "warning"], column: str | None,
            message: str, rows: Iterable[int] = (), value: str | None = None) -> None:
        """Add rows (and an offending value) to an issue, creating it on first use (first message kept)."""
        it = self.issues.setdefault(issue_id, {"code": code, "severity": severity, "column": column,
                                               "message": message, "rows": set(), "values": {}})
        it["rows"].update(rows)
        if value is not None:
            it["values"][value] = None  # dict as an insertion-ordered set

    @staticmethod
    def _message(d: dict) -> str:
        """The issue message; for value-listing codes, the distinct values (capped) instead of the first."""
        if d["code"] not in _VALUE_MESSAGES or not d["values"]:
            return d["message"]
        label, hint = _VALUE_MESSAGES[d["code"]]
        values = list(d["values"])
        shown = ", ".join(values[:_MAX_VALUES_IN_MESSAGE])
        more = len(values) - _MAX_VALUES_IN_MESSAGE
        text = f"{label}: {shown}" + (f" (+{more} more)" if more > 0 else "") + "."
        return f"{text} {hint}" if hint else text

    def report(self, file_id: str, row_count: int) -> ValidationReport:
        """Freeze into a ValidationReport (rows sorted)."""
        return ValidationReport(file_id=file_id, row_count=row_count, issues=[
            ValidationIssue(issue_id=iid, code=d["code"], severity=d["severity"], column=d["column"],
                            message=self._message(d), rows=sorted(d["rows"]))
            for iid, d in self.issues.items()])


def _row_issues(rows: list[ParsedRow], col: _Collector) -> None:
    """One issue per (code, column) from row problems."""
    for r in rows:
        for p in r.problems:
            col.add(f"{p.code}:{p.column or '-'}", p.code, p.severity, p.column, p.message, [r.row_number],
                    p.value)


def _duplicate_issues(rows: list[ParsedRow], col: _Collector) -> None:
    """Exact duplicates (warning) and same-key/different-value conflicts (error).

    Only rows without errors are compared (see module docstring). Rows are identical when value,
    unit code and unit multiplier all match; a gap (None) vs a value is a conflict.
    """
    by_key: dict[tuple, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.ok and r.period and r.region and r.indicator:
            by_key[(r.indicator, r.region.code, r.period.period)].append(r)
    for group in by_key.values():
        if len(group) < 2:
            continue
        values = {(g.value, g.unit.code if g.unit else None, g.unit.multiplier if g.unit else None) for g in group}
        if len(values) == 1:
            col.add("duplicate_row:-", "duplicate_row", "warning", None,
                    "Identical rows: only the first occurrence will be used.", [g.row_number for g in group[1:]])
        else:
            col.add("duplicate_key_conflict:-", "duplicate_key_conflict", "error", None,
                    "Different values for the same indicator, region and period.", [g.row_number for g in group])


def _unit_issue(ind: str, group: list[ParsedRow], mapping: ColumnMapping, col: _Collector) -> None:
    """Within one indicator: different unit codes (error), scales (warning) or spellings (warning)."""
    with_unit = [r for r in group if r.unit]
    codes = {r.unit.code for r in with_unit}
    scales = {(r.unit.code, r.unit.multiplier) for r in with_unit}
    spellings = ", ".join(sorted({r.unit.raw for r in with_unit}))
    issue_id, rows = f"inconsistent_unit:{mapping.unit_col or '-'}:{ind}", [r.row_number for r in with_unit]
    if len(codes) > 1:
        col.add(issue_id, "inconsistent_unit", "error", mapping.unit_col,
                f"{ind} uses different units: {', '.join(sorted(codes))}.", rows)
    elif len(scales) > 1:
        col.add(issue_id, "inconsistent_unit", "warning", mapping.unit_col,
                f"{ind} uses different scales: {spellings} — confirm values are comparable.", rows)
    elif len({r.unit.raw for r in with_unit}) > 1:
        col.add(issue_id, "inconsistent_unit", "warning", mapping.unit_col,
                f"{ind}: spellings {spellings} mean the same unit; confirm to treat them as one.", rows)


def _per_indicator_issues(rows: list[ParsedRow], mapping: ColumnMapping, col: _Collector) -> None:
    """Unit and frequency consistency within each indicator."""
    groups: dict[str, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.indicator:
            groups[r.indicator].append(r)
    for ind, group in groups.items():
        _unit_issue(ind, group, mapping, col)
        freqs = {r.period.frequency for r in group if r.period}
        if len(freqs) > 1:
            col.add(f"frequency_mismatch:{mapping.date_col}:{ind}", "frequency_mismatch", "error", mapping.date_col,
                    f"{ind} mixes frequencies ({', '.join(sorted(freqs))}).",
                    [r.row_number for r in group if r.period])


def _mixed_decimal_issue(table: RawTable, mapping: ColumnMapping, col: _Collector) -> None:
    """With an "auto" decimal, equal evidence for ',' and '.' in the value column → warning on those rows."""
    if mapping.decimal != "auto" or mapping.value_col not in table.columns:
        return
    idx = table.column_index(mapping.value_col)
    evidence = {rn: decimal_evidence(r[idx] if idx < len(r) else None)
                for r, rn in zip(table.rows, table.row_numbers, strict=True)}
    comma = [rn for rn, e in evidence.items() if e == ","]
    dot = [rn for rn, e in evidence.items() if e == "."]
    if comma and len(comma) == len(dot):
        col.add(f"ambiguous_number:{mapping.value_col}:mixed", "ambiguous_number", "warning", mapping.value_col,
                _MIXED_DECIMAL_MESSAGE, comma + dot)


def validate_table(table: RawTable, mapping: ColumnMapping, file_id: str,
                   known_columns: frozenset[str] = frozenset(),
                   rows: list[ParsedRow] | None = None) -> ValidationReport:
    """All issues for a table under a mapping; rows are referenced by original row number.

    `rows` lets a caller that already ran `parse_rows(table, mapping)` share that result instead of
    parsing twice; it must come from this exact table and mapping.
    """
    rows = parse_rows(table, mapping) if rows is None else rows
    col = _Collector()
    _row_issues(rows, col)
    _mixed_decimal_issue(table, mapping, col)
    _duplicate_issues(rows, col)
    _per_indicator_issues(rows, mapping, col)
    documented = mapping.mapped_columns() | set(mapping.column_descriptions) | set(known_columns)
    for c in table.columns:
        if c not in documented:
            col.add(f"undocumented_column:{c}", "undocumented_column", "warning", c,
                    f"Column {c!r} has no documented meaning; it will be ignored unless you describe it.")
    return col.report(file_id, len(table.rows))
