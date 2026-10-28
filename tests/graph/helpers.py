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
