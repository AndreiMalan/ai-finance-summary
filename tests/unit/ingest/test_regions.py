"""Tests for region resolution."""
import pytest

from statbrief.ingest.regions import resolve_region


@pytest.mark.parametrize("raw,code,level", [
    ("RO113", "RO113", "nuts3"), ("ro113", "RO113", "nuts3"), ("Cluj", "RO113", "nuts3"),
    ("Iași", "RO213", "nuts3"), ("Județul Timiș", "RO424", "nuts3"), ("Bistrița-Năsăud", "RO112", "nuts3"),
    ("Municipiul București", "RO321", "nuts3"), ("RO", "RO", "country"), ("Romania", "RO", "country"),
    ("România", "RO", "country"), ("GR", "EL", "country"), ("EU27_2020", "EU27_2020", "aggregate"),
    ("Nord-Vest", "RO11", "nuts2"), ("Caraș Severin", "RO422", "nuts3"),
])
def test_resolve(raw, code, level):
    """Codes, names, diacritics, prefixes and aliases resolve."""
    m = resolve_region(raw)
    assert m is not None and (m.code, m.level) == (code, level)


@pytest.mark.parametrize("raw", ["Atlantis", "", None, "RO999", 42])
def test_unknown(raw):
    """Unknown regions return None (never guessed)."""
    assert resolve_region(raw) is None


@pytest.mark.parametrize("raw,code", [("Iaşi", "RO213"), ("Jud. Timiş", "RO424"), ("  cluj  ", "RO113"),
                                      ("gb", "UK"), ("Uniunea Europeană", "EU27_2020")])
def test_resolve_legacy_cedilla_prefix_and_alias_codes(raw, code):
    """Legacy cedilla letters (cp1250), abbreviated county prefix, padding and code aliases resolve."""
    m = resolve_region(raw)
    assert m is not None and m.code == code


def test_regions_csv_loads_as_package_resource():
    """The region table ships as package data and every level is a valid RegionLevel."""
    from importlib import resources
    from typing import get_args

    from statbrief.domain.observation import RegionLevel

    text = resources.files("statbrief.ingest.data").joinpath("regions.csv").read_text(encoding="utf-8")
    levels = {line.split(",")[1] for line in text.splitlines()[1:] if line}
    assert levels <= set(get_args(RegionLevel))
