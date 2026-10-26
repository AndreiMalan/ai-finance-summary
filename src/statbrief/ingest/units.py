"""Normalise unit spellings to canonical codes; unknown units are kept verbatim (never invented).

Rules (matching is case-, spacing- and diacritics-insensitive):
- Canonical codes: %, pp, EUR, RON, USD, PERS, NR (English and Romanian spellings).
- A leading multiplier word sets a power of ten: thousand/thousands/mii/th → 3,
  million/millions/mil/milioane → 6, billion/billions/mld/miliarde → 9 ("mil. lei" → RON ×10^6).
- Index units "index 2015=100" / "indice (2021=100)" → I15 / I21.
- Anything else (including a multiplier word before an unknown unit) is kept verbatim, trimmed,
  with multiplier 0.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_CANON = {
    "%": "%", "percent": "%", "pct": "%", "procent": "%", "procente": "%",
    "pp": "pp", "p.p.": "pp", "percentage points": "pp", "puncte procentuale": "pp",
    "eur": "EUR", "euro": "EUR", "lei": "RON", "ron": "RON", "usd": "USD",
    "persons": "PERS", "persoane": "PERS", "number": "NR", "nr": "NR", "numar": "NR",
}
_MULT = {
    "thousand": 3, "thousands": 3, "mii": 3, "th": 3, "th.": 3,
    "million": 6, "millions": 6, "mil": 6, "mil.": 6, "milioane": 6,
    "billion": 9, "billions": 9, "mld": 9, "mld.": 9, "miliarde": 9,
}
_INDEX = re.compile(r"^(?:index|indice)\s*\(?\s*(\d{4})\s*=\s*100\s*\)?$")


@dataclass(frozen=True)
class NormalizedUnit:
    """Canonical unit code, power-of-ten multiplier, and the original spelling."""

    code: str
    multiplier: int
    raw: str


def _fold(s: str) -> str:
    """Lower-case, remove diacritics, collapse whitespace."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return " ".join(s.split())


def normalize_unit(raw: Any) -> NormalizedUnit | None:
    """Canonical unit for a cell, or None when empty."""
    if raw is None or not str(raw).strip():
        return None
    original = str(raw).strip()
    folded = _fold(original)
    if m := _INDEX.match(folded):
        return NormalizedUnit(f"I{m[1][2:]}", 0, original)
    if folded in _CANON:
        return NormalizedUnit(_CANON[folded], 0, original)
    head, _, rest = folded.partition(" ")
    if head in _MULT and rest:
        base = _CANON.get(rest)
        return NormalizedUnit(base or original, _MULT[head] if base else 0, original)
    return NormalizedUnit(original, 0, original)
