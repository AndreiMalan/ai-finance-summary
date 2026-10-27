"""Flag significant changes among computed metrics (AC 25, D-010).

Rules:
- Period-on-period changes only: level ``yoy``/``qoq``/``mom`` (in %) and rate ``pp_change`` whose base is one
  year or one period before its period (the rate equivalents of YoY/QoQ/MoM). Window changes
  (``abs_change``/``pct_change`` and window ``pp_change`` over other spans) and ``latest`` are never flagged.
- Thresholds (inclusive): |pp| ≥ ``significance_rate_pp`` for rates, |%| ≥ ``significance_level_pct`` for levels.
- Top movers: among YoY-equivalent metrics of one indicator, period and unit, when at least two regions are
  present, the ``top_movers`` largest |value| are flagged (ties broken by metric id). A metric can be flagged
  by both a threshold and as a top mover; each flag is listed once per rule.
"""
from collections import defaultdict

from statbrief.analytics.metrics import PERIODS_PER_YEAR, shift_period
from statbrief.config import Settings
from statbrief.domain.metrics import MetricResult, SignificantChange, period_frequency

_LEVEL_STEP_KINDS = frozenset({"yoy", "qoq", "mom"})


def _is_year_ago(r: MetricResult) -> bool:
    """True when a pp_change compares its period with the same period one year earlier."""
    return r.base_period == shift_period(r.period, -PERIODS_PER_YEAR[period_frequency(r.period)])


def _is_step(r: MetricResult) -> bool:
    """True for a period-on-period change: level YoY/QoQ/MoM or a rate pp_change over one year or one period."""
    if r.kind in _LEVEL_STEP_KINDS:
        return True
    return r.kind == "pp_change" and (_is_year_ago(r) or r.base_period == shift_period(r.period, -1))


def _is_yoy(r: MetricResult) -> bool:
    """True for a year-on-year change (level ``yoy`` or a year-ago rate ``pp_change``)."""
    return r.kind == "yoy" or (r.kind == "pp_change" and _is_year_ago(r))


def flag_significant(results: list[MetricResult], settings: Settings) -> list[SignificantChange]:
    """Threshold flags (in input order) followed by top movers (per group, largest first)."""
    flags: list[SignificantChange] = []
    for r in results:
        if not _is_step(r):
            continue
        rate = r.kind == "pp_change"
        limit = settings.significance_rate_pp if rate else settings.significance_level_pct
        if abs(r.value) >= limit:
            flags.append(SignificantChange(metric_id=r.metric_id, magnitude=abs(r.value),
                                           rule="rate_threshold" if rate else "level_threshold"))
    if settings.top_movers <= 0:
        return flags
    groups: dict[tuple, list[MetricResult]] = defaultdict(list)
    for r in results:
        if _is_yoy(r):
            groups[(r.indicator, r.period, r.unit)].append(r)
    for key in sorted(groups):
        members = groups[key]
        if len({m.region for m in members}) < 2:
            continue
        ranked = sorted(members, key=lambda m: (-abs(m.value), m.metric_id))
        flags += [SignificantChange(metric_id=m.metric_id, magnitude=abs(m.value), rule="top_mover")
                  for m in ranked[:settings.top_movers]]
    return flags
