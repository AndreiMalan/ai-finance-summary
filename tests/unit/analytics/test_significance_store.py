"""Tests for significance flags (AC 25), the observation store and single-metric recomputation."""
import pytest

from statbrief.analytics.metrics import compute_metrics
from statbrief.analytics.significance import flag_significant
from statbrief.analytics.store import MetricUnavailable, ObservationStore, compute_metric
from statbrief.config import Settings
from statbrief.domain.metrics import MetricResult
from tests.unit.analytics.helpers import monthly, obs


def _settings(**kw):
    """Settings with test defaults, overriding analysis tunables."""
    return Settings(database_url="postgresql://x/y", session_secret="s" * 32, _env_file=None, **kw)


def _yoy_rate(region, now, before):
    """Annual rate observations whose YoY pp change is now - before."""
    return [obs("2023", before, reg=region), obs("2024", now, reg=region)]


def _all_metrics(observations, indicator, regions):
    """compute_metrics over several regions."""
    out = []
    for r in regions:
        out += compute_metrics(observations, indicator=indicator, region=r)[0]
    return out


def test_settings_defaults_and_env(monkeypatch):
    """The D-010 defaults hold and env vars override them."""
    s = _settings()
    assert (s.significance_rate_pp, s.significance_level_pct, s.top_movers) == (1.0, 2.0, 3)
    assert (s.max_revisions, s.max_parse_retries) == (3, 2)
    monkeypatch.setenv("SIGNIFICANCE_RATE_PP", "0.5")
    assert _settings().significance_rate_pp == 0.5


def test_rate_threshold_in_pp():
    """A rate's YoY pp change is flagged at >= SIGNIFICANCE_RATE_PP, not below."""
    data = _yoy_rate("RO", 6.0, 5.0)  # exactly 1.0 pp
    flags = flag_significant(_all_metrics(data, "HICP", ["RO"]), _settings())
    assert [(f.metric_id, f.rule) for f in flags] == [("pp_change:HICP:RO:2024:2023", "rate_threshold")]
    assert flag_significant(_all_metrics(data, "HICP", ["RO"]), _settings(significance_rate_pp=1.5)) == []


def test_level_threshold_in_percent():
    """A level's YoY % change is flagged at >= SIGNIFICANCE_LEVEL_PCT; the window abs change never is."""
    data = [obs("2023", 100.0, ind="GDP", unit="RON"), obs("2024", 101.9, ind="GDP", unit="RON")]
    assert flag_significant(_all_metrics(data, "GDP", ["RO"]), _settings()) == []
    data = [obs("2023", 100.0, ind="GDP", unit="RON"), obs("2024", 97.5, ind="GDP", unit="RON")]
    flags = flag_significant(_all_metrics(data, "GDP", ["RO"]), _settings())
    assert [(f.metric_id, f.rule) for f in flags] == [("yoy:GDP:RO:2024", "level_threshold")]
    assert flags[0].magnitude == pytest.approx(2.5)


def test_top_movers_across_regions():
    """The TOP_MOVERS largest |YoY| per indicator and period are flagged when >= 2 regions exist."""
    data = []
    for reg, delta in [("A", 0.1), ("B", -0.4), ("C", 0.3), ("D", 0.2)]:
        data += _yoy_rate(reg, 5.0 + delta, 5.0)
    flags = flag_significant(_all_metrics(data, "HICP", "ABCD"), _settings(top_movers=2))
    movers = [f.metric_id for f in flags if f.rule == "top_mover"]
    assert movers == ["pp_change:HICP:B:2024:2023", "pp_change:HICP:C:2024:2023"]
    assert flag_significant(_all_metrics(data, "HICP", "A"), _settings(top_movers=2)) == []  # one region
    assert not [f for f in flag_significant(_all_metrics(data, "HICP", "ABCD"), _settings(top_movers=0))
                if f.rule == "top_mover"]


def test_top_movers_ignore_monthly_and_other_indicators():
    """Movers compare like with like: same indicator, period and unit."""
    data = _yoy_rate("A", 5.5, 5.0) + [obs("2023", 1.0, ind="GDP", reg="B", unit="RON"),
                                       obs("2024", 1.01, ind="GDP", reg="B", unit="RON")]
    results = _all_metrics(data, "HICP", ["A"]) + _all_metrics(data, "GDP", ["B"])
    assert [f for f in flag_significant(results, _settings()) if f.rule == "top_mover"] == []


def test_store_get_and_series():
    """The store indexes by observation id and by (indicator, region)."""
    data = monthly([1.0, 2.0], unit="RON") + [obs("2024", 3.0, reg="BG", unit="RON")]
    store = ObservationStore(data)
    assert store.get(data[0].obs_id) == data[0] and data[0].obs_id in store
    assert [o.period for o in store.series("HICP", "RO")] == ["2023-01", "2023-02"]
    assert store.series("HICP", "XX") == []
    with pytest.raises(KeyError):
        store.get("missing")


def test_store_rejects_conflicting_duplicate_ids():
    """One id with two different observations is a data error, not something to pick from."""
    a = obs("2024", 1.0, oid="x")
    assert len(ObservationStore([a, a])) == 1
    with pytest.raises(ValueError):
        ObservationStore([a, obs("2024", 2.0, oid="x")])


def test_compute_metric_equals_batch():
    """Recomputing any metric from its id reproduces the batch result exactly."""
    data = monthly([float(i) for i in range(1, 16)], unit="RON") + monthly(
        [5.0 + i / 10 for i in range(15)], ind="RATE")
    store = ObservationStore(data)
    for ind in ("HICP", "RATE"):
        results, _ = compute_metrics(data, indicator=ind, region="RO")
        assert results
        for r in results:
            assert compute_metric(r.metric_id, store) == r


@pytest.mark.parametrize("mid", [
    "yoy:HICP:XX:2024-03",            # unknown series
    "yoy:HICP:RO:2030-03",            # unknown period
    "mom:HICP:RO:2023",               # not computable (not monthly)
    "garbage",                        # malformed
])
def test_compute_metric_unknown_raises_keyerror(mid):
    """Ids that cannot be computed raise KeyError (MetricUnavailable) with a reason."""
    store = ObservationStore(monthly([1.0] * 15, unit="RON") + [obs("2023", 1.0, unit="RON")])
    with pytest.raises(KeyError) as exc:
        compute_metric(mid, store)
    assert isinstance(exc.value, MetricUnavailable) and exc.value.reason


def test_compute_metric_returns_result_type():
    """A computable id yields a MetricResult."""
    store = ObservationStore([obs("2023", 2.0, unit="RON"), obs("2024", 3.0, unit="RON")])
    assert isinstance(compute_metric("yoy:HICP:RO:2024", store), MetricResult)
