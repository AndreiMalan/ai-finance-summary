"""A user's report defaults kept in LangGraph's long-term store (AC 43): never a statistical source."""
from pydantic import BaseModel, ConfigDict, Field

from statbrief.domain.report import ExportFormat, ReportParams, ReportStyle


class UserPreferences(BaseModel):
    """Regions, indicators, style and export formats of the user's last approved report (form pre-fill only)."""

    model_config = ConfigDict(frozen=True)
    regions: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    style: ReportStyle = "concise"
    formats: list[ExportFormat] = Field(default_factory=lambda: ["pdf"])

    @classmethod
    def from_params(cls, params: ReportParams) -> "UserPreferences":
        """The preferences an approved report's parameters imply."""
        return cls(regions=list(params.regions), indicators=list(params.indicators), style=params.style,
                   formats=list(params.formats))
