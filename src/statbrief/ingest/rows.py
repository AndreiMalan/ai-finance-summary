"""Parse table rows under a mapping into typed values plus per-row problems (shared by validation and normalization).

No row is ever dropped: every problem is attached to the row it was found in, with the original row
number. A cell that cannot be read unambiguously is reported, never guessed:
- a formula without a saved value → `formula_without_value` (the cell is not parsed further);
- a mapped column missing from the table → `missing_required` on every row;
- with `decimal="auto"`, the decimal separator is inferred once per table from the value column; when
  that is undecided, a thousands look-alike such as "1,234" → `ambiguous_number` instead of a guess.
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from statbrief.domain.dataset import ColumnMapping, IssueCode
from statbrief.ingest.detect import is_date_named
from statbrief.ingest.numbers import NumberError, infer_decimal, is_thousands_lookalike, parse_number
from statbrief.ingest.periods import ParsedPeriod, PeriodError, parse_period
from statbrief.ingest.readers import FORMULA_NO_VALUE, RawTable
from statbrief.ingest.regions import RegionMatch, resolve_region
from statbrief.ingest.units import NormalizedUnit, normalize_unit


class _Absent:
    """Marker for a mapped column that does not exist in the table."""

    def __repr__(self) -> str:
        """Readable marker in debug output."""
        return "ABSENT"


_ABSENT = _Absent()


@dataclass(frozen=True)
class RowProblem:
    """One problem found in one row."""

    code: IssueCode
    column: str | None
    message: str
    severity: Literal["error", "warning"] = "error"
    value: str | None = None  # repr of the offending raw cell, for grouped messages


@dataclass
class ParsedRow:
    """Typed values of one source row and its problems."""

    row_number: int
    period: ParsedPeriod | None = None
    region: RegionMatch | None = None
    indicator: str | None = None
    unit: NormalizedUnit | None = None
    value: float | None = None
    problems: list[RowProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the row has no error problems (warnings allowed)."""
        return not any(p.severity == "error" for p in self.problems)


@dataclass(frozen=True)
class _Options:
    """Per-table parsing options derived once from the mapping (and the value column)."""

    day_order: Literal["DMY", "MDY"] | None
    excel_serial: bool
    yyyymm: bool  # six-digit year-month codes are dates (date-named column only)
    decimal: Literal[".", ","] | None
    decimal_decided: bool  # False → thousands look-alikes are ambiguous


def _blank(v: Any) -> bool:
    """None or whitespace-only."""
    return v is None or (isinstance(v, str) and not v.strip())


def _unreadable(v: Any) -> bool:
    """True for cells already reported as unreadable (formula without value, absent column); never parsed."""
    return v is FORMULA_NO_VALUE or v is _ABSENT


def _options(table: RawTable, mapping: ColumnMapping) -> _Options:
    """Date and decimal options; an "auto" decimal is inferred once from the whole value column."""
    day_order = mapping.date_format if mapping.date_format in ("DMY", "MDY") else None
    if mapping.decimal != "auto":
        decimal = mapping.decimal
    elif mapping.value_col in table.columns:
        idx = table.column_index(mapping.value_col)
        decimal = infer_decimal(r[idx] for r in table.rows if idx < len(r))
    else:
        decimal = None
    return _Options(day_order, mapping.date_format == "excel_serial", is_date_named(mapping.date_col), decimal,
                    decimal is not None)


def _shown(v: Any) -> str:
    """repr of a raw cell for messages (strings trimmed)."""
    return repr(v.strip() if isinstance(v, str) else v)


def _column_indices(table: RawTable, mapping: ColumnMapping) -> dict[str, int | None]:
    """Mapped column → its position in the table, or None when the table lacks it (computed once per table)."""
    return {c: table.column_index(c) if c in table.columns else None for c in mapping.mapped_columns()}


def _cells(row: list[Any], indices: dict[str, int | None]) -> dict[str, Any]:
    """Mapped column → cell value (`_ABSENT` when the table lacks the column; None past a short row)."""
    return {c: _ABSENT if i is None else (row[i] if i < len(row) else None) for c, i in indices.items()}


def _unreadable_problems(cells: dict[str, Any]) -> list[RowProblem]:
    """Problems for formula-without-value cells and mapped columns missing from the table."""
    problems = []
    for col in sorted(cells):
        if cells[col] is FORMULA_NO_VALUE:
            problems.append(RowProblem("formula_without_value", col,
                                       "Formula without a saved value (formulas are never calculated)."))
        elif cells[col] is _ABSENT:
            problems.append(RowProblem("missing_required", col, f"Mapped column {col!r} is not in the table."))
    return problems


def _role_value(cells: dict[str, Any], mapping: ColumnMapping, role: str) -> tuple[str | None, Any]:
    """(column, raw value) for region/indicator/unit, from its column or its constant."""
    col = getattr(mapping, f"{role}_col")
    return (col, cells[col]) if col else (None, mapping.constants.get(role))


def _parse_period(pr: ParsedRow, value: Any, column: str, opts: _Options) -> None:
    """Fill the period or record invalid_date / ambiguous_date."""
    try:
        pr.period = parse_period(value, day_order=opts.day_order, excel_serial=opts.excel_serial,
                                 yyyymm=opts.yyyymm)
    except PeriodError as exc:
        pr.problems.append(RowProblem(exc.code, column, str(exc), value=_shown(value)))


def _parse_region(pr: ParsedRow, value: Any, column: str | None) -> None:
    """Fill the region or record missing_required / unknown_region."""
    if _blank(value):
        pr.problems.append(RowProblem("missing_required", column, "Region is empty."))
    elif (match := resolve_region(value)) is None:
        pr.problems.append(RowProblem("unknown_region", column, f"Unknown region {_shown(value)}.",
                                      value=_shown(value)))
    else:
        pr.region = match


def _parse_indicator(pr: ParsedRow, value: Any, column: str | None) -> None:
    """Fill the indicator or record missing_required."""
    if _blank(value):
        pr.problems.append(RowProblem("missing_required", column, "Indicator is empty."))
    else:
        pr.indicator = str(value).strip()


def _parse_unit(pr: ParsedRow, value: Any, column: str | None) -> None:
    """Fill the unit or record missing_required."""
    pr.unit = normalize_unit(value)
    if pr.unit is None:
        pr.problems.append(RowProblem("missing_required", column, "Unit is empty."))


def _parse_value(pr: ParsedRow, value: Any, column: str, opts: _Options) -> None:
    """Fill the value; record ambiguous_number / non_numeric (errors) or missing_value (warning)."""
    if not opts.decimal_decided and is_thousands_lookalike(value):
        pr.problems.append(RowProblem("ambiguous_number", column,
                                      f"Is {_shown(value)} a decimal or a whole number with a thousands "
                                      "separator? Choose the decimal separator in the mapping.",
                                      value=_shown(value)))
        return
    try:
        pr.value = parse_number(value, opts.decimal)
    except NumberError:
        pr.problems.append(RowProblem("non_numeric", column, f"{_shown(value)} is not a number.",
                                      value=_shown(value)))
        return
    if pr.value is None:
        pr.problems.append(RowProblem("missing_value", column, "Value is missing.", "warning"))


def parse_rows(table: RawTable, mapping: ColumnMapping) -> list[ParsedRow]:
    """Parse every data row; never drops a row (problems are attached instead)."""
    opts = _options(table, mapping)
    indices = _column_indices(table, mapping)
    out = []
    for raw, rn in zip(table.rows, table.row_numbers, strict=True):
        pr = ParsedRow(row_number=rn)
        cells = _cells(raw, indices)
        pr.problems.extend(_unreadable_problems(cells))
        if not _unreadable(cells[mapping.date_col]):
            _parse_period(pr, cells[mapping.date_col], mapping.date_col, opts)
        for role, parse in (("region", _parse_region), ("indicator", _parse_indicator), ("unit", _parse_unit)):
            column, value = _role_value(cells, mapping, role)
            if not _unreadable(value):
                parse(pr, value, column)
        if not _unreadable(cells[mapping.value_col]):
            _parse_value(pr, cells[mapping.value_col], mapping.value_col, opts)
        out.append(pr)
    return out
