"""Result types shared by the API clients: observations plus every skipped value with its reason."""
from dataclasses import dataclass, field
from typing import Literal

from statbrief.domain.observation import Observation

SkipReason = Literal["missing_value", "unknown_geo", "invalid_period", "duplicate_period", "unknown_unit"]


@dataclass(frozen=True)
class SkippedValue:
    """A response cell (or requested region) that did not become an observation, and why.

    `region` and `period` are the raw codes from the request or response (None when not applicable);
    `status_flag` is the source's flag for the cell, e.g. Eurostat "c" (confidential) on a missing value.
    """

    region: str | None
    period: str | None
    reason: SkipReason
    status_flag: str | None = None


@dataclass(frozen=True)
class SeriesMeta:
    """Descriptive metadata of a fetched series, as reported by the source."""

    label: str | None
    unit_label: str | None
    last_update: str | None


@dataclass
class FetchResult:
    """Observations of one series plus every value that was skipped (nothing is dropped silently)."""

    observations: list[Observation] = field(default_factory=list)
    skipped: list[SkippedValue] = field(default_factory=list)
    meta: SeriesMeta = field(default_factory=lambda: SeriesMeta(None, None, None))
