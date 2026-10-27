"""Tests for the curated indicator catalog (D-005) and uncurated "Advanced" specs (AC 8)."""
import pytest

from statbrief.sources.catalog import CATALOG, IndicatorSpec, advanced_spec, canonical_filters


def test_catalog_codes_unique_and_match_keys():
    """Every entry is keyed by its own code and codes do not repeat."""
    assert all(code == spec.code for code, spec in CATALOG.items())
    assert len({s.code for s in CATALOG.values()}) == len(CATALOG)


def test_catalog_covers_d005_set():
    """The D-005 set: five Eurostat datasets and three World Bank indicators."""
    eurostat = {s.dataset for s in CATALOG.values() if s.source == "eurostat"}
    worldbank = {s.dataset for s in CATALOG.values() if s.source == "worldbank"}
    # prc_hicp_manr was discontinued by Eurostat (last period 2025-12); prc_hicp_minr replaces it.
    assert eurostat == {"prc_hicp_minr", "une_rt_m", "namq_10_gdp", "sts_inpr_m", "nama_10r_2gdp"}
    assert worldbank == {"NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "SP.POP.TOTL"}


def test_every_curated_spec_has_frequency_unit_and_rate_flag():
    """Curated specs declare frequency, unit and rate/level; nothing is left to the response."""
    for spec in CATALOG.values():
        assert spec.curated and spec.label != "uncurated"
        assert spec.frequency in ("A", "Q", "M") and spec.unit
        assert isinstance(spec.is_rate, bool) and spec.region_levels


def test_verified_filters():
    """Filter codes verified against the live Eurostat API on 2026-10-26."""
    assert CATALOG["HICP_YOY"].filters == {"coicop18": "TOTAL", "unit": "RCH_A"}
    assert CATALOG["UNEMP_RATE"].filters == {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"}
    assert CATALOG["GDP_VOL"].filters == {"na_item": "B1GQ", "s_adj": "SCA", "unit": "CLV10_MEUR"}
    assert CATALOG["IND_PROD"].filters == {"indic_bt": "PRD", "nace_r2": "B-D", "s_adj": "SCA", "unit": "I21"}
    assert CATALOG["GDP_PC_REG"].filters == {"unit": "EUR_HAB"}
    assert CATALOG["GDP_PC_REG"].frequency == "A" and "nuts2" in CATALOG["GDP_PC_REG"].region_levels
    assert CATALOG["GDP_VOL"].unit_multiplier == 6


def test_specs_are_immutable():
    """Catalog entries cannot be mutated by callers (filters included)."""
    spec = CATALOG["HICP_YOY"]
    with pytest.raises(Exception):
        spec.code = "X"  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.filters["unit"] = "I15"  # type: ignore[index]


def test_canonical_filters_sorted_and_stable():
    """Canonical filters are key-sorted k=v pairs joined by '&'."""
    assert canonical_filters({"unit": "PC_ACT", "age": "TOTAL"}) == "age=TOTAL&unit=PC_ACT"
    assert canonical_filters({}) == ""


def test_advanced_spec_is_flagged_uncurated_without_invented_metadata():
    """Uncurated specs carry no unit, frequency or rate flag unless the user states the unit."""
    s = advanced_spec("eurostat", "lfsq_urgaed", {"sex": "T", "unit": "PC"})
    assert isinstance(s, IndicatorSpec) and not s.curated and s.label == "uncurated"
    assert s.unit is None and s.frequency is None and s.is_rate is None
    assert s.code == "eurostat:lfsq_urgaed?sex=T&unit=PC"
    wb = advanced_spec("worldbank", "NE.EXP.GNFS.ZS", {}, unit="%")
    assert wb.code == "worldbank:NE.EXP.GNFS.ZS" and wb.unit == "%" and not wb.curated


@pytest.mark.parametrize("source,code,filters", [
    ("eurostat", "../une_rt_m", {}), ("eurostat", "une rt m", {}), ("eurostat", "", {}),
    ("eurostat", "une_rt_m", {"geo": "RO"}), ("eurostat", "une_rt_m", {"unit": "PC&x=1"}),
    ("eurostat", "une_rt_m", {"format": "SDMX"}), ("worldbank", "SP.POP/../x", {}),
    ("worldbank", "SP.POP.TOTL", {"x": "1"}), ("imf", "X", {}),
])
def test_advanced_spec_rejects_unsafe_or_reserved_codes(source, code, filters):
    """Codes become URL path segments and query params: only plain code characters are accepted."""
    with pytest.raises(ValueError):
        advanced_spec(source, code, filters)
