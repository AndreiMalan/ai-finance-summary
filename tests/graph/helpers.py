"""Shared builders for the graph tests: uploads, API observations, scripted draft payloads."""
from datetime import datetime, timezone
from typing import Any

from statbrief.domain.observation import ApiProvenance, Observation, make_obs_id


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


def draft_for(metric, shown: str, *, extra_caveat: str | None = None,
              acknowledged: list[str] | None = None) -> dict[str, Any]:
    """A ReportDraft payload stating one pp-change metric as `shown` (e.g. "0.6"), citing its inputs."""
    sentence = f"Unemployment rose by {shown} percentage points compared with January 2023."
    caveats = ["Figures for the latest month are provisional."] + ([extra_caveat] if extra_caveat else [])
    return {
        "title": "Unemployment in Romania",
        "executive_summary": [{"headline": sentence, "claim_ids": ["c1"]}],
        "sections": [{"indicator_code": metric.indicator, "title": "Unemployment", "paragraphs": [sentence],
                      "claim_ids": ["c1"], "citation_ids": []}],
        "claims": [{"claim_id": "c1", "text": sentence, "value": float(shown), "unit": "pp",
                    "metric_id": metric.metric_id, "observation_ids": list(metric.observation_ids)}],
        "citations": [], "caveats": caveats, "unknown_columns_acknowledged": acknowledged or [],
    }


def report_call(payload: dict[str, Any], call_id: str = "call_1"):
    """A model reply submitting `payload` as a ReportDraft tool call."""
    from tests.unit.llm.fakes import tool_call_message
    return tool_call_message("ReportDraft", payload, call_id=call_id)


def fake_model(*replies):
    """A tool-capable fake chat model answering the scripted replies in order."""
    from tests.unit.llm.fakes import ToolFakeChatModel
    return ToolFakeChatModel(messages=iter(replies))


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


def api_obs(indicator: str, region: str, values: dict[str, float], unit: str = "%") -> list[Observation]:
    """API observations (Eurostat provenance) of one series, {period: value}."""
    prov = ApiProvenance(api_name="eurostat", series_id=f"{indicator}?test", query_url="https://ec.europa.eu/x",
                         retrieved_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    out = []
    for period, value in values.items():
        freq = "Q" if "-Q" in period else "M" if "-" in period else "A"
        out.append(Observation(obs_id=make_obs_id("test", indicator, region, period), indicator_code=indicator,
                               region_code=region, region_level="country", period=period, frequency=freq,
                               value=value, unit=unit, provenance=prov))
    return out
