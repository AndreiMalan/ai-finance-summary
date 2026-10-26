"""Detect the header row and column roles of a grid, with confidence scores.

Detection only *proposes* a mapping; the user confirms or overrides it. Every assigned role
carries a confidence in [0, 1] so low-confidence guesses surface as `needs_review` instead of
being silently applied.

Algorithm:
1. Header row = the first of the first `scan_rows` grid rows with >= 2 non-empty cells where
   >= 60 % of non-empty cells are non-numeric strings **and** the next grid row has >= 2
   non-empty cells; fallback: the first grid row.
2. For each column take up to `sample_rows` non-empty values (formulas without a cached value
   are skipped). Content shares: `date` = share parseable by `parse_period` (with the inferred
   day order / Excel-serial flag), `region` = share resolved by `resolve_region`, `value` = share
   numeric (damped when the column is date-like, since pure years are numeric), `unit` = share
   whose `normalize_unit` code is canonical (`%`, `pp`, `EUR`, `RON`, `USD`, `PERS`, `NR`, `Iyy`),
   `indicator` = 1 for a text column with distinct/total <= 0.5 that is not region/date-like.
3. Name score: header tokens (diacritics folded, split on non-alphanumerics except `_`) matched
   against per-role hints. Exact token = 1.0, substring of a hint longer than 3 chars = 0.6.
4. Score = 0.6 x content + 0.4 x name per role; roles are assigned greedily by descending score
   (each role and each column once, score >= 0.3). Columns without sample values get no role.
   Other columns -> `ignore`, with `note="undocumented"` when not in `known_columns`.
5. `frequency` = most common frequency in the date column; `day_order`, `excel_serial` and
   `decimal` come from the date/value samples; `meta` copies the grid meta (delimiter, encoding).
"""
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from itertools import islice
from typing import Any

from statbrief.domain.dataset import ColumnGuess, DetectedSchema
from statbrief.ingest.numbers import NumberError, infer_decimal, parse_number
from statbrief.ingest.periods import DayOrder, PeriodError, infer_day_order, looks_like_excel_serial, parse_period
from statbrief.ingest.readers import FORMULA_NO_VALUE, Grid, to_table
from statbrief.ingest.regions import resolve_region
from statbrief.ingest.units import normalize_unit

_ROLES = ("date", "region", "indicator", "value", "unit")
_HINTS = {
    "date": ("date", "data", "period", "perioada", "luna", "month", "an", "year", "time", "time_period"),
    "region": ("region", "regiune", "judet", "county", "country", "tara", "geo", "nuts"),
    "indicator": ("indicator", "indicatorul", "series", "serie", "variable", "variabila", "measure"),
    "value": ("value", "valoare", "obs_value", "amount", "val"),
    "unit": ("unit", "unitate", "um", "unit_measure", "measure_unit"),
}
_CANONICAL_UNITS = frozenset({"%", "pp", "EUR", "RON", "USD", "PERS", "NR"})
_INDEX_UNIT = re.compile(r"^I\d{2}$")  # index units normalised to I15, I21, ...
_TOKEN_SPLIT = re.compile(r"[^a-z0-9_]+")
_HEADER_MIN_CELLS = 2           # non-empty cells needed in the header row and in the row after it
_HEADER_TEXT_SHARE = 0.6        # share of non-numeric cells that marks a header row
_EXCEL_SERIAL_SHARE = 0.8       # share of serial-looking values that marks an Excel-serial date column
_INDICATOR_MAX_DISTINCT = 0.5   # distinct/total ratio at or below which a text column looks categorical
_NOT_LIKE_SHARE = 0.5           # region/date share below which a categorical column may be an indicator
_DATE_LIKE_SHARE = 0.8          # date share above which numeric content is damped (years are numbers)
_DATE_LIKE_VALUE_DAMP = 0.4     # multiplier on the value share of date-like columns
_EXACT_NAME, _SUBSTRING_NAME = 1.0, 0.6  # name score for an exact hint token / a hint substring
_SUBSTRING_MIN_LEN = 3          # hints must be longer than this to match as substrings ("an" must not)
_CONTENT_W, _NAME_W = 0.6, 0.4  # score weights: content evidence dominates the header name
_MIN_ROLE_SCORE = 0.3           # below this a column is not assigned the role at all


def _fold(s: str) -> str:
    """Lower-case without diacritics."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _filled(v: Any) -> bool:
    """True for a cell with content (not None and not blank)."""
    return v is not None and bool(str(v).strip())


def _is_text(v: Any) -> bool:
    """Non-empty, non-numeric string (a missing marker other than "-" counts as text)."""
    if not isinstance(v, str) or not v.strip():
        return False
    try:
        number = parse_number(v)
    except NumberError:
        return True
    return number is None and v.strip() != "-"


def detect_header_row(grid: Grid, scan_rows: int) -> int:
    """Original row number of the most likely header row (ValueError for an empty grid)."""
    if not grid.rows:
        raise ValueError("cannot detect a header: the table has no non-empty rows")
    for i, row in enumerate(grid.rows[:scan_rows]):
        filled = [c for c in row if _filled(c)]
        nxt = grid.rows[i + 1] if i + 1 < len(grid.rows) else []
        if len(filled) >= _HEADER_MIN_CELLS \
                and sum(_is_text(c) for c in filled) / len(filled) >= _HEADER_TEXT_SHARE \
                and sum(_filled(c) for c in nxt) >= _HEADER_MIN_CELLS:
            return grid.row_numbers[i]
    return grid.row_numbers[0]


def _name_score(column: str, role: str) -> float:
    """1.0 for an exact hint token, 0.6 for a (long enough) hint substring, else 0."""
    folded = _fold(column)
    if set(_TOKEN_SPLIT.split(folded)) & set(_HINTS[role]):
        return _EXACT_NAME
    return _SUBSTRING_NAME if any(h in folded for h in _HINTS[role] if len(h) > _SUBSTRING_MIN_LEN) else 0.0


def _share(values: list[Any], ok: Callable[[Any], bool]) -> float:
    """Share of values for which ok(v) is true (0 for no values)."""
    return sum(1 for v in values if ok(v)) / len(values) if values else 0.0


def _content_scores(values: list[Any], day_order: DayOrder | None, excel_serial: bool) -> dict[str, float]:
    """Per-role content evidence in [0, 1] for one column's sample."""
    def is_date(v: Any) -> bool:
        """Parseable as a period; an ambiguous d/m/y date still counts as date-like."""
        try:
            parse_period(v, day_order=day_order, excel_serial=excel_serial)
            return True
        except PeriodError as exc:
            return exc.code == "ambiguous_date"

    def is_num(v: Any) -> bool:
        """Parseable as a number (missing markers do not count)."""
        try:
            return parse_number(v) is not None
        except NumberError:
            return False

    def is_unit(v: Any) -> bool:
        """Normalises to a canonical or index unit code (not kept verbatim)."""
        u = normalize_unit(v)
        return u is not None and (u.code in _CANONICAL_UNITS or bool(_INDEX_UNIT.match(u.code)))

    date = _share(values, is_date)
    region = _share(values, lambda v: resolve_region(v) is not None)
    text = [v for v in values if isinstance(v, str)]
    distinct = len(set(text)) / len(text) if text else 1.0
    categorical = 1.0 if (text and distinct <= _INDICATOR_MAX_DISTINCT
                          and region < _NOT_LIKE_SHARE and date < _NOT_LIKE_SHARE) else 0.0
    value = _share(values, is_num) * (_DATE_LIKE_VALUE_DAMP if date > _DATE_LIKE_SHARE else 1.0)
    return {"date": date, "region": region, "indicator": categorical, "value": value, "unit": _share(values, is_unit)}


def _sample(table_rows: list[list[Any]], index: int, sample_rows: int) -> list[Any]:
    """Up to `sample_rows` non-empty values of one column, skipping formulas without a cached value."""
    cells = (r[index] for r in table_rows)
    return list(islice((v for v in cells if v is not FORMULA_NO_VALUE and _filled(v)), sample_rows))


def detect_schema(grid: Grid, *, sample_rows: int, scan_rows: int,
                  known_columns: frozenset[str] = frozenset()) -> DetectedSchema:
    """Detect header row, column roles, frequency and parse options for a grid.

    Raises ValueError when the grid has no non-empty rows. A header without data rows yields
    every column as `ignore` (nothing to infer from), which makes the schema need review.
    """
    header = detect_header_row(grid, scan_rows)
    table = to_table(grid, header)
    samples = {col: _sample(table.rows, i, sample_rows) for i, col in enumerate(table.columns)}
    candidates: list[tuple[float, str, str]] = []
    flags: dict[str, tuple[DayOrder | None, bool]] = {}
    for col, values in samples.items():
        if not values:
            continue  # no evidence at all: never map a column on its name alone
        day_order = infer_day_order(values)
        serial = looks_like_excel_serial(values, _EXCEL_SERIAL_SHARE)
        flags[col] = (day_order, serial)
        content = _content_scores(values, day_order, serial)
        for role in _ROLES:
            candidates.append((_CONTENT_W * content[role] + _NAME_W * _name_score(col, role), col, role))
    assigned: dict[str, tuple[str, float]] = {}
    for score, col, role in sorted(candidates, key=lambda t: -t[0]):
        if score < _MIN_ROLE_SCORE or col in assigned or role in {r for r, _ in assigned.values()}:
            continue
        assigned[col] = (role, score)
    guesses = []
    for col in table.columns:
        if col in assigned:
            role, score = assigned[col]
            guesses.append(ColumnGuess(column=col, role=role, confidence=round(min(score, 1.0), 3)))
        else:
            guesses.append(ColumnGuess(column=col, role="ignore", confidence=0.0,
                                       note=None if col in known_columns else "undocumented"))
    date_col = next((c.column for c in guesses if c.role == "date"), None)
    value_col = next((c.column for c in guesses if c.role == "value"), None)
    frequency = day_order = None
    serial = False
    if date_col:
        day_order, serial = flags[date_col]
        freqs: Counter[str] = Counter()
        for v in samples[date_col]:
            try:
                freqs[parse_period(v, day_order=day_order, excel_serial=serial).frequency] += 1
            except PeriodError:
                pass
        frequency = freqs.most_common(1)[0][0] if freqs else None
    decimal = infer_decimal(samples[value_col]) if value_col else None
    return DetectedSchema(worksheet=grid.worksheet, header_row=header, columns=guesses, frequency=frequency,
                          day_order=day_order, excel_serial=serial, decimal=decimal, meta=dict(grid.meta))
