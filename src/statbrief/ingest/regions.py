"""Resolve region cells (codes, names, aliases, with or without diacritics) to region codes.

Lookup order: the code (case-insensitive, including short code aliases such as GR → EL and
GB → UK), then the normalised name or alias: lower-case, diacritics removed (comma-below and
legacy cedilla letters alike), hyphens and en/em dashes as spaces, and a leading "judetul",
"jud.", "municipiul" or "mun." dropped. Data comes from the bundled `statbrief.ingest.data/regions.csv`.
Unknown values return None; nothing is guessed.
"""
import csv
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from statbrief.domain.observation import RegionLevel

_PREFIXES = ("judetul ", "jud. ", "jud ", "municipiul ", "mun. ", "mun ")
_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-"})  # en/em dashes are typed or auto-corrected for "-"


@dataclass(frozen=True)
class RegionMatch:
    """A resolved region."""

    code: str
    level: RegionLevel
    name: str


def _key(s: str) -> str:
    """Normalised lookup key: lower-case, no diacritics, no county prefixes, hyphens/dashes as spaces."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    s = " ".join(s.translate(_DASHES).replace("-", " ").split())
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return s


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, RegionMatch], dict[str, RegionMatch]]:
    """(by_code, by_name) lookups built once from the bundled regions.csv."""
    by_code: dict[str, RegionMatch] = {}
    by_name: dict[str, RegionMatch] = {}
    text = resources.files("statbrief.ingest.data").joinpath("regions.csv").read_text(encoding="utf-8")
    for row in csv.DictReader(text.splitlines()):
        m = RegionMatch(row["code"], row["level"], row["name"])
        by_code[m.code.upper()] = m
        for alias in [m.name, *[a for a in (row["aliases"] or "").split(";") if a]]:
            target = by_code.get(alias.upper()) if alias.upper() in by_code and alias.isupper() else None
            by_name[_key(alias)] = target or m
        for alias in (row["aliases"] or "").split(";"):
            if alias and alias.isupper() and len(alias) <= 3:
                by_code.setdefault(alias.upper(), m)  # code aliases such as GR→EL, GB→UK
    return by_code, by_name


def resolve_region(raw: Any) -> RegionMatch | None:
    """RegionMatch for a code or name, or None when unknown (never guessed)."""
    if raw is None or isinstance(raw, bool) or not str(raw).strip():
        return None
    s = str(raw).strip()
    by_code, by_name = _index()
    return by_code.get(s.upper()) or by_name.get(_key(s))
