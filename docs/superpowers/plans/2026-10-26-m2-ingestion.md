# M2 — Common Schema, File Ingestion, Chunked Uploads — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users can upload CSV / XLSX / XLS / JSON files (up to 10 MB, in ≤ 4 MB chunks) into a report; the app sniffs and rejects unsafe files, previews the data, auto-detects column roles, accepts user mappings, validates every row with explicit issues, and normalizes accepted rows into provenance-tracked `Observation`s stored per report — all user-scoped.

**Architecture:** Pure, I/O-free parsing layers (`domain/` contracts → `ingest/` sniff, readers, cell parsers, detection, row parsing, validation, normalization) are unit-tested without a database. A storage layer adds Postgres tables and a `BlobStore` protocol (`LocalBlobStore` for dev/tests, `VercelBlobStore` via the official `vercel-blob` SDK with OIDC). A JSON API in `web/routes/uploads.py` ties them together; the HTMX wizard UI that calls it arrives in M8.

**Tech Stack:** Python 3.12, Pydantic 2, openpyxl 3.1.5, xlrd 2.0.2, vercel-blob 0.5.0, FastAPI 0.162, psycopg 3.3, pytest + pgserver.

## Global Constraints

- Every tunable comes from `statbrief.config.Settings` (+ `.env.example`); no hardcoded limits, sizes or thresholds in code (protocol constants such as format codes are fine, named and commented).
- Every function/class has a docstring. Tests need no network or API keys.
- `requirements.txt` and `[project].dependencies` in `pyproject.toml` must stay identical (enforced by `tests/unit/test_packaging.py`).
- AC 9: accept `.csv`, `.xlsx`, `.xls`, `.json`; limits `MAX_UPLOAD_MB` (10) and `MAX_FILES_PER_REPORT` (5).
- AC 10: validate by extension **and** content; reject `.xlsm`/`.xlsb`/`.xltm`, macro-bearing (`vbaProject.bin`), password-protected and malformed files with a specific reason.
- AC 11: never execute macros/formulas/scripts — Excel read with `data_only=True` (cached values); a formula cell without a cached value is an issue, not computed. CSV cells starting with `= + - @` are treated as plain text.
- AC 12–13: detection proposes header row, worksheet, roles (date/region/indicator/value/unit), date format/frequency with confidence; the user can override every mapping field and set constants for absent columns.
- AC 15–16: issues for missing required fields, duplicates, missing values, invalid/ambiguous dates, non-numeric values, inconsistent units, unknown regions, undocumented columns, formula without value, frequency mismatch. **No record is silently dropped**: every excluded row is listed with its reason, and error issues must be explicitly resolved (`excluded`) before normalization.
- AC 18–19: one `Observation` schema with discriminated provenance (`UploadProvenance` with filename, worksheet, row_number, column; `ApiProvenance` for M3).
- AC 6: every report/file/observation query is scoped by `user_id`.
- D-021: uploads arrive in chunks ≤ `UPLOAD_CHUNK_MB` (default 4.0, max 4.4) through an authenticated endpoint; reassembled server-side, stored in **private** Blob.
- D-018 (partial, now): state-changing JSON API routes require header `X-StatBrief-Client: 1` (forces a CORS preflight, blocking cross-site form posts).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
|---|---|
| `src/statbrief/domain/__init__.py` | package marker |
| `src/statbrief/domain/observation.py` | `Observation`, `UploadProvenance`, `ApiProvenance`, `make_obs_id` |
| `src/statbrief/domain/dataset.py` | `FileKind`, `DatasetRef`, `ColumnMapping`, `ColumnGuess`, `DetectedSchema`, `ValidationIssue`, `ValidationReport` |
| `src/statbrief/ingest/__init__.py` | package marker |
| `src/statbrief/ingest/sniff.py` | `validate_file_bytes`, `UploadRejected`, `FileCheck` |
| `src/statbrief/ingest/readers.py` | `Grid`, `RawTable`, `SheetInfo`, `read_grid`, `list_worksheets`, `to_table`, `FORMULA_NO_VALUE` |
| `src/statbrief/ingest/periods.py` | `parse_period`, `ParsedPeriod`, `PeriodError`, `infer_day_order`, `looks_like_excel_serial` |
| `src/statbrief/ingest/numbers.py` | `parse_number`, `NumberError`, `infer_decimal` |
| `src/statbrief/ingest/units.py` | `normalize_unit`, `NormalizedUnit` |
| `src/statbrief/ingest/regions.py` + `ingest/data/regions.csv` | `resolve_region`, `RegionMatch` |
| `src/statbrief/ingest/detect.py` | `detect_header_row`, `detect_schema` |
| `src/statbrief/ingest/rows.py` | `parse_rows`, `ParsedRow`, `RowProblem` |
| `src/statbrief/ingest/validate.py` | `validate_table` |
| `src/statbrief/ingest/normalize.py` | `to_observations`, `NormalizationResult`, `ExcludedRow`, `UnresolvedIssues` |
| `src/statbrief/ingest/template.py` | `TEMPLATE_COLUMNS`, `build_template_csv`, `build_template_xlsx` |
| `src/statbrief/storage/migrations/002_reports_files.sql` | reports, files, uploads, upload_chunks, file_mappings, validation_reports, observations |
| `src/statbrief/storage/repos.py` | `ReportRepo`, `FileRepo`, `UploadRepo`, `MappingRepo`, `ValidationRepo`, `ObservationRepo` |
| `src/statbrief/storage/blobs.py` | `BlobStore`, `LocalBlobStore`, `VercelBlobStore`, `get_blob_store` |
| `src/statbrief/web/routes/uploads.py` | JSON API: reports, chunked uploads, sheets, preview, mapping, validate, normalize, template |
| `tests/unit/ingest/…`, `tests/unit/test_repos.py`, `tests/unit/test_blobs.py`, `tests/web/test_uploads_api.py` | tests |
| `tests/fixtures.py` | in-memory builders for xlsx/xls/csv/json test files |

---

### Task 1: Dependencies and domain contracts

**Files:**
- Modify: `requirements.txt`, `pyproject.toml` (`[project].dependencies`), `scripts/bundle_probe_requirements.txt`
- Create: `src/statbrief/domain/__init__.py`, `src/statbrief/domain/observation.py`, `src/statbrief/domain/dataset.py`, `tests/unit/domain/__init__.py`, `tests/unit/domain/test_observation.py`, `tests/unit/domain/test_dataset.py`

**Interfaces — Produces:**
- `Frequency = Literal["A","Q","M"]`, `RegionLevel = Literal["country","nuts1","nuts2","nuts3","aggregate","other"]`
- `UploadProvenance(file_id, filename, worksheet=None, row_number=None, column)`, `ApiProvenance(api_name, series_id, query_url, retrieved_at)`, `Provenance` (discriminated on `source_type`)
- `Observation(obs_id, indicator_code, indicator_label=None, region_code, region_level, period, frequency, value: float|None, unit, unit_multiplier=0, status_flag=None, provenance)` — frozen; validates period format vs frequency and finite value
- `make_obs_id(*parts) -> str` (`"obs_" + 16 hex`)
- `FileKind = Literal["csv","xlsx","xls","json"]`; `DatasetRef(file_id, report_id, user_id, filename, kind, size_bytes, sha256, blob_key)`
- `ColumnRole = Literal["date","region","indicator","value","unit","ignore"]`
- `ColumnMapping(worksheet=None, header_row=1, date_col, value_col, region_col=None, indicator_col=None, unit_col=None, constants={}, date_format="auto", decimal="auto", column_descriptions={})` — validator: region/indicator/unit each come from a column **or** a constant; `date_format ∈ {"auto","DMY","MDY","excel_serial"}`; `decimal ∈ {"auto",".",","}`
- `ColumnGuess(column, role, confidence, note=None)`; `DetectedSchema(worksheet, header_row, columns, frequency, day_order, excel_serial, decimal, meta)` with `suggested_mapping() -> ColumnMapping | None` and `needs_review(threshold) -> bool`
- `IssueCode` literal; `ValidationIssue(issue_id, code, severity, rows, column, message, resolution="pending")`; `ValidationReport(file_id, row_count, issues)` with `.blocking`, `.with_resolution(issue_id, resolution)`

- [ ] **Step 1: Dependencies** — append to `requirements.txt` **and** the `dependencies` list in `pyproject.toml` (same order, same pins), and to `scripts/bundle_probe_requirements.txt`:
```
openpyxl==3.1.5
xlrd==2.0.2
vercel-blob==0.5.0
```
Install: `.venv/Scripts/python -m pip install -r requirements-dev.txt -q`. Verify: `.venv/Scripts/python -c "from vercel import blob; import openpyxl, xlrd; print('ok')"` → `ok`.

- [ ] **Step 2: Write the failing tests** — `tests/unit/domain/test_observation.py`:
```python
"""Tests for the common observation schema and provenance."""
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from statbrief.domain.observation import ApiProvenance, Observation, Provenance, UploadProvenance, make_obs_id

PROV = UploadProvenance(file_id="f1", filename="irs.xlsx", worksheet="Inflation", row_number=14, column="valoare")


def _obs(**kw):
    """Build a valid observation, overriding fields."""
    base = dict(obs_id="obs_1", indicator_code="HICP_YOY", region_code="RO113", region_level="nuts3",
                period="2024-03", frequency="M", value=6.6, unit="%", provenance=PROV)
    base.update(kw)
    return Observation(**base)


def test_valid_observation_roundtrip():
    """A valid observation serialises and re-parses with its provenance type intact."""
    o = _obs()
    again = Observation.model_validate_json(o.model_dump_json())
    assert again == o and isinstance(again.provenance, UploadProvenance)


@pytest.mark.parametrize("period,freq", [("2024", "A"), ("2024-Q1", "Q"), ("2024-03", "M"), ("2024-12", "M")])
def test_period_matches_frequency(period, freq):
    """Each frequency accepts its canonical period format."""
    assert _obs(period=period, frequency=freq).period == period


@pytest.mark.parametrize("period,freq", [("2024-03", "A"), ("2024", "M"), ("2024-13", "M"), ("2024-Q5", "Q"), ("24-03", "M")])
def test_period_frequency_mismatch_rejected(period, freq):
    """Malformed or mismatched periods are rejected."""
    with pytest.raises(ValidationError):
        _obs(period=period, frequency=freq)


def test_missing_value_allowed_but_nan_rejected():
    """A gap is None; NaN/inf are not valid values."""
    assert _obs(value=None).value is None
    with pytest.raises(ValidationError):
        _obs(value=float("nan"))
    with pytest.raises(ValidationError):
        _obs(value=float("inf"))


def test_api_provenance_discriminated():
    """Provenance parses to the right class from a dict."""
    api = {"source_type": "api", "api_name": "eurostat", "series_id": "prc_hicp_manr",
           "query_url": "https://ec.europa.eu/x", "retrieved_at": "2026-10-26T10:00:00Z"}
    p = TypeAdapter(Provenance).validate_python(api)
    assert isinstance(p, ApiProvenance) and p.retrieved_at == datetime(2026, 10, 26, 10, tzinfo=timezone.utc)


def test_observation_is_immutable():
    """Observations are frozen so provenance cannot be altered after creation."""
    with pytest.raises(ValidationError):
        _obs().value = 1.0  # type: ignore[misc]


def test_make_obs_id_stable_and_distinct():
    """Same parts → same id; different parts → different id."""
    a = make_obs_id("f1", 14, "HICP", "RO113", "2024-03")
    assert a == make_obs_id("f1", 14, "HICP", "RO113", "2024-03")
    assert a != make_obs_id("f1", 15, "HICP", "RO113", "2024-03")
    assert a.startswith("obs_") and len(a) == 20
```

`tests/unit/domain/test_dataset.py`:
```python
"""Tests for dataset contracts: mappings, detection results, validation reports."""
import pytest
from pydantic import ValidationError

from statbrief.domain.dataset import ColumnGuess, ColumnMapping, DetectedSchema, ValidationIssue, ValidationReport


def test_mapping_requires_region_indicator_unit_from_column_or_constant():
    """Each of region/indicator/unit must come from a column or a constant."""
    ColumnMapping(date_col="luna", value_col="v", region_col="judet", indicator_col="ind", unit_col="um")
    ColumnMapping(date_col="luna", value_col="v", constants={"region": "RO", "indicator": "HICP", "unit": "%"})
    with pytest.raises(ValidationError, match="region"):
        ColumnMapping(date_col="luna", value_col="v", indicator_col="ind", unit_col="um")


def test_mapping_rejects_unknown_options_and_role_reuse():
    """Unknown parse options and one column used for two roles are rejected."""
    with pytest.raises(ValidationError):
        ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "I", "unit": "%"},
                      date_format="YYYY")
    with pytest.raises(ValidationError, match="more than one role"):
        ColumnMapping(date_col="d", value_col="d", constants={"region": "RO", "indicator": "I", "unit": "%"})


def test_detected_schema_suggested_mapping_and_review():
    """A schema with date+value suggests a mapping; low confidence needs review."""
    cols = [ColumnGuess(column="luna", role="date", confidence=0.95),
            ColumnGuess(column="judet", role="region", confidence=0.9),
            ColumnGuess(column="indicator", role="indicator", confidence=0.85),
            ColumnGuess(column="valoare", role="value", confidence=0.97),
            ColumnGuess(column="um", role="unit", confidence=0.6),
            ColumnGuess(column="val_adj_x", role="ignore", confidence=0.0, note="undocumented")]
    s = DetectedSchema(worksheet="Inflation", header_row=3, columns=cols, frequency="M")
    m = s.suggested_mapping()
    assert m is not None and (m.date_col, m.region_col, m.unit_col, m.header_row) == ("luna", "judet", "um", 3)
    assert s.needs_review(0.8) is True
    assert DetectedSchema(worksheet=None, header_row=1, columns=cols[:1]).suggested_mapping() is None


def test_report_blocking_and_resolution():
    """Pending errors block; resolving them (or only warnings) unblocks; resolution returns a copy."""
    err = ValidationIssue(issue_id="invalid_date:luna", code="invalid_date", severity="error", rows=[18],
                          column="luna", message="bad")
    warn = ValidationIssue(issue_id="missing_value:v", code="missing_value", severity="warning", rows=[9],
                           column="v", message="gap")
    r = ValidationReport(file_id="f1", row_count=10, issues=[err, warn])
    assert r.blocking
    r2 = r.with_resolution("invalid_date:luna", "excluded")
    assert not r2.blocking and r.blocking
    with pytest.raises(KeyError):
        r.with_resolution("nope", "excluded")
```

- [ ] **Step 3: Run to verify failure** — `.venv/Scripts/python -m pytest tests/unit/domain -q` → FAIL (`ModuleNotFoundError: statbrief.domain`).

- [ ] **Step 4: Implement** — `src/statbrief/domain/__init__.py`:
```python
"""Pydantic contracts shared between ingestion, analysis, the LLM layer and the web layer (no I/O)."""
```
`src/statbrief/domain/observation.py`:
```python
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
```
`src/statbrief/domain/dataset.py`:
```python
"""Contracts for uploaded datasets: references, column mappings, detection results, validation reports."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from statbrief.domain.observation import Frequency

FileKind = Literal["csv", "xlsx", "xls", "json"]
ColumnRole = Literal["date", "region", "indicator", "value", "unit", "ignore"]
DateFormat = Literal["auto", "DMY", "MDY", "excel_serial"]
DecimalSep = Literal["auto", ".", ","]
IssueCode = Literal[
    "missing_required", "duplicate_row", "duplicate_key_conflict", "missing_value", "invalid_date",
    "ambiguous_date", "non_numeric", "inconsistent_unit", "unknown_region", "undocumented_column",
    "formula_without_value", "frequency_mismatch",
]
Resolution = Literal["pending", "excluded", "fixed", "accepted"]


class DatasetRef(BaseModel):
    """A stored uploaded file that belongs to one user's report."""

    model_config = ConfigDict(frozen=True)
    file_id: str
    report_id: str
    user_id: str
    filename: str
    kind: FileKind
    size_bytes: int = Field(ge=0)
    sha256: str
    blob_key: str


class ColumnMapping(BaseModel):
    """How the columns of one table map to observation fields, plus parsing options."""

    worksheet: str | None = None
    header_row: int = Field(1, ge=1)  # 1-based row number of the header line in the file/sheet
    date_col: str
    value_col: str
    region_col: str | None = None
    indicator_col: str | None = None
    unit_col: str | None = None
    constants: dict[Literal["region", "indicator", "unit"], str] = Field(default_factory=dict)
    date_format: DateFormat = "auto"
    decimal: DecimalSep = "auto"
    column_descriptions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _complete(self) -> "ColumnMapping":
        """Region/indicator/unit come from a column or a constant; no column serves two roles."""
        for role in ("region", "indicator", "unit"):
            if not getattr(self, f"{role}_col") and not self.constants.get(role, "").strip():
                raise ValueError(f"{role}: map a column or set a constant")
        used = [c for c in (self.date_col, self.value_col, self.region_col, self.indicator_col, self.unit_col) if c]
        if len(used) != len(set(used)):
            raise ValueError("a column is mapped to more than one role")
        return self

    def mapped_columns(self) -> set[str]:
        """Columns used by this mapping."""
        return {c for c in (self.date_col, self.value_col, self.region_col, self.indicator_col, self.unit_col) if c}


class ColumnGuess(BaseModel):
    """Detected role for one column, with confidence in [0, 1]."""

    column: str
    role: ColumnRole
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = None


class DetectedSchema(BaseModel):
    """Result of automatic detection for one table."""

    worksheet: str | None
    header_row: int = Field(ge=1)
    columns: list[ColumnGuess]
    frequency: Frequency | None = None
    day_order: Literal["DMY", "MDY"] | None = None
    excel_serial: bool = False
    decimal: Literal[".", ","] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    def _col(self, role: str) -> ColumnGuess | None:
        """The column guessed for `role`, if any."""
        return next((c for c in self.columns if c.role == role), None)

    def suggested_mapping(self) -> ColumnMapping | None:
        """Best-guess mapping; None when no date or value column was found (constants needed otherwise)."""
        date, value = self._col("date"), self._col("value")
        if not date or not value:
            return None
        region, indicator, unit = self._col("region"), self._col("indicator"), self._col("unit")
        date_format = "excel_serial" if self.excel_serial else (self.day_order or "auto")
        try:
            return ColumnMapping(
                worksheet=self.worksheet, header_row=self.header_row, date_col=date.column, value_col=value.column,
                region_col=region.column if region else None,
                indicator_col=indicator.column if indicator else None,
                unit_col=unit.column if unit else None,
                date_format=date_format, decimal=self.decimal or "auto",
            )
        except ValueError:
            return None  # missing region/indicator/unit: the user must supply constants

    def needs_review(self, threshold: float) -> bool:
        """True when any mapped role is below `threshold` or the suggestion is incomplete."""
        mapped = [c for c in self.columns if c.role != "ignore"]
        return self.suggested_mapping() is None or any(c.confidence < threshold for c in mapped)


class ValidationIssue(BaseModel):
    """One group of problems of the same kind in the same column."""

    issue_id: str
    code: IssueCode
    severity: Literal["error", "warning"]
    rows: list[int] = Field(default_factory=list)  # original 1-based row numbers
    column: str | None = None
    message: str
    resolution: Resolution = "pending"


class ValidationReport(BaseModel):
    """All issues found in one table under one mapping."""

    file_id: str
    row_count: int = Field(ge=0)
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """True while any error is unresolved."""
        return any(i.severity == "error" and i.resolution == "pending" for i in self.issues)

    def with_resolution(self, issue_id: str, resolution: Resolution) -> "ValidationReport":
        """Copy of the report with one issue's resolution changed (KeyError if unknown)."""
        if not any(i.issue_id == issue_id for i in self.issues):
            raise KeyError(issue_id)
        issues = [i.model_copy(update={"resolution": resolution}) if i.issue_id == issue_id else i
                  for i in self.issues]
        return self.model_copy(update={"issues": issues})
```
Create `tests/unit/domain/__init__.py` (empty).

- [ ] **Step 5: Run tests** — `.venv/Scripts/python -m pytest tests/unit/domain tests/unit/test_packaging.py -o addopts="" -q` → all pass.
- [ ] **Step 6: Commit** — `git add requirements.txt pyproject.toml scripts/bundle_probe_requirements.txt src/statbrief/domain tests/unit/domain && git commit -m "feat(m2): domain contracts for observations, mappings, validation; ingestion deps"`

---

### Task 2: Upload sniffing (AC 9–11)

**Files:** Create `src/statbrief/ingest/__init__.py`, `src/statbrief/ingest/sniff.py`, `tests/fixtures.py`, `tests/unit/ingest/__init__.py`, `tests/unit/ingest/test_sniff.py`

**Interfaces:**
- Consumes: `FileKind`.
- Produces: `class UploadRejected(Exception)` with `.code` ∈ {`empty`,`too_large`,`unsupported_extension`,`macro_enabled`,`password_protected`,`malformed`,`not_text`,`bad_json`} and `.message`; `FileCheck(kind: FileKind, size_bytes: int, sha256: str)`; `validate_file_bytes(filename: str, data: bytes, max_bytes: int, max_uncompressed_ratio: int) -> FileCheck`.
- `tests/fixtures.py` produces `xlsx_bytes(sheets: dict[str, list[list]], hidden: set[str]=frozenset()) -> bytes`, `xlsx_with_formula_no_cache() -> bytes`, `xls_bytes(rows) -> bytes` (see Task 3 for xls note), `csv_bytes(rows, delimiter=",", encoding="utf-8") -> bytes`.

- [ ] **Step 1: Fixtures** — `tests/fixtures.py`:
```python
"""In-memory builders for upload test files (no files on disk)."""
import csv
import io
import zipfile

from openpyxl import Workbook


def xlsx_bytes(sheets: dict[str, list[list]], hidden: frozenset[str] = frozenset()) -> bytes:
    """Build an .xlsx workbook with the given sheets (first sheet active)."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
        if name in hidden:
            ws.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_with_formula_no_cache() -> bytes:
    """Workbook whose B2 is a formula saved by openpyxl (which stores no cached value)."""
    return xlsx_bytes({"S": [["luna", "valoare"], ["2024-01", "=1+1"], ["2024-02", 7.1]]})


def add_zip_member(data: bytes, name: str, content: bytes) -> bytes:
    """Return a copy of a zip/xlsx with an extra member (e.g. a fake vbaProject.bin)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            z.writestr(item, src.read(item.filename))
        z.writestr(name, content)
    return out.getvalue()


def csv_bytes(rows: list[list], delimiter: str = ",", encoding: str = "utf-8") -> bytes:
    """Serialise rows as CSV in the given encoding."""
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode(encoding)
```
Add `xls_bytes` in Task 3 (needs `xlwt`-free approach — see Task 3).

- [ ] **Step 2: Failing tests** — `tests/unit/ingest/test_sniff.py`:
```python
"""Tests for upload content sniffing: extension + magic bytes + structure, never execution."""
import hashlib
import json

import pytest

from statbrief.ingest.sniff import UploadRejected, validate_file_bytes
from tests.fixtures import add_zip_member, csv_bytes, xlsx_bytes

MAX = 10 * 1024 * 1024
RATIO = 100
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def _check(name, data):
    """Call the validator with test limits."""
    return validate_file_bytes(name, data, max_bytes=MAX, max_uncompressed_ratio=RATIO)


def test_accepts_csv_json_xlsx_and_hashes():
    """Valid files pass with kind, size and sha256."""
    c = csv_bytes([["a", "b"], [1, 2]])
    r = _check("data.CSV", c)
    assert (r.kind, r.size_bytes, r.sha256) == ("csv", len(c), hashlib.sha256(c).hexdigest())
    assert _check("d.json", json.dumps([{"a": 1}]).encode()).kind == "json"
    assert _check("w.xlsx", xlsx_bytes({"S": [["a"], [1]]})).kind == "xlsx"


@pytest.mark.parametrize("name,data,code", [
    ("e.csv", b"", "empty"),
    ("x.xlsm", b"PK\x03\x04", "macro_enabled"),
    ("x.xlsb", b"PK\x03\x04", "macro_enabled"),
    ("x.exe", b"MZ", "unsupported_extension"),
    ("x.txt", b"a,b", "unsupported_extension"),
    ("x.xlsx", b"not a zip at all", "malformed"),
    ("x.xlsx", OLE, "password_protected"),
    ("x.xls", b"PK\x03\x04garbage", "malformed"),
    ("x.csv", b"a,b\x00\x01\x02", "not_text"),
    ("x.json", b"{not json", "bad_json"),
])
def test_rejections(name, data, code):
    """Each unsafe or malformed upload is rejected with a specific code."""
    with pytest.raises(UploadRejected) as exc:
        _check(name, data)
    assert exc.value.code == code and exc.value.message


def test_too_large():
    """Files above the limit are rejected before parsing."""
    with pytest.raises(UploadRejected) as exc:
        validate_file_bytes("a.csv", b"a" * 11, max_bytes=10, max_uncompressed_ratio=RATIO)
    assert exc.value.code == "too_large"


def test_xlsx_with_vba_project_rejected():
    """An .xlsx renamed from a macro workbook (contains vbaProject.bin) is rejected."""
    data = add_zip_member(xlsx_bytes({"S": [["a"]]}), "xl/vbaProject.bin", b"\x00" * 10)
    with pytest.raises(UploadRejected) as exc:
        _check("ok.xlsx", data)
    assert exc.value.code == "macro_enabled"


def test_zip_bomb_rejected():
    """A workbook whose uncompressed size exceeds ratio × compressed size is rejected."""
    data = add_zip_member(xlsx_bytes({"S": [["a"]]}), "xl/bomb.xml", b"0" * 5_000_000)
    with pytest.raises(UploadRejected) as exc:
        validate_file_bytes("b.xlsx", data, max_bytes=MAX, max_uncompressed_ratio=50)
    assert exc.value.code == "malformed"


def test_xls_magic_accepted():
    """A legacy .xls must start with the OLE2 signature (content parsed later)."""
    assert _check("old.xls", OLE).kind == "xls"
```
- [ ] **Step 3: Run** — `pytest tests/unit/ingest/test_sniff.py` → FAIL (module missing).
- [ ] **Step 4: Implement** — `src/statbrief/ingest/__init__.py`: `"""File ingestion: sniffing, reading, parsing, detection, validation, normalization (pure, no I/O)."""`

`src/statbrief/ingest/sniff.py`:
```python
"""Validate uploaded bytes by extension AND content; never execute or render anything."""
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from statbrief.domain.dataset import FileKind

_KIND_BY_EXT: dict[str, FileKind] = {".csv": "csv", ".xlsx": "xlsx", ".xls": "xls", ".json": "json"}
_MACRO_EXTS = {".xlsm", ".xlsb", ".xltm", ".xlam"}
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls and encrypted OOXML containers
_TEXT_PROBE_BYTES = 8192  # bytes inspected for binary content in text formats


class UploadRejected(Exception):
    """The upload is unsafe or unreadable; `code` is machine-readable, `message` user-facing."""

    def __init__(self, code: str, message: str):
        """Store the rejection reason."""
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class FileCheck:
    """Accepted file facts."""

    kind: FileKind
    size_bytes: int
    sha256: str


def _check_xlsx(data: bytes, max_uncompressed_ratio: int) -> None:
    """OOXML must be a zip with workbook parts, no VBA project, and a sane uncompressed size."""
    if data.startswith(_OLE_MAGIC):
        raise UploadRejected("password_protected", "This workbook is password-protected or encrypted. "
                             "Remove the password in Excel and upload it again.")
    if not data.startswith(_ZIP_MAGIC):
        raise UploadRejected("malformed", "This is not a valid .xlsx file.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            total = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as exc:
        raise UploadRejected("malformed", "This .xlsx file is damaged.") from exc
    if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
        raise UploadRejected("malformed", "This zip file is not an Excel workbook.")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise UploadRejected("macro_enabled", "Workbooks containing macros are not accepted. "
                             "Save it as a plain .xlsx without macros.")
    if total > max_uncompressed_ratio * max(len(data), 1):
        raise UploadRejected("malformed", "This workbook expands to an unexpectedly large size.")


def _check_text(data: bytes, kind: FileKind) -> None:
    """CSV/JSON must be text; JSON must parse."""
    if b"\x00" in data[:_TEXT_PROBE_BYTES]:
        raise UploadRejected("not_text", "This file contains binary data, not text.")
    if kind == "json":
        try:
            json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadRejected("bad_json", "This JSON file could not be parsed.") from exc


def validate_file_bytes(filename: str, data: bytes, max_bytes: int, max_uncompressed_ratio: int) -> FileCheck:
    """Return the file kind and hash, or raise UploadRejected with a specific reason."""
    ext = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if ext in _MACRO_EXTS:
        raise UploadRejected("macro_enabled", "Macro-enabled Excel files are not accepted. Save as .xlsx.")
    kind = _KIND_BY_EXT.get(ext)
    if kind is None:
        raise UploadRejected("unsupported_extension", "Only CSV, Excel (.xlsx, .xls) and JSON files are accepted.")
    if not data:
        raise UploadRejected("empty", "The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejected("too_large", f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    if kind == "xlsx":
        _check_xlsx(data, max_uncompressed_ratio)
    elif kind == "xls":
        if not data.startswith(_OLE_MAGIC):
            raise UploadRejected("malformed", "This is not a valid .xls file.")
    else:
        _check_text(data, kind)
    return FileCheck(kind=kind, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
```
- [ ] **Step 5: Run** → pass. **Step 6: Commit** `feat(m2): upload sniffing (extension + content, macros/encryption/zip-bomb rejected)`.

---

### Task 3: Readers — grid, worksheets, header → table (AC 11, 12 edge cases)

**Files:** Create `src/statbrief/ingest/readers.py`, `tests/unit/ingest/test_readers.py`; modify `tests/fixtures.py`.

**Interfaces:**
- Consumes: `FileKind`, `UploadRejected` (for unreadable content).
- Produces:
  - `FORMULA_NO_VALUE` — sentinel object for a formula cell without a cached value.
  - `@dataclass Grid(rows: list[list[Any]], row_numbers: list[int], worksheet: str|None, meta: dict)` — every non-empty source row, original 1-based row numbers.
  - `@dataclass SheetInfo(name: str, hidden: bool)`
  - `@dataclass RawTable(columns: list[str], rows: list[list[Any]], row_numbers: list[int], worksheet: str|None, meta: dict)` with `column_index(name) -> int`.
  - `list_worksheets(data: bytes, kind: FileKind) -> list[SheetInfo]` (`[]` for csv/json)
  - `read_grid(data: bytes, kind: FileKind, worksheet: str | None = None, max_rows: int | None = None) -> Grid` — CSV cells stay `str`; Excel cells are `str | int | float | datetime | None | FORMULA_NO_VALUE`; JSON produces a header row from the union of keys.
  - `to_table(grid: Grid, header_row: int) -> RawTable` — header is the grid row whose original row number == `header_row`; rows after it; blank header names become `column_N`; duplicates get `_2`, `_3`; fully empty rows dropped; ragged rows padded with `None`.

**CSV rules:** decode `utf-8-sig` → `cp1250` → `latin-1` (first that succeeds; record `meta["encoding"]`); delimiter sniffed among `,` `;` `\t` `|` (`meta["delimiter"]`); **no** type conversion; cells beginning with `= + - @` are kept verbatim as text (never evaluated).
**Excel rules:** `openpyxl.load_workbook(..., read_only=True, data_only=True)`; a second pass with `data_only=False` only to tell a formula (`str` starting `=`) from an empty cell → `FORMULA_NO_VALUE`; dates come back as `datetime`. `.xls` via `xlrd.open_workbook(file_contents=...)` (cached values only; date cells converted with `xlrd.xldate_as_datetime`). An xlrd "encrypted" error (FILEPASS) maps to `UploadRejected("password_protected", …)` (Task 2 review). Default worksheet = first **visible** sheet. Unreadable content → `UploadRejected("malformed", …)`.
**JSON rules:** top-level list of objects, or an object with a `data`/`records`/`rows` list of objects; otherwise `UploadRejected("malformed", "…not a table…")`. Nested values are kept as their JSON text.
**xls fixture:** add to `tests/fixtures.py` a committed tiny binary sample instead of generating: `tests/data/sample.xls` created once by the implementer with LibreOffice/Excel **or**, if neither is available, skip `.xls` content tests with `pytest.mark.skipif(not SAMPLE_XLS.exists(), …)` and record it in the report (xlrd reading is a thin wrapper).

- [ ] **Step 1: Failing tests** — `tests/unit/ingest/test_readers.py`:
```python
"""Tests for reading uploads into grids and tables without executing anything."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from statbrief.ingest.readers import FORMULA_NO_VALUE, list_worksheets, read_grid, to_table
from statbrief.ingest.sniff import UploadRejected
from tests.fixtures import csv_bytes, xlsx_bytes, xlsx_with_formula_no_cache

SAMPLE_XLS = Path(__file__).parents[2] / "data" / "sample.xls"


def test_csv_semicolon_decimal_comma_cp1250_and_formula_text():
    """Semicolon CSV in cp1250 keeps strings verbatim, incl. formula-looking cells."""
    data = csv_bytes([["luna", "județ", "valoare"], ["2024-01", "Iași", "7,4"], ["2024-02", "Cluj", "=1+1"]],
                     delimiter=";", encoding="cp1250")
    g = read_grid(data, "csv")
    assert g.meta["delimiter"] == ";" and g.meta["encoding"] in ("cp1250", "latin-1")
    assert g.rows[1] == ["2024-01", "Iași", "7,4"] and g.rows[2][2] == "=1+1"
    assert g.row_numbers == [1, 2, 3]


def test_csv_utf8_bom():
    """A UTF-8 BOM is removed from the first header."""
    g = read_grid("﻿a,b\n1,2\n".encode("utf-8"), "csv")
    assert g.rows[0] == ["a", "b"] and g.meta["encoding"] == "utf-8-sig"


def test_xlsx_header_not_in_row_1_hidden_sheet_and_dates():
    """Title rows above the header are kept in the grid with real row numbers; hidden sheets listed."""
    data = xlsx_bytes({"Notes": [["n"]], "Inflation": [["IRS regional"], [None], ["luna", "valoare"],
                                                       [datetime(2024, 1, 1), 7.4], [datetime(2024, 2, 1), 7.1]]},
                      hidden=frozenset({"Notes"}))
    sheets = list_worksheets(data, "xlsx")
    assert [(s.name, s.hidden) for s in sheets] == [("Notes", True), ("Inflation", False)]
    g = read_grid(data, "xlsx")  # default: first visible sheet
    assert g.worksheet == "Inflation" and g.row_numbers == [1, 3, 4, 5]
    t = to_table(g, header_row=3)
    assert t.columns == ["luna", "valoare"] and t.row_numbers == [4, 5]
    assert t.rows[0] == [datetime(2024, 1, 1), 7.4]


def test_xlsx_formula_without_cached_value_is_flagged_not_computed():
    """openpyxl-saved formulas have no cached value → FORMULA_NO_VALUE, never '2'."""
    g = read_grid(xlsx_with_formula_no_cache(), "xlsx")
    assert g.rows[1][1] is FORMULA_NO_VALUE and g.rows[2][1] == 7.1


def test_to_table_blank_and_duplicate_headers_and_ragged_rows():
    """Blank headers get names, duplicates get suffixes, short rows are padded."""
    data = csv_bytes([["a", "", "a"], ["1"], ["2", "3", "4"]])
    t = to_table(read_grid(data, "csv"), header_row=1)
    assert t.columns == ["a", "column_2", "a_2"]
    assert t.rows == [["1", None, None], ["2", "3", "4"]] and t.column_index("a_2") == 2


def test_json_records_and_wrapped():
    """List of objects and {data: [...]} both become tables; key union preserves order."""
    for payload in ([{"d": "2024", "v": 1}, {"d": "2025", "v": 2, "u": "%"}],
                    {"data": [{"d": "2024", "v": 1}, {"d": "2025", "v": 2, "u": "%"}]}):
        t = to_table(read_grid(json.dumps(payload).encode(), "json"), header_row=1)
        assert t.columns == ["d", "v", "u"] and t.rows[0] == ["2024", 1, None] and t.row_numbers == [2, 3]


def test_json_not_tabular_rejected():
    """Non-tabular JSON is rejected with guidance."""
    with pytest.raises(UploadRejected) as exc:
        read_grid(b'{"a": {"b": 1}}', "json")
    assert exc.value.code == "malformed"


def test_max_rows_limit():
    """Reading stops with a clear rejection when a file exceeds the row limit."""
    data = csv_bytes([["a"]] + [[i] for i in range(20)])
    with pytest.raises(UploadRejected) as exc:
        read_grid(data, "csv", max_rows=10)
    assert exc.value.code == "too_large"


@pytest.mark.skipif(not SAMPLE_XLS.exists(), reason="tests/data/sample.xls not available")
def test_xls_reads_cached_values():
    """Legacy .xls is read via xlrd (cached values only)."""
    g = read_grid(SAMPLE_XLS.read_bytes(), "xls")
    assert g.rows and g.worksheet
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `src/statbrief/ingest/readers.py`:
```python
"""Read uploaded bytes into a grid of cells (no type coercion, no formula evaluation), then into tables."""
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from statbrief.domain.dataset import FileKind
from statbrief.ingest.sniff import UploadRejected

_ENCODINGS = ("utf-8-sig", "cp1250", "latin-1")  # Romanian sources commonly use cp1250
_DELIMITERS = ",;\t|"
_SNIFF_BYTES = 64 * 1024
_JSON_TABLE_KEYS = ("data", "records", "rows")


class _FormulaNoValue:
    """Sentinel type: a formula cell whose cached value is missing (never computed)."""

    def __repr__(self) -> str:
        """Readable representation in test output."""
        return "FORMULA_NO_VALUE"


FORMULA_NO_VALUE = _FormulaNoValue()


@dataclass
class Grid:
    """Non-empty source rows with their original 1-based row numbers."""

    rows: list[list[Any]]
    row_numbers: list[int]
    worksheet: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SheetInfo:
    """A worksheet name and whether it is hidden."""

    name: str
    hidden: bool


@dataclass
class RawTable:
    """Header + data rows of one table, with original row numbers."""

    columns: list[str]
    rows: list[list[Any]]
    row_numbers: list[int]
    worksheet: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def column_index(self, name: str) -> int:
        """Position of a column (ValueError if absent)."""
        return self.columns.index(name)


def _is_empty(v: Any) -> bool:
    """True for None and blank strings."""
    return v is None or (isinstance(v, str) and not v.strip())


def _append(grid: Grid, cells: list[Any], row_number: int, max_rows: int | None) -> None:
    """Add a non-empty row, enforcing the row limit."""
    if all(_is_empty(c) for c in cells):
        return
    if max_rows is not None and len(grid.rows) >= max_rows:
        raise UploadRejected("too_large", f"The file has more than {max_rows} rows.")
    grid.rows.append(cells)
    grid.row_numbers.append(row_number)


def _read_csv(data: bytes, max_rows: int | None) -> Grid:
    """Decode with a fallback chain, sniff the delimiter, keep cells as strings."""
    for enc in _ENCODINGS:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 always decodes
        raise UploadRejected("malformed", "The file's text encoding is not supported.")
    try:
        delim = csv.Sniffer().sniff(text[:_SNIFF_BYTES], delimiters=_DELIMITERS).delimiter
    except csv.Error:
        delim = ","
    grid = Grid(rows=[], row_numbers=[], meta={"encoding": enc, "delimiter": delim})
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim), start=1):
        _append(grid, [c if c != "" else None for c in row], i, max_rows)
    return grid


def _xlsx_workbooks(data: bytes):
    """Open a workbook twice: cached values, and raw cells to recognise formulas."""
    from openpyxl import load_workbook

    try:
        values = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        raw = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:  # openpyxl raises many types for corrupt files
        raise UploadRejected("malformed", "This Excel file could not be read.") from exc
    return values, raw


def _pick_sheet(infos: list[SheetInfo], worksheet: str | None) -> str:
    """Requested sheet, else the first visible one."""
    if worksheet is not None:
        if worksheet not in {s.name for s in infos}:
            raise UploadRejected("malformed", f"Worksheet {worksheet!r} does not exist.")
        return worksheet
    visible = [s.name for s in infos if not s.hidden]
    if not visible:
        raise UploadRejected("malformed", "The workbook has no visible worksheet.")
    return visible[0]


def list_worksheets(data: bytes, kind: FileKind) -> list[SheetInfo]:
    """Worksheets of an Excel file in workbook order; empty list for CSV/JSON."""
    if kind == "xlsx":
        values, raw = _xlsx_workbooks(data)
        try:
            return [SheetInfo(ws.title, ws.sheet_state != "visible") for ws in values.worksheets]
        finally:
            values.close()
            raw.close()
    if kind == "xls":
        book = _open_xls(data)
        return [SheetInfo(s.name, bool(s.visibility)) for s in book.sheets()]
    return []


def _read_xlsx(data: bytes, worksheet: str | None, max_rows: int | None) -> Grid:
    """Cached cell values; formula cells without a cache become FORMULA_NO_VALUE."""
    values, raw = _xlsx_workbooks(data)
    try:
        infos = [SheetInfo(ws.title, ws.sheet_state != "visible") for ws in values.worksheets]
        name = _pick_sheet(infos, worksheet)
        grid = Grid(rows=[], row_numbers=[], worksheet=name)
        raw_rows = raw[name].iter_rows(values_only=True)
        for i, row in enumerate(values[name].iter_rows(values_only=True), start=1):
            raw_row = next(raw_rows, ())
            cells = []
            for j, v in enumerate(row):
                r = raw_row[j] if j < len(raw_row) else None
                cells.append(FORMULA_NO_VALUE if v is None and isinstance(r, str) and r.startswith("=") else v)
            _append(grid, cells, i, max_rows)
        return grid
    finally:
        values.close()
        raw.close()


def _open_xls(data: bytes):
    """Open a legacy .xls (cached values only)."""
    import xlrd

    try:
        return xlrd.open_workbook(file_contents=data, on_demand=True)
    except Exception as exc:
        raise UploadRejected("malformed", "This .xls file could not be read.") from exc


def _read_xls(data: bytes, worksheet: str | None, max_rows: int | None) -> Grid:
    """Legacy Excel via xlrd; date cells converted to datetime."""
    import xlrd

    book = _open_xls(data)
    infos = [SheetInfo(s.name, bool(s.visibility)) for s in book.sheets()]
    name = _pick_sheet(infos, worksheet)
    sheet = book.sheet_by_name(name)
    grid = Grid(rows=[], row_numbers=[], worksheet=name)
    for r in range(sheet.nrows):
        cells = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                cells.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
            elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                cells.append(None)
            else:
                cells.append(cell.value)
        _append(grid, cells, r + 1, max_rows)
    return grid


def _read_json(data: bytes, max_rows: int | None) -> Grid:
    """List of objects (optionally wrapped) → header row + value rows."""
    doc = json.loads(data.decode("utf-8-sig"))
    if isinstance(doc, dict):
        doc = next((doc[k] for k in _JSON_TABLE_KEYS if isinstance(doc.get(k), list)), None)
    if not isinstance(doc, list) or not doc or not all(isinstance(r, dict) for r in doc):
        raise UploadRejected("malformed", "This JSON is not a table. Use a list of objects, "
                             'e.g. [{"date": "2024-01", "region": "RO", "value": 1.2}].')
    columns: list[str] = []
    for rec in doc:
        columns.extend(k for k in rec if k not in columns)
    grid = Grid(rows=[], row_numbers=[], meta={})
    _append(grid, list(columns), 1, max_rows)
    for i, rec in enumerate(doc, start=2):
        cells = [rec.get(c) for c in columns]
        _append(grid, [json.dumps(v) if isinstance(v, (dict, list)) else v for v in cells], i, max_rows)
    return grid


def read_grid(data: bytes, kind: FileKind, worksheet: str | None = None, max_rows: int | None = None) -> Grid:
    """Read any supported file into a grid of raw cells."""
    if kind == "csv":
        return _read_csv(data, max_rows)
    if kind == "xlsx":
        return _read_xlsx(data, worksheet, max_rows)
    if kind == "xls":
        return _read_xls(data, worksheet, max_rows)
    return _read_json(data, max_rows)


def _header_name(value: Any, position: int) -> str:
    """Readable column name from a header cell."""
    if _is_empty(value) or value is FORMULA_NO_VALUE:
        return f"column_{position}"
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value).strip()


def to_table(grid: Grid, header_row: int) -> RawTable:
    """Use the grid row with original number `header_row` as the header; later rows are data."""
    if header_row not in grid.row_numbers:
        raise ValueError(f"row {header_row} is empty or outside the table")
    h = grid.row_numbers.index(header_row)
    names, seen = [], {}
    for pos, cell in enumerate(grid.rows[h], start=1):
        base = _header_name(cell, pos)
        seen[base] = seen.get(base, 0) + 1
        names.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    width = max([len(names)] + [len(r) for r in grid.rows[h + 1:]])
    names += [f"column_{p}" for p in range(len(names) + 1, width + 1)]
    rows = [r + [None] * (width - len(r)) for r in grid.rows[h + 1:]]
    return RawTable(columns=names, rows=rows, row_numbers=grid.row_numbers[h + 1:],
                    worksheet=grid.worksheet, meta=dict(grid.meta))
```
- [ ] **Step 4: Run** → pass (xls test may skip; say so in the report). **Step 5: Commit** `feat(m2): readers for csv/xlsx/xls/json with formula-safe Excel reading`.

---

### Task 4: Cell parsers — periods, numbers, units, regions

**Files:** Create `src/statbrief/ingest/periods.py`, `numbers.py`, `units.py`, `regions.py`, `src/statbrief/ingest/data/__init__.py`, `src/statbrief/ingest/data/regions.csv`; tests `tests/unit/ingest/test_periods.py`, `test_numbers.py`, `test_units.py`, `test_regions.py`. Add `"ingest/data/*.csv"` to `[tool.setuptools.package-data]` in `pyproject.toml`.

**Interfaces — Produces:**
- `PeriodError(code: Literal["invalid_date","ambiguous_date"], message)`; `ParsedPeriod(period: str, frequency: Frequency)`; `parse_period(value, *, day_order: Literal["DMY","MDY"] | None = None, excel_serial: bool = False) -> ParsedPeriod`; `infer_day_order(values) -> Literal["DMY","MDY"] | None`; `looks_like_excel_serial(values, min_share: float) -> bool`.
- `NumberError(ValueError)`; `parse_number(value, decimal: Literal[".", ","] | None = None) -> float | None`; `infer_decimal(values) -> Literal[".", ","] | None`; `MISSING_TOKENS`.
- `NormalizedUnit(code: str, multiplier: int, raw: str)`; `normalize_unit(raw) -> NormalizedUnit | None` (None for empty).
- `RegionMatch(code: str, level: RegionLevel, name: str)`; `resolve_region(raw) -> RegionMatch | None`.

Parsing rules (documented in module docstrings):
- Periods: `datetime`/`date` → monthly; ints 1800–2200 → annual; with `excel_serial=True` numbers in the Excel serial range → date → monthly; strings: `2024` (A), `2024-Q1`/`2024Q1`/`2024 Q1`/`Q1 2024`/`Q1-2024` (Q), `2024M03`/`2024-03`/`2024/03`/`2024.03`/`03/2024`/`03.2024`/`2024-03-15`/`Mar 2024`/`martie 2024` (M); `dd/mm/yyyy` with both parts ≤ 12 and parts unequal → `ambiguous_date` unless `day_order` given; month 13 / day 32 → `invalid_date`.
- Numbers: `None` → None; bool → error; `int`/`float` → float (NaN → None); strings: trim, strip NBSP/thin spaces/apostrophes, trailing `%`; tokens in `MISSING_TOKENS` (`"", "-", "–", ":", "..", "n/a", "na", "nan", "null", "none", "x"`) → None; decimal handling per `decimal` or inferred (`"1.234,5"` → 1234.5; `"1,234.5"` → 1234.5; single comma → decimal comma; multiple commas → thousands).
- Units: case/diacritics-insensitive canonical map (`%`, `pp`, `EUR`, `RON`, `USD`, `PERS`, `NR`), multiplier words (`thousand/thousands/mii/th` → 3, `million/millions/mil/milioane` → 6, `billion/mld/miliarde` → 9), index units `index 2015=100` / `indice (2021=100)` → `I15` / `I21`; anything else kept verbatim (trimmed) with multiplier 0 — never invented.
- Regions: lookup by code (case-insensitive) or by normalized name/alias (lower-case, diacritics removed, prefixes `judetul`, `jud.`, `municipiul` dropped, hyphens → spaces); data in `regions.csv`.

- [ ] **Step 1: Region data** — `src/statbrief/ingest/data/regions.csv` (exact content):
```csv
code,level,name,aliases
EU27_2020,aggregate,European Union (27),EU;EU27;European Union;Uniunea Europeana;UE
EA20,aggregate,Euro area (20),EA;Euro area;Zona euro
AT,country,Austria,
BE,country,Belgium,Belgia
BG,country,Bulgaria,
CY,country,Cyprus,Cipru
CZ,country,Czechia,Czech Republic;Cehia
DE,country,Germany,Germania
DK,country,Denmark,Danemarca
EE,country,Estonia,
EL,country,Greece,GR;Grecia
ES,country,Spain,Spania
FI,country,Finland,Finlanda
FR,country,France,Franta
HR,country,Croatia,Croatia
HU,country,Hungary,Ungaria
IE,country,Ireland,Irlanda
IT,country,Italy,Italia
LT,country,Lithuania,Lituania
LU,country,Luxembourg,Luxemburg
LV,country,Latvia,Letonia
MT,country,Malta,
NL,country,Netherlands,Olanda;Tarile de Jos
PL,country,Poland,Polonia
PT,country,Portugal,Portugalia
RO,country,Romania,
SE,country,Sweden,Suedia
SI,country,Slovenia,
SK,country,Slovakia,Slovacia
MD,country,Moldova,Republica Moldova
UA,country,Ukraine,Ucraina
RS,country,Serbia,
UK,country,United Kingdom,GB;Marea Britanie
RO1,nuts1,Macroregiunea Unu,Macroregiunea 1
RO2,nuts1,Macroregiunea Doi,Macroregiunea 2
RO3,nuts1,Macroregiunea Trei,Macroregiunea 3
RO4,nuts1,Macroregiunea Patru,Macroregiunea 4
RO11,nuts2,Nord-Vest,Regiunea Nord-Vest
RO12,nuts2,Centru,Regiunea Centru
RO21,nuts2,Nord-Est,Regiunea Nord-Est
RO22,nuts2,Sud-Est,Regiunea Sud-Est
RO31,nuts2,Sud-Muntenia,Regiunea Sud-Muntenia
RO32,nuts2,Bucuresti-Ilfov,Regiunea Bucuresti-Ilfov
RO41,nuts2,Sud-Vest Oltenia,Regiunea Sud-Vest Oltenia
RO42,nuts2,Vest,Regiunea Vest
RO111,nuts3,Bihor,
RO112,nuts3,Bistrita-Nasaud,
RO113,nuts3,Cluj,
RO114,nuts3,Maramures,
RO115,nuts3,Satu Mare,
RO116,nuts3,Salaj,
RO121,nuts3,Alba,
RO122,nuts3,Brasov,
RO123,nuts3,Covasna,
RO124,nuts3,Harghita,
RO125,nuts3,Mures,
RO126,nuts3,Sibiu,
RO211,nuts3,Bacau,
RO212,nuts3,Botosani,
RO213,nuts3,Iasi,
RO214,nuts3,Neamt,
RO215,nuts3,Suceava,
RO216,nuts3,Vaslui,
RO221,nuts3,Braila,
RO222,nuts3,Buzau,
RO223,nuts3,Constanta,
RO224,nuts3,Galati,
RO225,nuts3,Tulcea,
RO226,nuts3,Vrancea,
RO311,nuts3,Arges,
RO312,nuts3,Calarasi,
RO313,nuts3,Dambovita,
RO314,nuts3,Giurgiu,
RO315,nuts3,Ialomita,
RO316,nuts3,Prahova,
RO317,nuts3,Teleorman,
RO321,nuts3,Bucuresti,Bucharest;Municipiul Bucuresti
RO322,nuts3,Ilfov,
RO411,nuts3,Dolj,
RO412,nuts3,Gorj,
RO413,nuts3,Mehedinti,
RO414,nuts3,Olt,
RO415,nuts3,Valcea,
RO421,nuts3,Arad,
RO422,nuts3,Caras-Severin,
RO423,nuts3,Hunedoara,
RO424,nuts3,Timis,
```
- [ ] **Step 2: Failing tests** — `tests/unit/ingest/test_periods.py`:
```python
"""Tests for period parsing."""
from datetime import date, datetime

import pytest

from statbrief.ingest.periods import PeriodError, infer_day_order, looks_like_excel_serial, parse_period


@pytest.mark.parametrize("raw,expected", [
    ("2024", ("2024", "A")), (2024, ("2024", "A")), (2024.0, ("2024", "A")),
    ("2024-Q1", ("2024-Q1", "Q")), ("2024Q3", ("2024-Q3", "Q")), ("Q2 2024", ("2024-Q2", "Q")),
    ("2024 Q4", ("2024-Q4", "Q")), ("q1-2024", ("2024-Q1", "Q")),
    ("2024M03", ("2024-03", "M")), ("2024-03", ("2024-03", "M")), ("2024/3", ("2024-03", "M")),
    ("03/2024", ("2024-03", "M")), ("3.2024", ("2024-03", "M")), ("2024-03-15", ("2024-03", "M")),
    ("Mar 2024", ("2024-03", "M")), ("martie 2024", ("2024-03", "M")), ("Ian. 2024", ("2024-01", "M")),
    (datetime(2024, 3, 1), ("2024-03", "M")), (date(2024, 12, 31), ("2024-12", "M")),
    ("25/03/2024", ("2024-03", "M")), ("03/25/2024", ("2024-03", "M")), ("05/05/2024", ("2024-05", "M")),
])
def test_parse_period(raw, expected):
    """Supported formats map to canonical periods."""
    p = parse_period(raw)
    assert (p.period, p.frequency) == expected


def test_ambiguous_day_month_needs_day_order():
    """01/02/2024 is ambiguous without a day order; resolved with one."""
    with pytest.raises(PeriodError) as exc:
        parse_period("01/02/2024")
    assert exc.value.code == "ambiguous_date"
    assert parse_period("01/02/2024", day_order="DMY").period == "2024-02"
    assert parse_period("01/02/2024", day_order="MDY").period == "2024-01"


@pytest.mark.parametrize("raw", ["2024-13", "2024M13", "32/01/2024", "", None, "hello", "2024-Q5", "12345"])
def test_invalid(raw):
    """Garbage and out-of-range values are invalid_date."""
    with pytest.raises(PeriodError) as exc:
        parse_period(raw)
    assert exc.value.code == "invalid_date"


def test_excel_serial_only_when_enabled():
    """45352 is 2024-03-01 as an Excel serial; without the flag it is invalid."""
    assert parse_period(45352, excel_serial=True).period == "2024-03"
    with pytest.raises(PeriodError):
        parse_period(45352)


def test_infer_day_order_and_serial_detection():
    """Day order is inferred from unambiguous values; serial columns are recognised."""
    assert infer_day_order(["01/02/2024", "25/02/2024"]) == "DMY"
    assert infer_day_order(["02/25/2024"]) == "MDY"
    assert infer_day_order(["01/02/2024"]) is None
    assert looks_like_excel_serial([45292, 45323, 45352], min_share=0.8) is True
    assert looks_like_excel_serial([2023, 2024], min_share=0.8) is False
```
`tests/unit/ingest/test_numbers.py`:
```python
"""Tests for numeric cell parsing."""
import pytest

from statbrief.ingest.numbers import NumberError, infer_decimal, parse_number


@pytest.mark.parametrize("raw,decimal,expected", [
    ("7,4", None, 7.4), ("7.4", None, 7.4), ("1.234,5", None, 1234.5), ("1,234.5", None, 1234.5),
    ("1 234,5", ",", 1234.5), ("1 234", None, 1234.0), ("1,234,567", None, 1234567.0),
    ("6.6%", None, 6.6), ("-0,3", None, -0.3), (3, None, 3.0), (2.5, None, 2.5),
    ("1.234", ",", 1234.0), ("1,5", ".", 15.0),
])
def test_parse_number(raw, decimal, expected):
    """Common separators and formats parse correctly."""
    assert parse_number(raw, decimal) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", " ", "-", ":", "..", "n/a", "NA", "null", float("nan")])
def test_missing_tokens(raw):
    """Missing markers become None (a gap), never 0."""
    assert parse_number(raw) is None


@pytest.mark.parametrize("raw", ["abc", "7,4,x", True, "1e", "=1+1"])
def test_non_numeric(raw):
    """Non-numeric content raises NumberError."""
    with pytest.raises(NumberError):
        parse_number(raw)


def test_infer_decimal():
    """The dominant decimal separator is inferred from sample values."""
    assert infer_decimal(["7,4", "7,1", "6,9"]) == ","
    assert infer_decimal(["7.4", "7.1"]) == "."
    assert infer_decimal(["7", "8"]) is None
```
`tests/unit/ingest/test_units.py`:
```python
"""Tests for unit normalisation."""
import pytest

from statbrief.ingest.units import normalize_unit


@pytest.mark.parametrize("raw,code,mult", [
    ("%", "%", 0), ("percent", "%", 0), ("Procente", "%", 0), ("p.p.", "pp", 0),
    ("EUR", "EUR", 0), ("mil. lei", "RON", 6), ("thousand persons", "PERS", 3), ("mii persoane", "PERS", 3),
    ("Index 2015=100", "I15", 0), ("indice (2021=100)", "I21", 0), ("mld. EUR", "EUR", 9),
    ("tonnes", "tonnes", 0),
])
def test_normalize_unit(raw, code, mult):
    """Known spellings map to canonical codes; unknown units are kept verbatim."""
    u = normalize_unit(raw)
    assert (u.code, u.multiplier, u.raw) == (code, mult, raw)


def test_empty_unit_is_none():
    """Blank units are missing, not invented."""
    assert normalize_unit("  ") is None and normalize_unit(None) is None
```
`tests/unit/ingest/test_regions.py`:
```python
"""Tests for region resolution."""
import pytest

from statbrief.ingest.regions import resolve_region


@pytest.mark.parametrize("raw,code,level", [
    ("RO113", "RO113", "nuts3"), ("ro113", "RO113", "nuts3"), ("Cluj", "RO113", "nuts3"),
    ("Iași", "RO213", "nuts3"), ("Județul Timiș", "RO424", "nuts3"), ("Bistrița-Năsăud", "RO112", "nuts3"),
    ("Municipiul București", "RO321", "nuts3"), ("RO", "RO", "country"), ("Romania", "RO", "country"),
    ("România", "RO", "country"), ("GR", "EL", "country"), ("EU27_2020", "EU27_2020", "aggregate"),
    ("Nord-Vest", "RO11", "nuts2"), ("Caraș Severin", "RO422", "nuts3"),
])
def test_resolve(raw, code, level):
    """Codes, names, diacritics, prefixes and aliases resolve."""
    m = resolve_region(raw)
    assert m is not None and (m.code, m.level) == (code, level)


@pytest.mark.parametrize("raw", ["Atlantis", "", None, "RO999", 42])
def test_unknown(raw):
    """Unknown regions return None (never guessed)."""
    assert resolve_region(raw) is None
```
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement** `src/statbrief/ingest/periods.py`:
```python
"""Parse period cells (years, quarters, months, dates, Excel serials) into canonical periods.

Daily dates are reduced to their month: StatBrief works with A/Q/M frequencies only (spec AC 18).
Ambiguous dd/mm vs mm/dd values are never guessed: they raise `ambiguous_date` until a day order is set.
"""
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from statbrief.domain.observation import Frequency

DayOrder = Literal["DMY", "MDY"]
_EXCEL_EPOCH = date(1899, 12, 30)  # Excel's day 0 (1900 date system, incl. the leap-year bug offset)
_EXCEL_SERIAL_MIN, _EXCEL_SERIAL_MAX = 18264, 73050  # 1950-01-01 .. 2099-12-31: plausible statistics dates
_YEAR_MIN, _YEAR_MAX = 1800, 2200
_MONTH_NAMES = {
    1: ("jan", "january", "ian", "ianuarie"), 2: ("feb", "february", "februarie"),
    3: ("mar", "march", "martie"), 4: ("apr", "april", "aprilie"), 5: ("may", "mai"),
    6: ("jun", "june", "iunie"), 7: ("jul", "july", "iulie"), 8: ("aug", "august"),
    9: ("sep", "sept", "september", "septembrie"), 10: ("oct", "october", "octombrie"),
    11: ("nov", "november", "noiembrie"), 12: ("dec", "december", "decembrie"),
}
_MONTH_LOOKUP = {name: num for num, names in _MONTH_NAMES.items() for name in names}

_RE_YEAR = re.compile(r"^(\d{4})$")
_RE_Q1 = re.compile(r"^(\d{4})\s*[-/ ]?\s*q([1-4])$", re.I)
_RE_Q2 = re.compile(r"^q([1-4])\s*[-/ ]?\s*(\d{4})$", re.I)
_RE_EUROSTAT_M = re.compile(r"^(\d{4})m(\d{1,2})$", re.I)
_RE_YM = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_RE_MY = re.compile(r"^(\d{1,2})[-/.](\d{4})$")
_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$")
_RE_DMY = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")
_RE_NAMED = re.compile(r"^([^\W\d_]+)\.?\s+(\d{4})$", re.U)


class PeriodError(ValueError):
    """A cell could not be turned into a period."""

    def __init__(self, code: Literal["invalid_date", "ambiguous_date"], message: str):
        """Store the issue code and a user-facing message."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedPeriod:
    """Canonical period string and its frequency."""

    period: str
    frequency: Frequency


def _strip_accents(s: str) -> str:
    """Lower-case and remove diacritics."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _month(year: int, month: int) -> ParsedPeriod:
    """Monthly period with range checks."""
    if not (_YEAR_MIN <= year <= _YEAR_MAX and 1 <= month <= 12):
        raise PeriodError("invalid_date", f"Month {month} of year {year} is not a valid date.")
    return ParsedPeriod(f"{year:04d}-{month:02d}", "M")


def _from_dmy(a: int, b: int, year: int, day_order: DayOrder | None) -> ParsedPeriod:
    """Resolve a d/m/y or m/d/y triple, refusing to guess when ambiguous."""
    if a > 12 and b > 12:
        raise PeriodError("invalid_date", "Neither part can be a month.")
    if a > 12 or (day_order == "DMY" and b <= 12):
        day, month = a, b
    elif b > 12 or day_order == "MDY":
        day, month = b, a
    elif a == b:
        day, month = a, b
    else:
        raise PeriodError("ambiguous_date", "Day and month order is ambiguous; choose DD/MM or MM/DD.")
    try:
        date(year, month, day)
    except ValueError as exc:
        raise PeriodError("invalid_date", "This date does not exist.") from exc
    return _month(year, month)


def parse_period(value: Any, *, day_order: DayOrder | None = None, excel_serial: bool = False) -> ParsedPeriod:
    """Parse one cell into a canonical period or raise PeriodError."""
    if isinstance(value, datetime | date):
        return _month(value.year, value.month)
    if isinstance(value, bool) or value is None:
        raise PeriodError("invalid_date", "Empty or invalid date.")
    if isinstance(value, int | float):
        if excel_serial and float(value).is_integer() and _EXCEL_SERIAL_MIN <= value <= _EXCEL_SERIAL_MAX:
            d = _EXCEL_EPOCH + timedelta(days=int(value))
            return _month(d.year, d.month)
        if float(value).is_integer() and _YEAR_MIN <= value <= _YEAR_MAX:
            return ParsedPeriod(f"{int(value):04d}", "A")
        raise PeriodError("invalid_date", f"{value!r} is not a year or date.")
    s = str(value).strip()
    if excel_serial and s.isdigit():
        return parse_period(int(s), excel_serial=True)
    if m := _RE_YEAR.match(s):
        if not _YEAR_MIN <= int(m[1]) <= _YEAR_MAX:
            raise PeriodError("invalid_date", f"Year {m[1]} is out of range.")
        return ParsedPeriod(m[1], "A")
    if m := _RE_Q1.match(s):
        return ParsedPeriod(f"{m[1]}-Q{m[2]}", "Q")
    if m := _RE_Q2.match(s):
        return ParsedPeriod(f"{m[2]}-Q{m[1]}", "Q")
    if m := _RE_EUROSTAT_M.match(s) or _RE_YM.match(s):
        return _month(int(m[1]), int(m[2]))
    if m := _RE_MY.match(s):
        return _month(int(m[2]), int(m[1]))
    if m := _RE_ISO.match(s):
        try:
            date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError as exc:
            raise PeriodError("invalid_date", "This date does not exist.") from exc
        return _month(int(m[1]), int(m[2]))
    if m := _RE_DMY.match(s):
        return _from_dmy(int(m[1]), int(m[2]), int(m[3]), day_order)
    if m := _RE_NAMED.match(s):
        month = _MONTH_LOOKUP.get(_strip_accents(m[1]))
        if month:
            return _month(int(m[2]), month)
    raise PeriodError("invalid_date", f"{s!r} is not a recognised date or period.")


def infer_day_order(values: Iterable[Any]) -> DayOrder | None:
    """DMY/MDY when some d/m/y value is unambiguous; None when nothing decides it."""
    for v in values:
        m = _RE_DMY.match(str(v).strip()) if v is not None else None
        if m:
            a, b = int(m[1]), int(m[2])
            if a > 12 >= b:
                return "DMY"
            if b > 12 >= a:
                return "MDY"
    return None


def looks_like_excel_serial(values: Iterable[Any], min_share: float) -> bool:
    """True when at least `min_share` of non-empty values are integers in the Excel serial range."""
    vals = [v for v in values if v is not None and str(v).strip()]
    if not vals:
        return False
    hits = 0
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        hits += f.is_integer() and _EXCEL_SERIAL_MIN <= f <= _EXCEL_SERIAL_MAX
    return hits / len(vals) >= min_share
```
`src/statbrief/ingest/numbers.py`:
```python
"""Parse numeric cells; missing markers become None, never zero."""
import math
import re
from collections.abc import Iterable
from typing import Any, Literal

MISSING_TOKENS = frozenset({"", "-", "–", ":", "..", "n/a", "na", "nan", "null", "none", "x"})
_SPACES = re.compile(r"[\s   ']")
_DEC_COMMA = re.compile(r"^-?\d+,\d+$")
_DEC_DOT = re.compile(r"^-?\d+\.\d+$")


class NumberError(ValueError):
    """A cell is not numeric."""


def parse_number(value: Any, decimal: Literal[".", ","] | None = None) -> float | None:
    """Return a float, None for a missing marker, or raise NumberError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise NumberError("boolean is not a number")
    if isinstance(value, int | float):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    s = str(value).strip()
    if s.lower() in MISSING_TOKENS:
        return None
    s = _SPACES.sub("", s).removesuffix("%")
    if decimal is None:
        if "," in s and "." in s:
            decimal = "," if s.rfind(",") > s.rfind(".") else "."
        elif s.count(",") == 1:
            decimal = ","
        else:
            decimal = "."
    s = s.replace(".", "").replace(",", ".") if decimal == "," else s.replace(",", "")
    try:
        result = float(s)
    except ValueError as exc:
        raise NumberError(f"{value!r} is not a number") from exc
    if not math.isfinite(result):
        raise NumberError(f"{value!r} is not a finite number")
    return result


def infer_decimal(values: Iterable[Any]) -> Literal[".", ","] | None:
    """Dominant decimal separator among string cells, or None when undecided."""
    comma = dot = 0
    for v in values:
        if isinstance(v, str):
            s = _SPACES.sub("", v)
            comma += bool(_DEC_COMMA.match(s))
            dot += bool(_DEC_DOT.match(s))
    if comma == dot:
        return None
    return "," if comma > dot else "."
```
`src/statbrief/ingest/units.py`:
```python
"""Normalise unit spellings to canonical codes; unknown units are kept verbatim (never invented)."""
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_CANON = {
    "%": "%", "percent": "%", "pct": "%", "procent": "%", "procente": "%",
    "pp": "pp", "p.p.": "pp", "percentage points": "pp", "puncte procentuale": "pp",
    "eur": "EUR", "euro": "EUR", "lei": "RON", "ron": "RON", "usd": "USD",
    "persons": "PERS", "persoane": "PERS", "number": "NR", "nr": "NR", "numar": "NR",
}
_MULT = {
    "thousand": 3, "thousands": 3, "mii": 3, "th": 3, "th.": 3,
    "million": 6, "millions": 6, "mil": 6, "mil.": 6, "milioane": 6,
    "billion": 9, "billions": 9, "mld": 9, "mld.": 9, "miliarde": 9,
}
_INDEX = re.compile(r"^(?:index|indice)\s*\(?\s*(\d{4})\s*=\s*100\s*\)?$")


@dataclass(frozen=True)
class NormalizedUnit:
    """Canonical unit code, power-of-ten multiplier, and the original spelling."""

    code: str
    multiplier: int
    raw: str


def _fold(s: str) -> str:
    """Lower-case, remove diacritics, collapse whitespace."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return " ".join(s.split())


def normalize_unit(raw: Any) -> NormalizedUnit | None:
    """Canonical unit for a cell, or None when empty."""
    if raw is None or not str(raw).strip():
        return None
    original = str(raw).strip()
    folded = _fold(original)
    if m := _INDEX.match(folded):
        return NormalizedUnit(f"I{m[1][2:]}", 0, original)
    if folded in _CANON:
        return NormalizedUnit(_CANON[folded], 0, original)
    head, _, rest = folded.partition(" ")
    if head in _MULT and rest:
        base = _CANON.get(rest)
        return NormalizedUnit(base or original, _MULT[head] if base else 0, original)
    return NormalizedUnit(original, 0, original)
```
`src/statbrief/ingest/regions.py`:
```python
"""Resolve region cells (codes, names, aliases, with or without diacritics) to region codes."""
import csv
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from statbrief.domain.observation import RegionLevel

_PREFIXES = ("judetul ", "jud. ", "jud ", "municipiul ")


@dataclass(frozen=True)
class RegionMatch:
    """A resolved region."""

    code: str
    level: RegionLevel
    name: str


def _key(s: str) -> str:
    """Normalised lookup key: lower-case, no diacritics, no county prefixes, hyphens as spaces."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    s = " ".join(s.replace("-", " ").split())
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return s


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, RegionMatch], dict[str, RegionMatch]]:
    """(by_code, by_name) lookups built once from the bundled regions.csv."""
    by_code: dict[str, RegionMatch] = {}
    by_name: dict[str, RegionMatch] = {}
    text = resources.files("statbrief.ingest.data").joinpath("regions.csv").read_text(encoding="utf-8")
    for row in csv.DictReader(text.splitlines()):
        m = RegionMatch(row["code"], row["level"], row["name"])
        by_code[m.code.upper()] = m
        for alias in [m.name, *[a for a in (row["aliases"] or "").split(";") if a]]:
            target = by_code.get(alias.upper()) if alias.upper() in by_code and alias.isupper() else None
            by_name[_key(alias)] = target or m
        for alias in (row["aliases"] or "").split(";"):
            if alias and alias.isupper() and len(alias) <= 3:
                by_code.setdefault(alias.upper(), m)  # code aliases such as GR→EL, GB→UK
    return by_code, by_name


def resolve_region(raw: Any) -> RegionMatch | None:
    """RegionMatch for a code or name, or None when unknown (never guessed)."""
    if raw is None or isinstance(raw, bool) or not str(raw).strip():
        return None
    s = str(raw).strip()
    by_code, by_name = _index()
    return by_code.get(s.upper()) or by_name.get(_key(s))
```
`src/statbrief/ingest/data/__init__.py`: `"""Bundled reference data for ingestion (region codes)."""`
- [ ] **Step 5: Run** all four test files → pass. **Step 6: Commit** `feat(m2): cell parsers for periods, numbers, units and regions`.

---

### Task 5: Schema detection (AC 12)

**Files:** Create `src/statbrief/ingest/detect.py`, `tests/unit/ingest/test_detect.py`. Modify `src/statbrief/config.py` (+ `.env.example`): add
```python
    detect_sample_rows: int = Field(200, ge=10)        # rows sampled per column for detection
    detect_header_scan_rows: int = Field(20, ge=1)     # grid rows scanned to find the header
    mapping_min_confidence: float = Field(0.8, ge=0.0, le=1.0)  # below → user must confirm the mapping
```
**Interfaces:**
- Consumes: `Grid`, `RawTable`, `to_table`, cell parsers, `DetectedSchema`, `ColumnGuess`.
- Produces: `detect_header_row(grid: Grid, scan_rows: int) -> int` (original row number); `detect_schema(grid: Grid, *, sample_rows: int, scan_rows: int, known_columns: frozenset[str] = frozenset()) -> DetectedSchema`.

Algorithm (put in module docstring):
1. Header row = first of the first `scan_rows` grid rows with ≥ 2 non-empty cells where ≥ 60 % of non-empty cells are non-numeric strings **and** the next grid row has ≥ 2 non-empty cells; fallback: first grid row.
2. For each column take up to `sample_rows` non-empty values. Content shares: `date` = share parseable by `parse_period` (with inferred day order / Excel-serial flag), `region` = share resolved, `value` = share numeric (excluding pure years when the column is date-like), `unit` = share where `normalize_unit` returns a canonical code (`%`, `pp`, `EUR`, `RON`, `USD`, `PERS`, `NR`, `I..`), `indicator` = text column with distinct/total ≤ 0.5 that is not region/date-like.
3. Name score: header tokens (folded, split on non-letters) matched against hints — date: `date, data, period, perioada, luna, month, an, year, time, time_period`; region: `region, regiune, judet, county, country, tara, geo, nuts`; indicator: `indicator, indicatorul, series, serie, variable, variabila, measure`; value: `value, valoare, obs_value, amount, val`; unit: `unit, unitate, um, unit_measure, measure_unit`. Exact token = 1.0, substring = 0.6.
4. Score = 0.6 × content + 0.4 × name for each role; assign roles greedily by descending score (each role once, score ≥ 0.3); others → `ignore`, with `note="undocumented"` when the column is not in `known_columns`.
5. `frequency` = most common frequency in the date column; `day_order`, `excel_serial`, `decimal` from the date/value samples; `meta` copies grid meta (delimiter, encoding).

- [ ] **Step 1: Failing tests** — `tests/unit/ingest/test_detect.py`:
```python
"""Tests for automatic header/role detection."""
from datetime import datetime

from statbrief.ingest.detect import detect_header_row, detect_schema
from statbrief.ingest.readers import read_grid
from tests.fixtures import csv_bytes, xlsx_bytes

KW = dict(sample_rows=200, scan_rows=20)


def _irs_grid():
    """IRS-like sheet: title rows, Romanian headers, an undocumented column."""
    rows = [["Institutul Român de Statistică — Inflație regională"], [None],
            ["luna", "judet", "indicator", "valoare", "um", "val_adj_x"]]
    for m in (1, 2, 3):
        for county, v in (("Cluj", 7.4), ("Timiș", 7.0), ("Iași", 7.9)):
            rows.append([datetime(2024, m, 1), county, "HICP_YOY", v - m / 10, "%", 1.01])
    return read_grid(xlsx_bytes({"Inflation": rows}), "xlsx")


def test_header_row_skips_title_rows():
    """The title and blank rows above the header are skipped."""
    assert detect_header_row(_irs_grid(), scan_rows=20) == 3


def test_roles_detected_with_confidence_and_undocumented_flag():
    """All five roles are found on Romanian headers; unknown columns are flagged."""
    s = detect_schema(_irs_grid(), **KW)
    roles = {c.column: c.role for c in s.columns}
    assert roles == {"luna": "date", "judet": "region", "indicator": "indicator", "valoare": "value",
                     "um": "unit", "val_adj_x": "ignore"}
    assert next(c for c in s.columns if c.column == "val_adj_x").note == "undocumented"
    assert s.frequency == "M" and s.header_row == 3 and s.worksheet == "Inflation"
    m = s.suggested_mapping()
    assert m is not None and m.value_col == "valoare"
    assert not s.needs_review(0.5)


def test_known_columns_not_flagged():
    """A column described in a data dictionary is not flagged as undocumented."""
    s = detect_schema(_irs_grid(), known_columns=frozenset({"val_adj_x"}), **KW)
    assert next(c for c in s.columns if c.column == "val_adj_x").note is None


def test_csv_decimal_comma_quarterly_english_headers():
    """English headers, quarterly periods and decimal commas are detected."""
    rows = [["Period", "Country", "Value", "Unit"]] + [[f"2023-Q{q}", "RO", f"{5 + q},5", "%"] for q in (1, 2, 3, 4)]
    s = detect_schema(read_grid(csv_bytes(rows, delimiter=";"), "csv"), **KW)
    assert {c.column: c.role for c in s.columns} == {"Period": "date", "Country": "region", "Value": "value",
                                                     "Unit": "unit"}
    assert s.frequency == "Q" and s.decimal == "," and s.meta["delimiter"] == ";"
    assert s.suggested_mapping() is None  # no indicator column → user must set a constant


def test_ambiguous_day_order_left_undecided():
    """dd/mm vs mm/dd that never disambiguates leaves day_order None (user decides)."""
    rows = [["date", "value"], ["01/02/2024", "1"], ["03/04/2024", "2"]]
    s = detect_schema(read_grid(csv_bytes(rows), "csv"), **KW)
    assert s.day_order is None
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `src/statbrief/ingest/detect.py`:
```python
"""Detect the header row and column roles of a grid, with confidence scores (see plan Task 5 algorithm)."""
import re
import unicodedata
from collections import Counter
from typing import Any

from statbrief.domain.dataset import ColumnGuess, DetectedSchema
from statbrief.ingest.numbers import NumberError, infer_decimal, parse_number
from statbrief.ingest.periods import PeriodError, infer_day_order, looks_like_excel_serial, parse_period
from statbrief.ingest.readers import FORMULA_NO_VALUE, Grid, to_table
from statbrief.ingest.regions import resolve_region
from statbrief.ingest.units import normalize_unit

_ROLES = ("date", "region", "indicator", "value", "unit")
_HINTS = {
    "date": ("date", "data", "period", "perioada", "luna", "month", "an", "year", "time", "time_period"),
    "region": ("region", "regiune", "judet", "county", "country", "tara", "geo", "nuts"),
    "indicator": ("indicator", "indicatorul", "series", "serie", "variable", "variabila", "measure"),
    "value": ("value", "valoare", "obs_value", "amount", "val"),
    "unit": ("unit", "unitate", "um", "unit_measure", "measure_unit"),
}
_CANONICAL_UNITS = {"%", "pp", "EUR", "RON", "USD", "PERS", "NR"}
_HEADER_TEXT_SHARE = 0.6        # share of non-numeric cells that marks a header row
_EXCEL_SERIAL_SHARE = 0.8       # share of serial-looking values that marks an Excel-serial date column
_INDICATOR_MAX_DISTINCT = 0.5   # distinct/total ratio below which a text column looks categorical
_CONTENT_W, _NAME_W = 0.6, 0.4  # score weights
_MIN_ROLE_SCORE = 0.3


def _fold(s: str) -> str:
    """Lower-case without diacritics."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _is_text(v: Any) -> bool:
    """Non-empty, non-numeric string."""
    if not isinstance(v, str) or not v.strip():
        return False
    try:
        parse_number(v)
    except NumberError:
        return True
    return parse_number(v) is None and v.strip() not in ("", "-")


def detect_header_row(grid: Grid, scan_rows: int) -> int:
    """Original row number of the most likely header row."""
    rows = grid.rows[:scan_rows]
    for i, row in enumerate(rows):
        filled = [c for c in row if c is not None and str(c).strip()]
        nxt = grid.rows[i + 1] if i + 1 < len(grid.rows) else []
        if len(filled) >= 2 and sum(_is_text(c) for c in filled) / len(filled) >= _HEADER_TEXT_SHARE \
                and sum(c is not None and str(c).strip() != "" for c in nxt) >= 2:
            return grid.row_numbers[i]
    return grid.row_numbers[0]


def _name_score(column: str, role: str) -> float:
    """1.0 for an exact hint token, 0.6 for a substring hint, else 0."""
    folded = _fold(column)
    tokens = set(re.split(r"[^a-z0-9_]+", folded))
    if tokens & set(_HINTS[role]):
        return 1.0
    return 0.6 if any(h in folded for h in _HINTS[role] if len(h) > 3) else 0.0


def _share(values: list[Any], ok) -> float:
    """Share of values for which ok(v) is true."""
    return sum(1 for v in values if ok(v)) / len(values) if values else 0.0


def _content_scores(values: list[Any], day_order, excel_serial: bool) -> dict[str, float]:
    """Per-role content evidence for one column's sample."""
    def is_date(v):
        try:
            parse_period(v, day_order=day_order, excel_serial=excel_serial)
            return True
        except PeriodError as exc:
            return exc.code == "ambiguous_date"

    def is_num(v):
        try:
            return parse_number(v) is not None
        except NumberError:
            return False

    def is_unit(v):
        u = normalize_unit(v)
        return bool(u) and (u.code in _CANONICAL_UNITS or u.code.startswith("I"))

    date = _share(values, is_date)
    region = _share(values, lambda v: resolve_region(v) is not None)
    text = [v for v in values if isinstance(v, str)]
    distinct = len(set(text)) / len(text) if text else 1.0
    categorical = 1.0 if text and distinct <= _INDICATOR_MAX_DISTINCT and region < 0.5 and date < 0.5 else 0.0
    value = _share(values, is_num) * (0.4 if date > 0.8 else 1.0)  # year columns are numeric but are dates
    return {"date": date, "region": region, "indicator": categorical, "value": value, "unit": _share(values, is_unit)}


def detect_schema(grid: Grid, *, sample_rows: int, scan_rows: int,
                  known_columns: frozenset[str] = frozenset()) -> DetectedSchema:
    """Detect header row, column roles, frequency and parse options for a grid."""
    header = detect_header_row(grid, scan_rows)
    table = to_table(grid, header)
    samples = {
        col: [r[i] for r in table.rows if r[i] is not None and r[i] is not FORMULA_NO_VALUE
              and str(r[i]).strip()][:sample_rows]
        for i, col in enumerate(table.columns)
    }
    candidates = []
    flags = {}
    for col, values in samples.items():
        day_order = infer_day_order(values)
        serial = looks_like_excel_serial(values, _EXCEL_SERIAL_SHARE)
        flags[col] = (day_order, serial)
        content = _content_scores(values, day_order, serial)
        for role in _ROLES:
            candidates.append((_CONTENT_W * content[role] + _NAME_W * _name_score(col, role), col, role))
    assigned: dict[str, tuple[str, float]] = {}
    for score, col, role in sorted(candidates, key=lambda t: -t[0]):
        if score < _MIN_ROLE_SCORE or col in assigned or role in {r for r, _ in assigned.values()}:
            continue
        assigned[col] = (role, score)
    guesses = []
    for col in table.columns:
        if col in assigned:
            role, score = assigned[col]
            guesses.append(ColumnGuess(column=col, role=role, confidence=round(min(score, 1.0), 3)))
        else:
            guesses.append(ColumnGuess(column=col, role="ignore", confidence=0.0,
                                       note=None if col in known_columns else "undocumented"))
    date_col = next((c.column for c in guesses if c.role == "date"), None)
    value_col = next((c.column for c in guesses if c.role == "value"), None)
    frequency = day_order = None
    serial = False
    if date_col:
        day_order, serial = flags[date_col]
        freqs = Counter()
        for v in samples[date_col]:
            try:
                freqs[parse_period(v, day_order=day_order, excel_serial=serial).frequency] += 1
            except PeriodError:
                pass
        frequency = freqs.most_common(1)[0][0] if freqs else None
    decimal = infer_decimal(samples[value_col]) if value_col else None
    return DetectedSchema(worksheet=grid.worksheet, header_row=header, columns=guesses, frequency=frequency,
                          day_order=day_order, excel_serial=serial, decimal=decimal, meta=dict(grid.meta))
```
- [ ] **Step 4: Run** → pass; if a role assignment test fails, tune only the documented weights/thresholds (keep them as named constants) and note it in the report. **Step 5: Commit** `feat(m2): header and column-role detection with confidence`.

---

### Task 6: Row parsing and validation (AC 15–16)

**Files:** Create `src/statbrief/ingest/rows.py`, `src/statbrief/ingest/validate.py`, tests `tests/unit/ingest/test_validate.py`.

**Interfaces:**
- Consumes: `RawTable`, `ColumnMapping`, cell parsers, `FORMULA_NO_VALUE`, `ValidationIssue`, `ValidationReport`.
- Produces:
  - `RowProblem(code: IssueCode, column: str | None, message: str, severity: Literal["error","warning"])`
  - `ParsedRow(row_number: int, period: ParsedPeriod | None, region: RegionMatch | None, indicator: str | None, unit: NormalizedUnit | None, value: float | None, problems: list[RowProblem])` with `.ok` (no error problems)
  - `parse_rows(table: RawTable, mapping: ColumnMapping) -> list[ParsedRow]`
  - `validate_table(table: RawTable, mapping: ColumnMapping, file_id: str, known_columns: frozenset[str] = frozenset()) -> ValidationReport`

Rules:
- Row-level (in `parse_rows`): `FORMULA_NO_VALUE` in a mapped cell → `formula_without_value` (error); date → `invalid_date` / `ambiguous_date` (error), using `mapping.date_format` (`DMY`/`MDY` → day order, `excel_serial` → flag); region (column or constant) → `unknown_region` (error) or `missing_required` (error) when empty; indicator empty → `missing_required`; unit empty → `missing_required`; value → `non_numeric` (error), None → `missing_value` (warning, row kept as a gap).
- Table-level (in `validate_table`): exact duplicate rows (same period, region, indicator, unit, value) → `duplicate_row` (warning; rows of the 2nd+ occurrences); same (indicator, region, period) with different values → `duplicate_key_conflict` (error; all rows involved); within an indicator, >1 canonical unit code → `inconsistent_unit` (error); same canonical code but different raw spellings → `inconsistent_unit` (warning, message lists spellings); >1 frequency within an indicator → `frequency_mismatch` (error); every column that is not mapped, not in `mapping.column_descriptions` and not in `known_columns` → `undocumented_column` (warning, no rows).
- One `ValidationIssue` per (code, column); `issue_id = f"{code}:{column or '-'}"` (plus `:<indicator>` for per-indicator unit/frequency issues); rows sorted, unique.

- [ ] **Step 1: Failing tests** — `tests/unit/ingest/test_validate.py`:
```python
"""Tests for row parsing and table validation."""
from datetime import datetime

from statbrief.domain.dataset import ColumnMapping
from statbrief.ingest.readers import FORMULA_NO_VALUE, RawTable
from statbrief.ingest.rows import parse_rows
from statbrief.ingest.validate import validate_table

COLS = ["luna", "judet", "indicator", "valoare", "um", "val_adj_x"]
MAP = ColumnMapping(date_col="luna", region_col="judet", indicator_col="indicator", value_col="valoare",
                    unit_col="um", header_row=3)


def _table(rows):
    """Table with row numbers starting at 4 (header on row 3)."""
    return RawTable(columns=COLS, rows=rows, row_numbers=list(range(4, 4 + len(rows))), worksheet="Inflation")


def _issues(report):
    """{issue_id: (severity, rows)}."""
    return {i.issue_id: (i.severity, i.rows) for i in report.issues}


def test_clean_table_only_undocumented_column():
    """A clean table yields just the undocumented-column warning."""
    t = _table([[datetime(2024, 1, 1), "Cluj", "HICP", 7.4, "%", 1.0],
                [datetime(2024, 2, 1), "Cluj", "HICP", 7.1, "%", 1.0]])
    r = validate_table(t, MAP, "f1")
    assert _issues(r) == {"undocumented_column:val_adj_x": ("warning", [])}
    assert not r.blocking and r.row_count == 2
    rows = parse_rows(t, MAP)
    assert rows[0].ok and rows[0].period.period == "2024-01" and rows[0].region.code == "RO113"


def test_every_problem_type_is_reported_with_row_numbers():
    """Each documented issue code is produced with original row numbers; nothing dropped silently."""
    t = _table([
        ["2024-13", "Cluj", "HICP", "7,4", "%", 1],          # 4 invalid date
        ["01/02/2024", "Cluj", "HICP", "7,1", "%", 1],       # 5 ambiguous date
        ["2024-03", "Atlantis", "HICP", "6,6", "%", 1],      # 6 unknown region
        ["2024-03", "Cluj", "HICP", "abc", "%", 1],          # 7 non numeric
        ["2024-04", "Cluj", "HICP", "n/a", "%", 1],          # 8 missing value (warning)
        ["2024-05", "Cluj", "HICP", FORMULA_NO_VALUE, "%", 1],  # 9 formula without value
        ["2024-06", "Cluj", "", "6,0", "%", 1],              # 10 missing indicator
        ["2024-07", "Cluj", "HICP", "5,9", "%", 1],          # 11
        ["2024-07", "Cluj", "HICP", "5,9", "%", 1],          # 12 exact duplicate of 11
        ["2024-08", "Cluj", "HICP", "5,8", "%", 1],          # 13
        ["2024-08", "Cluj", "HICP", "5,1", "%", 1],          # 14 conflicting value for same key
        ["2024-09", "Cluj", "HICP", "5,7", "percent", 1],    # 15 spelling variant of %
        ["2024-10", "Cluj", "HICP", "5,6", "EUR", 1],        # 16 different unit
        ["2024", "Cluj", "HICP", "5,5", "%", 1],             # 17 annual inside monthly indicator
    ])
    issues = _issues(validate_table(t, MAP, "f1"))
    assert issues["invalid_date:luna"] == ("error", [4])
    assert issues["ambiguous_date:luna"] == ("error", [5])
    assert issues["unknown_region:judet"] == ("error", [6])
    assert issues["non_numeric:valoare"] == ("error", [7])
    assert issues["missing_value:valoare"] == ("warning", [8])
    assert issues["formula_without_value:valoare"] == ("error", [9])
    assert issues["missing_required:indicator"] == ("error", [10])
    assert issues["duplicate_row:-"] == ("warning", [12])
    assert issues["duplicate_key_conflict:-"] == ("error", [13, 14])
    assert issues["inconsistent_unit:um:HICP"][0] == "error"
    assert issues["frequency_mismatch:luna:HICP"][0] == "error"
    assert "undocumented_column:val_adj_x" in issues


def test_constants_and_date_format_options():
    """Constants replace missing columns; DMY resolves ambiguity; descriptions document columns."""
    m = ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "UNEMP", "unit": "%"},
                      date_format="DMY", decimal=",", column_descriptions={"note": "free text"})
    t = RawTable(columns=["d", "v", "note"], rows=[["01/02/2024", "5,4", "x"]], row_numbers=[2])
    r = validate_table(t, m, "f1")
    assert r.issues == [] and parse_rows(t, m)[0].period.period == "2024-02"


def test_spelling_only_unit_variation_is_warning():
    """'%' vs 'percent' within one indicator is a warning, not an error."""
    t = _table([["2024-01", "Cluj", "HICP", "1", "%", 1], ["2024-02", "Cluj", "HICP", "2", "percent", 1]])
    issues = _issues(validate_table(t, MAP, "f1", known_columns=frozenset({"val_adj_x"})))
    assert issues == {"inconsistent_unit:um:HICP": ("warning", [4, 5])}
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — `src/statbrief/ingest/rows.py`:
```python
"""Parse table rows under a mapping into typed values plus per-row problems (shared by validation and normalization)."""
from dataclasses import dataclass, field
from typing import Any, Literal

from statbrief.domain.dataset import ColumnMapping, IssueCode
from statbrief.ingest.numbers import NumberError, parse_number
from statbrief.ingest.periods import ParsedPeriod, PeriodError, parse_period
from statbrief.ingest.readers import FORMULA_NO_VALUE, RawTable
from statbrief.ingest.regions import RegionMatch, resolve_region
from statbrief.ingest.units import NormalizedUnit, normalize_unit


@dataclass(frozen=True)
class RowProblem:
    """One problem found in one row."""

    code: IssueCode
    column: str | None
    message: str
    severity: Literal["error", "warning"] = "error"


@dataclass
class ParsedRow:
    """Typed values of one source row and its problems."""

    row_number: int
    period: ParsedPeriod | None = None
    region: RegionMatch | None = None
    indicator: str | None = None
    unit: NormalizedUnit | None = None
    value: float | None = None
    problems: list[RowProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the row has no error problems (warnings allowed)."""
        return not any(p.severity == "error" for p in self.problems)


def _cell(table: RawTable, row: list[Any], column: str | None) -> Any:
    """Cell value for a mapped column (None when unmapped)."""
    return row[table.column_index(column)] if column else None


def _blank(v: Any) -> bool:
    """None or whitespace-only."""
    return v is None or (isinstance(v, str) and not v.strip())


def parse_rows(table: RawTable, mapping: ColumnMapping) -> list[ParsedRow]:
    """Parse every data row; never drops a row (problems are attached instead)."""
    day_order = mapping.date_format if mapping.date_format in ("DMY", "MDY") else None
    excel_serial = mapping.date_format == "excel_serial"
    decimal = None if mapping.decimal == "auto" else mapping.decimal
    out = []
    for raw, rn in zip(table.rows, table.row_numbers):
        pr = ParsedRow(row_number=rn)
        cells = {c: _cell(table, raw, c) for c in mapping.mapped_columns()}
        for col, v in cells.items():
            if v is FORMULA_NO_VALUE:
                pr.problems.append(RowProblem("formula_without_value", col,
                                              "Formula without a saved value (formulas are never calculated)."))
        if cells[mapping.date_col] is not FORMULA_NO_VALUE:
            try:
                pr.period = parse_period(cells[mapping.date_col], day_order=day_order, excel_serial=excel_serial)
            except PeriodError as exc:
                pr.problems.append(RowProblem(exc.code, mapping.date_col, str(exc)))
        region_raw = cells.get(mapping.region_col) if mapping.region_col else mapping.constants.get("region")
        if region_raw is not FORMULA_NO_VALUE:
            if _blank(region_raw):
                pr.problems.append(RowProblem("missing_required", mapping.region_col, "Region is empty."))
            elif (match := resolve_region(region_raw)) is None:
                pr.problems.append(RowProblem("unknown_region", mapping.region_col,
                                              f"Unknown region {str(region_raw).strip()!r}."))
            else:
                pr.region = match
        ind = cells.get(mapping.indicator_col) if mapping.indicator_col else mapping.constants.get("indicator")
        if ind is not FORMULA_NO_VALUE:
            if _blank(ind):
                pr.problems.append(RowProblem("missing_required", mapping.indicator_col, "Indicator is empty."))
            else:
                pr.indicator = str(ind).strip()
        unit_raw = cells.get(mapping.unit_col) if mapping.unit_col else mapping.constants.get("unit")
        if unit_raw is not FORMULA_NO_VALUE:
            pr.unit = normalize_unit(unit_raw)
            if pr.unit is None:
                pr.problems.append(RowProblem("missing_required", mapping.unit_col, "Unit is empty."))
        v = cells[mapping.value_col]
        if v is not FORMULA_NO_VALUE:
            try:
                pr.value = parse_number(v, decimal)
                if pr.value is None:
                    pr.problems.append(RowProblem("missing_value", mapping.value_col, "Value is missing.", "warning"))
            except NumberError:
                pr.problems.append(RowProblem("non_numeric", mapping.value_col, f"{v!r} is not a number."))
        out.append(pr)
    return out
```
`src/statbrief/ingest/validate.py`:
```python
"""Validate a table under a mapping: row problems + table-level checks → grouped, explicit issues."""
from collections import defaultdict

from statbrief.domain.dataset import ColumnMapping, ValidationIssue, ValidationReport
from statbrief.ingest.readers import RawTable
from statbrief.ingest.rows import ParsedRow, parse_rows


class _Collector:
    """Groups problems into one issue per (issue_id)."""

    def __init__(self):
        """Start empty."""
        self.issues: dict[str, dict] = {}

    def add(self, issue_id, code, severity, column, message, rows=()):
        """Add rows to an issue, creating it on first use."""
        it = self.issues.setdefault(issue_id, {"code": code, "severity": severity, "column": column,
                                               "message": message, "rows": set()})
        it["rows"].update(rows)

    def report(self, file_id: str, row_count: int) -> ValidationReport:
        """Freeze into a ValidationReport (rows sorted)."""
        return ValidationReport(file_id=file_id, row_count=row_count, issues=[
            ValidationIssue(issue_id=iid, code=d["code"], severity=d["severity"], column=d["column"],
                            message=d["message"], rows=sorted(d["rows"]))
            for iid, d in self.issues.items()])


def _row_issues(rows: list[ParsedRow], col: _Collector) -> None:
    """One issue per (code, column) from row problems."""
    for r in rows:
        for p in r.problems:
            col.add(f"{p.code}:{p.column or '-'}", p.code, p.severity, p.column, p.message, [r.row_number])


def _duplicate_issues(rows: list[ParsedRow], col: _Collector) -> None:
    """Exact duplicates (warning) and same-key/different-value conflicts (error)."""
    by_key: dict[tuple, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.ok and r.period and r.region and r.indicator:
            by_key[(r.indicator, r.region.code, r.period.period)].append(r)
    for group in by_key.values():
        if len(group) < 2:
            continue
        values = {(g.value, g.unit.code if g.unit else None) for g in group}
        if len(values) == 1:
            col.add("duplicate_row:-", "duplicate_row", "warning", None,
                    "Identical rows: only the first occurrence will be used.", [g.row_number for g in group[1:]])
        else:
            col.add("duplicate_key_conflict:-", "duplicate_key_conflict", "error", None,
                    "Different values for the same indicator, region and period.", [g.row_number for g in group])


def _per_indicator_issues(rows: list[ParsedRow], mapping: ColumnMapping, col: _Collector) -> None:
    """Unit and frequency consistency within each indicator."""
    groups: dict[str, list[ParsedRow]] = defaultdict(list)
    for r in rows:
        if r.indicator:
            groups[r.indicator].append(r)
    for ind, group in groups.items():
        with_unit = [r for r in group if r.unit]
        codes = {r.unit.code for r in with_unit}
        spellings = {r.unit.raw for r in with_unit}
        if len(codes) > 1:
            col.add(f"inconsistent_unit:{mapping.unit_col or '-'}:{ind}", "inconsistent_unit", "error",
                    mapping.unit_col, f"{ind} uses different units: {', '.join(sorted(codes))}.",
                    [r.row_number for r in with_unit])
        elif len(spellings) > 1:
            col.add(f"inconsistent_unit:{mapping.unit_col or '-'}:{ind}", "inconsistent_unit", "warning",
                    mapping.unit_col, f"{ind}: spellings {', '.join(sorted(spellings))} mean the same unit; "
                    "confirm to treat them as one.", [r.row_number for r in with_unit])
        freqs = {r.period.frequency for r in group if r.period}
        if len(freqs) > 1:
            col.add(f"frequency_mismatch:{mapping.date_col}:{ind}", "frequency_mismatch", "error", mapping.date_col,
                    f"{ind} mixes frequencies ({', '.join(sorted(freqs))}).",
                    [r.row_number for r in group if r.period])


def validate_table(table: RawTable, mapping: ColumnMapping, file_id: str,
                   known_columns: frozenset[str] = frozenset()) -> ValidationReport:
    """All issues for a table under a mapping; rows are referenced by original row number."""
    rows = parse_rows(table, mapping)
    col = _Collector()
    _row_issues(rows, col)
    _duplicate_issues(rows, col)
    _per_indicator_issues(rows, mapping, col)
    documented = mapping.mapped_columns() | set(mapping.column_descriptions) | set(known_columns)
    for c in table.columns:
        if c not in documented:
            col.add(f"undocumented_column:{c}", "undocumented_column", "warning", c,
                    f"Column {c!r} has no documented meaning; it will be ignored unless you describe it.")
    return col.report(file_id, len(table.rows))
```
- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(m2): row parsing and validation with explicit grouped issues`.

---

### Task 7: Normalization with provenance (AC 16, 18, 19)

**Files:** Create `src/statbrief/ingest/normalize.py`, `tests/unit/ingest/test_normalize.py`.

**Interfaces:**
- Consumes: `parse_rows`, `ValidationReport`, `DatasetRef`, `Observation`, `UploadProvenance`, `make_obs_id`.
- Produces: `class UnresolvedIssues(Exception)` (`.issue_ids: list[str]`); `ExcludedRow(row_number: int, reasons: list[str])`; `NormalizationResult(observations: list[Observation], excluded: list[ExcludedRow])`; `to_observations(table, mapping, ref: DatasetRef, report: ValidationReport) -> NormalizationResult`.

Rules: raise `UnresolvedIssues` if `report.blocking`. A row is excluded (and listed with every reason) when it has any error problem, or is a later occurrence in a `duplicate_row` issue, or belongs to an error issue resolved as `excluded` (e.g. `duplicate_key_conflict`, `inconsistent_unit`, `frequency_mismatch`). Error issues resolved as `accepted` are not allowed to keep rows that cannot be parsed (row-level errors always exclude). `obs_id = make_obs_id(ref.file_id, row_number, indicator, region_code, period)`; provenance `UploadProvenance(file_id, filename, worksheet, row_number, column=mapping.value_col)`.

- [ ] **Step 1: Failing tests** — `tests/unit/ingest/test_normalize.py`:
```python
"""Tests for normalization into provenance-tracked observations."""
import pytest

from statbrief.domain.dataset import ColumnMapping, DatasetRef
from statbrief.ingest.normalize import UnresolvedIssues, to_observations
from statbrief.ingest.readers import RawTable
from statbrief.ingest.validate import validate_table

MAP = ColumnMapping(date_col="luna", region_col="judet", indicator_col="ind", value_col="v", unit_col="um",
                    worksheet="Inflation", header_row=3)
REF = DatasetRef(file_id="f1", report_id="r1", user_id="u1", filename="IRS.xlsx", kind="xlsx", size_bytes=10,
                 sha256="x", blob_key="b")


def _t(rows):
    """Table with rows numbered from 4."""
    return RawTable(columns=["luna", "judet", "ind", "v", "um"], rows=rows,
                    row_numbers=list(range(4, 4 + len(rows))), worksheet="Inflation")


def test_observations_carry_full_provenance():
    """Each observation points to file, sheet, row and column."""
    t = _t([["2024-01", "Cluj", "HICP", "7,4", "%"], ["2024-02", "Iași", "HICP", "n/a", "%"]])
    res = to_observations(t, MAP, REF, validate_table(t, MAP, "f1"))
    o = res.observations[0]
    assert (o.region_code, o.region_level, o.period, o.frequency, o.value, o.unit) == \
        ("RO113", "nuts3", "2024-01", "M", 7.4, "%")
    p = o.provenance
    assert (p.file_id, p.filename, p.worksheet, p.row_number, p.column) == ("f1", "IRS.xlsx", "Inflation", 4, "v")
    assert res.observations[1].value is None  # gap kept, not dropped
    assert res.excluded == []


def test_blocking_report_refuses():
    """Unresolved errors stop normalization."""
    t = _t([["2024-13", "Cluj", "HICP", "1", "%"]])
    with pytest.raises(UnresolvedIssues) as exc:
        to_observations(t, MAP, REF, validate_table(t, MAP, "f1"))
    assert exc.value.issue_ids == ["invalid_date:luna"]


def test_excluded_rows_listed_with_reasons_and_duplicates_dropped():
    """Explicitly excluded and duplicate rows are listed, never silently discarded."""
    t = _t([["2024-13", "Cluj", "HICP", "1", "%"], ["2024-02", "Cluj", "HICP", "2", "%"],
            ["2024-02", "Cluj", "HICP", "2", "%"]])
    report = validate_table(t, MAP, "f1").with_resolution("invalid_date:luna", "excluded")
    res = to_observations(t, MAP, REF, report)
    assert [o.provenance.row_number for o in res.observations] == [5]
    assert [(e.row_number, e.reasons[0].split(":")[0]) for e in res.excluded] == \
        [(4, "invalid_date"), (6, "duplicate_row")]


def test_obs_ids_unique_and_stable():
    """Re-normalizing yields the same ids; ids differ per row."""
    t = _t([["2024-01", "Cluj", "HICP", "1", "%"], ["2024-02", "Cluj", "HICP", "2", "%"]])
    r = validate_table(t, MAP, "f1")
    a, b = to_observations(t, MAP, REF, r), to_observations(t, MAP, REF, r)
    assert [o.obs_id for o in a.observations] == [o.obs_id for o in b.observations]
    assert len({o.obs_id for o in a.observations}) == 2
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `src/statbrief/ingest/normalize.py`:
```python
"""Turn validated rows into Observations with upload provenance; every exclusion is listed with reasons."""
from dataclasses import dataclass, field

from statbrief.domain.dataset import ColumnMapping, DatasetRef, ValidationReport
from statbrief.domain.observation import Observation, UploadProvenance, make_obs_id
from statbrief.ingest.readers import RawTable
from statbrief.ingest.rows import parse_rows


class UnresolvedIssues(Exception):
    """Normalization was attempted while errors are still pending."""

    def __init__(self, issue_ids: list[str]):
        """Store the pending error issue ids."""
        super().__init__(f"unresolved issues: {', '.join(issue_ids)}")
        self.issue_ids = issue_ids


@dataclass(frozen=True)
class ExcludedRow:
    """A source row not turned into an observation, with every reason."""

    row_number: int
    reasons: list[str]


@dataclass
class NormalizationResult:
    """Observations plus the explicit list of excluded rows."""

    observations: list[Observation] = field(default_factory=list)
    excluded: list[ExcludedRow] = field(default_factory=list)


def to_observations(table: RawTable, mapping: ColumnMapping, ref: DatasetRef,
                    report: ValidationReport) -> NormalizationResult:
    """Normalize a table whose errors have all been resolved."""
    pending = [i.issue_id for i in report.issues if i.severity == "error" and i.resolution == "pending"]
    if pending:
        raise UnresolvedIssues(pending)
    reasons: dict[int, list[str]] = {}
    for issue in report.issues:
        drop = issue.code == "duplicate_row" or (issue.severity == "error" and issue.resolution == "excluded")
        if drop:
            for rn in issue.rows:
                reasons.setdefault(rn, []).append(f"{issue.code}: {issue.message}")
    result = NormalizationResult()
    for row in parse_rows(table, mapping):
        row_reasons = list(reasons.get(row.row_number, []))
        row_reasons += [f"{p.code}: {p.message}" for p in row.problems if p.severity == "error"
                        and not any(r.startswith(f"{p.code}:") for r in row_reasons)]
        if row_reasons or not row.ok:
            result.excluded.append(ExcludedRow(row.row_number, row_reasons))
            continue
        result.observations.append(Observation(
            obs_id=make_obs_id(ref.file_id, row.row_number, row.indicator, row.region.code, row.period.period),
            indicator_code=row.indicator, region_code=row.region.code, region_level=row.region.level,
            period=row.period.period, frequency=row.period.frequency, value=row.value,
            unit=row.unit.code, unit_multiplier=row.unit.multiplier,
            provenance=UploadProvenance(file_id=ref.file_id, filename=ref.filename, worksheet=table.worksheet,
                                        row_number=row.row_number, column=mapping.value_col),
        ))
    result.excluded.sort(key=lambda e: e.row_number)
    return result
```
- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(m2): normalization to provenance-tracked observations`.

---

### Task 8: Storage — tables, user-scoped repositories, blob stores

**Files:** Create `src/statbrief/storage/migrations/002_reports_files.sql`, `src/statbrief/storage/repos.py`, `src/statbrief/storage/blobs.py`, `tests/unit/test_repos.py`, `tests/unit/test_blobs.py`. Modify `src/statbrief/config.py` (+ `.env.example`):
```python
    blob_backend: Literal["local", "vercel"] = "local"
    local_blob_dir: str = ".local_blobs"
```
Add `.local_blobs/` to `.gitignore`.

**Interfaces — Produces:**
- `ReportRepo(pool)`: `create(user_id, title) -> dict`, `get(user_id, report_id) -> dict | None`, `list_for_user(user_id) -> list[dict]`, `touch(user_id, report_id) -> None`.
- `FileRepo(pool)`: `add(ref: DatasetRef) -> None`, `get(user_id, file_id) -> DatasetRef | None`, `list_for_report(user_id, report_id) -> list[DatasetRef]`, `count_for_report(user_id, report_id) -> int`, `set_status(user_id, file_id, status) -> None`, `delete(user_id, file_id) -> DatasetRef | None`.
- `UploadRepo(pool)`: `start(user_id, report_id, filename, total_bytes, chunk_count) -> str`, `get(user_id, upload_id) -> dict | None`, `put_chunk(user_id, upload_id, idx, data) -> None` (idempotent per idx), `assemble(user_id, upload_id) -> bytes` (raises `ValueError` if chunks missing), `delete(user_id, upload_id) -> None`.
- `MappingRepo(pool)`: `save(user_id, file_id, mapping: ColumnMapping)`, `get(user_id, file_id) -> ColumnMapping | None`.
- `ValidationRepo(pool)`: `save(user_id, report: ValidationReport)`, `get(user_id, file_id) -> ValidationReport | None`.
- `ObservationRepo(pool)`: `replace_for_file(user_id, report_id, file_id, observations) -> int`, `list_for_report(user_id, report_id) -> list[Observation]`.
- `BlobStore` Protocol: `put(key: str, data: bytes, content_type: str) -> str` (returns stored key), `get(key) -> bytes`, `delete(key) -> None`. `LocalBlobStore(root: Path)`, `VercelBlobStore()` (uses `vercel.blob.sync`: `put_blob(key, data, access="private", content_type=..., add_random_suffix=True).pathname`, `get_blob(key, access="private").body`, `delete_blob(key)`), `get_blob_store(settings) -> BlobStore`. Blob keys: `users/{user_id}/reports/{report_id}/{file_id}/{safe_filename}`.

- [ ] **Step 1: Migration** — `002_reports_files.sql`:
```sql
-- Reports, uploaded files (blob refs), chunked uploads, per-file mapping/validation, normalized observations.
CREATE TABLE IF NOT EXISTS reports (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            text NOT NULL,
    status           text NOT NULL DEFAULT 'draft',
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_activity_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reports_user ON reports (user_id, last_activity_at DESC);

CREATE TABLE IF NOT EXISTS files (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id   uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('csv', 'xlsx', 'xls', 'json')),
    size_bytes  bigint NOT NULL,
    sha256      text NOT NULL,
    blob_key    text NOT NULL,
    status      text NOT NULL DEFAULT 'uploaded',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS files_report ON files (report_id);

CREATE TABLE IF NOT EXISTS uploads (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id    uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    filename     text NOT NULL,
    total_bytes  bigint NOT NULL,
    chunk_count  int NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS upload_chunks (
    upload_id uuid NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    idx       int NOT NULL,
    data      bytea NOT NULL,
    PRIMARY KEY (upload_id, idx)
);

CREATE TABLE IF NOT EXISTS file_mappings (
    file_id    uuid PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    mapping    jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS validation_reports (
    file_id    uuid PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    report     jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS observations (
    report_id      uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    obs_id         text NOT NULL,
    file_id        uuid REFERENCES files(id) ON DELETE CASCADE,
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    indicator_code text NOT NULL,
    region_code    text NOT NULL,
    period         text NOT NULL,
    payload        jsonb NOT NULL,
    PRIMARY KEY (report_id, obs_id)
);
CREATE INDEX IF NOT EXISTS observations_key ON observations (report_id, indicator_code, region_code, period);
```
- [ ] **Step 2: Failing tests** — `tests/unit/test_repos.py`:
```python
"""Tests for user-scoped repositories (AC 6)."""
import pytest

from statbrief.auth.users import create_user
from statbrief.domain.dataset import ColumnMapping, DatasetRef, ValidationIssue, ValidationReport
from statbrief.domain.observation import Observation, UploadProvenance
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.repos import (FileRepo, MappingRepo, ObservationRepo, ReportRepo, UploadRepo,
                                     ValidationRepo)


@pytest.fixture
def ctx(settings):
    """Pool + two users (alice, bob) and alice's report."""
    bootstrap(settings)
    pool = create_pool(settings)
    a = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    b = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    rep = ReportRepo(pool).create(a.id, "Inflation Q1")
    yield pool, a, b, rep
    pool.close()


def _ref(user, rep, fid="00000000-0000-0000-0000-000000000001"):
    """A DatasetRef for tests."""
    return DatasetRef(file_id=fid, report_id=str(rep["id"]), user_id=user.id, filename="f.csv", kind="csv",
                      size_bytes=3, sha256="h", blob_key="k")


def test_reports_are_user_scoped(ctx):
    """Bob can neither see nor touch Alice's report."""
    pool, a, b, rep = ctx
    repos = ReportRepo(pool)
    assert repos.get(a.id, str(rep["id"]))["title"] == "Inflation Q1"
    assert repos.get(b.id, str(rep["id"])) is None
    assert [r["title"] for r in repos.list_for_user(b.id)] == []


def test_files_mapping_validation_scoped(ctx):
    """File, mapping and validation rows are only visible to their owner."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    assert FileRepo(pool).get(a.id, ref.file_id) == ref and FileRepo(pool).get(b.id, ref.file_id) is None
    assert FileRepo(pool).count_for_report(a.id, ref.report_id) == 1
    m = ColumnMapping(date_col="d", value_col="v", constants={"region": "RO", "indicator": "I", "unit": "%"})
    MappingRepo(pool).save(a.id, ref.file_id, m)
    assert MappingRepo(pool).get(a.id, ref.file_id) == m and MappingRepo(pool).get(b.id, ref.file_id) is None
    vr = ValidationReport(file_id=ref.file_id, row_count=1, issues=[ValidationIssue(
        issue_id="x:-", code="missing_value", severity="warning", rows=[2], message="m")])
    ValidationRepo(pool).save(a.id, vr)
    assert ValidationRepo(pool).get(a.id, ref.file_id) == vr and ValidationRepo(pool).get(b.id, ref.file_id) is None
    with pytest.raises(PermissionError):
        MappingRepo(pool).save(b.id, ref.file_id, m)


def test_chunked_upload_assembly(ctx):
    """Chunks arrive in any order, re-sends are idempotent, missing chunks are detected."""
    pool, a, b, rep = ctx
    up = UploadRepo(pool)
    uid = up.start(a.id, str(rep["id"]), "f.csv", total_bytes=6, chunk_count=3)
    up.put_chunk(a.id, uid, 2, b"ef")
    up.put_chunk(a.id, uid, 0, b"ab")
    with pytest.raises(ValueError, match="missing"):
        up.assemble(a.id, uid)
    up.put_chunk(a.id, uid, 1, b"cd")
    up.put_chunk(a.id, uid, 1, b"cd")
    assert up.assemble(a.id, uid) == b"abcdef"
    assert up.get(b.id, uid) is None
    with pytest.raises(PermissionError):
        up.put_chunk(b.id, uid, 0, b"zz")


def test_observations_replace_and_list(ctx):
    """Re-normalizing a file replaces its observations; listing is user-scoped."""
    pool, a, b, rep = ctx
    ref = _ref(a, rep)
    FileRepo(pool).add(ref)
    prov = UploadProvenance(file_id=ref.file_id, filename="f.csv", row_number=2, column="v")
    obs = [Observation(obs_id=f"obs_{i}", indicator_code="I", region_code="RO", region_level="country",
                       period=f"2024-0{i}", frequency="M", value=float(i), unit="%", provenance=prov)
           for i in (1, 2)]
    repo = ObservationRepo(pool)
    assert repo.replace_for_file(a.id, ref.report_id, ref.file_id, obs) == 2
    assert repo.replace_for_file(a.id, ref.report_id, ref.file_id, obs[:1]) == 1
    assert [o.obs_id for o in repo.list_for_report(a.id, ref.report_id)] == ["obs_1"]
    assert repo.list_for_report(b.id, ref.report_id) == []
```
`tests/unit/test_blobs.py`:
```python
"""Tests for blob stores (no network: the Vercel SDK is replaced by a fake)."""
from types import SimpleNamespace

import pytest

from statbrief.storage import blobs
from statbrief.storage.blobs import LocalBlobStore, VercelBlobStore, blob_key, get_blob_store


def test_local_roundtrip(tmp_path):
    """Local store writes, reads and deletes; keys cannot escape the root."""
    s = LocalBlobStore(tmp_path)
    k = s.put("users/u/reports/r/f/data.csv", b"abc", "text/csv")
    assert s.get(k) == b"abc"
    s.delete(k)
    with pytest.raises(FileNotFoundError):
        s.get(k)
    with pytest.raises(ValueError):
        s.put("../escape.txt", b"x", "text/plain")


def test_blob_key_sanitises_filename():
    """User filenames cannot inject path segments."""
    assert blob_key("u", "r", "f", "../../evil name.csv") == "users/u/reports/r/f/evil_name.csv"


def test_vercel_store_uses_private_access(monkeypatch):
    """The Vercel store always uploads/downloads with access='private'."""
    calls = []
    fake = SimpleNamespace(
        put_blob=lambda key, data, **kw: calls.append(("put", key, kw)) or SimpleNamespace(pathname=key + "-x1"),
        get_blob=lambda key, **kw: calls.append(("get", key, kw)) or SimpleNamespace(body=b"data"),
        delete_blob=lambda key, **kw: calls.append(("delete", key, kw)),
    )
    monkeypatch.setattr(blobs, "_vercel_sync", lambda: fake)
    s = VercelBlobStore()
    assert s.put("users/u/x.csv", b"d", "text/csv") == "users/u/x.csv-x1"
    assert s.get("users/u/x.csv-x1") == b"data"
    s.delete("users/u/x.csv-x1")
    assert calls[0][2]["access"] == "private" and calls[0][2]["add_random_suffix"] is True
    assert calls[1][2]["access"] == "private"


def test_get_blob_store_by_setting(settings, tmp_path):
    """The backend setting selects the implementation."""
    assert isinstance(get_blob_store(settings.model_copy(update={"local_blob_dir": str(tmp_path)})), LocalBlobStore)
    assert isinstance(get_blob_store(settings.model_copy(update={"blob_backend": "vercel"})), VercelBlobStore)
```
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement** `src/statbrief/storage/blobs.py`:
```python
"""Blob storage for uploaded files: local filesystem (dev/tests) or private Vercel Blob (production, OIDC)."""
import re
from pathlib import Path
from typing import Protocol

from statbrief.config import Settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class BlobStore(Protocol):
    """Minimal private object store."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return the key to read them back."""

    def get(self, key: str) -> bytes:
        """Read stored bytes (FileNotFoundError when absent)."""

    def delete(self, key: str) -> None:
        """Delete stored bytes (no error when absent)."""


def blob_key(user_id: str, report_id: str, file_id: str, filename: str) -> str:
    """Owner-scoped key with a sanitised filename (no path segments from user input)."""
    name = _SAFE.sub("_", filename.replace("\\", "/").rsplit("/", 1)[-1]).strip("._") or "upload"
    return f"users/{user_id}/reports/{report_id}/{file_id}/{name}"


class LocalBlobStore:
    """Files under a root directory; keys may not escape it."""

    def __init__(self, root: Path):
        """Use (and create) `root`."""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Absolute path for a key, refusing traversal."""
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("blob key escapes the storage root")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Write bytes to disk."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        """Read bytes from disk."""
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        """Remove the file if present."""
        self._path(key).unlink(missing_ok=True)


def _vercel_sync():
    """The synchronous Vercel Blob SDK module (imported lazily)."""
    from vercel.blob import sync

    return sync


class VercelBlobStore:
    """Private Vercel Blob store; credentials resolved by the SDK (BLOB_STORE_ID + Vercel OIDC)."""

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Upload privately with a random suffix; return the stored pathname."""
        result = _vercel_sync().put_blob(key, data, access="private", content_type=content_type,
                                         add_random_suffix=True)
        return result.pathname

    def get(self, key: str) -> bytes:
        """Download a private blob."""
        return _vercel_sync().get_blob(key, access="private").body

    def delete(self, key: str) -> None:
        """Delete a blob."""
        _vercel_sync().delete_blob(key)


def get_blob_store(settings: Settings) -> BlobStore:
    """Blob store selected by BLOB_BACKEND."""
    if settings.blob_backend == "vercel":
        return VercelBlobStore()
    return LocalBlobStore(Path(settings.local_blob_dir))
```
`src/statbrief/storage/repos.py`:
```python
"""User-scoped repositories for reports, files, uploads, mappings, validation reports and observations.

Every read takes `user_id` and returns None/empty for other users' rows; writes to rows the user does not
own raise PermissionError (spec AC 6).
"""
import uuid

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from statbrief.domain.dataset import ColumnMapping, DatasetRef, ValidationReport
from statbrief.domain.observation import Observation


def _uuid(value: str) -> uuid.UUID | None:
    """Parse a UUID; None when malformed (treated as not found)."""
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


class ReportRepo:
    """Reports owned by users."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def create(self, user_id: str, title: str) -> dict:
        """Create a draft report."""
        with self.pool.connection() as c:
            return c.execute("INSERT INTO reports (user_id, title) VALUES (%s, %s) RETURNING *",
                             (user_id, title)).fetchone()

    def get(self, user_id: str, report_id: str) -> dict | None:
        """A report if it belongs to the user."""
        rid = _uuid(report_id)
        if rid is None:
            return None
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM reports WHERE id = %s AND user_id = %s", (rid, user_id)).fetchone()

    def list_for_user(self, user_id: str) -> list[dict]:
        """The user's reports, most recently active first."""
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM reports WHERE user_id = %s ORDER BY last_activity_at DESC",
                             (user_id,)).fetchall()

    def touch(self, user_id: str, report_id: str) -> None:
        """Record activity (drives retention, D-009)."""
        with self.pool.connection() as c:
            c.execute("UPDATE reports SET last_activity_at = now() WHERE id = %s AND user_id = %s",
                      (_uuid(report_id), user_id))


class FileRepo:
    """Uploaded files (metadata + blob key)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    @staticmethod
    def _ref(row: dict) -> DatasetRef:
        """Row → DatasetRef."""
        return DatasetRef(file_id=str(row["id"]), report_id=str(row["report_id"]), user_id=str(row["user_id"]),
                          filename=row["filename"], kind=row["kind"], size_bytes=row["size_bytes"],
                          sha256=row["sha256"], blob_key=row["blob_key"])

    def add(self, ref: DatasetRef) -> None:
        """Insert a file row (the report must belong to ref.user_id)."""
        with self.pool.connection() as c:
            owned = c.execute("SELECT 1 FROM reports WHERE id = %s AND user_id = %s",
                              (_uuid(ref.report_id), ref.user_id)).fetchone()
            if not owned:
                raise PermissionError("report not owned by user")
            c.execute("INSERT INTO files (id, report_id, user_id, filename, kind, size_bytes, sha256, blob_key) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                      (ref.file_id, ref.report_id, ref.user_id, ref.filename, ref.kind, ref.size_bytes,
                       ref.sha256, ref.blob_key))

    def get(self, user_id: str, file_id: str) -> DatasetRef | None:
        """A file if it belongs to the user."""
        fid = _uuid(file_id)
        if fid is None:
            return None
        with self.pool.connection() as c:
            row = c.execute("SELECT * FROM files WHERE id = %s AND user_id = %s", (fid, user_id)).fetchone()
        return self._ref(row) if row else None

    def list_for_report(self, user_id: str, report_id: str) -> list[DatasetRef]:
        """The user's files in a report, oldest first."""
        with self.pool.connection() as c:
            rows = c.execute("SELECT * FROM files WHERE report_id = %s AND user_id = %s ORDER BY created_at",
                             (_uuid(report_id), user_id)).fetchall()
        return [self._ref(r) for r in rows]

    def count_for_report(self, user_id: str, report_id: str) -> int:
        """Number of files in the user's report (MAX_FILES_PER_REPORT check)."""
        with self.pool.connection() as c:
            return c.execute("SELECT count(*) AS n FROM files WHERE report_id = %s AND user_id = %s",
                             (_uuid(report_id), user_id)).fetchone()["n"]

    def set_status(self, user_id: str, file_id: str, status: str) -> None:
        """Update the processing status of the user's file."""
        with self.pool.connection() as c:
            c.execute("UPDATE files SET status = %s WHERE id = %s AND user_id = %s",
                      (status, _uuid(file_id), user_id))

    def delete(self, user_id: str, file_id: str) -> DatasetRef | None:
        """Delete the user's file row (cascades); returns the ref so the caller can delete the blob."""
        with self.pool.connection() as c:
            row = c.execute("DELETE FROM files WHERE id = %s AND user_id = %s RETURNING *",
                            (_uuid(file_id), user_id)).fetchone()
        return self._ref(row) if row else None


class UploadRepo:
    """Chunked uploads staged in Postgres until complete (D-021)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def start(self, user_id: str, report_id: str, filename: str, total_bytes: int, chunk_count: int) -> str:
        """Open an upload for the user's report."""
        with self.pool.connection() as c:
            if not c.execute("SELECT 1 FROM reports WHERE id = %s AND user_id = %s",
                             (_uuid(report_id), user_id)).fetchone():
                raise PermissionError("report not owned by user")
            return str(c.execute("INSERT INTO uploads (user_id, report_id, filename, total_bytes, chunk_count) "
                                 "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                 (user_id, report_id, filename, total_bytes, chunk_count)).fetchone()["id"])

    def get(self, user_id: str, upload_id: str) -> dict | None:
        """Upload metadata if owned by the user."""
        uid = _uuid(upload_id)
        if uid is None:
            return None
        with self.pool.connection() as c:
            return c.execute("SELECT * FROM uploads WHERE id = %s AND user_id = %s", (uid, user_id)).fetchone()

    def put_chunk(self, user_id: str, upload_id: str, idx: int, data: bytes) -> None:
        """Store one chunk (re-sending the same index replaces it)."""
        up = self.get(user_id, upload_id)
        if up is None:
            raise PermissionError("upload not owned by user")
        if not 0 <= idx < up["chunk_count"]:
            raise ValueError("chunk index out of range")
        with self.pool.connection() as c:
            c.execute("INSERT INTO upload_chunks (upload_id, idx, data) VALUES (%s, %s, %s) "
                      "ON CONFLICT (upload_id, idx) DO UPDATE SET data = EXCLUDED.data", (up["id"], idx, data))

    def assemble(self, user_id: str, upload_id: str) -> bytes:
        """Concatenate all chunks in order (ValueError when any is missing)."""
        up = self.get(user_id, upload_id)
        if up is None:
            raise PermissionError("upload not owned by user")
        with self.pool.connection() as c:
            rows = c.execute("SELECT idx, data FROM upload_chunks WHERE upload_id = %s ORDER BY idx",
                             (up["id"],)).fetchall()
        if [r["idx"] for r in rows] != list(range(up["chunk_count"])):
            raise ValueError("upload has missing chunks")
        return b"".join(bytes(r["data"]) for r in rows)

    def delete(self, user_id: str, upload_id: str) -> None:
        """Remove an upload and its chunks."""
        with self.pool.connection() as c:
            c.execute("DELETE FROM uploads WHERE id = %s AND user_id = %s", (_uuid(upload_id), user_id))


def _owns_file(c, user_id: str, file_id: str) -> bool:
    """True when the user owns the file."""
    return bool(c.execute("SELECT 1 FROM files WHERE id = %s AND user_id = %s",
                          (_uuid(file_id), user_id)).fetchone())


class MappingRepo:
    """Column mappings per file (survive resume, AC 58)."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def save(self, user_id: str, file_id: str, mapping: ColumnMapping) -> None:
        """Insert or replace the mapping of the user's file."""
        with self.pool.connection() as c:
            if not _owns_file(c, user_id, file_id):
                raise PermissionError("file not owned by user")
            c.execute("INSERT INTO file_mappings (file_id, mapping) VALUES (%s, %s) ON CONFLICT (file_id) "
                      "DO UPDATE SET mapping = EXCLUDED.mapping, updated_at = now()",
                      (file_id, Jsonb(mapping.model_dump(mode="json"))))

    def get(self, user_id: str, file_id: str) -> ColumnMapping | None:
        """The mapping of the user's file."""
        with self.pool.connection() as c:
            row = c.execute("SELECT m.mapping FROM file_mappings m JOIN files f ON f.id = m.file_id "
                            "WHERE m.file_id = %s AND f.user_id = %s", (_uuid(file_id), user_id)).fetchone()
        return ColumnMapping.model_validate(row["mapping"]) if row else None


class ValidationRepo:
    """Latest validation report per file."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def save(self, user_id: str, report: ValidationReport) -> None:
        """Insert or replace the user's file report."""
        with self.pool.connection() as c:
            if not _owns_file(c, user_id, report.file_id):
                raise PermissionError("file not owned by user")
            c.execute("INSERT INTO validation_reports (file_id, report) VALUES (%s, %s) ON CONFLICT (file_id) "
                      "DO UPDATE SET report = EXCLUDED.report, updated_at = now()",
                      (report.file_id, Jsonb(report.model_dump(mode="json"))))

    def get(self, user_id: str, file_id: str) -> ValidationReport | None:
        """The latest report of the user's file."""
        with self.pool.connection() as c:
            row = c.execute("SELECT v.report FROM validation_reports v JOIN files f ON f.id = v.file_id "
                            "WHERE v.file_id = %s AND f.user_id = %s", (_uuid(file_id), user_id)).fetchone()
        return ValidationReport.model_validate(row["report"]) if row else None


class ObservationRepo:
    """Normalized observations per report."""

    def __init__(self, pool: ConnectionPool):
        """Bind to a pool."""
        self.pool = pool

    def replace_for_file(self, user_id: str, report_id: str, file_id: str, observations: list[Observation]) -> int:
        """Atomically replace one file's observations; returns how many were stored."""
        with self.pool.connection() as c, c.transaction():
            if not _owns_file(c, user_id, file_id):
                raise PermissionError("file not owned by user")
            c.execute("DELETE FROM observations WHERE file_id = %s", (_uuid(file_id),))
            with c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO observations (report_id, obs_id, file_id, user_id, indicator_code, region_code, "
                    "period, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(report_id, o.obs_id, file_id, user_id, o.indicator_code, o.region_code, o.period,
                      Jsonb(o.model_dump(mode="json"))) for o in observations])
        return len(observations)

    def list_for_report(self, user_id: str, report_id: str) -> list[Observation]:
        """The user's observations in a report (stable order)."""
        with self.pool.connection() as c:
            rows = c.execute("SELECT payload FROM observations WHERE report_id = %s AND user_id = %s "
                             "ORDER BY indicator_code, region_code, period, obs_id",
                             (_uuid(report_id), user_id)).fetchall()
        return [Observation.model_validate(r["payload"]) for r in rows]
```
- [ ] **Step 5: Run** `pytest tests/unit/test_repos.py tests/unit/test_blobs.py tests/unit/test_db.py` → pass (test_db's migration list assertion must be updated to include `002_reports_files`). **Step 6: Commit** `feat(m2): reports/files/uploads/observations tables, user-scoped repos, blob stores`.

---

### Task 9: Upload & ingestion JSON API + template download (D-021, AC 9–17)

**Files:** Create `src/statbrief/ingest/template.py`, `src/statbrief/web/routes/uploads.py`, `tests/unit/ingest/test_template.py`, `tests/web/test_uploads_api.py`. Modify `src/statbrief/web/main.py` (include router), `src/statbrief/web/runtime.py` (add `blobs: BlobStore` to `Runtime`, built with `get_blob_store(settings)`), `src/statbrief/config.py` (+ `.env.example`):
```python
    upload_chunk_mb: float = Field(4.0, gt=0, le=4.4)   # below Vercel's 4.5 MB request body limit (D-021)
    max_uncompressed_ratio: int = Field(100, ge=2)      # zip-bomb guard for .xlsx
    max_uncompressed_mb: int = Field(200, ge=1)         # absolute expansion cap for .xlsx (Task 2 review)
    max_rows_per_file: int = Field(200_000, ge=1)
    preview_rows: int = Field(50, ge=1, le=500)
```

**Interfaces:**
- Consumes: everything above; `current_user`, `get_runtime`, `consume`-free (no LLM here).
- Produces routes (all JSON, all require login; state-changing ones require header `X-StatBrief-Client: 1` else 403):
  - `POST /api/reports` `{title}` → 201 `{id, title}`
  - `POST /api/reports/{rid}/uploads` `{filename, total_bytes}` → 201 `{upload_id, chunk_bytes, chunk_count}`; 404 unknown report; 413 too large; 409 file limit reached; 422 unsupported extension (early check by extension only)
  - `PUT /api/uploads/{uid}/chunks/{idx}` raw body (`application/octet-stream`, ≤ chunk_bytes) → 204; 413 when larger; 404 unknown upload
  - `POST /api/uploads/{uid}/complete` → 201 `{file_id, kind, size_bytes, worksheets: [{name, hidden}]}`; 422 `{code, message}` on `UploadRejected`; 400 missing chunks
  - `GET /api/files/{fid}/preview?worksheet=&header_row=` → `{worksheet, columns, rows, row_numbers, detected: DetectedSchema, needs_review}` (rows limited to `preview_rows`, cells JSON-safe: datetime → ISO, FORMULA_NO_VALUE → `{"formula_without_value": true}`)
  - `PUT /api/files/{fid}/mapping` `ColumnMapping` → 200 (validates columns exist in the table; 422 otherwise)
  - `POST /api/files/{fid}/validate` → `ValidationReport` (saved)
  - `POST /api/files/{fid}/issues/{issue_id}` `{resolution}` → updated `ValidationReport` (resolution ∈ excluded/accepted; `accepted` only for warnings)
  - `POST /api/files/{fid}/normalize` → `{observations: n, excluded: [{row_number, reasons}]}`; 409 `{unresolved: [...]}` when blocking
  - `GET /api/template.csv`, `GET /api/template.xlsx` (no header requirement; login required)
- `template.py`: `TEMPLATE_COLUMNS = ["date", "region", "indicator", "value", "unit"]`, `build_template_csv() -> bytes`, `build_template_xlsx() -> bytes` (sheet `Data` with 3 example rows; sheet `README` explaining each column, accepted date formats, region codes, units; values only).

- [ ] **Step 1: Failing tests** — `tests/unit/ingest/test_template.py`:
```python
"""Tests for the downloadable template."""
from statbrief.ingest.readers import read_grid, to_table
from statbrief.ingest.template import TEMPLATE_COLUMNS, build_template_csv, build_template_xlsx
from statbrief.ingest.validate import validate_table
from statbrief.domain.dataset import ColumnMapping

MAP = ColumnMapping(date_col="date", region_col="region", indicator_col="indicator", value_col="value",
                    unit_col="unit")


def test_template_files_validate_cleanly():
    """Both template formats parse and validate without issues under the canonical mapping."""
    for kind, data in (("csv", build_template_csv()), ("xlsx", build_template_xlsx())):
        t = to_table(read_grid(data, kind, worksheet="Data" if kind == "xlsx" else None), header_row=1)
        assert t.columns == TEMPLATE_COLUMNS
        assert validate_table(t, MAP, "tmpl").issues == []
```
`tests/web/test_uploads_api.py`:
```python
"""End-to-end tests of the chunked upload and ingestion API."""
import json

import pytest
from fastapi.testclient import TestClient

from statbrief.web.main import create_app
from tests.fixtures import csv_bytes, xlsx_bytes

H = {"X-StatBrief-Client": "1"}


@pytest.fixture
def client(settings, tmp_path):
    """Logged-in client with a local blob store and tiny chunks."""
    s = settings.model_copy(update={"local_blob_dir": str(tmp_path), "upload_chunk_mb": 0.0001})
    with TestClient(create_app(s), follow_redirects=False) as c:
        c.post("/signup", data={"email": "u@example.com", "password": "long-enough-pw"})
        yield c


def _upload(client, rid, name, data):
    """Upload `data` in chunks; return the completion response."""
    r = client.post(f"/api/reports/{rid}/uploads", json={"filename": name, "total_bytes": len(data)}, headers=H)
    assert r.status_code == 201, r.text
    up = r.json()
    size = up["chunk_bytes"]
    for i in range(up["chunk_count"]):
        chunk = data[i * size:(i + 1) * size]
        assert client.put(f"/api/uploads/{up['upload_id']}/chunks/{i}", content=chunk,
                          headers={**H, "Content-Type": "application/octet-stream"}).status_code == 204
    return client.post(f"/api/uploads/{up['upload_id']}/complete", headers=H)


def _report(client):
    """Create a report and return its id."""
    r = client.post("/api/reports", json={"title": "Q1"}, headers=H)
    assert r.status_code == 201
    return r.json()["id"]


def test_full_flow_upload_preview_map_validate_resolve_normalize(client):
    """Upload → preview/detect → mapping → validate → resolve → normalize, with provenance."""
    rid = _report(client)
    rows = [["IRS"], [None], ["luna", "judet", "indicator", "valoare", "um"],
            ["2024-01", "Cluj", "HICP", 7.4, "%"], ["2024-13", "Cluj", "HICP", 7.1, "%"],
            ["2024-03", "Iași", "HICP", 6.6, "%"]]
    done = _upload(client, rid, "IRS.xlsx", xlsx_bytes({"Inflation": rows}))
    assert done.status_code == 201, done.text
    fid = done.json()["file_id"]
    prev = client.get(f"/api/files/{fid}/preview").json()
    assert prev["detected"]["header_row"] == 3 and prev["columns"][0] == "luna"
    mapping = {"worksheet": "Inflation", "header_row": 3, "date_col": "luna", "region_col": "judet",
               "indicator_col": "indicator", "value_col": "valoare", "unit_col": "um"}
    assert client.put(f"/api/files/{fid}/mapping", json=mapping, headers=H).status_code == 200
    report = client.post(f"/api/files/{fid}/validate", headers=H).json()
    assert [i["issue_id"] for i in report["issues"]] == ["invalid_date:luna"]
    blocked = client.post(f"/api/files/{fid}/normalize", headers=H)
    assert blocked.status_code == 409 and blocked.json()["unresolved"] == ["invalid_date:luna"]
    client.post(f"/api/files/{fid}/issues/invalid_date:luna", json={"resolution": "excluded"}, headers=H)
    res = client.post(f"/api/files/{fid}/normalize", headers=H).json()
    assert res["observations"] == 2 and res["excluded"][0]["row_number"] == 5


def test_rejections_and_limits(client, settings):
    """Unsafe content, oversize uploads, file limits and missing CSRF header are refused."""
    rid = _report(client)
    bad = _upload(client, rid, "m.xlsx", b"not a zip")
    assert bad.status_code == 422 and bad.json()["code"] == "malformed"
    too_big = client.post(f"/api/reports/{rid}/uploads",
                          json={"filename": "x.csv", "total_bytes": settings.max_upload_mb * 1024 * 1024 + 1},
                          headers=H)
    assert too_big.status_code == 413
    assert client.post(f"/api/reports/{rid}/uploads", json={"filename": "x.exe", "total_bytes": 3},
                       headers=H).status_code == 422
    assert client.post("/api/reports", json={"title": "x"}).status_code == 403  # no X-StatBrief-Client


def test_other_users_cannot_access(client, settings, tmp_path):
    """A second user gets 404 for the first user's report and file."""
    rid = _report(client)
    fid = _upload(client, rid, "d.csv", csv_bytes([["date", "value"], ["2024", "1"]])).json()["file_id"]
    s = settings.model_copy(update={"local_blob_dir": str(tmp_path)})
    with TestClient(create_app(s), follow_redirects=False) as other:
        other.post("/signup", data={"email": "o@example.com", "password": "long-enough-pw"})
        assert other.get(f"/api/files/{fid}/preview").status_code == 404
        assert other.post(f"/api/reports/{rid}/uploads", json={"filename": "a.csv", "total_bytes": 3},
                          headers=H).status_code == 404


def test_template_downloads(client):
    """CSV and XLSX templates download with the right content types."""
    r = client.get("/api/template.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == "date,region,indicator,value,unit"
    x = client.get("/api/template.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `src/statbrief/ingest/template.py`:
```python
"""Downloadable data template (long format) in CSV and XLSX, values only."""
import csv
import io

from openpyxl import Workbook

TEMPLATE_COLUMNS = ["date", "region", "indicator", "value", "unit"]
_EXAMPLE_ROWS = [
    ["2024-01", "RO113", "HICP_YOY", 7.4, "%"],
    ["2024-02", "RO113", "HICP_YOY", 7.1, "%"],
    ["2024-03", "RO113", "HICP_YOY", 6.6, "%"],
]
_README = [
    ["Column", "Meaning"],
    ["date", "Period: 2024 (year), 2024-Q1 (quarter), 2024-03 or 2024M03 (month)."],
    ["region", "Region code (RO, RO113, EU27_2020) or name (Cluj, Romania)."],
    ["indicator", "Your indicator code, e.g. HICP_YOY or UNEMP_RATE."],
    ["value", "Number; use a dot or comma as decimal separator; leave empty for a missing value."],
    ["unit", "Unit, e.g. %, pp, EUR, RON, thousand persons, Index 2015=100."],
]


def build_template_csv() -> bytes:
    """Template as UTF-8 CSV."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TEMPLATE_COLUMNS)
    w.writerows(_EXAMPLE_ROWS)
    return buf.getvalue().encode("utf-8")


def build_template_xlsx() -> bytes:
    """Template as XLSX with a Data sheet and a README sheet."""
    wb = Workbook()
    data = wb.active
    data.title = "Data"
    data.append(TEMPLATE_COLUMNS)
    for r in _EXAMPLE_ROWS:
        data.append(r)
    readme = wb.create_sheet("README")
    for r in _README:
        readme.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
```
`src/statbrief/web/routes/uploads.py`:
```python
"""JSON API for chunked uploads and file ingestion (preview, mapping, validation, normalization)."""
import math
import uuid
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from statbrief.auth.users import User
from statbrief.domain.dataset import ColumnMapping, DatasetRef
from statbrief.ingest.detect import detect_schema
from statbrief.ingest.normalize import UnresolvedIssues, to_observations
from statbrief.ingest.readers import FORMULA_NO_VALUE, list_worksheets, read_grid, to_table
from statbrief.ingest.sniff import UploadRejected, validate_file_bytes
from statbrief.ingest.template import build_template_csv, build_template_xlsx
from statbrief.ingest.validate import validate_table
from statbrief.storage.blobs import blob_key
from statbrief.storage.repos import FileRepo, MappingRepo, ObservationRepo, ReportRepo, UploadRepo, ValidationRepo
from statbrief.web.deps import current_user, get_runtime
from statbrief.web.runtime import Runtime

router = APIRouter(prefix="/api")
_CLIENT_HEADER = "X-StatBrief-Client"
_ALLOWED_EXTS = {".csv", ".xlsx", ".xls", ".json"}
_CONTENT_TYPES = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  "xls": "application/vnd.ms-excel", "json": "application/json"}

UserDep = Annotated[User, Depends(current_user)]
RtDep = Annotated[Runtime, Depends(get_runtime)]


def require_client_header(request: Request) -> None:
    """CSRF guard for state-changing API calls: a custom header forces a CORS preflight (D-018)."""
    if request.headers.get(_CLIENT_HEADER) != "1":
        raise HTTPException(status_code=403, detail="missing client header")


Guard = Depends(require_client_header)


class NewReport(BaseModel):
    """Request body: create a report."""

    title: str = Field(min_length=1, max_length=200)


class NewUpload(BaseModel):
    """Request body: start a chunked upload."""

    filename: str = Field(min_length=1, max_length=255)
    total_bytes: int = Field(gt=0)


class ResolutionBody(BaseModel):
    """Request body: resolve a validation issue."""

    resolution: Literal["excluded", "accepted"]


def _json_cell(v: Any) -> Any:
    """Make a cell JSON-safe for previews."""
    if v is FORMULA_NO_VALUE:
        return {"formula_without_value": True}
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _file_or_404(rt: Runtime, user: User, file_id: str) -> DatasetRef:
    """The user's file or 404."""
    ref = FileRepo(rt.pool).get(user.id, file_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="file not found")
    return ref


def _grid(rt: Runtime, ref: DatasetRef, worksheet: str | None):
    """Read the stored file into a grid."""
    return read_grid(rt.blobs.get(ref.blob_key), ref.kind, worksheet=worksheet,
                     max_rows=rt.settings.max_rows_per_file)


@router.post("/reports", status_code=201, dependencies=[Guard])
def create_report(body: NewReport, user: UserDep, rt: RtDep) -> dict:
    """Create a draft report."""
    row = ReportRepo(rt.pool).create(user.id, body.title)
    return {"id": str(row["id"]), "title": row["title"]}


@router.post("/reports/{report_id}/uploads", status_code=201, dependencies=[Guard])
def start_upload(report_id: str, body: NewUpload, user: UserDep, rt: RtDep) -> dict:
    """Open a chunked upload after cheap checks (ownership, size, count, extension)."""
    s = rt.settings
    if ReportRepo(rt.pool).get(user.id, report_id) is None:
        raise HTTPException(status_code=404, detail="report not found")
    if PurePosixPath(body.filename.replace("\\", "/")).suffix.lower() not in _ALLOWED_EXTS:
        raise HTTPException(status_code=422, detail="Only CSV, Excel (.xlsx, .xls) and JSON files are accepted.")
    if body.total_bytes > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Files are limited to {s.max_upload_mb} MB.")
    if FileRepo(rt.pool).count_for_report(user.id, report_id) >= s.max_files_per_report:
        raise HTTPException(status_code=409, detail=f"A report can have at most {s.max_files_per_report} files.")
    chunk_bytes = max(1, int(s.upload_chunk_mb * 1024 * 1024))
    chunk_count = math.ceil(body.total_bytes / chunk_bytes)
    upload_id = UploadRepo(rt.pool).start(user.id, report_id, body.filename, body.total_bytes, chunk_count)
    return {"upload_id": upload_id, "chunk_bytes": chunk_bytes, "chunk_count": chunk_count}


@router.put("/uploads/{upload_id}/chunks/{idx}", status_code=204, dependencies=[Guard])
async def put_chunk(upload_id: str, idx: int, request: Request, user: UserDep, rt: RtDep) -> Response:
    """Store one chunk (≤ chunk size)."""
    chunk_bytes = max(1, int(rt.settings.upload_chunk_mb * 1024 * 1024))
    data = await request.body()
    if len(data) > chunk_bytes:
        raise HTTPException(status_code=413, detail="chunk too large")
    try:
        UploadRepo(rt.pool).put_chunk(user.id, upload_id, idx, data)
    except PermissionError:
        raise HTTPException(status_code=404, detail="upload not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return Response(status_code=204)


@router.post("/uploads/{upload_id}/complete", status_code=201, dependencies=[Guard])
def complete_upload(upload_id: str, user: UserDep, rt: RtDep):
    """Reassemble, sniff, store privately, register the file, drop the staged chunks."""
    uploads = UploadRepo(rt.pool)
    up = uploads.get(user.id, upload_id)
    if up is None:
        raise HTTPException(status_code=404, detail="upload not found")
    try:
        data = uploads.assemble(user.id, upload_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if len(data) != up["total_bytes"]:
        uploads.delete(user.id, upload_id)
        return JSONResponse({"code": "malformed", "message": "The upload was incomplete."}, status_code=422)
    s = rt.settings
    try:
        check = validate_file_bytes(up["filename"], data, s.max_upload_mb * 1024 * 1024, s.max_uncompressed_ratio,
                                    s.max_uncompressed_mb * 1024 * 1024)
        sheets = list_worksheets(data, check.kind)
    except UploadRejected as exc:
        uploads.delete(user.id, upload_id)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)
    file_id = str(uuid.uuid4())
    report_id = str(up["report_id"])
    key = rt.blobs.put(blob_key(user.id, report_id, file_id, up["filename"]), data, _CONTENT_TYPES[check.kind])
    FileRepo(rt.pool).add(DatasetRef(file_id=file_id, report_id=report_id, user_id=user.id,
                                     filename=up["filename"], kind=check.kind, size_bytes=check.size_bytes,
                                     sha256=check.sha256, blob_key=key))
    uploads.delete(user.id, upload_id)
    ReportRepo(rt.pool).touch(user.id, report_id)
    return JSONResponse({"file_id": file_id, "kind": check.kind, "size_bytes": check.size_bytes,
                         "worksheets": [{"name": w.name, "hidden": w.hidden} for w in sheets]}, status_code=201)


@router.get("/files/{file_id}/preview")
def preview(file_id: str, user: UserDep, rt: RtDep, worksheet: str | None = None,
            header_row: int | None = None) -> dict:
    """First rows of the table plus detection results."""
    ref = _file_or_404(rt, user, file_id)
    s = rt.settings
    try:
        grid = _grid(rt, ref, worksheet)
    except UploadRejected as exc:
        raise HTTPException(status_code=422, detail=exc.message) from None
    detected = detect_schema(grid, sample_rows=s.detect_sample_rows, scan_rows=s.detect_header_scan_rows)
    table = to_table(grid, header_row or detected.header_row)
    return {"worksheet": grid.worksheet, "columns": table.columns,
            "rows": [[_json_cell(v) for v in r] for r in table.rows[:s.preview_rows]],
            "row_numbers": table.row_numbers[:s.preview_rows],
            "detected": detected.model_dump(mode="json"),
            "needs_review": detected.needs_review(s.mapping_min_confidence)}


@router.put("/files/{file_id}/mapping", dependencies=[Guard])
def save_mapping(file_id: str, mapping: ColumnMapping, user: UserDep, rt: RtDep) -> dict:
    """Store a mapping after checking every mapped column exists."""
    ref = _file_or_404(rt, user, file_id)
    try:
        table = to_table(_grid(rt, ref, mapping.worksheet), mapping.header_row)
    except (ValueError, UploadRejected) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    missing = sorted(mapping.mapped_columns() - set(table.columns))
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown columns: {', '.join(missing)}")
    MappingRepo(rt.pool).save(user.id, file_id, mapping)
    FileRepo(rt.pool).set_status(user.id, file_id, "mapped")
    return {"ok": True}


def _table_and_mapping(rt: Runtime, user: User, file_id: str):
    """Stored file, its mapping (409 when absent) and the parsed table."""
    ref = _file_or_404(rt, user, file_id)
    mapping = MappingRepo(rt.pool).get(user.id, file_id)
    if mapping is None:
        raise HTTPException(status_code=409, detail="map the columns first")
    return ref, mapping, to_table(_grid(rt, ref, mapping.worksheet), mapping.header_row)


@router.post("/files/{file_id}/validate", dependencies=[Guard])
def validate(file_id: str, user: UserDep, rt: RtDep) -> dict:
    """Validate the file under its mapping and store the report."""
    ref, mapping, table = _table_and_mapping(rt, user, file_id)
    report = validate_table(table, mapping, ref.file_id)
    ValidationRepo(rt.pool).save(user.id, report)
    FileRepo(rt.pool).set_status(user.id, file_id, "validated")
    return report.model_dump(mode="json")


@router.post("/files/{file_id}/issues/{issue_id}", dependencies=[Guard])
def resolve_issue(file_id: str, issue_id: str, body: ResolutionBody, user: UserDep, rt: RtDep) -> dict:
    """Mark an issue excluded (errors or warnings) or accepted (warnings only)."""
    _file_or_404(rt, user, file_id)
    report = ValidationRepo(rt.pool).get(user.id, file_id)
    if report is None:
        raise HTTPException(status_code=409, detail="validate first")
    issue = next((i for i in report.issues if i.issue_id == issue_id), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    if body.resolution == "accepted" and issue.severity == "error":
        raise HTTPException(status_code=422, detail="errors can only be excluded or fixed")
    updated = report.with_resolution(issue_id, body.resolution)
    ValidationRepo(rt.pool).save(user.id, updated)
    return updated.model_dump(mode="json")


@router.post("/files/{file_id}/normalize", dependencies=[Guard])
def normalize(file_id: str, user: UserDep, rt: RtDep):
    """Store observations for the file; 409 lists unresolved errors."""
    ref, mapping, table = _table_and_mapping(rt, user, file_id)
    report = ValidationRepo(rt.pool).get(user.id, file_id)
    if report is None:
        raise HTTPException(status_code=409, detail="validate first")
    try:
        result = to_observations(table, mapping, ref, report)
    except UnresolvedIssues as exc:
        return JSONResponse({"unresolved": exc.issue_ids}, status_code=409)
    n = ObservationRepo(rt.pool).replace_for_file(user.id, ref.report_id, ref.file_id, result.observations)
    FileRepo(rt.pool).set_status(user.id, file_id, "normalized")
    return {"observations": n,
            "excluded": [{"row_number": e.row_number, "reasons": e.reasons} for e in result.excluded]}


@router.get("/template.csv")
def template_csv(user: UserDep) -> Response:
    """Download the CSV template."""
    return Response(build_template_csv(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="statbrief_template.csv"'})


@router.get("/template.xlsx")
def template_xlsx(user: UserDep) -> Response:
    """Download the XLSX template."""
    return Response(build_template_xlsx(), media_type=_CONTENT_TYPES["xlsx"],
                    headers={"Content-Disposition": 'attachment; filename="statbrief_template.xlsx"'})
```
`runtime.py`: add `blobs: BlobStore` to `Runtime` (constructed with `get_blob_store(s)` next to the pool). `main.py`: `from statbrief.web.routes import uploads` and `app.include_router(uploads.router)`.

Production env (record in report, not code): set `BLOB_BACKEND=vercel` in Vercel Production/Preview (the controller does this with `vercel env add`).

- [ ] **Step 4: Run** `pytest tests/web/test_uploads_api.py tests/unit/ingest/test_template.py -o addopts="" -q`, then the full suite → all pass. **Step 5: Commit** `feat(m2): chunked upload + ingestion JSON API, template downloads`.

---

## Self-review

1. **Spec coverage:** AC 9 → Tasks 2, 9 (limits, extensions, chunking); AC 10 → Task 2; AC 11 → Tasks 2–3 (`data_only`, FORMULA_NO_VALUE, CSV formulas as text); AC 12 → Tasks 3, 5; AC 13 → Task 1 (`ColumnMapping` incl. constants, date/decimal options) + Task 9 mapping endpoint; AC 14 → Task 9 preview (UI in M8); AC 15 → Task 6; AC 16 → Tasks 6–7 (explicit exclusions, blocking); AC 17 → Task 9 template; AC 18–19 → Tasks 1, 7; AC 6 → Task 8 (+ Task 9 404s); D-021 → Tasks 8–9; D-018 partial → Task 9 header guard.
2. **Placeholder scan:** none; the `.xls` fixture is an explicit conditional (sample file or documented skip).
3. **Type consistency:** `ColumnMapping`, `DetectedSchema.suggested_mapping`, `ValidationReport.with_resolution`, `parse_rows`, `validate_table(table, mapping, file_id, known_columns)`, `to_observations(table, mapping, ref, report)`, `Runtime.blobs`, `blob_key(user_id, report_id, file_id, filename)` used consistently across Tasks 1–9.
