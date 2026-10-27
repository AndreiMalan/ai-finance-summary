"""Tests for deterministic metric computation (AC 23–24)."""
import pytest

from statbrief.analytics.metrics import compute_metrics, compute_single, is_rate, shift_period
from statbrief.domain.metrics import MetricResult, MetricSkip
from tests.unit.analytics.helpers import monthly, obs


def _by_id(results):
    """Index results by metric id."""
    return {r.metric_id: r for r in results}


def _skip(skips, kind):
    """The single skip of this kind."""
    found = [s for s in skips if s.kind == kind]
    assert len(found) == 1, skips
    return found[0]


@pytest.mark.parametrize("unit,expected", [("%", True), ("pp", True), ("RON", False), ("I15", False), ("EUR", False)])
def test_is_rate(unit, expected):
    """Only percent and percentage-point units are rates."""
    assert is_rate(unit) is expected


@pytest.mark.parametrize("period,n,expected", [
    ("2024-03", -12, "2023-03"), ("2024-01", -1, "2023-12"), ("2024-Q1", -1, "2023-Q4"), ("2024-Q3", -4, "2023-Q3"),
    ("2024", -1, "2023"), ("2023-12", 1, "2024-01"),
])
def test_shift_period(period, n, expected):
    """Periods shift by whole steps of their own frequency."""
    assert shift_period(period, n) == expected


def test_monthly_rate_yoy_is_pp_change():
    """A rate's year-on-year change is a percentage-point difference, not a percent change."""
    data = monthly([5.0] * 12 + [6.6, 6.0, 5.27], start_year=2023)  # 2023-01 .. 2024-03
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    r = _by_id(res)
    yoy = r["pp_change:HICP:RO:2024-03:2023-03"]
    assert yoy.value == 5.27 - 5.0 and yoy.unit == "pp"
    assert yoy.observation_ids == ["HICP|RO|2024-03|%", "HICP|RO|2023-03|%"]
    assert "5.27" in yoy.formula and "5.0" in yoy.formula
    assert r["latest:HICP:RO:2024-03"].value == 5.27
    assert r["pp_change:HICP:RO:2024-03:2024-02"].value == 5.27 - 6.0  # MoM of a rate, in pp
    assert not any(x.kind in ("yoy", "mom", "pct_change") for x in res)
    assert "rate" in _skip(skips, "pct_change").reason


def test_level_yoy_is_percent_unrounded():
    """A level's YoY is (v_t / v_{t-1y} - 1) * 100 with no rounding, and the formula shows the inputs."""
    data = [obs("2023", 100.9, unit="I15"), obs("2024", 104.2, unit="I15")]
    res, _ = compute_metrics(data, indicator="HICP", region="RO")
    r = _by_id(res)
    yoy = r["yoy:HICP:RO:2024"]
    assert yoy.value == (104.2 / 100.9 - 1) * 100 and yoy.unit == "%"
    assert "104.2" in yoy.formula and "100.9" in yoy.formula and repr(yoy.value) in yoy.formula
    assert r["abs_change:HICP:RO:2024:2023"].value == 104.2 - 100.9
    assert r["abs_change:HICP:RO:2024:2023"].unit == "I15"
    assert r["pct_change:HICP:RO:2024:2023"].value == yoy.value


def test_mom_not_on_quarterly():
    """MoM needs monthly data; QoQ is computed for quarterly levels."""
    data = [obs(p, v, unit="RON", mult=6) for p, v in [("2023-Q4", 200.0), ("2024-Q1", 210.0)]]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "monthly" in _skip(skips, "mom").reason
    q = _by_id(res)["qoq:HICP:RO:2024-Q1"]
    assert q.value == (210.0 / 200.0 - 1) * 100
    assert _by_id(res)["latest:HICP:RO:2024-Q1"].unit_multiplier == 6


def test_missing_base_skipped_with_reason():
    """Without the observation one year earlier, YoY is skipped, not guessed."""
    data = monthly([1.0, 2.0, 3.0], start_year=2024, unit="RON")
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "2023-03" in _skip(skips, "yoy").reason
    assert all(r.kind != "yoy" for r in res)


def test_gap_at_window_start_skipped():
    """A missing value at the base of a comparison is a recorded skip."""
    data = [obs("2023", None, unit="RON"), obs("2024", 5.0, unit="RON")]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "missing" in _skip(skips, "yoy").reason
    assert "missing" in _skip(skips, "pct_change").reason
    assert [r.kind for r in res] == ["latest"]


def test_latest_is_last_non_missing():
    """A trailing gap leaves latest at the last non-missing period."""
    data = [obs("2022", 1.0, unit="RON"), obs("2023", 2.0, unit="RON"), obs("2024", None, unit="RON")]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert _by_id(res)["latest:HICP:RO:2023"].value == 2.0
    assert _by_id(res)["yoy:HICP:RO:2023"].value == 100.0
    assert "missing" in _skip(skips, "abs_change").reason  # window end 2024 is a gap


def test_zero_base_skipped():
    """A zero base makes a percent change undefined: skipped with a reason."""
    data = [obs("2023", 0.0, unit="RON"), obs("2024", 5.0, unit="RON")]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "zero" in _skip(skips, "yoy").reason
    assert _by_id(res)["abs_change:HICP:RO:2024:2023"].value == 5.0


def test_negative_base_percent_skipped():
    """A percent change from a negative base is not meaningful: skipped."""
    data = [obs("2023", -4.0, unit="RON"), obs("2024", 2.0, unit="RON")]
    _, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "negative" in _skip(skips, "yoy").reason


def test_mixed_units_skipped():
    """Never compute across different units or multipliers."""
    data = [obs("2023", 10.0, unit="RON", mult=6), obs("2024", 12.0, unit="RON", mult=3)]
    _, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "unit" in _skip(skips, "yoy").reason
    assert "unit" in _skip(skips, "abs_change").reason
    data = [obs("2023", 10.0, unit="EUR"), obs("2024", 12.0, unit="RON")]
    _, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert "unit" in _skip(skips, "yoy").reason


def test_rate_and_level_mixed_skipped():
    """A series mixing a rate unit and a level unit never yields a change."""
    data = [obs("2023", 10.0, unit="RON"), obs("2024", 12.0, unit="%")]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert [r.kind for r in res] == ["latest"]
    assert all("unit" in s.reason for s in skips if s.kind in ("yoy", "pp_change", "abs_change"))


def test_duplicate_period_skipped():
    """Two observations for one period are ambiguous: metrics touching it are skipped."""
    data = [obs("2023", 10.0, unit="RON"), obs("2024", 12.0, unit="RON", oid="a"),
            obs("2024", 13.0, unit="RON", oid="b")]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert res == []
    assert "2 observations" in _skip(skips, "latest").reason


def test_other_series_and_empty():
    """Only the requested indicator/region is used; none at all is a recorded skip."""
    data = [obs("2024", 1.0, ind="GDP", unit="RON")]
    res, skips = compute_metrics(data, indicator="HICP", region="RO")
    assert res == [] and "no observations" in _skip(skips, "latest").reason


def test_deterministic_ordering():
    """Same input in any order gives identical output, with unique ids."""
    data = monthly([float(i) for i in range(1, 16)], start_year=2023, unit="RON")
    a = compute_metrics(data, indicator="HICP", region="RO")
    b = compute_metrics(list(reversed(data)), indicator="HICP", region="RO")
    assert a == b
    assert len({r.metric_id for r in a[0]}) == len(a[0])


@pytest.mark.parametrize("kind,period,base,reason", [
    ("yoy", "2024", None, "rate"),                # rates use pp_change
    ("pp_change", "2024", "2023", None),
    ("pct_change", "2024", "2023", "rate"),
    ("abs_change", "2024", "2023", "pp_change"),
    ("pp_change", "2023", "2024", "earlier"),      # base must precede period
    ("latest", "2023", None, "latest"),            # 2023 is not the latest period
    ("mom", "2024", None, "monthly"),
    ("qoq", "2024", None, "quarterly"),
])
def test_compute_single_rate_rules(kind, period, base, reason):
    """Single-metric computation enforces the same validity rules as the batch."""
    data = [obs("2023", 5.0), obs("2024", 6.5)]
    out = compute_single(data, kind=kind, indicator="HICP", region="RO", period=period, base_period=base)
    if reason is None:
        assert isinstance(out, MetricResult) and out.value == 1.5
    else:
        assert isinstance(out, MetricSkip) and reason in out.reason


def test_compute_single_pp_change_on_level_skipped():
    """A percentage-point change is only defined for rates."""
    data = [obs("2023", 5.0, unit="RON"), obs("2024", 6.5, unit="RON")]
    out = compute_single(data, kind="pp_change", indicator="HICP", region="RO", period="2024", base_period="2023")
    assert isinstance(out, MetricSkip) and "rate" in out.reason


def test_mixed_frequencies_kept_apart():
    """Annual and monthly observations of one series are analysed separately."""
    data = [obs("2023", 1.0, unit="RON"), obs("2024", 2.0, unit="RON")] + monthly([3.0, 4.0], start_year=2024,
                                                                                   unit="RON")
    res, _ = compute_metrics(data, indicator="HICP", region="RO")
    ids = _by_id(res)
    assert ids["latest:HICP:RO:2024"].value == 2.0 and ids["latest:HICP:RO:2024-02"].value == 4.0
    assert ids["mom:HICP:RO:2024-02"].value == (4.0 / 3.0 - 1) * 100
