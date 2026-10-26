"""Contracts for combining sources: indicator links, value conflicts, unit mismatches, resolutions, gap fills."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConflictChoice = Literal["prefer", "keep_both", "exclude"]


class IndicatorLink(BaseModel):
    """User-supplied statement that an uploaded indicator code means a catalog/API indicator (never inferred)."""

    model_config = ConfigDict(frozen=True)
    upload_code: str = Field(min_length=1)
    catalog_code: str = Field(min_length=1)


class Conflict(BaseModel):
    """Values from different sources for one (indicator, region, period, unit) that disagree beyond tolerance.

    `candidates`, `values` and `sources` are aligned; `rel_diff` is the largest cross-source
    |a - b| / max(|a|, |b|).
    """

    model_config = ConfigDict(frozen=True)
    conflict_id: str
    indicator: str
    region: str
    period: str
    unit: str
    unit_multiplier: int = 0
    candidates: list[str]
    values: list[float]
    sources: list[str]
    rel_diff: float


class UnitMismatch(BaseModel):
    """Values for one (indicator, region, period) from different sources in different units (never compared).

    `units` lists each distinct unit as written, with a power-of-ten multiplier when non-zero (e.g. "EUR×10^6").
    """

    model_config = ConfigDict(frozen=True)
    indicator: str
    region: str
    period: str
    units: list[str]
    candidates: list[str]


class ConflictScan(BaseModel):
    """Result of conflict detection: value conflicts plus unit mismatches reported separately."""

    conflicts: list[Conflict] = Field(default_factory=list)
    unit_mismatches: list[UnitMismatch] = Field(default_factory=list)


class ConflictResolution(BaseModel):
    """The user's decision for one conflict: prefer one candidate, keep both (labelled), or exclude all."""

    model_config = ConfigDict(frozen=True)
    conflict_id: str = Field(min_length=1)
    choice: ConflictChoice
    preferred_obs_id: str | None = None

    @model_validator(mode="after")
    def _preferred_only_for_prefer(self) -> "ConflictResolution":
        """'prefer' needs preferred_obs_id; the other choices must not set it."""
        if (self.choice == "prefer") != (self.preferred_obs_id is not None):
            raise ValueError("preferred_obs_id is required for 'prefer' and only allowed there")
        return self


class ExcludedObservation(BaseModel):
    """An observation removed by a conflict resolution, with the reason (shown to the user)."""

    model_config = ConfigDict(frozen=True)
    obs_id: str
    reason: str


class UnresolvedConflicts(Exception):
    """Conflicts still lack a resolution; drafting is blocked until each is resolved (AC 21)."""

    def __init__(self, conflict_ids: list[str]):
        """Store the unresolved conflict ids."""
        super().__init__(f"unresolved conflicts: {', '.join(conflict_ids)}")
        self.conflict_ids = conflict_ids


class FilledPoint(BaseModel):
    """One gap in uploaded data filled from an API observation (which keeps its API provenance)."""

    model_config = ConfigDict(frozen=True)
    indicator: str
    region: str
    period: str
    obs_id: str
    api_name: str
    series_id: str
    replaced_obs_id: str | None = None  # the uploaded observation with a missing value, if there was one


class GapNotFilled(BaseModel):
    """A gap that API data could have filled but did not, and why (e.g. unit_mismatch)."""

    model_config = ConfigDict(frozen=True)
    indicator: str
    region: str
    period: str
    obs_id: str
    reason: Literal["unit_mismatch"]


class GapFillReport(BaseModel):
    """Every filled key (and every gap left open with its reason); empty without consent."""

    consent: bool
    filled: list[FilledPoint] = Field(default_factory=list)
    not_filled: list[GapNotFilled] = Field(default_factory=list)
