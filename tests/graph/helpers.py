"""Shared builders for the graph tests: scripted draft payloads and fake models."""
from typing import Any


def draft_payload(value: float = 0.6, text_value: str | None = None) -> dict[str, Any]:
    """A schema-valid ReportDraft payload (as a model sends it) stating one pp change of UNEMP_RATE in RO."""
    shown = text_value or f"{value}"
    sentence = f"Unemployment rose by {shown} percentage points compared with January 2023."
    return {
        "title": "Unemployment in Romania",
        "executive_summary": [{"headline": sentence, "claim_ids": ["c1"]}],
        "sections": [{"indicator_code": "UNEMP_RATE", "title": "Unemployment", "paragraphs": [sentence],
                      "claim_ids": ["c1"], "citation_ids": ["file-1"]}],
        "claims": [{"claim_id": "c1", "text": sentence, "value": value, "unit": "pp",
                    "metric_id": "pp_change:UNEMP_RATE:RO:2024-01:2023-01", "observation_ids": ["u13", "u01"]}],
        "citations": [{"kind": "upload", "ref_id": "file-1", "label": "IRS_regional_monthly.xlsx"}],
        "caveats": ["Figures for the latest month are provisional."],
    }


def long_csv(rows: list[tuple[str, str, str, float | None, str]],
             header: tuple[str, ...] = ("date", "region", "indicator", "value", "unit")) -> bytes:
    """A long-format CSV (date, region, indicator, value, unit) as uploaded by a user."""
    lines = [",".join(header)]
    lines += [",".join("" if v is None else str(v) for v in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def unemp_rows(indicator: str = "IRS_UNEMP", region: str = "RO") -> list[tuple[str, str, str, float, str]]:
    """Thirteen monthly unemployment rates 2023-01..2024-01 (5.01..5.12, then 5.6)."""
    rows = [(f"2023-{m:02d}", region, indicator, round(5.0 + m / 100, 2), "%") for m in range(1, 13)]
    return rows + [("2024-01", region, indicator, 5.6, "%")]


def monthly(start_year: int, values: list[float]) -> dict[str, float]:
    """{period: value} for consecutive months starting in January of `start_year`."""
    return {f"{start_year + i // 12}-{i % 12 + 1:02d}": v for i, v in enumerate(values)}
