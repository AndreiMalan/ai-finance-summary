"""Compute report metrics for one (indicator, region) series with pandas, never beyond what the data supports.

Rules (AC 23–24, D-010):
- Each frequency of a series (A/Q/M) is analysed on its own; periods never mix across frequencies.
- The reference period is the last period with a non-missing value; `latest`, YoY and MoM/QoQ are computed
  there. The window change (`abs_change`/`pct_change`, or `pp_change` for rates) runs from the first to the
  last period present in the input (the caller passes the requested window); a gap at either end is a skip.
- Rates (unit ``%`` or ``pp``): every change is a percentage-point difference reported as kind ``pp_change``
  with its base period; percent changes and ``abs_change`` are not applied to rates.
- Levels: YoY/MoM/QoQ/pct_change are ``(v_t / v_base − 1) × 100`` in ``%``; abs_change is in the series unit.
- Never across different units or unit multipliers; never with a missing, duplicated, zero or negative base
  (zero/negative only matter for percent changes). Each refusal is a `MetricSkip` with its reason.
- Values are exact floats: no rounding here (rounding is a presentation concern).
"""
from collections.abc import Iterable

import pandas as pd

from statbrief.domain.metrics import BASED_KINDS, MetricResult, MetricSkip, metric_id, period_frequency
from statbrief.domain.observation import Observation

RATE_UNITS: frozenset[str] = frozenset({"%", "pp"})  # canonical unit codes (statbrief.ingest.units)
PERIODS_PER_YEAR = {"A": 1, "Q": 4, "M": 12}  # calendar facts, not tunables
_STEP_KIND = {"M": "mom", "Q": "qoq"}  # period-on-period kind per frequency
_COLUMNS = ["period", "value", "unit", "unit_multiplier", "obs_id"]


def is_rate(unit: str) -> bool:
    """True for rate units (percent and percentage points), whose changes are percentage-point differences."""
    return unit in RATE_UNITS


def shift_period(period: str, n: int) -> str:
    """The period `n` steps of its own frequency later (negative `n` = earlier).

    Raises ValueError for a non-canonical period or a result outside years 0001–9999.
    """
    freq = period_frequency(period)
    year = int(period[:4])
    if freq == "A":
        index, per_year = year + n, 1
        new_year, sub = index, 0
    else:
        per_year = PERIODS_PER_YEAR[freq]
        sub0 = int(period[6:]) - 1 if freq == "Q" else int(period[5:]) - 1
        new_year, sub = divmod(year * per_year + sub0 + n, per_year)
    if not 1 <= new_year <= 9999:
        raise ValueError(f"shifting {period!r} by {n} leaves the supported year range")
    if freq == "A":
        return f"{new_year:04d}"
    return f"{new_year:04d}-Q{sub + 1}" if freq == "Q" else f"{new_year:04d}-{sub + 1:02d}"


def _frame(observations: Iterable[Observation], indicator: str, region: str) -> pd.DataFrame:
    """The (indicator, region) observations as a DataFrame with a frequency column, sorted deterministically."""
    rows = [{"period": o.period, "frequency": o.frequency, "value": o.value, "unit": o.unit,
             "unit_multiplier": o.unit_multiplier, "obs_id": o.obs_id}
            for o in observations if o.indicator_code == indicator and o.region_code == region]
    df = pd.DataFrame(rows, columns=["frequency", *_COLUMNS])
    return df.sort_values(["frequency", "period", "obs_id"], kind="mergesort").reset_index(drop=True)


def _reference_period(series: pd.DataFrame) -> str | None:
    """The last period with a non-missing value in a one-frequency series, or None."""
    valued = series[series["value"].notna()]
    return None if valued.empty else str(valued["period"].iloc[-1])


def _lookup(series: pd.DataFrame, period: str, role: str) -> pd.Series | str:
    """The single row of `period` with a value, or the reason it cannot be used."""
    rows = series[series["period"] == period]
    if rows.empty:
        return f"no observation for {role} period {period}"
    if len(rows) > 1:
        return f"ambiguous: {len(rows)} observations for {period}"
    row = rows.iloc[0]
    if pd.isna(row["value"]):
        return f"missing value for {role} period {period}"
    return row


class _Ctx:
    """The identity of the metric being computed, to build results and skips."""

    def __init__(self, kind: str, indicator: str, region: str, period: str, base: str | None) -> None:
        """Store the metric's identifying parts."""
        self.kind, self.indicator, self.region, self.period, self.base = kind, indicator, region, period, base

    def skip(self, reason: str) -> MetricSkip:
        """A recorded refusal for this metric."""
        return MetricSkip(kind=self.kind, indicator=self.indicator, region=self.region, period=self.period,
                          base_period=self.base, reason=reason)

    def result(self, value: float, unit: str, mult: int, ids: list[str], formula: str,
               base_in_id: bool) -> MetricResult:
        """A computed metric for this context."""
        base = self.base if base_in_id else None
        return MetricResult(
            metric_id=metric_id(self.kind, self.indicator, self.region, self.period, base),
            kind=self.kind, indicator=self.indicator, region=self.region, period=self.period, base_period=base,
            value=value, unit=unit, unit_multiplier=mult, observation_ids=ids, formula=formula)


def _base_for(kind: str, period: str, base_period: str | None) -> str | None:
    """The comparison base implied by the kind (YoY: one year earlier; MoM/QoQ: one step), else the given one."""
    freq = period_frequency(period)
    if kind == "yoy":
        return shift_period(period, -PERIODS_PER_YEAR[freq])
    if kind in ("mom", "qoq"):
        return shift_period(period, -1)
    return base_period


def compute_single(observations: Iterable[Observation], *, kind: str, indicator: str, region: str, period: str,
                   base_period: str | None = None) -> MetricResult | MetricSkip:
    """Compute exactly one metric (identified by its id parts) or return why it cannot be computed.

    Raises ValueError for malformed id parts (see `statbrief.domain.metrics.metric_id`).
    """
    metric_id(kind, indicator, region, period, base_period)  # validates the parts
    freq = period_frequency(period)
    df = _frame(observations, indicator, region)
    series = df[df["frequency"] == freq]
    base = _base_for(kind, period, base_period)
    ctx = _Ctx(kind, indicator, region, period, base)
    if kind == "mom" and freq != "M":
        return ctx.skip("MoM needs monthly data")
    if kind == "qoq" and freq != "Q":
        return ctx.skip("QoQ needs quarterly data")
    if series.empty:
        return ctx.skip("no observations for this indicator, region and frequency")
    if kind == "latest":
        return _latest(series, ctx)
    if base >= period:  # canonical periods of one frequency sort chronologically as strings
        return ctx.skip(f"base period {base} must be earlier than {period}")
    current, prior = _lookup(series, period, "current"), _lookup(series, base, "base")
    for found in (current, prior):
        if isinstance(found, str):
            return ctx.skip(found)
    return _change(current, prior, ctx)


def _latest(series: pd.DataFrame, ctx: _Ctx) -> MetricResult | MetricSkip:
    """The value at the reference period, which must be the series' last non-missing period."""
    ref = _reference_period(series)
    if ref is None:
        return ctx.skip("all values are missing")
    if ctx.period != ref:
        return ctx.skip(f"{ctx.period} is not the latest period with a value ({ref})")
    row = _lookup(series, ref, "latest")
    if isinstance(row, str):
        return ctx.skip(row)
    v = float(row["value"])
    return ctx.result(v, row["unit"], int(row["unit_multiplier"]), [row["obs_id"]],
                      f"latest value [{ref}] = {v!r}", base_in_id=False)


def _change(current: pd.Series, prior: pd.Series, ctx: _Ctx) -> MetricResult | MetricSkip:
    """A change between two validated observations, applying the unit, rate and base rules."""
    if (current["unit"], current["unit_multiplier"]) != (prior["unit"], prior["unit_multiplier"]):
        return ctx.skip(f"different units: {prior['unit']} (×10^{prior['unit_multiplier']}) at {ctx.base} vs "
                        f"{current['unit']} (×10^{current['unit_multiplier']}) at {ctx.period}")
    unit, mult = current["unit"], int(current["unit_multiplier"])
    vt, vb = float(current["value"]), float(prior["value"])
    ids = [current["obs_id"], prior["obs_id"]]
    based = ctx.kind in BASED_KINDS
    rate = is_rate(unit)
    if ctx.kind == "pp_change" or ctx.kind == "abs_change":
        if ctx.kind == "pp_change" and not rate:
            return ctx.skip(f"pp_change only applies to rates (unit {unit}); use abs_change or pct_change")
        if ctx.kind == "abs_change" and rate:
            return ctx.skip(f"rate series ({unit}): the absolute change is reported as pp_change")
        diff = vt - vb
        out_unit = "pp" if rate else unit
        return ctx.result(diff, out_unit, 0 if rate else mult, ids,
                          f"{vt!r} [{ctx.period}] − {vb!r} [{ctx.base}] = {diff!r}", based)
    if rate:
        return ctx.skip(f"rate series ({unit}): percent change not applied to rates; use pp_change")
    if vb == 0:
        return ctx.skip(f"zero base at {ctx.base}: percent change undefined (division by zero)")
    if vb < 0:
        return ctx.skip(f"negative base at {ctx.base}: percent change not meaningful")
    pct = (vt / vb - 1) * 100
    return ctx.result(pct, "%", 0, ids, f"({vt!r} [{ctx.period}] / {vb!r} [{ctx.base}] − 1) × 100 = {pct!r}", based)


def _series_metrics(series: pd.DataFrame, indicator: str, region: str, freq: str,
                    observations: list[Observation]) -> list[MetricResult | MetricSkip]:
    """All standard metrics of one frequency of one series, in a fixed order."""
    one = dict(indicator=indicator, region=region)
    ref = _reference_period(series)
    if ref is None:
        return [MetricSkip(kind="latest", period=str(series["period"].iloc[-1]), reason="all values are missing",
                           **one)]
    out: list[MetricResult | MetricSkip] = [compute_single(observations, kind="latest", period=ref, **one)]
    ref_rows = series[series["period"] == ref]
    rate = is_rate(str(ref_rows["unit"].iloc[0]))
    year_ago = shift_period(ref, -PERIODS_PER_YEAR[freq])
    out.append(compute_single(observations, kind="pp_change", period=ref, base_period=year_ago, **one) if rate
               else compute_single(observations, kind="yoy", period=ref, **one))
    for step_freq, kind in _STEP_KIND.items():
        if freq != step_freq:
            out.append(compute_single(observations, kind=kind, period=ref, **one))  # recorded skip
        elif rate:
            out.append(compute_single(observations, kind="pp_change", period=ref,
                                      base_period=shift_period(ref, -1), **one))
        else:
            out.append(compute_single(observations, kind=kind, period=ref, **one))
    first, last = str(series["period"].iloc[0]), str(series["period"].iloc[-1])
    if first == last:
        out.append(MetricSkip(kind="abs_change", period=last, reason="only one period: no change over the window",
                              **one))
        return out
    for kind in ("pp_change", "abs_change", "pct_change") if rate else ("abs_change", "pct_change"):
        out.append(compute_single(observations, kind=kind, period=last, base_period=first, **one))
    return out


def compute_metrics(observations: Iterable[Observation], *, indicator: str,
                    region: str) -> tuple[list[MetricResult], list[MetricSkip]]:
    """Every metric the (indicator, region) series supports, plus a recorded skip for each one it does not.

    Output is deterministic (independent of input order) and free of duplicate metric ids.
    """
    observations = list(observations)
    df = _frame(observations, indicator, region)
    if df.empty:
        return [], [MetricSkip(kind="latest", indicator=indicator, region=region,
                               reason="no observations for this indicator and region")]
    results: dict[str, MetricResult] = {}
    skips: dict[tuple, MetricSkip] = {}
    for freq in sorted(df["frequency"].unique()):
        series = df[df["frequency"] == freq]
        for item in _series_metrics(series, indicator, region, freq, observations):
            if isinstance(item, MetricResult):
                results.setdefault(item.metric_id, item)
            else:
                skips.setdefault((item.kind, item.period, item.base_period, item.reason), item)
    return list(results.values()), list(skips.values())
