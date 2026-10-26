"""Tests for the common observation schema and provenance."""
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from statbrief.domain.observation import ApiProvenance, Observation, Provenance, UploadProvenance, make_obs_id

PROV = UploadProvenance(file_id="f1", filename="irs.xlsx", worksheet="Inflation", row_number=14, column="valoare")


def _obs(**kw):
    """Build a valid observation, overriding fields."""
    base = dict(obs_id="obs_1", indicator_code="HICP_YOY", region_code="RO113", region_level="nuts3",
                period="2024-03", frequency="M", value=6.6, unit="%", provenance=PROV)
    base.update(kw)
    return Observation(**base)


def test_valid_observation_roundtrip():
    """A valid observation serialises and re-parses with its provenance type intact."""
    o = _obs()
    again = Observation.model_validate_json(o.model_dump_json())
    assert again == o and isinstance(again.provenance, UploadProvenance)


@pytest.mark.parametrize("period,freq", [("2024", "A"), ("2024-Q1", "Q"), ("2024-03", "M"), ("2024-12", "M")])
def test_period_matches_frequency(period, freq):
    """Each frequency accepts its canonical period format."""
    assert _obs(period=period, frequency=freq).period == period


@pytest.mark.parametrize("period,freq", [("2024-03", "A"), ("2024", "M"), ("2024-13", "M"), ("2024-Q5", "Q"), ("24-03", "M")])
def test_period_frequency_mismatch_rejected(period, freq):
    """Malformed or mismatched periods are rejected."""
    with pytest.raises(ValidationError):
        _obs(period=period, frequency=freq)


def test_missing_value_allowed_but_nan_rejected():
    """A gap is None; NaN/inf are not valid values."""
    assert _obs(value=None).value is None
    with pytest.raises(ValidationError):
        _obs(value=float("nan"))
    with pytest.raises(ValidationError):
        _obs(value=float("inf"))


def test_api_provenance_discriminated():
    """Provenance parses to the right class from a dict."""
    api = {"source_type": "api", "api_name": "eurostat", "series_id": "prc_hicp_manr",
           "query_url": "https://ec.europa.eu/x", "retrieved_at": "2026-10-26T10:00:00Z"}
    p = TypeAdapter(Provenance).validate_python(api)
    assert isinstance(p, ApiProvenance) and p.retrieved_at == datetime(2026, 10, 26, 10, tzinfo=timezone.utc)


def test_observation_is_immutable():
    """Observations are frozen so provenance cannot be altered after creation."""
    with pytest.raises(ValidationError):
        _obs().value = 1.0  # type: ignore[misc]


def test_make_obs_id_stable_and_distinct():
    """Same parts → same id; different parts → different id."""
    a = make_obs_id("f1", 14, "HICP", "RO113", "2024-03")
    assert a == make_obs_id("f1", 14, "HICP", "RO113", "2024-03")
    assert a != make_obs_id("f1", 15, "HICP", "RO113", "2024-03")
    assert a.startswith("obs_") and len(a) == 20
