"""Loading reference documents into LangChain Documents and splitting them into chunks (Task 2)."""
import io
from pathlib import Path

import pytest
from langchain_core.documents import Document
from pypdf import PdfWriter

from statbrief.ingest.sniff import UploadRejected
from statbrief.rag.loaders import (METADATA_KEYS, detect_sections, load_reference, load_user_reference,
                                   split_documents)

KB = Path(__file__).resolve().parents[3] / "knowledge_base"
HICP = KB / "ESMB_Regulation_2023-14_Harmonised_Price_Statistics.pdf"
NUTS = KB / "ESMB_Regulation_Territorial_Classification_NUTS.md"
QUALITY = KB / "ESMB_Data_Quality_and_Revisions_Policy.txt"
SHARED = {"doc_id": "ESMB-X", "title": "T", "issuer": "ESMB"}


def _assert_metadata(docs: list[Document], owner=None):
    """Every document carries the full citation metadata."""
    assert docs
    for d in docs:
        assert set(METADATA_KEYS) <= set(d.metadata), d.metadata
        assert d.metadata["owner"] == owner
        assert d.page_content.strip()


def test_pdf_yields_pages_and_sections():
    """PDF pages become documents with 1-based page numbers and the article in force as section."""
    docs = load_reference(HICP, HICP.name, **SHARED)
    _assert_metadata(docs)
    assert {d.metadata["page"] for d in docs} == set(range(1, 6))
    assert all(d.metadata["source_file"] == HICP.name for d in docs)
    base_year = [d for d in docs if "common index reference period for the HICP" in d.page_content.replace("\n", " ")]
    assert base_year and base_year[0].metadata["section"] == "Article 6 Index reference period (base year)"


def test_pdf_from_bytes_equals_pdf_from_path():
    """Bytes and paths load identically."""
    assert load_reference(HICP.read_bytes(), HICP.name, **SHARED) == load_reference(HICP, HICP.name, **SHARED)


def test_markdown_yields_header_sections_without_pages():
    """Markdown is split on headers; the deepest header is the section; there are no pages."""
    docs = load_reference(NUTS, NUTS.name, **SHARED)
    _assert_metadata(docs)
    assert all(d.metadata["page"] is None for d in docs)
    cluj = [d for d in docs if "| RO113 | Cluj |" in d.page_content]
    assert len(cluj) == 1 and cluj[0].metadata["section"] == "6.4 Romanian NUTS 3 regions"
    assert cluj[0].page_content.startswith("### 6.4 Romanian NUTS 3 regions")  # headers kept for search


def test_text_yields_numbered_sections():
    """Plain text is split at numbered headings."""
    docs = load_reference(QUALITY, QUALITY.name, **SHARED)
    _assert_metadata(docs)
    routine = [d for d in docs if "Routine revisions are scheduled" in d.page_content]
    assert routine[0].metadata["section"] == "3.2 Routine revisions"
    assert docs[0].metadata["page"] is None


def test_detect_sections_ignores_table_rows_and_numbered_prose():
    """Table rows such as '01 Food' or '10 Education' and wrapped prose are not headings."""
    text = ("1 Scope\nIntro text.\n2 Codes\n01 Food and drinks\n10 Education services\n"
            "2.1 Detail\n15 calendar days after the end.\n2018, as listed in Annex I.\n3 Next\nEnd.")
    sections = [s for s, _ in detect_sections(text)]
    assert sections == ["1 Scope", "2 Codes", "2.1 Detail", "3 Next"]


def test_pdf_data_dictionary_column_sections():
    """Lower-case column headings of the IRS dictionary ("2.2 luna" ...) are sections too."""
    path = KB / "IRS_Data_Dictionary_Regional_Monthly_Indicators.pdf"
    sections = {d.metadata["section"] for d in load_reference(path, path.name, **SHARED)}
    assert {"2.2 luna", "2.3 judet", "2.4 indicator", "2.5 valoare", "2.6 um"} <= sections


def test_detect_sections_carries_section_across_pages():
    """A page that starts mid-section inherits the section in force at the end of the previous page."""
    assert detect_sections("continued text", carry="2.1 Detail") == [("2.1 Detail", "continued text")]


def test_split_documents_preserves_metadata_and_numbers_chunks():
    """Chunks respect the size limit, keep metadata, and are numbered per document."""
    docs = load_reference(NUTS, NUTS.name, **SHARED) + load_reference(QUALITY, QUALITY.name, doc_id="ESMB-Y",
                                                                       title="Q", issuer="ESMB")
    chunks = split_documents(docs, chunk_size=400, chunk_overlap=50)
    assert len(chunks) > len(docs)
    assert all(len(c.page_content) <= 400 for c in chunks)
    _assert_metadata(chunks)
    for doc_id in ("ESMB-X", "ESMB-Y"):
        numbers = [c.metadata["chunk"] for c in chunks if c.metadata["doc_id"] == doc_id]
        assert numbers == list(range(len(numbers)))


def test_split_documents_rejects_overlap_not_below_size():
    """An overlap as large as the chunk would never advance."""
    with pytest.raises(ValueError):
        split_documents([Document(page_content="x")], chunk_size=100, chunk_overlap=100)


def test_user_upload_gets_owner_and_server_side_doc_id():
    """Uploads are validated, owned by the uploader and get a 'user-' doc id derived from content."""
    data = b"# My notes\n\n## 1 Method\nWe use chain-linked volumes.\n"
    docs = load_user_reference(data, "notes.md", owner="11111111-1111-1111-1111-111111111111",
                               max_bytes=1024, max_pages=10)
    _assert_metadata(docs, owner="11111111-1111-1111-1111-111111111111")
    meta = docs[0].metadata
    assert meta["doc_id"].startswith("user-") and meta["source_type"] == "user_upload"
    assert meta["title"] == "notes" and meta["section"] == "1 Method"
    again = load_user_reference(data, "renamed.md", owner="11111111-1111-1111-1111-111111111111",
                                max_bytes=1024, max_pages=10)
    assert again[0].metadata["doc_id"] == meta["doc_id"]


def test_user_upload_rejections():
    """Oversize, malformed and text-less uploads are refused with a code."""
    with pytest.raises(UploadRejected) as exc:
        load_user_reference(b"x" * 2048, "a.txt", owner="u", max_bytes=1024, max_pages=10)
    assert exc.value.code == "too_large"
    with pytest.raises(UploadRejected) as exc:
        load_user_reference(b"%PDF-1.4 nonsense", "a.pdf", owner="u", max_bytes=1024, max_pages=10)
    assert exc.value.code == "malformed"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(UploadRejected) as exc:
        load_user_reference(buf.getvalue(), "scan.pdf", owner="u", max_bytes=10_000, max_pages=10)
    assert exc.value.code == "no_text"
    with pytest.raises(UploadRejected) as exc:
        load_user_reference(b"   \n\n  ", "blank.txt", owner="u", max_bytes=1024, max_pages=10)
    assert exc.value.code == "no_text"


def test_user_upload_requires_owner():
    """A user upload without an owner would become visible to everyone."""
    with pytest.raises(ValueError):
        load_user_reference(b"text", "a.txt", owner=None, max_bytes=1024, max_pages=10)
