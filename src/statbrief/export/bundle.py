"""`ReportBundle`: everything an export needs, assembled from the approved draft and the stored data (no I/O).

Exports are built from the approved `ReportDraft` plus the observations, metrics and data-quality records
behind it, never from LLM free text alone (AC 47-49). The bundle validates that every claim resolves to
observations it carries (and to a metric when the claim names one) and that every citation of the draft has
a resolved source label, so an export can never show a number whose provenance it cannot print.

The function that assembles a bundle from graph state and repositories is wired after M7 merges (see
`.superpowers/sdd/m9-early-report.md`); `ReportBundle.example()` builds a realistic bundle for tests and
previews. `ExportParams` mirrors the export-relevant fields of M7's `ReportParams` (same names), so the
wiring is `ExportParams.model_validate(params.model_dump(include=EXPORT_PARAM_FIELDS))`.
"""
from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from statbrief.domain.conflict import (Conflict, ConflictResolution, ExcludedObservation, FilledPoint,
                                       GapFillReport, UnitMismatch)
from statbrief.domain.metrics import MetricResult, metric_id
from statbrief.domain.observation import ApiProvenance, Observation, UploadProvenance, make_obs_id
from statbrief.domain.report import Citation, Finding, NumericalClaim, ReportDraft, Section

ExportFormat = Literal["pdf", "xlsx"]
CitationKind = Literal["upload", "api", "reference"]
# Citation groups in export order with their headings (AC 47 / AC 59: local files vs public APIs vs documents).
CITATION_GROUPS: tuple[tuple[CitationKind, str], ...] = (
    ("upload", "Uploaded files"), ("api", "Public APIs"), ("reference", "Reference documents"),
)
EXPORT_PARAM_FIELDS = frozenset({"mode", "indicators", "regions", "period_from", "period_to", "style", "formats"})


class ExportParams(BaseModel):
    """The report parameters an export prints (subset of M7's ReportParams, same field names)."""

    model_config = ConfigDict(frozen=True)
    mode: Literal["api", "upload", "combined"]
    indicators: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    period_from: str | None = None
    period_to: str | None = None
    style: Literal["concise", "detailed"] = "concise"
    formats: list[ExportFormat] = Field(default_factory=lambda: ["pdf"])


class ResolvedCitation(BaseModel):
    """A draft citation with the human-readable source it points to.

    `source_label` names the source ("Upload: irs.xlsx", "Eurostat: prc_hicp_minr?...", or a document title
    with its issuer); `locator` narrows it (sheet, section, page); `url` is the API query URL when there is one.
    """

    model_config = ConfigDict(frozen=True)
    kind: CitationKind
    ref_id: str = Field(min_length=1)
    label: str
    source_label: str = Field(min_length=1)
    locator: str | None = None
    url: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Identity shared with the draft's `Citation` (kind, ref_id)."""
        return self.kind, self.ref_id


class ValidationExclusion(BaseModel):
    """Rows of an uploaded file excluded at validation (an issue resolved as "excluded")."""

    model_config = ConfigDict(frozen=True)
    file_id: str
    filename: str
    worksheet: str | None = None
    issue_code: str
    column: str | None = None
    rows: list[int] = Field(default_factory=list)
    message: str


class ConflictNote(BaseModel):
    """One value conflict and the user's resolution of it."""

    model_config = ConfigDict(frozen=True)
    conflict: Conflict
    resolution: ConflictResolution

    @model_validator(mode="after")
    def _same_conflict(self) -> "ConflictNote":
        """The resolution must belong to this conflict."""
        if self.resolution.conflict_id != self.conflict.conflict_id:
            raise ValueError("resolution does not belong to the conflict")
        return self


class ReportBundle(BaseModel):
    """Everything one export of an approved report needs (see module doc).

    `observations` are the observations the report used (kept after conflict resolution, gap fills
    included); `conflict_exclusions` are the candidates removed by resolutions; `labels` are the
    "keep both" source labels from conflict resolution.
    """

    model_config = ConfigDict(frozen=True)
    report_id: str = Field(min_length=1)
    params: ExportParams
    draft: ReportDraft
    metrics: list[MetricResult] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    citations: list[ResolvedCitation] = Field(default_factory=list)
    validation_exclusions: list[ValidationExclusion] = Field(default_factory=list)
    gap_fill: GapFillReport | None = None
    conflicts: list[ConflictNote] = Field(default_factory=list)
    conflict_exclusions: list[ExcludedObservation] = Field(default_factory=list)
    unit_mismatches: list[UnitMismatch] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    approved_by: str = Field(min_length=1)  # the approver's email
    approved_at: AwareDatetime
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _traceable(self) -> "ReportBundle":
        """Every claim resolves to carried observations (and its metric); every draft citation is resolved."""
        obs_ids = {o.obs_id for o in self.observations}
        if len(obs_ids) != len(self.observations):
            raise ValueError("duplicate obs_id in observations")
        metric_ids = {m.metric_id for m in self.metrics}
        for claim in self.draft.claims:
            missing = [o for o in claim.observation_ids if o not in obs_ids]
            if missing:
                raise ValueError(f"claim {claim.claim_id!r} cites observations not in the bundle: {missing}")
            if claim.metric_id is not None and claim.metric_id not in metric_ids:
                raise ValueError(f"claim {claim.claim_id!r} names metric {claim.metric_id!r} not in the bundle")
        for m in self.metrics:
            if any(o not in obs_ids for o in m.observation_ids):
                raise ValueError(f"metric {m.metric_id!r} uses observations not in the bundle")
        resolved = {c.key for c in self.citations}
        unresolved = [c.ref_id for c in self.draft.citations if (c.kind, c.ref_id) not in resolved]
        if unresolved:
            raise ValueError(f"draft citations without a resolved source: {unresolved}")
        return self

    def observation(self, obs_id: str) -> Observation | None:
        """The carried observation with this id, or None."""
        return next((o for o in self.observations if o.obs_id == obs_id), None)

    def metric(self, mid: str) -> MetricResult | None:
        """The carried metric with this id, or None."""
        return next((m for m in self.metrics if m.metric_id == mid), None)

    def citations_by_group(self) -> list[tuple[str, list[ResolvedCitation]]]:
        """(heading, citations) for every group in CITATION_GROUPS order; empty groups are kept."""
        return [(heading, [c for c in self.citations if c.kind == kind]) for kind, heading in CITATION_GROUPS]

    @classmethod
    def example(cls) -> "ReportBundle":
        """A small, realistic bundle (upload + Eurostat + reference document, a resolved conflict, a gap fill,
        a validation exclusion and a base-year unit mismatch) for tests and previews."""
        return _example_bundle()


def _example_bundle() -> ReportBundle:
    """Build the bundle returned by `ReportBundle.example()` (fictional IRS file, recorded-style Eurostat data)."""
    t = datetime(2025, 1, 20, 9, 30, tzinfo=timezone.utc)
    file_id = "0b9f3d4e-1111-4c2a-9a55-5c1e8f1d2a01"
    url = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"
           "?format=JSON&lang=EN&coicop18=TOTAL&unit=RCH_A&geo=RO")
    series = "prc_hicp_minr?coicop18=TOTAL&unit=RCH_A"
    api_prov = ApiProvenance(api_name="eurostat", series_id=series, query_url=url, retrieved_at=t)

    def up(period: str, region: str, value: float | None, row: int, indicator: str = "HICP_YOY",
           unit: str = "%") -> Observation:
        """One uploaded observation of the example IRS workbook."""
        return Observation(obs_id=make_obs_id("upload", file_id, "Preturi", row), indicator_code=indicator,
                           indicator_label="Consumer prices, annual rate of change", region_code=region,
                           region_level="country" if region == "RO" else "nuts3", period=period, frequency="M",
                           value=value, unit=unit,
                           provenance=UploadProvenance(file_id=file_id, filename="IRS_regional_monthly_2023-2024.xlsx",
                                                       worksheet="Preturi", row_number=row, column="valoare"))

    def api(period: str, value: float) -> Observation:
        """One Eurostat HICP annual-rate observation for Romania."""
        return Observation(obs_id=make_obs_id("eurostat", "prc_hicp_minr", "coicop18=TOTAL&unit=RCH_A", "RO", period),
                           indicator_code="HICP_YOY", indicator_label="HICP inflation, annual rate of change",
                           region_code="RO", region_level="country", period=period, frequency="M", value=value,
                           unit="%", provenance=api_prov)

    cluj = [up("2024-01", "RO113", 7.1, 10), up("2024-02", "RO113", 6.8, 11), up("2024-03", "RO113", 6.4, 12),
            up("2023-03", "RO113", 14.2, 4)]
    ro_api = [api("2023-03", 12.1), api("2024-01", 7.3), api("2024-02", 7.1), api("2024-03", 6.7)]
    gap_source = api("2024-04", 6.5)
    gap = gap_source.model_copy(update={"obs_id": make_obs_id("gapfill", gap_source.obs_id, "IPC_VAR_AN")})
    upload_ro = up("2024-03", "RO", 6.6, 20)  # within one rounding unit of Eurostat's 6.7: kept, no conflict
    conflicting_upload = up("2024-02", "RO", 7.7, 19)  # conflicts with Eurostat's 7.1; excluded by the resolution
    observations = [*cluj, *ro_api, gap, upload_ro]
    pp = MetricResult(metric_id=metric_id("pp_change", "HICP_YOY", "RO113", "2024-03", "2023-03"), kind="pp_change",
                      indicator="HICP_YOY", region="RO113", period="2024-03", base_period="2023-03", value=6.4 - 14.2,
                      unit="pp", observation_ids=[cluj[2].obs_id, cluj[3].obs_id],
                      formula="HICP_YOY[RO113, 2024-03] - HICP_YOY[RO113, 2023-03] = 6.4 - 14.2")
    latest = MetricResult(metric_id=metric_id("latest", "HICP_YOY", "RO", "2024-03"), kind="latest",
                          indicator="HICP_YOY", region="RO", period="2024-03", value=6.7, unit="%",
                          observation_ids=[ro_api[3].obs_id], formula="HICP_YOY[RO, 2024-03] = 6.7")
    claims = [
        NumericalClaim(claim_id="c1", text="Annual inflation in Cluj fell to 6.4% in March 2024.", value=6.4,
                       unit="%", metric_id=None, observation_ids=[cluj[2].obs_id]),
        NumericalClaim(claim_id="c2", text="That is 7.8 percentage points below March 2023.", value=-7.8,
                       unit="pp", metric_id=pp.metric_id, observation_ids=list(pp.observation_ids)),
        NumericalClaim(claim_id="c3", text="Eurostat's HICP rate for Romania was 6.7% in March 2024.", value=6.7,
                       unit="%", metric_id=latest.metric_id, observation_ids=[ro_api[3].obs_id]),
    ]
    citations = [
        Citation(kind="upload", ref_id=file_id, label="IRS regional monthly workbook", locator="sheet Preturi"),
        Citation(kind="api", ref_id=series, label="Eurostat HICP, annual rate of change"),
        Citation(kind="reference", ref_id="IRS_Technical_Note_Differences_vs_European_Series",
                 label="IRS Technical Note TN-2025-03", locator="§3.1"),
    ]
    draft = ReportDraft(
        title="Consumer prices in Cluj and Romania, March 2024",
        executive_summary=[Finding(headline="Inflation in Cluj fell to 6.4% in March 2024.", claim_ids=["c1"]),
                           Finding(headline="The decline from March 2023 was 7.8 percentage points.",
                                   claim_ids=["c2"])],
        sections=[Section(indicator_code="HICP_YOY", title="Consumer price inflation",
                          paragraphs=["Annual inflation in Cluj fell to 6.4% in March 2024. That is 7.8 percentage "
                                      "points below March 2023.",
                                      "Eurostat's HICP rate for Romania was 6.7% in March 2024; the IRS and HICP "
                                      "rates differ in weights and coverage (IRS Technical Note, §3.1)."],
                          claim_ids=["c1", "c2", "c3"], citation_ids=[c.ref_id for c in citations])],
        claims=claims, citations=citations,
        caveats=["County CPI rates are not harmonised with the HICP (IRS Technical Note §3.1)."])
    conflict = Conflict(conflict_id="cf_example000000001", indicator="HICP_YOY", region="RO", period="2024-02",
                        unit="%", candidates=[conflicting_upload.obs_id, ro_api[2].obs_id], values=[7.7, 7.1],
                        sources=[f"upload:{file_id}", f"api:eurostat:{series}"], rel_diff=(7.7 - 7.1) / 7.7)
    resolution = ConflictResolution(conflict_id=conflict.conflict_id, choice="prefer", preferred_obs_id=ro_api[2].obs_id)
    return ReportBundle(
        report_id="7c4e2b0a-2222-4d3b-8b66-6d2f9a0e3b02",
        params=ExportParams(mode="combined", indicators=["HICP_YOY"], regions=["RO", "RO113"],
                            period_from="2023-03", period_to="2024-04", formats=["pdf", "xlsx"]),
        draft=draft, metrics=[pp, latest], observations=observations,
        citations=[
            ResolvedCitation(kind="upload", ref_id=file_id, label=citations[0].label,
                             source_label="Upload: IRS_regional_monthly_2023-2024.xlsx", locator="sheet Preturi"),
            ResolvedCitation(kind="api", ref_id=series, label=citations[1].label,
                             source_label=f"Eurostat: {series}", url=url),
            ResolvedCitation(kind="reference", ref_id=citations[2].ref_id, label=citations[2].label,
                             source_label="IRS: Technical Note on Differences between IRS "
                                          "Regional Series and European Harmonised Series", locator="§3.1"),
        ],
        validation_exclusions=[ValidationExclusion(file_id=file_id, filename="IRS_regional_monthly_2023-2024.xlsx",
                                                   worksheet="Preturi", issue_code="non_numeric", column="valoare",
                                                   rows=[27], message="Not numbers: n/a.")],
        gap_fill=GapFillReport(consent=True, filled=[FilledPoint(
            indicator="HICP_YOY", upload_indicator="IPC_VAR_AN", region="RO", period="2024-04", obs_id=gap.obs_id,
            source_obs_id=gap_source.obs_id, api_name="eurostat", series_id=series)]),
        conflicts=[ConflictNote(conflict=conflict, resolution=resolution)],
        conflict_exclusions=[ExcludedObservation(obs_id=conflicting_upload.obs_id,
                                                 reason=f"conflict {conflict.conflict_id}: resolved in favour of "
                                                        f"{ro_api[2].obs_id}")],
        unit_mismatches=[UnitMismatch(indicator="HICP_INDEX", region="RO", period="2024-03", units=["I21", "I15"],
                                      candidates=["obs_ipc_example", "obs_hicp_i15_example"])],
        approved_by="analyst@example.com", approved_at=t, generated_at=t,
    )
