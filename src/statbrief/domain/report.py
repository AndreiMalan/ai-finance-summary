"""Contracts for a report: its parameters, the LLM-written draft (numerical claims, citations, findings,
sections) and the export status (no I/O).

Shapes follow docs/architecture.md §2. Every number the draft states must be backed by a `NumericalClaim`
that the deterministic verifier (`statbrief.analytics.verify`) recomputes from the cited observations.
"""
import math
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from statbrief.domain.conflict import IndicatorLink
from statbrief.domain.metrics import period_frequency


class NumericalClaim(BaseModel):
    """One number stated in the report, with the metric or observations it comes from."""

    model_config = ConfigDict(frozen=True)
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)  # the sentence (or fragment) that states the number
    value: float
    unit: str = Field(min_length=1)
    metric_id: str | None = None  # e.g. "pp_change:HICP_YOY:RO:2024-03:2023-03" (rates) or "yoy:GDP_VOL:RO:2024-Q1"
    observation_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _finite(self) -> "NumericalClaim":
        """The stated value must be finite."""
        if not math.isfinite(self.value):
            raise ValueError("claim value must be finite")
        return self


class Citation(BaseModel):
    """A source the report cites: an uploaded file, an API series or a reference document."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["upload", "api", "reference"]
    ref_id: str = Field(min_length=1)
    label: str
    locator: str | None = None


class Finding(BaseModel):
    """One executive-summary headline and the claims that back its numbers."""

    model_config = ConfigDict(frozen=True)
    headline: str
    claim_ids: list[str] = Field(default_factory=list)


class Section(BaseModel):
    """One report section per indicator."""

    model_config = ConfigDict(frozen=True)
    indicator_code: str
    title: str
    paragraphs: list[str]
    claim_ids: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)


class ReportDraft(BaseModel):
    """The structured report the LLM returns; verified before anyone sees it."""

    model_config = ConfigDict(frozen=True)
    title: str
    executive_summary: list[Finding] = Field(min_length=1, max_length=5)
    sections: list[Section]
    claims: list[NumericalClaim]
    citations: list[Citation]
    caveats: list[str]
    unknown_columns_acknowledged: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_claim_ids(self) -> "ReportDraft":
        """Claim ids must be unique so every reference resolves to exactly one claim."""
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate claim_id in claims")
        return self

    def claim(self, claim_id: str) -> NumericalClaim | None:
        """The claim with this id, or None."""
        return next((c for c in self.claims if c.claim_id == claim_id), None)


SourceMode = Literal["api", "upload", "combined"]
ReportStyle = Literal["concise", "detailed"]
ExportFormat = Literal["pdf", "xlsx"]


class ReportParams(BaseModel):
    """What one report covers: source mode, indicators, regions, period window, style and export formats.

    `indicators` are catalog codes (fetched from the public APIs in "api"/"combined" mode) and/or indicator
    codes of uploaded data; empty means every indicator found in the data. `regions` empty means every
    region in the data (API modes need at least one). Periods are canonical ("2024", "2024-Q1", "2024-03"),
    inclusive, share one frequency and are in order. `links` are the user's own statements that an
    uploaded indicator code means a catalog indicator (never inferred); `consent_gap_fill` allows filling
    gaps of linked uploaded series from API data (AC 22).
    """

    model_config = ConfigDict(frozen=True)
    mode: SourceMode
    indicators: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    period_from: str | None = None
    period_to: str | None = None
    style: ReportStyle = "concise"
    formats: list[ExportFormat] = Field(default_factory=lambda: ["pdf"])
    consent_gap_fill: bool = False
    links: list[IndicatorLink] = Field(default_factory=list)

    @field_validator("indicators", "regions")
    @classmethod
    def _clean_codes(cls, v: list[str]) -> list[str]:
        """Strip codes, reject blank ones and drop repeats (first occurrence kept)."""
        out: list[str] = []
        for code in v:
            code = code.strip()
            if not code:
                raise ValueError("codes must not be blank")
            if code not in out:
                out.append(code)
        return out

    @model_validator(mode="after")
    def _consistent(self) -> "ReportParams":
        """Canonical, ordered, same-frequency period bounds; API modes name at least one region."""
        bounds = [p for p in (self.period_from, self.period_to) if p is not None]
        freqs = {period_frequency(p) for p in bounds}  # ValueError for a non-canonical period
        if len(freqs) > 1:
            raise ValueError("period_from and period_to must have the same frequency")
        if len(bounds) == 2 and self.period_from > self.period_to:  # type: ignore[operator]
            raise ValueError("period_from must not be after period_to")
        if self.mode != "upload" and not self.regions:
            raise ValueError("API data needs at least one region")
        return self


class ExportStatus(BaseModel):
    """State of the report export: requested formats, and the stored files once built (M9)."""

    model_config = ConfigDict(frozen=True)
    state: Literal["pending", "done", "failed"]
    formats: list[ExportFormat]
    requested_at: AwareDatetime
    message: str | None = None
