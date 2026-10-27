"""Load reference documents (PDF / plain text / Markdown) into LangChain Documents with citation metadata.

Every Document produced here carries METADATA_KEYS so a retrieved passage can always be cited by
document, issuer, section and page. PDFs are read with pypdf (text extraction only: no script,
attachment or action is ever executed); Markdown is split on headers with LangChain's
MarkdownHeaderTextSplitter; PDF and plain-text sections are recognised by numbered headings.
"""
import io
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from statbrief.ingest.sniff import UploadRejected, reference_kind, validate_reference_bytes

METADATA_KEYS = ("doc_id", "title", "issuer", "owner", "section", "page", "source_file", "source_type")
USER_DOC_PREFIX = "user-"  # doc ids of user uploads; shared corpus ids never start with it
USER_ISSUER = "User upload"
PREAMBLE = "Preamble"  # section of text that precedes the first heading of a document
_USER_DOC_HASH_CHARS = 16
_MAX_TITLE_CHARS = 200
_MARKDOWN_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
# Numbered heading: "3", "3.2", "3.2.1" (no leading zero, so table rows like "01 Food" are not headings),
# optionally followed by a dot, then a word. Lower-case titles (e.g. "2.2 luna", a column name) are
# accepted only for short sub-section headings (see _heading).
_NUMBERED_HEADING = re.compile(r"^(?P<num>[1-9]\d?(?:\.\d{1,2}){0,3})\.?\s+(?P<first>[A-Za-z])[A-Za-z_]")
_MAX_LOWERCASE_HEADING_WORDS = 3
# Named heading: "Article 6 Index ...", "Annex A Counties", "Chapter II Compilation".
_NAMED_HEADING = re.compile(r"^(?:Article|Annex|Chapter|Part|Section)\s+(?:\d{1,3}|[IVXLC]{1,6}|[A-Z])\s+[A-Z]")
_MAX_HEADING_CHARS = 100
_HEADING_TRAILING_PUNCTUATION = ".,;:"


class PdfTextLoader(BaseLoader):
    """LangChain loader yielding one Document per PDF page (1-based `page` metadata), text only."""

    def __init__(self, data: bytes, source_file: str):
        """Bind to the PDF bytes and the original file name."""
        self.data, self.source_file = data, source_file

    def lazy_load(self) -> Iterator[Document]:
        """Extract the text of each page in order."""
        from pypdf import PdfReader  # deferred: only PDF loading needs pypdf

        reader = PdfReader(io.BytesIO(self.data), strict=False)
        for number, page in enumerate(reader.pages, start=1):
            yield Document(page_content=page.extract_text() or "",
                           metadata={"page": number, "source_file": self.source_file})


class TextBytesLoader(BaseLoader):
    """LangChain loader yielding the whole UTF-8 text (BOM tolerated) as one Document without a page."""

    def __init__(self, data: bytes, source_file: str):
        """Bind to the text bytes and the original file name."""
        self.data, self.source_file = data, source_file

    def lazy_load(self) -> Iterator[Document]:
        """Decode the text."""
        yield Document(page_content=self.data.decode("utf-8-sig"),
                       metadata={"page": None, "source_file": self.source_file})


def _top_number(section: str | None) -> int | None:
    """The top-level number of a numbered section title ("3.2 Foo" -> 3), else None."""
    match = _NUMBERED_HEADING.match(section or "")
    return int(match.group("num").split(".")[0]) if match else None


def _heading(line: str, last_top: int | None) -> str | None:
    """Return the line when it is a section heading in this position, else None.

    A numbered heading must continue the numbering: "N Title" needs N = previous top + 1 and
    "N.M Title" needs N = previous top or previous top + 1 (any number is accepted before the first
    numbered heading). This rejects table rows and wrapped prose that happen to start with a number.
    """
    if len(line) > _MAX_HEADING_CHARS or line[-1] in _HEADING_TRAILING_PUNCTUATION:
        return None
    if _NAMED_HEADING.match(line):
        return line
    match = _NUMBERED_HEADING.match(line)
    if not match:
        return None
    parts = match.group("num").split(".")
    top = int(parts[0])
    if match.group("first").islower() and (len(parts) == 1 or len(line.split()) > _MAX_LOWERCASE_HEADING_WORDS):
        return None
    if last_top is None:
        return line
    allowed = {last_top + 1} if len(parts) == 1 else {last_top, last_top + 1}
    return line if top in allowed else None


def detect_sections(text: str, carry: str | None = None) -> list[tuple[str | None, str]]:
    """Split text at headings into (section, body) pairs; text before the first heading keeps `carry`
    (PREAMBLE when there is no carried section).

    `carry` is the section in force at the end of the previous page, so a page that starts in the
    middle of a section is attributed to it. Heading lines stay in their body (useful for search).
    """
    sections: list[tuple[str | None, list[str]]] = [(carry or PREAMBLE, [])]
    last_top = _top_number(carry)
    for raw in text.splitlines():
        line = raw.strip()
        heading = _heading(line, last_top) if line else None
        if heading:
            sections.append((heading, []))
            last_top = _top_number(heading) or last_top
        sections[-1][1].append(raw)
    return [(section, "\n".join(lines).strip()) for section, lines in sections if "\n".join(lines).strip()]


def _markdown_sections(text: str) -> list[tuple[str | None, str]]:
    """Split Markdown on headers; the deepest header is the section; headers are kept in the text."""
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=_MARKDOWN_HEADERS, strip_headers=False)
    out = []
    for doc in splitter.split_text(text):
        section = next((doc.metadata[k] for k in ("h4", "h3", "h2", "h1") if k in doc.metadata), PREAMBLE)
        out.append((section, doc.page_content))
    return out


def _read(path_or_bytes: Path | str | bytes) -> bytes:
    """Bytes as given, or the content of the file at the path."""
    return path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()


def load_reference(path_or_bytes: Path | str | bytes, filename: str, *, doc_id: str, title: str, issuer: str,
                   owner: str | None = None, source_type: str = "reference",
                   version: str | None = None) -> list[Document]:
    """Load one reference document into section-level Documents carrying METADATA_KEYS (+ version).

    PDFs yield at least one Document per page that has text; Markdown and plain text yield one per
    section. `owner` None marks a shared corpus document. Raises UploadRejected("no_text") when no
    text can be extracted and UploadRejected("malformed") when the PDF cannot be read.
    """
    data, kind = _read(path_or_bytes), reference_kind(filename)
    if kind is None:
        raise UploadRejected("unsupported_extension", "Only PDF, .txt and .md reference documents are supported.")
    base = {"doc_id": doc_id, "title": title, "issuer": issuer, "owner": owner, "source_file": filename,
            "source_type": source_type, "version": version}
    docs: list[Document] = []
    if kind == "pdf":
        try:
            pages = PdfTextLoader(data, filename).load()
        except Exception as exc:  # pypdf raises many exception types on damaged files
            raise UploadRejected("malformed", "The text of this PDF could not be extracted.") from exc
        carry = None
        for page in pages:
            for section, body in detect_sections(page.page_content, carry):
                docs.append(Document(page_content=body, metadata={**base, "section": section,
                                                                  "page": page.metadata["page"]}))
                carry = section
    else:
        (whole,) = TextBytesLoader(data, filename).load()
        pairs = _markdown_sections(whole.page_content) if kind == "md" else detect_sections(whole.page_content)
        docs = [Document(page_content=body, metadata={**base, "section": section, "page": None})
                for section, body in pairs if body.strip()]
    if not docs:
        raise UploadRejected("no_text", "No text could be extracted from this document (is it a scanned image?).")
    return docs


def load_user_reference(data: bytes, filename: str, owner: str | None, max_bytes: int,
                        max_pages: int) -> list[Document]:
    """Validate and load a user-uploaded reference document, visible only to `owner`.

    The doc id is derived server-side from the content hash ("user-<hash>") so a user can neither
    impersonate a shared corpus document nor collide with another document of their own.
    """
    if not owner:
        raise ValueError("a user-uploaded reference document needs an owner")
    check = validate_reference_bytes(filename, data, max_bytes=max_bytes, max_pages=max_pages)
    title = PurePosixPath(filename.replace("\\", "/")).stem[:_MAX_TITLE_CHARS] or "Uploaded document"
    return load_reference(data, filename, doc_id=USER_DOC_PREFIX + check.sha256[:_USER_DOC_HASH_CHARS],
                          title=title, issuer=USER_ISSUER, owner=owner, source_type="user_upload")


def split_documents(docs: list[Document], chunk_size: int, chunk_overlap: int) -> list[Document]:
    """Split Documents into chunks with RecursiveCharacterTextSplitter, keeping metadata.

    Each chunk gets `chunk`, its 0-based position within its doc_id (stable ordering for ties).
    """
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_documents(docs)
    counters: Counter[str] = Counter()
    for chunk in chunks:
        doc_id = chunk.metadata.get("doc_id")
        chunk.metadata["chunk"] = counters[doc_id]
        counters[doc_id] += 1
    return chunks
