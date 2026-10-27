"""In-memory observation index and single-metric recomputation (used by the verifier and LLM tools)."""
from collections import defaultdict
from collections.abc import Iterable

from statbrief.analytics.metrics import compute_single
from statbrief.domain.metrics import MetricResult, parse_metric_id
from statbrief.domain.observation import Observation


class MetricUnavailable(KeyError):
    """A metric id that cannot be computed from the store; `reason` says why."""

    def __init__(self, metric_id: str, reason: str) -> None:
        """Keep the id and the human-readable reason."""
        super().__init__(f"{metric_id}: {reason}")
        self.metric_id, self.reason = metric_id, reason


class ObservationStore:
    """Read-only index over normalized observations by id and by (indicator, region)."""

    def __init__(self, observations: Iterable[Observation]) -> None:
        """Index the observations; raises ValueError when one id maps to two different observations."""
        self._by_id: dict[str, Observation] = {}
        self._series: dict[tuple[str, str], list[Observation]] = defaultdict(list)
        for o in observations:
            seen = self._by_id.get(o.obs_id)
            if seen is not None:
                if seen != o:
                    raise ValueError(f"observation id {o.obs_id} refers to two different observations")
                continue
            self._by_id[o.obs_id] = o
            self._series[(o.indicator_code, o.region_code)].append(o)
        for items in self._series.values():
            items.sort(key=lambda o: (o.frequency, o.period, o.obs_id))

    def __len__(self) -> int:
        """Number of distinct observations."""
        return len(self._by_id)

    def __contains__(self, obs_id: object) -> bool:
        """Whether an observation with this id exists."""
        return obs_id in self._by_id

    def get(self, obs_id: str) -> Observation:
        """The observation with this id; raises KeyError when unknown."""
        return self._by_id[obs_id]

    @property
    def regions(self) -> frozenset[str]:
        """Every region code present in the store."""
        return frozenset(region for _, region in self._series)

    def series(self, indicator: str, region: str) -> list[Observation]:
        """Observations of one (indicator, region), sorted by frequency and period (empty when none)."""
        return list(self._series.get((indicator, region), ()))


def compute_metric(metric_id: str, store: ObservationStore) -> MetricResult:
    """Recompute exactly one metric from its id.

    Raises MetricUnavailable (a KeyError) for a malformed id, an unknown series or period, or a metric the
    data does not support (the reason is the same one `compute_metrics` would record as a skip).
    """
    try:
        p = parse_metric_id(metric_id)
    except ValueError as exc:
        raise MetricUnavailable(metric_id, str(exc)) from None
    out = compute_single(store.series(p.indicator, p.region), kind=p.kind, indicator=p.indicator,
                         region=p.region, period=p.period, base_period=p.base_period)
    if not isinstance(out, MetricResult):
        raise MetricUnavailable(metric_id, out.reason)
    return out
