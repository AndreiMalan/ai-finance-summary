"""Effective observations (M7): drops, period window, links, consented gap filling, resolutions, slices."""
from datetime import datetime, timezone

import pytest

from statbrief.analytics.combine import detect_conflicts
from statbrief.domain.conflict import ConflictResolution, IndicatorLink, UnresolvedConflicts
from statbrief.domain.observation import ApiProvenance, Observation, UploadProvenance
from statbrief.graph.observations import (effective_observations, effective_set, in_window, lookback_start,
                                          month_span)
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
    assert "u2" not in ids and "a2" not in ids and len(filled) == 3  # u1, u3 and the filled copy of a2 (C1)


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


# --- review C1: one row per key; agreeing duplicates collapse to the upload; gap fill never duplicates ------

LINK = [IndicatorLink(upload_code="IPC", catalog_code="HICP")]


def _metrics(obs, ind="HICP"):
    """compute_metrics for RO."""
    from statbrief.analytics.metrics import compute_metrics
    return compute_metrics(obs, indicator=ind, region="RO")


def test_agreeing_upload_and_api_collapse_to_the_upload_with_corroboration():
    """Probe case B: identical linked values give the same metrics as the upload alone, no ambiguous skips."""
    upload = [_o("u1", "2024-01", 2.0), _o("u2", "2024-02", 2.1), _o("u3", "2024-03", 2.2)]
    api = [_o("a1", "2024-01", 2.0, ind="HICP", prov=API), _o("a2", "2024-02", 2.1, ind="HICP", prov=API),
           _o("a3", "2024-03", 2.2, ind="HICP", prov=API)]
    assert detect_conflicts(effective_observations(upload + api, _view(links=LINK), resolve=False),
                            rel_tol=0.005).conflicts == []
    eff = effective_set(upload + api, _view(links=LINK))
    assert [o.obs_id for o in eff.observations] == ["u1", "u2", "u3"]
    assert eff.corroborations == {"u1": ["a1"], "u2": ["a2"], "u3": ["a3"]}
    both, alone = _metrics(eff.observations), _metrics(effective_observations(upload, _view(links=LINK)))
    assert both == alone and both[0] and not any("ambiguous" in s.reason for s in both[1])


def test_within_tolerance_values_keep_the_upload_value():
    """2.1 vs 2.12 is within one rounding unit: no conflict, the upload value is the one analysed."""
    raw = [_o("u1", "2024-01", 2.1), _o("a1", "2024-01", 2.12, ind="HICP", prov=API)]
    view = _view(links=LINK)
    assert detect_conflicts(effective_observations(raw, view, resolve=False), rel_tol=0.005).conflicts == []
    out = effective_observations(raw, view)
    assert [(o.obs_id, o.value) for o in out] == [("u1", 2.1)]


def test_gap_fill_has_one_row_per_key_and_makes_the_change_computable():
    """Probe case A: the filled copy replaces both the missing upload and the API original."""
    raw = [_o("u1", "2024-01", 2.0), _o("u2", "2024-02", None), _o("u3", "2024-03", 2.2),
           _o("a2", "2024-02", 2.1, ind="HICP", prov=API)]
    out = effective_observations(raw, _view(links=LINK, consent_gap_fill=True))
    feb = [o for o in out if o.period == "2024-02"]
    assert len(out) == 3 and len(feb) == 1 and feb[0].value == 2.1 and feb[0].obs_id not in {"u2", "a2"}
    metrics, skips = _metrics(out)
    step = next(m for m in metrics if m.kind == "pp_change" and m.base_period == "2024-02")
    assert step.value == pytest.approx(0.1) and feb[0].obs_id in step.observation_ids
    assert not any("ambiguous" in s.reason for s in skips)
    no_consent = effective_observations(raw, _view(links=LINK))  # the user's missing value stays primary
    assert [o.obs_id for o in no_consent if o.period == "2024-02"] == ["u2"]


def test_resolved_conflicts_keep_one_row_and_keep_both_stays_ambiguous():
    """prefer/exclude leave one (or no) row per key; keep_both keeps both and the metric is skipped, not picked."""
    raw = [_o("u1", "2024-01", 2.0, ind="HICP"), _o("a1", "2024-01", 3.5, ind="HICP", prov=API),
           _o("u0", "2023-12", 1.0, ind="HICP")]
    conflicts = detect_conflicts(raw, rel_tol=0.005).conflicts
    cid = conflicts[0].conflict_id
    prefer = effective_observations(raw, _view(conflicts=conflicts, resolutions={
        cid: ConflictResolution(conflict_id=cid, choice="prefer", preferred_obs_id="u1")}))
    assert [o.obs_id for o in prefer if o.period == "2024-01"] == ["u1"]
    both = effective_observations(raw, _view(conflicts=conflicts, resolutions={
        cid: ConflictResolution(conflict_id=cid, choice="keep_both")}))
    assert sorted(o.obs_id for o in both if o.period == "2024-01") == ["a1", "u1"]
    assert any("ambiguous" in s.reason for s in _metrics(both)[1])


def test_lookback_start_is_one_year_frequency_aware():
    """D-035: 12 months / 4 quarters / 1 year before period_from."""
    assert (lookback_start("2024-03"), lookback_start("2024-Q2"), lookback_start("2024")) == \
        ("2023-03", "2023-Q2", "2023")
    assert lookback_start(None) is None
