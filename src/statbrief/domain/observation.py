"""The common observation schema shared by uploaded files and public APIs, with provenance."""
import hashlib
import math
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Frequency = Literal["A", "Q", "M"]
RegionLevel = Literal["country", "nuts1", "nuts2", "nuts3", "aggregate", "other"]

# Canonical period formats per frequency (protocol, not tunables).
_PERIOD_PATTERNS = {
    "A": re.compile(r"^\d{4}$"),
    "Q": re.compile(r"^\d{4}-Q[1-4]$"),
    "M": re.compile(r"^\d{4}-(0[1-9]|1[0-2])$"),
}


class UploadProvenance(BaseModel):
    """Where an observation came from inside an uploaded file."""

    model_config = ConfigDict(frozen=True)
    source_type: Literal["upload"] = "upload"
    file_id: str
    filename: str
    worksheet: str | None = None
    row_number: int | None = Field(default=None, ge=1)
    column: str


class ApiProvenance(BaseModel):
    """Where an observation came from in a public statistics API."""

    model_config = ConfigDict(frozen=True)
    source_type: Literal["api"] = "api"
    api_name: Literal["eurostat", "worldbank"]
    series_id: str
    query_url: str
    retrieved_at: datetime


Provenance = Annotated[UploadProvenance | ApiProvenance, Field(discriminator="source_type")]


class Observation(BaseModel):
    """One statistical value for an indicator, region and period, with its origin."""

    model_config = ConfigDict(frozen=True)
    obs_id: str
    indicator_code: str = Field(min_length=1)
    indicator_label: str | None = None
    region_code: str = Field(min_length=1)
    region_level: RegionLevel
    period: str
    frequency: Frequency
    value: float | None
    unit: str = Field(min_length=1)
    unit_multiplier: int = 0
    status_flag: str | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> "Observation":
        """Period must match its frequency's canonical format; values must be finite."""
        if not _PERIOD_PATTERNS[self.frequency].match(self.period):
            raise ValueError(f"period {self.period!r} does not match frequency {self.frequency}")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("value must be finite (use None for a missing observation)")
        return self


def make_obs_id(*parts: object) -> str:
    """Deterministic observation id from its identifying parts."""
    digest = hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"obs_{digest[:16]}"
