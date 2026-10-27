"""Contracts for computed metrics: ids, results, recorded skips and significant changes (no I/O).

Metric id grammar (protocol, not a tunable): ``kind:indicator:region:period`` for kinds whose base period
is implied by the kind (``latest``, ``yoy``, ``mom``, ``qoq``) and ``kind:indicator:region:period:base``
for change kinds whose base must be stated (``abs_change``, ``pct_change``, ``pp_change``). No part may
contain ``:``; periods use the canonical observation formats and base and period share one frequency.
"""
import math
from typing import Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from statbrief.domain.observation import PERIOD_PATTERNS, Frequency

MetricKind = Literal["latest", "yoy", "mom", "qoq", "abs_change", "pct_change", "pp_change"]
METRIC_KINDS: tuple[str, ...] = get_args(MetricKind)
# Kinds whose id carries an explicit base period (the comparison base cannot be derived from the kind).
BASED_KINDS: frozenset[str] = frozenset({"abs_change", "pct_change", "pp_change"})
_SEP = ":"


class ParsedMetricId(NamedTuple):
    """The parts of a metric id."""

    kind: str
    indicator: str
    region: str
    period: str
    base_period: str | None


def period_frequency(period: str) -> Frequency:
    """Frequency of a canonical period string ("2024" → A, "2024-Q1" → Q, "2024-03" → M).

    Raises ValueError for a string that is not a canonical period.
    """
    for freq, pattern in PERIOD_PATTERNS.items():
        if pattern.fullmatch(period):
            return freq  # type: ignore[return-value]
    raise ValueError(f"not a canonical period: {period!r}")


def _check_parts(kind: str, indicator: str, region: str, period: str, base_period: str | None) -> None:
    """Raise ValueError unless the parts form a valid metric id."""
    if kind not in METRIC_KINDS:
        raise ValueError(f"unknown metric kind {kind!r}")
    for name, part in (("indicator", indicator), ("region", region)):
        if not part or _SEP in part or part != part.strip():
            raise ValueError(f"invalid {name} {part!r} in metric id")
    freq = period_frequency(period)
    if kind in BASED_KINDS:
        if base_period is None:
            raise ValueError(f"{kind} needs a base period")
        if period_frequency(base_period) != freq:
            raise ValueError(f"base period {base_period!r} and period {period!r} differ in frequency")
    elif base_period is not None:
        raise ValueError(f"{kind} derives its base period; none may be given")


def metric_id(kind: str, indicator: str, region: str, period: str, base_period: str | None = None) -> str:
    """Build the canonical id of one metric; raises ValueError for invalid parts."""
    _check_parts(kind, indicator, region, period, base_period)
    parts = [kind, indicator, region, period] + ([base_period] if base_period is not None else [])
    return _SEP.join(parts)


def parse_metric_id(mid: str) -> ParsedMetricId:
    """Split a metric id into its parts; raises ValueError for a malformed id."""
    parts = mid.split(_SEP) if isinstance(mid, str) else []
    if len(parts) not in (4, 5):
        raise ValueError(f"malformed metric id {mid!r}")
    parsed = ParsedMetricId(*parts, None) if len(parts) == 4 else ParsedMetricId(*parts)
    _check_parts(*parsed)
    return parsed


class MetricResult(BaseModel):
    """One computed metric with its exact (unrounded) value, inputs and a human-readable formula."""

    model_config = ConfigDict(frozen=True)
    metric_id: str
    kind: MetricKind
    indicator: str
    region: str
    period: str
    base_period: str | None = None
    value: float
    unit: str = Field(min_length=1)
    unit_multiplier: int = 0  # power of ten of `value` in `unit` (0 for % and pp)
    observation_ids: list[str] = Field(min_length=1)
    formula: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "MetricResult":
        """The id must equal the id built from the fields, and the value must be finite."""
        if not math.isfinite(self.value):
            raise ValueError("metric value must be finite")
        if self.metric_id != metric_id(self.kind, self.indicator, self.region, self.period, self.base_period):
            raise ValueError(f"metric_id {self.metric_id!r} does not match the result's fields")
        return self


class MetricSkip(BaseModel):
    """A metric that was not computed, with the reason (invalid calculations are recorded, never guessed)."""

    model_config = ConfigDict(frozen=True)
    kind: MetricKind
    indicator: str
    region: str
    period: str | None = None
    base_period: str | None = None
    reason: str = Field(min_length=1)


class SignificantChange(BaseModel):
    """A metric flagged as noteworthy, with the absolute magnitude and the rule that flagged it."""

    model_config = ConfigDict(frozen=True)
    metric_id: str
    magnitude: float
    rule: Literal["rate_threshold", "level_threshold", "top_mover"]
