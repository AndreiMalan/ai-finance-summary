"""Metadata of live public-API series (Eurostat, World Bank) as citable LangChain Documents.

These are the only documents attributed to real institutions (D-006): they describe data actually
fetched from their APIs and are cited by the query URL. The issuer is derived from the URL host,
never from caller-supplied text. M3 owns the API clients; until it merges, this module depends only
on the small `SeriesMeta` protocol below (fields `label`, `unit_label`, `last_update`).
"""
import datetime as dt
import hashlib
from typing import Protocol
from urllib.parse import urlsplit

from langchain_core.documents import Document

API_DOC_PREFIX = "api-"  # never used by the ESMB/IRS corpus or by user uploads ("user-")
SOURCE_TYPE = "api_metadata"
SECTION = "Dataset metadata"
NOT_STATED = "not stated"
# API host -> issuer. A URL matches a host exactly or as a sub-domain of it.
API_ISSUERS: dict[str, str] = {"ec.europa.eu": "Eurostat", "api.worldbank.org": "World Bank"}
_URL_HASH_CHARS = 16


class SeriesMeta(Protocol):
    """What the API clients (M3) report about a series."""

    label: str
    unit_label: str | None
    last_update: str | dt.date | dt.datetime | None


def issuer_for_url(query_url: str) -> str:
    """The institution behind an HTTPS API URL; ValueError for any other scheme or host."""
    parts = urlsplit(query_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise ValueError("API metadata must come from an https:// URL")
    for domain, issuer in API_ISSUERS.items():
        if host == domain or host.endswith("." + domain):
            return issuer
    raise ValueError(f"no known API issuer for host {host!r}")


def _slug(text: str) -> str:
    """Lower-case, hyphen-separated form of an issuer name ("World Bank" -> "world-bank")."""
    return "-".join(text.lower().split())


def _format_date(value: str | dt.date | dt.datetime | None) -> str:
    """ISO 8601 for dates/datetimes, the text as given, or 'not stated'."""
    if value is None or value == "":
        return NOT_STATED
    return value.isoformat() if isinstance(value, dt.date) else str(value)


def metadata_documents(meta: SeriesMeta, query_url: str) -> list[Document]:
    """Describe one API series as shared Documents (dataset label, unit, last update), cited by URL.

    The doc id is stable per URL ("api-<issuer>-<hash>") and cannot collide with corpus or upload ids.
    """
    issuer = issuer_for_url(query_url)
    doc_id = f"{API_DOC_PREFIX}{_slug(issuer)}-{hashlib.sha256(query_url.encode()).hexdigest()[:_URL_HASH_CHARS]}"
    last_update = _format_date(meta.last_update)
    text = (f"Dataset: {meta.label}\n"
            f"Unit of measure: {meta.unit_label or NOT_STATED}\n"
            f"Last update: {last_update}\n"
            f"Source: {issuer} API, {query_url}")
    metadata = {"doc_id": doc_id, "title": meta.label, "issuer": issuer, "owner": None, "section": SECTION,
                "page": None, "source_file": query_url, "source_type": SOURCE_TYPE, "url": query_url,
                "version": None if last_update == NOT_STATED else last_update}
    return [Document(page_content=text, metadata=metadata)]
