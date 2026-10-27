"""Sample observations, metrics, sources and passages shared by the LLM-layer tests."""
from datetime import datetime, timezone

from statbrief.analytics.metrics import compute_metrics
from statbrief.analytics.significance import flag_significant
from statbrief.analytics.store import ObservationStore
from statbrief.domain.observation import ApiProvenance, Observation, UploadProvenance
from statbrief.rag.retriever import RetrievedPassage
from tests.unit.llm.fakes import make_settings

UPLOAD = UploadProvenance(file_id="file-1", filename="IRS_regional_monthly.xlsx", worksheet="Date", row_number=2,
                          column="valoare")
API = ApiProvenance(api_name="eurostat", series_id="HICP_YOY|RO",
                    query_url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr",
                    retrieved_at=datetime(2026, 10, 27, tzinfo=timezone.utc))
# Distinctive raw values: tests assert none of them leaks into prompts or logs unless a metric states it.
RAW_VALUES = (7.123456, 6.987654, 5.271828)


def _obs(oid, ind, reg, period, value, unit="%", prov=UPLOAD, column="valoare"):
    """One observation with an explicit id."""
    freq = "Q" if "-Q" in period else "M" if "-" in period else "A"
    if isinstance(prov, UploadProvenance) and column != prov.column:
        prov = prov.model_copy(update={"column": column})
    return Observation(obs_id=oid, indicator_code=ind, region_code=reg, region_level="country", period=period,
                       frequency=freq, value=value, unit=unit, provenance=prov)


def observations() -> list[Observation]:
    """Unemployment (upload) for RO over 13 months, HICP (API) for RO, and one val_adj_x observation."""
    obs = [_obs(f"u{m:02d}", "UNEMP_RATE", "RO", f"2023-{m:02d}", 5.0 + m / 100) for m in range(1, 13)]
    obs.append(_obs("u13", "UNEMP_RATE", "RO", "2024-01", 5.6))
    obs.append(_obs("h1", "HICP_YOY", "RO", "2023-03", RAW_VALUES[0], prov=API))
    obs.append(_obs("h2", "HICP_YOY", "RO", "2024-03", RAW_VALUES[1], prov=API))
    obs.append(_obs("x1", "ADJ", "RO", "2024-01", RAW_VALUES[2], unit="IDX", column="val_adj_x"))
    return obs


def store() -> ObservationStore:
    """ObservationStore over `observations()`."""
    return ObservationStore(observations())


def metrics():
    """(metrics, significant changes) computed deterministically from the sample observations."""
    out = []
    obs = observations()
    for ind, reg in (("UNEMP_RATE", "RO"), ("HICP_YOY", "RO")):
        res, _ = compute_metrics(obs, indicator=ind, region=reg)
        out.extend(res)
    return out, flag_significant(out, make_settings())


def passages() -> list[RetrievedPassage]:
    """Two retrieved passages (one with section and page)."""
    return [
        RetrievedPassage(text="A change in a rate is expressed in percentage points.", doc_id="esmb-g-2023-02",
                         title="ESMB Guidelines for Statistical Commentary", issuer="ESMB", section="2.1",
                         page=None, score=0.91, method="vector", source_file="ESMB_Guidelines.md",
                         source_type="reference"),
        RetrievedPassage(text="Registered unemployment differs from the LFS rate.", doc_id="irs-dict",
                         title="IRS Data Dictionary", issuer="IRS", section="3", page=4, score=0.5,
                         method="vector", source_file="IRS_dict.pdf", source_type="reference"),
    ]
