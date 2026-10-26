"""Parse numeric cells; missing markers become None, never zero.

Rules:
- None, NaN and the tokens in MISSING_TOKENS (case-insensitive, after trimming) are gaps → None.
- Booleans, infinities and anything that is not a plain decimal literal raise NumberError.
- Strings: whitespace, NBSP, thin/narrow spaces and apostrophes are removed (thousands
  separators) and one trailing "%" is dropped.
- Decimal separator: the `decimal` argument when given; otherwise the last of "," / "." when both
  occur ("1.234,5" and "1,234.5" are both 1234.5), a single comma is a decimal comma ("7,4"),
  several commas are thousands ("1,234,567") and several dots are thousands ("1.234.567").
"""
import math
import re
from collections.abc import Iterable
from typing import Any, Literal

MISSING_TOKENS = frozenset({"", "-", "–", ":", "..", "n/a", "na", "nan", "null", "none", "x"})
# Whitespace incl. NBSP, thin space and narrow NBSP (thousands separators), and apostrophes (Swiss style).
_SPACES = re.compile(r"[\s\u00a0\u2009\u202f']")
_DEC_COMMA = re.compile(r"^-?\d+,\d+$")
_DEC_DOT = re.compile(r"^-?\d+\.\d+$")
# Plain decimal literal after separator handling; rejects Python-only forms float() accepts
# ("1_000", "infinity", "nan") so only spreadsheet-style numbers pass.
_PLAIN_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


class NumberError(ValueError):
    """A cell is not numeric."""


def parse_number(value: Any, decimal: Literal[".", ","] | None = None) -> float | None:
    """Return a float, None for a missing marker, or raise NumberError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise NumberError("boolean is not a number")
    if isinstance(value, int | float):
        if isinstance(value, float) and math.isnan(value):
            return None
        try:
            result = float(value)
        except OverflowError as exc:  # an int too large for a float
            raise NumberError(f"{value!r} is too large") from exc
        if not math.isfinite(result):
            raise NumberError(f"{value!r} is not a finite number")
        return result
    s = str(value).strip()
    if s.lower() in MISSING_TOKENS:
        return None
    s = _SPACES.sub("", s).removesuffix("%")
    if decimal is None:
        if "," in s and "." in s:
            decimal = "," if s.rfind(",") > s.rfind(".") else "."
        elif s.count(",") == 1 or s.count(".") > 1:  # "7,4" decimal comma; "1.234.567" dotted thousands
            decimal = ","
        else:
            decimal = "."
    s = s.replace(".", "").replace(",", ".") if decimal == "," else s.replace(",", "")
    if not _PLAIN_NUMBER.match(s):
        raise NumberError(f"{value!r} is not a number")
    result = float(s)
    if not math.isfinite(result):
        raise NumberError(f"{value!r} is not a finite number")
    return result


def infer_decimal(values: Iterable[Any]) -> Literal[".", ","] | None:
    """Dominant decimal separator among string cells, or None when undecided."""
    comma = dot = 0
    for v in values:
        if isinstance(v, str):
            s = _SPACES.sub("", v)
            comma += bool(_DEC_COMMA.match(s))
            dot += bool(_DEC_DOT.match(s))
    if comma == dot:
        return None
    return "," if comma > dot else "."
