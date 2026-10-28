"""Effective observations (M7): drops, period window, links, consented gap filling, resolutions, slices."""
from datetime import datetime, timezone

import pytest

from statbrief.analytics.combine import detect_conflicts
from statbrief.domain.conflict import ConflictResolution, IndicatorLink, UnresolvedConflicts
from statbrief.domain.observation import ApiProvenance, Observation, UploadProvenance
from statbrief.graph.observations import effective_observations, in_window, month_span
from statbrief.graph.state import ObservationView

UP = UploadProvenance(file_id="f1", filename="irs.csv", row_number=2, column="value")
API = ApiProvenance(api_name="eurostat", series_id="prc_hicp_minr?x", query_url="https://ec.europa.eu/x",
                    retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


def _o(oid, period, value, *, ind="IPC", reg="RO", prov=UP):
    """One monthly/quarterly/annual observation."""
    freq = "Q" if "-Q" in period else "M" if "-" in period else "A"
    return Observation(obs_id=oid, indicator_code=ind, region_code=reg, region_level="country", period=period,
                       frequency=freq, value=value, unit="%", provenance=prov)


def _view(**kw):
    """A view with defaults."""
    return ObservationView(user_id="u", report_id="r", **kw)


def test_month_span_and_window():
    """Periods are kept only when wholly inside the window; open bounds are unbounded."""
    assert month_span("2024-Q2")[1] - month_span("2024-Q2")[0] == 2
    assert in_window("2024-Q1", "2024-01", "2024-03") and not in_window("2024", "2024-01", "2024-03")
    assert in_window("2024-03", "2024", None) and not in_window("2023-12", "2024", None)
    assert in_window("1999", None, None)


def test_drop_and_window():
    """Dropped source keys and periods outside the window are left out."""
    raw = [_o("a", "2023-12", 1.0), _o("b", "2024-01", 2.0), _o("c", "2024-01", 3.0, ind="HICP", prov=API)]
    out = effective_observations(raw, _view(dropped_keys=["api:eurostat:prc_hicp_minr?x"], period_from="2024-01"))
    assert [o.obs_id for o in out] == ["b"]


def test_links_and_consented_gap_fill():
    """A linked upload code is renamed; with consent its missing value is filled from the API point."""
    raw = [_o("u1", "2024-01", 2.0), _o("u2", "2024-02", None), _o("u3", "2024-03", 2.2),
           _o("a2", "2024-02", 2.1, ind="HICP", prov=API)]
    link = [IndicatorLink(upload_code="IPC", catalog_code="HICP")]
    without = effective_observations(raw, _view(links=link))
    assert {o.indicator_code for o in without} == {"HICP"} and "u2" in {o.obs_id for o in without}
    filled = effective_observations(raw, _view(links=link, consent_gap_fill=True))
    ids = {o.obs_id for o in filled}
    assert "u2" not in ids and len(filled) == 4  # u1, u3, a2 and the filled copy of a2


def test_resolutions_applied_and_required():
    """Resolved conflicts keep the preferred value; an unresolved one raises (analysis never guesses)."""
    raw = [_o("u1", "2024-01", 2.0, ind="HICP"), _o("a1", "2024-01", 3.5, ind="HICP", prov=API)]
    conflicts = detect_conflicts(raw, rel_tol=0.005).conflicts
    assert len(conflicts) == 1
    cid = conflicts[0].conflict_id
    with pytest.raises(UnresolvedConflicts):
        effective_observations(raw, _view(conflicts=conflicts))
    assert len(effective_observations(raw, _view(conflicts=conflicts), resolve=False)) == 2
    res = {cid: ConflictResolution(conflict_id=cid, choice="prefer", preferred_obs_id="a1")}
    assert [o.obs_id for o in effective_observations(raw, _view(conflicts=conflicts, resolutions=res))] == ["a1"]
