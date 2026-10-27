"""Tests for combining uploaded and API observations, conflict detection/resolution and gap filling (AC 20-22)."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from statbrief.analytics.combine import (apply_resolutions, combine, detect_conflicts, fill_gaps,
                                         rounding_unit)
from statbrief.config import Settings
from statbrief.domain.conflict import ConflictResolution, IndicatorLink, UnresolvedConflicts
from statbrief.domain.observation import ApiProvenance, Observation, UploadProvenance

T0 = datetime(2026, 10, 26, tzinfo=timezone.utc)
TOL = 0.005
LINK = IndicatorLink(upload_code="inflatie", catalog_code="HICP_YOY")


def _up(period, value, *, code="inflatie", region="RO", unit="%", mult=0, file_id="f1", n=None, freq="M"):
    """An uploaded observation."""
    prov = UploadProvenance(file_id=file_id, filename="irs.xlsx", row_number=2, column="valoare")
    return Observation(obs_id=n or f"up_{file_id}_{code}_{region}_{period}", indicator_code=code,
                       indicator_label="Inflatie", region_code=region, region_level="country", period=period,
                       frequency=freq, value=value, unit=unit, unit_multiplier=mult, provenance=prov)


def _api(period, value, *, code="HICP_YOY", region="RO", unit="%", mult=0, api="eurostat", freq="M"):
    """An API observation."""
    prov = ApiProvenance(api_name=api, series_id="prc_hicp_minr?coicop18=TOTAL&unit=RCH_A",
                         query_url="https://ec.europa.eu/x", retrieved_at=T0)
    return Observation(obs_id=f"api_{api}_{code}_{region}_{period}", indicator_code=code, indicator_label="HICP",
                       region_code=region, region_level="country", period=period, frequency=freq, value=value,
                       unit=unit, unit_multiplier=mult, provenance=prov)


def test_settings_default_conflict_tolerance():
    """D-010: 0.5 % relative tolerance."""
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None)
    assert s.conflict_rel_tol == 0.005


def test_rounding_unit_from_written_precision():
    """The rounding unit is the last written decimal place (whole numbers → 1)."""
    assert str(rounding_unit(3.3)) == "0.1" and str(rounding_unit(3.27)) == "0.01"
    assert rounding_unit(34100.0) == 1 and rounding_unit(-2.5) == rounding_unit(0.5)


def test_combine_applies_links_to_copies_only():
    """Linked upload codes are renamed on copies for comparison; originals and provenance untouched."""
    up = [_up("2024-03", 3.27), _up("2024-03", 1.0, code="other")]
    api = [_api("2024-03", 3.3)]
    out = combine(up, api, [LINK])
    assert [o.indicator_code for o in out] == ["HICP_YOY", "other", "HICP_YOY"]
    assert up[0].indicator_code == "inflatie" and out[0].provenance == up[0].provenance
    assert out[0].obs_id == up[0].obs_id


def test_links_must_be_one_to_one():
    """One upload code linked to two catalog codes (or vice versa) is refused: links are never guessed."""
    with pytest.raises(ValueError):
        combine([], [], [LINK, IndicatorLink(upload_code="inflatie", catalog_code="WB_CPI")])
    with pytest.raises(ValueError):
        combine([], [], [LINK, IndicatorLink(upload_code="cpi", catalog_code="HICP_YOY")])


def test_rounding_equal_is_not_a_conflict():
    """3.27 (upload) vs 3.3 (Eurostat, 1 decimal): within the coarser rounding unit."""
    scan = detect_conflicts(combine([_up("2024-03", 3.27)], [_api("2024-03", 3.3)], [LINK]), rel_tol=TOL)
    assert scan.conflicts == [] and scan.unit_mismatches == []


def test_real_difference_is_a_conflict():
    """6.6 vs 6.2 differs by more than max(0.5 %, 0.1)."""
    up, api = _up("2024-03", 6.6), _api("2024-03", 6.2)
    scan = detect_conflicts(combine([up], [api], [LINK]), rel_tol=TOL)
    [c] = scan.conflicts
    assert (c.indicator, c.region, c.period, c.unit) == ("HICP_YOY", "RO", "2024-03", "%")
    assert set(c.candidates) == {up.obs_id, api.obs_id} and sorted(c.values) == [6.2, 6.6]
    assert c.rel_diff == pytest.approx(0.4 / 6.6) and c.conflict_id.startswith("cf_")


def test_boundary_exactly_at_rounding_unit_is_not_a_conflict():
    """3.3 vs 3.2: difference equals the rounding unit (not greater) — decimal arithmetic, no float noise."""
    scan = detect_conflicts(combine([_up("2024-03", 3.3)], [_api("2024-03", 3.2)], [LINK]), rel_tol=TOL)
    assert scan.conflicts == []


def test_relative_tolerance_dominates_for_large_values():
    """Levels: 1000.0 vs 1004.0 is within 0.5 %; 1000 vs 1006 is not."""
    ok = detect_conflicts([_up("2024-03", 1000.0, code="X"), _api("2024-03", 1004.0, code="X")], rel_tol=TOL)
    bad = detect_conflicts([_up("2024-03", 1000.0, code="X"), _api("2024-03", 1006.0, code="X")], rel_tol=TOL)
    assert ok.conflicts == [] and len(bad.conflicts) == 1


def test_without_rounding_tolerance_only_relative_counts():
    """ignore_rounding=False compares with the relative tolerance only."""
    obs = combine([_up("2024-03", 3.27)], [_api("2024-03", 3.3)], [LINK])
    assert len(detect_conflicts(obs, rel_tol=TOL, ignore_rounding=False).conflicts) == 1


def test_same_source_values_are_not_conflicts():
    """Two values from the same file (a duplicate, handled by validation) are not a cross-source conflict."""
    obs = [_up("2024-03", 1.0, n="a"), _up("2024-03", 9.0, n="b")]
    assert detect_conflicts(obs, rel_tol=TOL).conflicts == []


def test_two_uploaded_files_are_different_sources():
    """Different files disagreeing on the same key do conflict."""
    obs = [_up("2024-03", 1.0, file_id="f1"), _up("2024-03", 9.0, file_id="f2")]
    assert len(detect_conflicts(obs, rel_tol=TOL).conflicts) == 1


def test_missing_values_never_conflict():
    """A None value is a gap, not a disagreement."""
    obs = combine([_up("2024-03", None)], [_api("2024-03", 6.2)], [LINK])
    assert detect_conflicts(obs, rel_tol=TOL).conflicts == []


def test_unit_mismatch_reported_not_compared():
    """'%' vs 'pp' (or another multiplier) are never compared numerically; the mismatch is reported."""
    obs = combine([_up("2024-03", 6.6, unit="pp")], [_api("2024-03", 6.2)], [LINK])
    scan = detect_conflicts(obs, rel_tol=TOL)
    assert scan.conflicts == []
    [m] = scan.unit_mismatches
    assert (m.indicator, m.region, m.period) == ("HICP_YOY", "RO", "2024-03")
    assert sorted(m.units) == ["%", "pp"] and len(m.candidates) == 2
    scaled = detect_conflicts([_up("2024-Q1", 5.0, code="G", mult=6, freq="Q"),
                               _api("2024-Q1", 5.0, code="G", mult=3, freq="Q")], rel_tol=TOL)
    assert scaled.conflicts == [] and len(scaled.unit_mismatches) == 1


def _conflict_setup():
    """One conflict between an upload and Eurostat for 2024-03, plus an unrelated point."""
    up, api, other = _up("2024-03", 6.6), _api("2024-03", 6.2), _api("2024-04", 6.0)
    obs = combine([up], [api, other], [LINK])
    return obs, detect_conflicts(obs, rel_tol=TOL).conflicts, up, api, other


def test_prefer_keeps_chosen_source():
    """'prefer' keeps the preferred candidate and excludes the others with a reason."""
    obs, [c], up, api, other = _conflict_setup()
    kept, excluded = apply_resolutions(obs, [c], [ConflictResolution(conflict_id=c.conflict_id, choice="prefer",
                                                                      preferred_obs_id=api.obs_id)])
    assert {o.obs_id for o in kept} == {api.obs_id, other.obs_id}
    assert [e.obs_id for e in excluded] == [up.obs_id] and c.conflict_id in excluded[0].reason


def test_keep_both_and_exclude():
    """'keep_both' keeps every candidate; 'exclude' drops them all with reasons."""
    obs, [c], up, api, other = _conflict_setup()
    kept, excluded = apply_resolutions(obs, [c], [ConflictResolution(conflict_id=c.conflict_id, choice="keep_both")])
    assert len(kept) == 3 and excluded == []
    kept, excluded = apply_resolutions(obs, [c], [ConflictResolution(conflict_id=c.conflict_id, choice="exclude")])
    assert [o.obs_id for o in kept] == [other.obs_id] and {e.obs_id for e in excluded} == {up.obs_id, api.obs_id}


def test_unresolved_conflicts_raise():
    """Drafting is blocked while any conflict has no resolution (AC 21)."""
    obs, [c], *_ = _conflict_setup()
    with pytest.raises(UnresolvedConflicts) as exc:
        apply_resolutions(obs, [c], [])
    assert exc.value.conflict_ids == [c.conflict_id]


def test_invalid_resolutions_rejected():
    """'prefer' needs a candidate obs id; unknown conflict ids and duplicate resolutions are errors."""
    obs, [c], up, *_ = _conflict_setup()
    with pytest.raises(ValidationError):
        ConflictResolution(conflict_id=c.conflict_id, choice="prefer")
    with pytest.raises(ValidationError):
        ConflictResolution(conflict_id=c.conflict_id, choice="exclude", preferred_obs_id=up.obs_id)
    with pytest.raises(ValueError):
        apply_resolutions(obs, [c], [ConflictResolution(conflict_id=c.conflict_id, choice="prefer",
                                                         preferred_obs_id="obs_not_a_candidate")])
    with pytest.raises(ValueError):
        apply_resolutions(obs, [c], [ConflictResolution(conflict_id=c.conflict_id, choice="keep_both"),
                                     ConflictResolution(conflict_id="cf_unknown", choice="keep_both")])
    with pytest.raises(ValueError):
        apply_resolutions(obs, [c], [ConflictResolution(conflict_id=c.conflict_id, choice="keep_both")] * 2)


def test_gap_fill_requires_consent():
    """Without explicit consent nothing is filled (AC 22)."""
    up = [_up("2024-01", 6.0), _up("2024-03", 6.6)]
    api = [_api("2024-02", 6.1)]
    filled, report = fill_gaps(up, api, [LINK], consent=False)
    assert filled == [] and report.consent is False and report.filled == []


def test_gap_fill_with_consent_keeps_api_provenance():
    """Internal gaps (absent or None-valued periods) are filled from linked API data, provenance kept."""
    up = [_up("2024-01", 6.0), _up("2024-03", None), _up("2024-04", 6.6)]
    api = [_api("2024-02", 6.1), _api("2024-03", 6.3), _api("2024-04", 6.5), _api("2024-05", 6.4),
           _api("2024-02", 1.0, region="HU")]
    filled, report = fill_gaps(up, api, [LINK], consent=True)
    assert [(o.period, o.value) for o in filled] == [("2024-02", 6.1), ("2024-03", 6.3)]
    assert all(isinstance(o.provenance, ApiProvenance) for o in filled)
    assert all(o.indicator_code == "inflatie" for o in filled)
    assert [(f.period, f.obs_id, f.replaced_obs_id) for f in report.filled] == [
        ("2024-02", api[0].obs_id, None), ("2024-03", api[1].obs_id, up[1].obs_id)]
    assert report.filled[0].api_name == "eurostat" and report.consent is True


def test_gap_fill_never_crosses_units_or_unlinked_indicators():
    """A unit mismatch is reported instead of filled; unlinked indicators are never used."""
    up = [_up("2024-01", 6.0), _up("2024-03", 6.6)]
    filled, report = fill_gaps(up, [_api("2024-02", 6.1, unit="pp")], [LINK], consent=True)
    assert filled == [] and [(s.period, s.reason) for s in report.not_filled] == [("2024-02", "unit_mismatch")]
    filled, _ = fill_gaps(up, [_api("2024-02", 6.1, code="UNEMP_RATE")], [LINK], consent=True)
    assert filled == []


def test_gap_fill_includes_none_valued_endpoints_but_never_extends_the_span():
    """A missing first value is a gap; periods before the first or after the last upload are not."""
    up = [_up("2024-02", None), _up("2024-03", 6.6)]
    api = [_api("2024-01", 6.0), _api("2024-02", 6.1), _api("2024-04", 6.4)]
    filled, report = fill_gaps(up, api, [LINK], consent=True)
    assert [o.period for o in filled] == ["2024-02"] and report.filled[0].replaced_obs_id == up[0].obs_id


@pytest.mark.parametrize("a,b,conflict", [
    (5.0, 5.9, True), (0.0, 0.9, True), (3.27, 3.3, False), (6.0, 6.0, False), (6.0, 7.0, False), (6.0, 8.0, True),
    (100.0, 100.4, False), (2.0, 2.95, True),
])
def test_integral_rounding_unit_only_when_both_values_integral(a, b, conflict):
    """A whole number written as 5.0 does not widen the tolerance to 1 against a decimal value."""
    obs = [_up("2024-03", a, code="X"), _api("2024-03", b, code="X")]
    assert bool(detect_conflicts(obs, rel_tol=TOL).conflicts) is conflict
