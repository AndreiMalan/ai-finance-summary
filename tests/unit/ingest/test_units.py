"""Tests for unit normalisation."""
import pytest

from statbrief.ingest.units import normalize_unit


@pytest.mark.parametrize("raw,code,mult", [
    ("%", "%", 0), ("percent", "%", 0), ("Procente", "%", 0), ("p.p.", "pp", 0),
    ("EUR", "EUR", 0), ("mil. lei", "RON", 6), ("thousand persons", "PERS", 3), ("mii persoane", "PERS", 3),
    ("Index 2015=100", "I15", 0), ("indice (2021=100)", "I21", 0), ("mld. EUR", "EUR", 9),
    ("tonnes", "tonnes", 0),
])
def test_normalize_unit(raw, code, mult):
    """Known spellings map to canonical codes; unknown units are kept verbatim."""
    u = normalize_unit(raw)
    assert (u.code, u.multiplier, u.raw) == (code, mult, raw)


def test_empty_unit_is_none():
    """Blank units are missing, not invented."""
    assert normalize_unit("  ") is None and normalize_unit(None) is None


@pytest.mark.parametrize("raw,code,mult", [
    ("thousand tonnes", "thousand tonnes", 0), ("Mii  Persoane", "PERS", 3), ("puncte procentuale", "pp", 0),
    ("  RON ", "RON", 0), (5, "5", 0),
])
def test_normalize_unit_more(raw, code, mult):
    """Unknown base units with a multiplier word stay verbatim; spacing/case are folded; raw is trimmed text."""
    u = normalize_unit(raw)
    assert (u.code, u.multiplier) == (code, mult)
