"""Live API metadata (Eurostat / World Bank) as citable LangChain Documents (Task 4)."""
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from statbrief.rag.api_metadata import API_DOC_PREFIX, metadata_documents
from statbrief.rag.loaders import METADATA_KEYS

KB = Path(__file__).resolve().parents[3] / "knowledge_base"
EUROSTAT_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr"
                "?geo=RO&coicop=CP00&format=JSON")
WB_URL = "https://api.worldbank.org/v2/country/RO/indicator/FP.CPI.TOTL.ZG?format=json"


@dataclass
class Meta:
    """Minimal stand-in for M3's SeriesMeta."""

    label: str
    unit_label: str | None
    last_update: str | dt.datetime | None


def test_eurostat_metadata_document():
    """Eurostat metadata becomes one shared document cited by URL and issued by Eurostat."""
    (doc,) = metadata_documents(Meta("HICP - monthly data (annual rate of change)", "Annual rate of change",
                                     "2026-10-17T11:00:00+0200"), EUROSTAT_URL)
    m = doc.metadata
    assert set(METADATA_KEYS) <= set(m)
    assert (m["issuer"], m["source_type"], m["url"], m["owner"]) == ("Eurostat", "api_metadata", EUROSTAT_URL, None)
    assert m["title"] == "HICP - monthly data (annual rate of change)" and m["page"] is None
    assert "Annual rate of change" in doc.page_content and "2026-10-17" in doc.page_content
    assert EUROSTAT_URL in doc.page_content


def test_world_bank_metadata_document_and_missing_fields():
    """World Bank metadata is issued by the World Bank; missing unit/date are stated, not invented."""
    (doc,) = metadata_documents(Meta("Inflation, consumer prices (annual %)", None, None), WB_URL)
    assert doc.metadata["issuer"] == "World Bank" and doc.metadata["url"] == WB_URL
    assert "Unit of measure: not stated" in doc.page_content and "Last update: not stated" in doc.page_content


def test_datetime_last_update_is_iso_formatted():
    """A datetime last update is rendered in ISO 8601."""
    (doc,) = metadata_documents(Meta("x", "%", dt.datetime(2026, 10, 1, 9, 30)), WB_URL)
    assert "Last update: 2026-10-01T09:30:00" in doc.page_content


def test_doc_ids_never_mix_with_corpus_ids():
    """API doc ids carry the 'api-' prefix, are stable per URL and never collide with ESMB/IRS ids."""
    corpus_ids = {e["doc_id"] for e in json.loads((KB / "manifest.json").read_text(encoding="utf-8"))["documents"]}
    a = metadata_documents(Meta("a", "%", None), EUROSTAT_URL)[0].metadata["doc_id"]
    b = metadata_documents(Meta("b", "%", None), WB_URL)[0].metadata["doc_id"]
    assert a.startswith(API_DOC_PREFIX + "eurostat-") and b.startswith(API_DOC_PREFIX + "world-bank-")
    assert a != b and not {a, b} & corpus_ids
    assert not any(i.startswith(API_DOC_PREFIX) for i in corpus_ids)
    assert metadata_documents(Meta("other label", "%", None), EUROSTAT_URL)[0].metadata["doc_id"] == a


@pytest.mark.parametrize("url", [
    "http://ec.europa.eu/eurostat/api/x",           # not https
    "https://ec.europa.eu.evil.example/eurostat",   # look-alike host
    "https://example.org/data",                     # unknown issuer
    "not a url",
])
def test_rejects_urls_not_from_a_known_api(url):
    """Only HTTPS URLs on the Eurostat or World Bank API hosts are attributed to those institutions."""
    with pytest.raises(ValueError):
        metadata_documents(Meta("x", "%", None), url)
