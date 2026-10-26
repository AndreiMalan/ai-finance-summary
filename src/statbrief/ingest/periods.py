"""Parse period cells (years, quarters, months, dates, Excel serials) into canonical periods.

Daily dates are reduced to their month: StatBrief works with A/Q/M frequencies only (spec AC 18).
Ambiguous dd/mm vs mm/dd values are never guessed: they raise `ambiguous_date` until a day order is set.

Accepted forms (years must lie in 1800-2200):
- datetime/date → monthly; integral numbers 1800-2200 → annual; with `excel_serial=True`,
  numbers in the Excel serial range (1950-2099) → their date's month (a fractional part is the
  time of day and is dropped, also for ASCII text such as "45352.5"; `looks_like_excel_serial`
  still counts whole numbers only).
- Annual "2024"; quarterly "2024-Q1", "2024Q1", "2024 Q1", "Q1 2024", "Q1-2024".
- Monthly "2024M03", "2024-03", "2024/03", "2024.03", "03/2024", "03.2024", ISO "2024-03-15"
  (optionally with a time "T08:30" / " 08:30:00.5"),
  month names in English or Romanian ("Mar 2024", "martie 2024", "Ian. 2024").
- "dd/mm/yyyy" (separator "/", "." or "-", the same one twice): a part above 12 fixes the
  order; both parts <= 12 and unequal → `ambiguous_date` unless `day_order` is given.
  Month 13, day 32 and similar → `invalid_date`.
"""
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from statbrief.domain.observation import Frequency

DayOrder = Literal["DMY", "MDY"]
_EXCEL_EPOCH = date(1899, 12, 30)  # Excel's day 0 (1900 date system, incl. the leap-year bug offset)
_EXCEL_SERIAL_MIN, _EXCEL_SERIAL_MAX = 18264, 73050  # 1950-01-01 .. 2099-12-31: plausible statistics dates
_YEAR_MIN, _YEAR_MAX = 1800, 2200
_MONTH_NAMES = {
    1: ("jan", "january", "ian", "ianuarie"), 2: ("feb", "february", "februarie"),
    3: ("mar", "march", "martie"), 4: ("apr", "april", "aprilie"), 5: ("may", "mai"),
    6: ("jun", "june", "iun", "iunie"), 7: ("jul", "july", "iul", "iulie"), 8: ("aug", "august"),
    9: ("sep", "sept", "september", "septembrie"), 10: ("oct", "october", "octombrie"),
    11: ("nov", "november", "noi", "noiembrie"), 12: ("dec", "december", "decembrie"),
}
_MONTH_LOOKUP = {name: num for num, names in _MONTH_NAMES.items() for name in names}

_RE_YEAR = re.compile(r"^(\d{4})$")
_RE_DECIMAL = re.compile(r"^\d+\.\d+$")  # fractional Excel serial written as text, e.g. "45352.5"
_RE_Q1 = re.compile(r"^(\d{4})\s*[-/ ]?\s*q([1-4])$", re.I)
_RE_Q2 = re.compile(r"^q([1-4])\s*[-/ ]?\s*(\d{4})$", re.I)
_RE_EUROSTAT_M = re.compile(r"^(\d{4})m(\d{1,2})$", re.I)
_RE_YM = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_RE_MY = re.compile(r"^(\d{1,2})[-/.](\d{4})$")
_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")  # optional time of day
_RE_DMY = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{4})$")  # both separators must be the same
_RE_NAMED = re.compile(r"^([^\W\d_]+)\.?\s+(\d{4})$", re.U)


class PeriodError(ValueError):
    """A cell could not be turned into a period."""

    def __init__(self, code: Literal["invalid_date", "ambiguous_date"], message: str):
        """Store the issue code and a user-facing message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedPeriod:
    """Canonical period string and its frequency."""

    period: str
    frequency: Frequency


def _strip_accents(s: str) -> str:
    """Lower-case and remove diacritics."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _month(year: int, month: int) -> ParsedPeriod:
    """Monthly period with range checks."""
    if not (_YEAR_MIN <= year <= _YEAR_MAX and 1 <= month <= 12):
        raise PeriodError("invalid_date", f"Month {month} of year {year} is not a valid date.")
    return ParsedPeriod(f"{year:04d}-{month:02d}", "M")


def _quarter(year: int, quarter: int) -> ParsedPeriod:
    """Quarterly period with a year range check (the regexes already restrict the quarter to 1-4)."""
    if not _YEAR_MIN <= year <= _YEAR_MAX:
        raise PeriodError("invalid_date", f"Year {year} is out of range.")
    return ParsedPeriod(f"{year:04d}-Q{quarter}", "Q")


def _from_dmy(a: int, b: int, year: int, day_order: DayOrder | None) -> ParsedPeriod:
    """Resolve a d/m/y or m/d/y triple, refusing to guess when ambiguous."""
    if a > 12 and b > 12:
        raise PeriodError("invalid_date", "Neither part can be a month.")
    if a > 12 or (day_order == "DMY" and b <= 12):
        day, month = a, b
    elif b > 12 or day_order == "MDY":
        day, month = b, a
    elif a == b:
        day, month = a, b
    else:
        raise PeriodError("ambiguous_date", "Day and month order is ambiguous; choose DD/MM or MM/DD.")
    try:
        date(year, month, day)
    except ValueError as exc:
        raise PeriodError("invalid_date", "This date does not exist.") from exc
    return _month(year, month)


def parse_period(value: Any, *, day_order: DayOrder | None = None, excel_serial: bool = False) -> ParsedPeriod:
    """Parse one cell into a canonical period or raise PeriodError."""
    if isinstance(value, datetime | date):
        return _month(value.year, value.month)
    if isinstance(value, bool) or value is None:
        raise PeriodError("invalid_date", "Empty or invalid date.")
    if isinstance(value, int | float):
        if excel_serial and _EXCEL_SERIAL_MIN <= value < _EXCEL_SERIAL_MAX + 1:
            d = _EXCEL_EPOCH + timedelta(days=math.floor(value))  # a fraction is the time of day
            return _month(d.year, d.month)
        if float(value).is_integer() and _YEAR_MIN <= value <= _YEAR_MAX:
            return ParsedPeriod(f"{int(value):04d}", "A")
        raise PeriodError("invalid_date", f"{value!r} is not a year or date.")
    s = str(value).strip()
    if excel_serial and s.isascii() and s.isdigit():
        return parse_period(int(s), excel_serial=True)
    if excel_serial and s.isascii() and _RE_DECIMAL.match(s) and _EXCEL_SERIAL_MIN <= float(s) < _EXCEL_SERIAL_MAX + 1:
        return parse_period(float(s), excel_serial=True)  # serial with a time of day; "2024.03" falls through
    if m := _RE_YEAR.match(s):
        if not _YEAR_MIN <= int(m[1]) <= _YEAR_MAX:
            raise PeriodError("invalid_date", f"Year {m[1]} is out of range.")
        return ParsedPeriod(m[1], "A")
    if m := _RE_Q1.match(s):
        return _quarter(int(m[1]), int(m[2]))
    if m := _RE_Q2.match(s):
        return _quarter(int(m[2]), int(m[1]))
    if m := _RE_EUROSTAT_M.match(s) or _RE_YM.match(s):
        return _month(int(m[1]), int(m[2]))
    if m := _RE_MY.match(s):
        return _month(int(m[2]), int(m[1]))
    if m := _RE_ISO.match(s):
        try:
            date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError as exc:
            raise PeriodError("invalid_date", "This date does not exist.") from exc
        return _month(int(m[1]), int(m[2]))
    if m := _RE_DMY.match(s):
        return _from_dmy(int(m[1]), int(m[3]), int(m[4]), day_order)
    if m := _RE_NAMED.match(s):
        month = _MONTH_LOOKUP.get(_strip_accents(m[1]))
        if month:
            return _month(int(m[2]), month)
    raise PeriodError("invalid_date", f"{s!r} is not a recognised date or period.")


def infer_day_order(values: Iterable[Any]) -> DayOrder | None:
    """DMY/MDY when unambiguous d/m/y values all agree; None when nothing decides it or they conflict."""
    seen: set[DayOrder] = set()
    for v in values:
        m = _RE_DMY.match(str(v).strip()) if v is not None else None
        if m:
            a, b = int(m[1]), int(m[3])
            if a > 12 >= b:
                seen.add("DMY")
            elif b > 12 >= a:
                seen.add("MDY")
    return seen.pop() if len(seen) == 1 else None


def looks_like_excel_serial(values: Iterable[Any], min_share: float) -> bool:
    """True when at least `min_share` of non-empty values are integers in the Excel serial range."""
    vals = [v for v in values if v is not None and str(v).strip()]
    if not vals:
        return False
    hits = 0
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        hits += f.is_integer() and _EXCEL_SERIAL_MIN <= f <= _EXCEL_SERIAL_MAX
    return hits / len(vals) >= min_share
