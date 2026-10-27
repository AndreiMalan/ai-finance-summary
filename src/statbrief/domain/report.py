"""Contracts for the LLM-written report draft: numerical claims, citations, findings and sections (no I/O).

Shapes follow docs/architecture.md §2. Every number the draft states must be backed by a `NumericalClaim`
that the deterministic verifier (`statbrief.analytics.verify`) recomputes from the cited observations.
"""
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NumericalClaim(BaseModel):
    """One number stated in the report, with the metric or observations it comes from."""

    model_config = ConfigDict(frozen=True)
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)  # the sentence (or fragment) that states the number
    value: float
    unit: str = Field(min_length=1)
    metric_id: str | None = None  # e.g. "yoy:prc_hicp_manr:RO:2024-03"
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
