"""Tests for the World Bank client on recorded fixtures: mapping, pagination, nulls, error payloads."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from statbrief.config import Settings
from statbrief.domain.observation import make_obs_id
from statbrief.sources.catalog import CATALOG, advanced_spec
from statbrief.sources.http import SourceError
from statbrief.sources.worldbank import ISO3_TO_GEO, fetch_worldbank, parse_worldbank

DATA = Path(__file__).resolve().parents[2] / "data" / "worldbank"
T0 = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
URL = "https://api.worldbank.org/v2/country/ROU/indicator/X?format=json"


def _load(name: str):
    """A recorded World Bank response (recorded 2026-10-26)."""
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _settings(**kw):
    """Settings with default API configuration (overridable)."""
    return Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None, **kw)


def test_iso3_mapping_covers_planned_codes():
    """EU27 members, neighbours, GBR → UK, GRC → EL and the EU aggregate map to Eurostat geo codes."""
    expected = {"ROU": "RO", "HUN": "HU", "BGR": "BG", "GRC": "EL", "GBR": "UK", "MDA": "MD", "UKR": "UA",
                "SRB": "RS", "EUU": "EU27_2020", "DEU": "DE", "AUT": "AT", "SWE": "SE"}
    assert expected.items() <= ISO3_TO_GEO.items()
    assert len([g for g in ISO3_TO_GEO.values() if g not in ("EU27_2020", "MD", "UA", "RS", "UK")]) == 27


def test_parse_gdp_growth():
    """Annual % observations with World Bank provenance; the EU aggregate maps to EU27_2020."""
    r = parse_worldbank([_load("gdp_growth.json")], CATALOG["WB_GDP_GROWTH"], URL, T0)
    obs = {(o.region_code, o.period): o for o in r.observations}
    assert len(obs) == 15 and r.skipped == []
    eu = obs[("EU27_2020", "2024")]
    assert eu.value == pytest.approx(1.08657885620458) and eu.region_level == "aggregate"
    assert eu.unit == "%" and eu.frequency == "A" and eu.indicator_code == "WB_GDP_GROWTH"
    assert eu.provenance.api_name == "worldbank" and eu.provenance.series_id == "NY.GDP.MKTP.KD.ZG"
    assert eu.obs_id == make_obs_id("worldbank", "NY.GDP.MKTP.KD.ZG", "EU27_2020", "2024")
    assert r.meta.label == "GDP growth (annual %)" and r.meta.last_update == "2026-10-08"


def test_null_values_skipped_not_invented():
    """value: null → missing_value (Moldova/Romania 1989-90, Serbia 1989-91)."""
    r = parse_worldbank([_load("gdp_growth_nulls.json")], CATALOG["WB_GDP_GROWTH"], URL, T0)
    assert sorted((o.region_code, o.period) for o in r.observations) == [("MD", "1991"), ("RO", "1991")]
    assert sorted((s.region, s.period) for s in r.skipped if s.reason == "missing_value") == [
        ("MDA", "1989"), ("MDA", "1990"), ("ROU", "1989"), ("ROU", "1990"),
        ("SRB", "1989"), ("SRB", "1990"), ("SRB", "1991")]


def test_population_unit_is_persons():
    """SP.POP.TOTL → PERS with integral values."""
    r = parse_worldbank([_load("population.json")], CATALOG["WB_POP"], URL, T0)
    ro = {o.period: o for o in r.observations if o.region_code == "RO"}
    assert ro["2024"].value == 19051804.0 and ro["2024"].unit == "PERS"


def test_unknown_country_skipped_and_requested_absent_reported():
    """Kosovo (XKX) has no Eurostat mapping → unknown_geo; a requested region with no rows is reported."""
    pages = [_load(f"cpi_page{i}.json") for i in (1, 2, 3)]
    r = parse_worldbank(pages, CATALOG["WB_CPI"], URL, T0, requested_geos=["RO", "EL", "HU"])
    assert len(r.observations) == 8 and {o.region_code for o in r.observations} == {"RO", "EL"}
    assert sorted((s.region, s.reason) for s in r.skipped) == [("HU", "missing_value")] + [
        ("XKX", "unknown_geo")] * 4


def test_error_payload_raises_source_error():
    """`[{"message": [...]}]` (HTTP 200) is an API error, reported by id/key only."""
    with pytest.raises(SourceError, match="120") as exc:
        parse_worldbank([_load("error_invalid_value.json")], CATALOG["WB_POP"], URL, T0)
    assert exc.value.source == "worldbank"


@pytest.mark.parametrize("bad", [{}, [1], [{"page": 1}, "x"], "text"])
def test_malformed_payload_raises(bad):
    """Anything but [meta, rows] is refused."""
    with pytest.raises(SourceError):
        parse_worldbank([bad], CATALOG["WB_POP"], URL, T0)


def test_no_data_page_is_empty():
    """[meta, null] (no rows for the selection) is an empty result."""
    page = [{"page": 1, "pages": 0, "per_page": 1000, "total": 0}, None]
    assert parse_worldbank([page], CATALOG["WB_POP"], URL, T0).observations == []


def test_other_indicator_rows_refused():
    """Rows for a different indicator than requested are an error, not data."""
    with pytest.raises(SourceError, match="indicator"):
        parse_worldbank([_load("population.json")], CATALOG["WB_CPI"], URL, T0)


def test_advanced_indicator_without_unit_raises_unless_stated():
    """World Bank rows carry no unit; an uncurated indicator needs the user's stated unit."""
    page = _load("population.json")
    with pytest.raises(SourceError, match="unit"):
        parse_worldbank([page], advanced_spec("worldbank", "SP.POP.TOTL", {}), URL, T0)
    r = parse_worldbank([page], advanced_spec("worldbank", "SP.POP.TOTL", {}, unit="PERS"), URL, T0)
    o = r.observations[0]
    assert o.unit == "PERS" and o.indicator_code == "worldbank:SP.POP.TOTL" and o.indicator_label == "Population, total"


def test_frequency_mismatch_skipped():
    """A non-annual date in an annual curated series is invalid_period."""
    page = _load("population.json")
    page[1][0]["date"] = "2024Q1"
    r = parse_worldbank([page], CATALOG["WB_POP"], URL, T0)
    assert [(s.period, s.reason) for s in r.skipped] == [("2024Q1", "invalid_period")]


def _serve_pages(seen):
    """MockTransport handler serving the recorded CPI pages by `page` parameter."""
    def handler(req):
        """Return the requested page."""
        seen.append(req)
        return httpx.Response(200, json=_load(f"cpi_page{req.url.params.get('page', '1')}.json"))
    return handler


def test_fetch_follows_pagination_and_maps_geos_to_iso3():
    """All pages are fetched; Eurostat codes become ISO3 in the path; date range and per_page are sent."""
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(_serve_pages(seen)))
    r = fetch_worldbank(client, CATALOG["WB_CPI"], ["RO", "EL", "XX"], since=2021, until=2024,
                        settings=_settings(worldbank_per_page=5))
    assert [q.url.params.get("page", "1") for q in seen] == ["1", "2", "3"]
    assert seen[0].url.path == "/v2/country/ROU;GRC/indicator/FP.CPI.TOTL.ZG"
    assert seen[0].url.params["date"] == "2021:2024" and seen[0].url.params["per_page"] == "5"
    assert seen[0].url.params["format"] == "json"
    assert len(r.observations) == 8
    assert ("XX", None, "unknown_geo") in [(s.region, s.period, s.reason) for s in r.skipped]
    assert r.observations[0].provenance.query_url == str(seen[0].url)


def test_fetch_refuses_too_many_pages():
    """More pages than WORLDBANK_MAX_PAGES → SourceError (no unbounded loops)."""
    client = httpx.Client(transport=httpx.MockTransport(_serve_pages([])))
    with pytest.raises(SourceError, match="pages"):
        fetch_worldbank(client, CATALOG["WB_CPI"], ["RO"], settings=_settings(worldbank_per_page=5,
                                                                              worldbank_max_pages=2))


def test_fetch_inconsistent_pagination_raises():
    """A total that does not match the rows received (data changed mid-pagination) is refused."""
    def handler(req):
        """Page 2 claims a different total."""
        page = _load(f"cpi_page{req.url.params.get('page', '1')}.json")
        if req.url.params.get("page") == "2":
            page[0]["total"] = 13
        return httpx.Response(200, json=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="pagination"):
        fetch_worldbank(client, CATALOG["WB_CPI"], ["RO"], settings=_settings(worldbank_per_page=5))


def test_fetch_with_no_mappable_geo_makes_no_request():
    """Only unknown regions → no HTTP call, every region reported."""
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(_serve_pages(seen)))
    r = fetch_worldbank(client, CATALOG["WB_CPI"], ["XX"], settings=_settings())
    assert seen == [] and r.observations == [] and [s.reason for s in r.skipped] == ["unknown_geo"]


def test_fetch_rejects_bad_year_range():
    """since > until or partial ranges are argument errors."""
    client = httpx.Client(transport=httpx.MockTransport(_serve_pages([])))
    with pytest.raises(ValueError):
        fetch_worldbank(client, CATALOG["WB_CPI"], ["RO"], since=2024, until=2020, settings=_settings())


@pytest.mark.live
def test_live_ro_gdp_growth():
    """Live: Romania's GDP growth 2020-2024 (RUN_LIVE_TESTS=1)."""
    with httpx.Client() as client:
        r = fetch_worldbank(client, CATALOG["WB_GDP_GROWTH"], ["RO"], since=2020, until=2024, settings=_settings())
    assert {o.period for o in r.observations} == {"2020", "2021", "2022", "2023", "2024"}


def test_non_dict_row_raises():
    """A row that is not an object is a format error."""
    page = [{"page": 1, "pages": 1, "total": 1}, ["oops"]]
    with pytest.raises(SourceError, match="structure"):
        parse_worldbank([page], CATALOG["WB_POP"], URL, T0)
