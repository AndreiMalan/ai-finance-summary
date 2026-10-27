"""Contracts for verification outcomes and the human review decision (no I/O)."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClaimCheck(BaseModel):
    """Verdict on one claim: `expected` is the recomputed value when one could be computed."""

    model_config = ConfigDict(frozen=True)
    claim_id: str
    ok: bool
    expected: float | None = None
    reason: str | None = None


class VerificationResult(BaseModel):
    """Outcome of verifying a whole draft; `feedback` feeds the revise prompt."""

    model_config = ConfigDict(frozen=True)
    ok: bool
    checks: list[ClaimCheck] = Field(default_factory=list)
    orphan_numbers: list[str] = Field(default_factory=list)
    feedback: str = ""


class ReviewDecision(BaseModel):
    """The human reviewer's decision on a verified draft."""

    model_config = ConfigDict(frozen=True)
    action: Literal["approve", "reject", "request_changes"]
    comment: str | None = None
