"""Parse numeric cells; missing markers become None, never zero.

Rules:
- None, NaN and the tokens in MISSING_TOKENS (case-insensitive, after trimming) are gaps → None.
- Booleans, infinities and anything that is not a plain decimal literal raise NumberError.
- Strings: whitespace, NBSP, thin/narrow spaces and apostrophes are removed (thousands
  separators) and one trailing "%" is dropped.
- Decimal separator: the `decimal` argument when given; otherwise the last of "," / "." when both
  occur ("1.234,5" and "1,234.5" are both 1234.5), a single comma is a decimal comma ("7,4"),
  several commas are thousands ("1,234,567") and several dots are thousands ("1.234.567").
- Thousands separators (the other of "," / ".") may appear only in the integer part, as a 1-3
  digit head followed by 3-digit groups ("1.234.567"); "15.03.2024", "1,23.4" and a decimal
  separator used twice raise NumberError instead of being read as some number.
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
# Valid thousands grouping of an integer part, per thousands separator: 1-3 digit head, 3-digit groups.
_GROUPED = {sep: re.compile(rf"^[+-]?\d{{1,3}}(?:{re.escape(sep)}\d{{3}})+$") for sep in ",."}
# "1,234" / "1.234": one separator followed by exactly three digits reads as thousands or as a
# decimal, so such values are no evidence for either separator in infer_decimal.
_THOUSANDS_LOOKALIKE = re.compile(r"^[+-]?\d{1,3}[.,]\d{3}$")


class NumberError(ValueError):
    """A cell is not numeric."""


def _last_separator(s: str) -> Literal[".", ","]:
    """The separator of "," / "." that occurs last in `s` (the decimal one when both appear)."""
    return "," if s.rfind(",") > s.rfind(".") else "."


def _to_plain(s: str, decimal: Literal[".", ","]) -> str | None:
    """`s` (spaces already removed) as a plain "." decimal literal, or None if malformed.

    Thousands separators (the other of "," / ".") are allowed only in the integer part, as a 1-3
    digit head followed by 3-digit groups; the fractional part may hold neither separator.
    """
    thousands = "." if decimal == "," else ","
    int_part, _, frac_part = s.partition(decimal)
    if decimal in frac_part or thousands in frac_part:
        return None
    if thousands in int_part and not _GROUPED[thousands].match(int_part):
        return None
    plain = s.replace(thousands, "").replace(decimal, ".")
    return plain if _PLAIN_NUMBER.match(plain) else None


def parse_number(value: Any, decimal: Literal[".", ","] | None = None) -> float | None:
    """Return a float, None for a missing marker, or raise NumberError.

    Without an explicit `decimal`, a lone "1,234" is read per cell as the decimal 1.234 (single
    comma = decimal comma). Column-level callers must resolve that ambiguity themselves, via
    `infer_decimal` or the user's mapping, and pass the result as `decimal`.
    """
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
            decimal = _last_separator(s)
        elif s.count(",") == 1 or s.count(".") > 1:  # "7,4" decimal comma; "1.234.567" dotted thousands
            decimal = ","
        else:
            decimal = "."
    plain = _to_plain(s, decimal)
    if plain is None:
        raise NumberError(f"{value!r} is not a number (or has malformed thousands separators)")
    result = float(plain)
    if not math.isfinite(result):
        raise NumberError(f"{value!r} is not a finite number")
    return result


def infer_decimal(values: Iterable[Any]) -> Literal[".", ","] | None:
    """Dominant decimal separator among string cells, or None when undecided.

    Evidence: a plain decimal ("7,4" / "7.4"), or a well-grouped value with both separators
    ("1.234,5" / "1,234.5"), which supports the one occurring last. Thousands look-alikes such as
    "1,234" are skipped: they would support either separator.
    """
    comma = dot = 0
    for v in values:
        if isinstance(v, str):
            s = _SPACES.sub("", v)
            if _THOUSANDS_LOOKALIKE.match(s):
                continue
            if "," in s and "." in s:
                sep = _last_separator(s)
                if _to_plain(s, sep) is not None:
                    comma += sep == ","
                    dot += sep == "."
                continue
            comma += bool(_DEC_COMMA.match(s))
            dot += bool(_DEC_DOT.match(s))
    if comma == dot:
        return None
    return "," if comma > dot else "."
