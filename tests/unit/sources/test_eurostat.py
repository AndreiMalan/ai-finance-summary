"""Tests for the Eurostat client: JSON-stat 2.0 parsing on recorded fixtures, request building, caching."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from statbrief.config import Settings
from statbrief.domain.observation import make_obs_id
from statbrief.sources.catalog import CATALOG, advanced_spec
from statbrief.sources.eurostat import build_params, fetch_eurostat, parse_jsonstat
from statbrief.sources.http import HttpPolicy, SourceError

DATA = Path(__file__).resolve().parents[2] / "data" / "eurostat"
T0 = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/x?format=JSON"


def _load(name: str) -> dict:
    """A recorded Eurostat response (recorded 2026-10-26)."""
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _by_key(result):
    """{(region, period): observation}."""
    return {(o.region_code, o.period): o for o in result.observations}


def test_multi_geo_hicp():
    """Three geos × six months → 18 monthly % observations with Eurostat provenance."""
    spec = CATALOG["HICP_YOY"]
    r = parse_jsonstat(_load("hicp_multi_geo.json"), spec, URL, T0)
    obs = _by_key(r)
    assert len(r.observations) == 18 and r.skipped == []
    ro = obs[("RO", "2026-03")]
    assert ro.value == 9.0 and ro.unit == "%" and ro.unit_multiplier == 0 and ro.frequency == "M"
    assert ro.indicator_code == "HICP_YOY" and ro.region_level == "country" and ro.status_flag is None
    assert obs[("EU27_2020", "2026-01")].value == 2.0 and obs[("EU27_2020", "2026-01")].region_level == "aggregate"
    assert obs[("HU", "2026-06")].value == 2.1
    p = ro.provenance
    assert p.api_name == "eurostat" and p.series_id == "prc_hicp_minr?coicop18=TOTAL&unit=RCH_A"
    assert p.query_url == URL and p.retrieved_at == T0
    assert ro.obs_id == make_obs_id("eurostat", "prc_hicp_minr", "coicop18=TOTAL&unit=RCH_A", "RO", "2026-03")
    assert r.meta.unit_label == "Annual rate of change" and r.meta.last_update == "2026-10-16T23:00:00+0200"


def test_quarterly_gdp_with_flags_and_absent_requested_geo():
    """Quarterly periods, provisional flags kept; a requested geo absent from the response is reported."""
    r = parse_jsonstat(_load("gdp_quarterly_flags.json"), CATALOG["GDP_VOL"], URL, T0,
                       requested_geos=["RO", "EL", "UK"])
    obs = _by_key(r)
    assert len(obs) == 8 and {o.status_flag for o in obs.values()} == {"p"}
    assert obs[("RO", "2025-Q3")].value == 50695.7 and obs[("RO", "2025-Q3")].frequency == "Q"
    assert obs[("EL", "2026-Q2")].unit == "CLV10_EUR" and obs[("EL", "2026-Q2")].unit_multiplier == 6
    assert [(s.region, s.period, s.reason) for s in r.skipped] == [("UK", None, "missing_value")]


def test_nuts2_regional_with_unknown_geos():
    """NUTS2 regions resolve; unknown regions (HU11, extra-regio ROZZ) are skipped per cell, not invented."""
    r = parse_jsonstat(_load("gdp_pc_nuts2.json"), CATALOG["GDP_PC_REG"], URL, T0)
    obs = _by_key(r)
    assert len(obs) == 8 and obs[("RO32", "2021")].value == 29500.0 and obs[("RO32", "2021")].frequency == "A"
    assert obs[("RO11", "2024")].region_level == "nuts2" and obs[("RO11", "2024")].status_flag == "p"
    assert obs[("RO11", "2023")].status_flag is None
    unknown = [(s.region, s.period) for s in r.skipped if s.reason == "unknown_geo"]
    assert len(r.skipped) == 8 and {g for g, _ in unknown} == {"HU11", "ROZZ"}


def test_missing_values_are_skipped_with_reason():
    """Sparse cells without a value (latest month, and UK entirely) are reported, never filled."""
    r = parse_jsonstat(_load("unemp_missing.json"), CATALOG["UNEMP_RATE"], URL, T0)
    assert len(r.observations) == 12
    missing = {(s.region, s.period) for s in r.skipped if s.reason == "missing_value"}
    assert missing == {("EU27_2020", "2026-09"), ("EL", "2026-09"), ("IT", "2026-09"), ("RO", "2026-09"),
                       ("UK", "2026-06"), ("UK", "2026-07"), ("UK", "2026-08"), ("UK", "2026-09")}
    assert _by_key(r)[("EL", "2026-06")].value == 8.1


def test_index_units():
    """Industrial production keeps the index unit I21."""
    r = parse_jsonstat(_load("ind_prod.json"), CATALOG["IND_PROD"], URL, T0)
    assert len(r.observations) == 6 and {o.unit for o in r.observations} == {"I21"}


def _tiny(values, status=None, unit=("PC",), times=("2024M01", "2024M02"), freq="M", extra=None):
    """A minimal JSON-stat 2.0 payload (one geo RO) for edge cases."""
    dims = {"freq": [freq], "unit": list(unit), "geo": ["RO"], "time": list(times)}
    if extra:
        dims = {**extra, **dims}
    payload = {"version": "2.0", "class": "dataset", "label": "Tiny", "updated": "2026-01-01",
               "id": list(dims), "size": [len(v) for v in dims.values()], "value": values,
               "dimension": {d: {"category": {"index": {c: i for i, c in enumerate(cs)},
                                              "label": {c: c.lower() for c in cs}}} for d, cs in dims.items()}}
    if status is not None:
        payload["status"] = status
    return payload


ADV = advanced_spec("eurostat", "tiny_ds", {"unit": "PC"})


def test_colon_value_and_flag_without_value_are_missing():
    """':' or null values (e.g. confidential 'c') are skipped with their flag; dense lists are supported."""
    r = parse_jsonstat(_tiny({"0": ":", "1": 1.5}, status={"0": "c"}), ADV, URL, T0)
    assert [(o.period, o.value) for o in r.observations] == [("2024-02", 1.5)]
    assert [(s.period, s.reason, s.status_flag) for s in r.skipped] == [("2024M01", "missing_value", "c")]
    dense = parse_jsonstat(_tiny([None, 2.5], status=["", "e"]), ADV, URL, T0)
    assert [(o.period, o.value, o.status_flag) for o in dense.observations] == [("2024-02", 2.5, "e")]


def test_advanced_spec_takes_unit_from_response_and_label_from_dataset():
    """Uncurated: unit code from the single unit category, indicator label from the dataset label."""
    r = parse_jsonstat(_tiny({"0": 1.0, "1": 2.0}), ADV, URL, T0)
    o = r.observations[0]
    assert o.unit == "PC" and o.indicator_code == "eurostat:tiny_ds?unit=PC" and o.indicator_label == "Tiny"


def test_unit_unknown_raises():
    """No unit dimension and no stated unit → error, never an invented unit."""
    payload = _tiny({"0": 1.0, "1": 2.0})
    payload["id"].remove("unit")
    payload["size"].pop(1)
    del payload["dimension"]["unit"]
    with pytest.raises(SourceError, match="unit"):
        parse_jsonstat(payload, advanced_spec("eurostat", "tiny_ds", {}), URL, T0)
    stated = parse_jsonstat(payload, advanced_spec("eurostat", "tiny_ds", {}, unit="%"), URL, T0)
    assert stated.observations[0].unit == "%"


def test_ambiguous_extra_dimension_raises():
    """A non-geo/time dimension with several categories would mix series: refused, not merged."""
    payload = _tiny({"0": 1.0}, unit=("PC", "THS"))
    with pytest.raises(SourceError, match="unit"):
        parse_jsonstat(payload, advanced_spec("eurostat", "tiny_ds", {}), URL, T0)


def test_filter_not_honoured_raises():
    """A response whose filtered dimension differs from the filter (or lacks it) is refused."""
    with pytest.raises(SourceError):
        parse_jsonstat(_tiny({"0": 1.0}, unit=("THS",)), ADV, URL, T0)
    with pytest.raises(SourceError):
        parse_jsonstat(_tiny({"0": 1.0}), advanced_spec("eurostat", "tiny_ds", {"unit": "PC", "sex": "T"}),
                       URL, T0)


def test_unsupported_or_mismatched_frequency_raises():
    """Daily/semester data is refused (never collapsed into months); curated frequency must match."""
    with pytest.raises(SourceError, match="frequency"):
        parse_jsonstat(_tiny({"0": 1.0}, freq="D", times=("2024-01-01", "2024-01-02")), ADV, URL, T0)
    hicp = _load("hicp_multi_geo.json")
    hicp["dimension"]["freq"]["category"] = {"index": {"Q": 0}, "label": {"Q": "Quarterly"}}
    with pytest.raises(SourceError, match="frequency"):
        parse_jsonstat(hicp, CATALOG["HICP_YOY"], URL, T0)


def test_invalid_and_duplicate_periods_skipped():
    """Unparseable periods and two codes for one period are skipped, not guessed or overwritten."""
    r = parse_jsonstat(_tiny({"0": 1.0, "1": 2.0, "2": 3.0}, times=("2024M01", "2024-01", "20X4")), ADV, URL, T0)
    assert [(o.period, o.value) for o in r.observations] == [("2024-01", 1.0)]
    assert sorted((s.period, s.reason) for s in r.skipped) == [("2024-01", "duplicate_period"),
                                                               ("20X4", "invalid_period")]


def test_empty_selection_returns_no_observations():
    """A dimension of size 0 (e.g. no data in range) is an empty result, not an error."""
    payload = _tiny({}, times=())
    assert parse_jsonstat(payload, ADV, URL, T0).observations == []


def test_malformed_payload_raises():
    """A response that is not JSON-stat raises SourceError (no partial parse)."""
    for bad in ({"error": [{"status": 404}]}, {"id": ["geo"], "size": [1, 2]}, []):
        with pytest.raises(SourceError):
            parse_jsonstat(bad, ADV, URL, T0)  # type: ignore[arg-type]


def test_build_params():
    """format/lang, sorted filters, one geo param per region, canonical time bounds."""
    p = build_params(CATALOG["UNEMP_RATE"], ["RO", "EU27_2020"], since="2024M01", until="2024-06")
    assert p == [("format", "JSON"), ("lang", "EN"), ("age", "TOTAL"), ("s_adj", "SA"), ("sex", "T"),
                 ("unit", "PC_ACT"), ("geo", "RO"), ("geo", "EU27_2020"), ("sinceTimePeriod", "2024-01"),
                 ("untilTimePeriod", "2024-06")]
    assert build_params(CATALOG["GDP_VOL"], ["RO"], last_n=3)[-1] == ("lastTimePeriod", "3")


@pytest.mark.parametrize("geos", [[], ["RO&x=1"], ["ro"]])
def test_build_params_rejects_bad_geos(geos):
    """At least one geo; only upper-case code characters."""
    with pytest.raises(ValueError):
        build_params(CATALOG["UNEMP_RATE"], geos)


def _settings():
    """Settings with default API configuration."""
    return Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)


def test_fetch_eurostat_requests_dataset_and_parses():
    """The dataset is the last path segment; the final URL becomes the provenance query URL."""
    seen = []

    def handler(req):
        """Serve the recorded HICP fixture."""
        seen.append(req)
        return httpx.Response(200, json=_load("hicp_multi_geo.json"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    s = _settings()
    r = fetch_eurostat(client, CATALOG["HICP_YOY"], ["RO", "HU", "EU27_2020"], since="2026-01", until="2026-06",
                       settings=s, now=lambda: T0)
    req = seen[0]
    assert req.url.path == "/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"
    assert req.url.params.get_list("geo") == ["RO", "HU", "EU27_2020"]
    assert len(r.observations) == 18
    assert r.observations[0].provenance.query_url == str(req.url)
    assert r.observations[0].provenance.retrieved_at == T0


def test_fetch_eurostat_error_is_source_error():
    """A 404 for an unknown dataset surfaces as SourceError without the body."""
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"error": [{"label": "ERR_NOT_FOUND_4: NOSUCH"}]})))
    with pytest.raises(SourceError) as exc:
        fetch_eurostat(client, advanced_spec("eurostat", "nosuch", {}), ["RO"], settings=_settings())
    assert exc.value.status == 404 and "NOSUCH" not in str(exc.value)


def test_fixture_not_mutated_by_parse():
    """Parsing never mutates the (possibly cached) payload."""
    payload = _load("unemp_missing.json")
    before = copy.deepcopy(payload)
    parse_jsonstat(payload, CATALOG["UNEMP_RATE"], URL, T0)
    assert payload == before


@pytest.mark.live
def test_live_ro_hicp_last_three_months():
    """Live: Romania's HICP annual rate for the last 3 months (RUN_LIVE_TESTS=1)."""
    s = _settings()
    with httpx.Client() as client:
        r = fetch_eurostat(client, CATALOG["HICP_YOY"], ["RO"], last_n=3, settings=s,
                           policy=HttpPolicy.from_settings(s))
    assert len(r.observations) == 3 and all(o.region_code == "RO" and o.unit == "%" for o in r.observations)


def test_non_finite_values_are_missing():
    """NaN/Infinity (accepted by Python's JSON parser) are skipped, never stored."""
    r = parse_jsonstat(_tiny({"0": float("nan"), "1": float("inf")}), ADV, URL, T0)
    assert r.observations == [] and [s.reason for s in r.skipped] == ["missing_value", "missing_value"]


def test_empty_filter_dimension_raises_no_data():
    """A filter value unknown to the dataset (size-0 dimension) is an error, not an empty series."""
    payload = _tiny({}, unit=())
    with pytest.raises(SourceError, match="no data for filter unit=PC"):
        parse_jsonstat(payload, ADV, URL, T0, requested_geos=["RO"])
    no_geo = _tiny({})
    no_geo["size"][no_geo["id"].index("geo")] = 0
    no_geo["dimension"]["geo"]["category"] = {"index": {}, "label": {}}
    with pytest.raises(SourceError, match="no data"):
        parse_jsonstat(no_geo, ADV, URL, T0)


def test_non_standard_axis_order_and_sparse_status():
    """Axis order time, unit, geo with out-of-order category indices maps every cell to the right key."""
    cube = {"version": "2.0", "class": "dataset", "id": ["time", "unit", "geo"], "size": [3, 1, 2],
            "dimension": {"time": {"category": {"index": {"2024-03": 2, "2024-01": 0, "2024-02": 1}}},
                          "unit": {"category": {"index": {"PC": 0}}},
                          "geo": {"category": {"index": {"RO": 0, "HU": 1}}}},
            "value": {"0": 10, "1": 20, "2": 11, "4": 12, "5": 22}, "status": {"3": "c", "5": "p"}}
    r = parse_jsonstat(cube, ADV, URL, T0, requested_geos=["RO", "HU", "DE"])
    assert sorted((o.region_code, o.period, o.value, o.status_flag) for o in r.observations) == [
        ("HU", "2024-01", 20.0, None), ("HU", "2024-03", 22.0, "p"),
        ("RO", "2024-01", 10.0, None), ("RO", "2024-02", 11.0, None), ("RO", "2024-03", 12.0, None)]
    assert sorted((s.region, s.period, s.reason, s.status_flag) for s in r.skipped) == [
        ("DE", None, "missing_value", None), ("HU", "2024-02", "missing_value", "c")]
