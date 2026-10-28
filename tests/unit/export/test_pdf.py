"""PDF export: structure, grouped citations, appendix, determinism, escaping."""
import io

from pypdf import PdfReader

from statbrief.export import pdf
from statbrief.export.bundle import ReportBundle
from statbrief.export.format import ExportOptions
from statbrief.export.pdf import APPENDIX, CITATIONS, DATA_QUALITY, EXECUTIVE_SUMMARY, build_pdf


def _text(data: bytes) -> str:
    """All page text of a PDF, whitespace-normalised."""
    reader = PdfReader(io.BytesIO(data))
    return " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages)


def test_pdf_opens_and_has_every_section():
    """The PDF parses and contains the cover data, every section title and all three citation groups."""
    b = ReportBundle.example()
    data = build_pdf(b)
    assert data.startswith(b"%PDF")
    text = _text(data)
    for title in (b.draft.title, EXECUTIVE_SUMMARY, "1. Consumer price inflation", DATA_QUALITY, CITATIONS,
                  "Uploaded files", "Public APIs", "Reference documents", APPENDIX,
                  "Validation exclusions", "Gap fills", "Conflict resolutions", "Unit differences", "Caveats"):
        assert title in text, title
    assert "Approved by: analyst@example.com" in text and "2023-03 to 2024-04" in text


def test_appendix_lists_every_claim_metric_and_rounds_at_claim_precision():
    """Each claim's metric id appears; values are shown with the precision of the claim text."""
    b = ReportBundle.example()
    text = _text(build_pdf(b)).replace(" ", "")
    for claim in b.draft.claims:
        if claim.metric_id:
            assert claim.metric_id in text
    assert "-7.8pp" in text  # value -7.7999... stated as "7.8 percentage points"


def test_deterministic():
    """The same bundle yields byte-identical PDFs (invariant mode; only the bundle's timestamps print)."""
    b = ReportBundle.example()
    assert build_pdf(b) == build_pdf(b)


def test_markup_in_draft_text_is_escaped():
    """LLM text with reportlab markup is printed literally, not interpreted."""
    data = ReportBundle.example().model_dump()
    data["draft"]["title"] = 'Prices <a href="https://evil.example">click</a> & <b>bold'
    data["draft"]["sections"][0]["paragraphs"] = ["<font size=40>big</font> 5 < 6"]
    text = _text(build_pdf(ReportBundle.model_validate(data)))
    assert '<a href="https://evil.example">click</a>' in text
    assert "<font size=40>big</font> 5 < 6" in text


def test_empty_groups_and_notes_print_none():
    """A bundle without data-quality records or references still prints every heading, with "None."."""
    data = ReportBundle.example().model_dump()
    data["draft"]["citations"] = [c for c in data["draft"]["citations"] if c["kind"] != "reference"]
    data.update(citations=[c for c in data["citations"] if c["kind"] != "reference"], validation_exclusions=[],
                gap_fill=None, conflicts=[], conflict_exclusions=[], unit_mismatches=[])
    text = _text(build_pdf(ReportBundle.model_validate(data)))
    assert "Reference documents None." in text and "Gap fills None." in text


def test_many_series_are_capped(monkeypatch):
    """Charts draw at most chart_max_series lines."""
    seen = []
    real = pdf._chart

    def spy(unit, series, n):
        """Record how many lines the chart will draw."""
        seen.append(min(len(series), n))
        return real(unit, series, n)

    monkeypatch.setattr(pdf, "_chart", spy)
    build_pdf(ReportBundle.example(), ExportOptions(chart_max_series=1))
    assert seen and max(seen) == 1
