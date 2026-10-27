"""Reference-document uploads (.pdf/.txt/.md): extension + magic bytes + structure, text extraction only."""
import hashlib
import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

from statbrief.ingest.sniff import UploadRejected, _pdf_has_active_content, validate_reference_bytes

KB = Path(__file__).resolve().parents[3] / "knowledge_base"
PDF = (KB / "ESMB_Manual_Labour_Force_and_Unemployment.pdf").read_bytes()
MAX = 10 * 1024 * 1024
MAX_PAGES = 50


def _check(name: str, data: bytes, max_bytes: int = MAX, max_pages: int = MAX_PAGES):
    """Call the validator with test limits."""
    return validate_reference_bytes(name, data, max_bytes=max_bytes, max_pages=max_pages)


def _rewrite(mutate) -> bytes:
    """Copy the sample PDF through PdfWriter after applying `mutate(writer)`."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(PDF)))
    mutate(writer)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _code(name: str, data: bytes, **limits) -> str:
    """The rejection code for a file that must be rejected."""
    with pytest.raises(UploadRejected) as exc:
        _check(name, data, **limits)
    return exc.value.code


def test_accepts_pdf_txt_md_with_hash():
    """Valid reference files pass with kind, size and sha256."""
    r = _check("Manual.PDF", PDF)
    assert (r.kind, r.size_bytes, r.sha256) == ("pdf", len(PDF), hashlib.sha256(PDF).hexdigest())
    assert _check("notes.txt", "Notă metodologică\n1. Scope\n".encode()).kind == "txt"
    assert _check("guide.md", b"# Guide\n\n## 1 Scope\ntext\n").kind == "md"


@pytest.mark.parametrize("name", ["doc.docx", "doc.html", "doc", "doc.pdf.exe", "doc.csv"])
def test_rejects_other_extensions(name):
    """Only .pdf, .txt and .md are reference documents."""
    assert _code(name, b"text") == "unsupported_extension"


def test_rejects_empty_and_oversize():
    """Empty files and files above the reference size limit are refused."""
    assert _code("a.txt", b"") == "empty"
    assert _code("a.txt", b"x" * 1025, max_bytes=1024) == "too_large"
    assert _code("a.pdf", PDF, max_bytes=len(PDF) - 1) == "too_large"


def test_rejects_pdf_without_magic_and_malformed_pdf():
    """A .pdf must start with %PDF- and parse; truncated or garbage PDFs are malformed."""
    assert _code("a.pdf", b"<html>not a pdf</html>") == "malformed"
    assert _code("a.pdf", b"%PDF-1.7\n garbage without objects") == "malformed"
    assert _code("a.pdf", PDF[: len(PDF) // 3]) == "malformed"


def test_rejects_encrypted_pdf():
    """Password-protected PDFs cannot be read and are refused."""
    assert _code("a.pdf", _rewrite(lambda w: w.encrypt("secret"))) == "password_protected"


def _action(kind: str) -> DictionaryObject:
    """A PDF action dictionary of the given /S type."""
    return DictionaryObject({NameObject("/S"): NameObject(kind), NameObject("/JS"): TextStringObject("app.alert(1)")})


def _open_action(writer: PdfWriter, kind: str = "/JavaScript") -> None:
    """Set a document open action of the given type."""
    writer._root_object[NameObject("/OpenAction")] = _action(kind)


def _launch_link(writer: PdfWriter) -> None:
    """Add a link annotation with a /Launch action to the first page."""
    annot = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
                              NameObject("/A"): _action("/Launch")})
    writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(annot)])


def test_rejects_pdf_with_javascript():
    """PDFs carrying JavaScript are refused (active content is never accepted, only text is extracted)."""
    assert _code("a.pdf", _rewrite(_open_action)) == "active_content"


def test_structural_active_content_check():
    """The structural check (used when markers hide in compressed object streams) finds actions and annots."""
    def reader(mutate) -> PdfReader:
        """Parse the sample PDF after `mutate`."""
        return PdfReader(io.BytesIO(_rewrite(mutate)))

    assert not _pdf_has_active_content(PdfReader(io.BytesIO(PDF)))
    assert _pdf_has_active_content(reader(_open_action))
    assert _pdf_has_active_content(reader(lambda w: _open_action(w, "/Launch")))
    assert _pdf_has_active_content(reader(_launch_link))
    assert _pdf_has_active_content(reader(lambda w: w.add_attachment("x.exe", b"MZ")))
    assert not _pdf_has_active_content(reader(lambda w: _open_action(w, "/GoTo")))


def test_rejects_pdf_with_embedded_file():
    """PDFs with embedded file attachments are refused."""
    assert _code("a.pdf", _rewrite(lambda w: w.add_attachment("x.exe", b"MZ..."))) == "active_content"


def test_rejects_pdf_with_too_many_pages():
    """The page cap bounds text-extraction work."""
    assert _code("a.pdf", PDF, max_pages=2) == "too_many_pages"


def test_rejects_binary_or_non_utf8_text():
    """Text references must be UTF-8 text without NUL bytes."""
    assert _code("a.txt", b"abc\x00def") == "not_text"
    assert _code("a.md", "x".encode("utf-16")) == "not_text"
    assert _code("a.txt", "Braşov".encode("cp1250")) == "not_utf8"
