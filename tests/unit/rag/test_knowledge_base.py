"""The committed RAG corpus (knowledge_base/): manifest, fictional issuers (D-006), planted facts, PDF build."""
import csv
import importlib.util
import json
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[3]
KB = ROOT / "knowledge_base"
REGIONS_CSV = ROOT / "src" / "statbrief" / "ingest" / "data" / "regions.csv"
REQUIRED_FIELDS = {"doc_id", "title", "issuer", "format", "version", "published", "file"}
# D-006: invented documents must never be attributed to real institutions.
FORBIDDEN = [re.compile(p) for p in (r"Eurostat", r"World Bank", r"\bINS\b", r"Institutul National de Statistic",
                                     r"Institutul Național de Statistic")]


def _manifest() -> list[dict]:
    """The manifest entries."""
    return json.loads((KB / "manifest.json").read_text(encoding="utf-8"))["documents"]


def _text(entry: dict) -> str:
    """Full text of one corpus document (PDF text extracted with pypdf)."""
    path = KB / entry["file"]
    if entry["format"] == "pdf":
        return "\n".join(page.extract_text() for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8")


def _by_id(doc_id: str) -> dict:
    """The manifest entry with this doc_id."""
    return next(e for e in _manifest() if e["doc_id"] == doc_id)


def test_manifest_lists_nine_documents_present_on_disk():
    """Nine entries, unique ids, all fields, the file exists and its extension matches the format."""
    entries = _manifest()
    assert len(entries) == 9
    assert len({e["doc_id"] for e in entries}) == 9
    for e in entries:
        assert REQUIRED_FIELDS <= set(e), e
        assert (KB / e["file"]).is_file(), e["file"]
        assert Path(e["file"]).suffix == "." + e["format"]
        assert e["format"] in {"pdf", "md", "txt"}
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["published"])


def test_formats_mix_pdf_md_txt():
    """The corpus exercises every loader: 4 PDF, 3 Markdown, 2 plain text."""
    formats = [e["format"] for e in _manifest()]
    assert (formats.count("pdf"), formats.count("md"), formats.count("txt")) == (4, 3, 2)


def test_issuers_are_fictional_only():
    """Every document is issued by ESMB or IRS (D-006)."""
    assert {e["issuer"] for e in _manifest()} == {"ESMB", "IRS"}
    for e in _manifest():
        assert e["doc_id"].startswith(e["issuer"] + "-")


@pytest.mark.parametrize("doc_id", [e["doc_id"] for e in _manifest()])
def test_no_real_institution_is_named(doc_id):
    """No document names Eurostat, the World Bank or INS (would impersonate real issuers, D-006)."""
    text = _text(_by_id(doc_id))
    assert len(text) > 3000, "documents must be substantial"
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{doc_id} mentions {pattern.pattern}"


def test_irs_data_dictionary_documents_columns_but_not_val_adj_x():
    """The dictionary defines luna, judet, indicator, valoare, um and never mentions val_adj_x (AC 34 demo)."""
    text = _text(_by_id("IRS-DD-REG-MONTHLY"))
    for column in ("luna", "judet", "indicator", "valoare", "um"):
        assert re.search(rf"\b{column}\b", text), column
    assert "val_adj_x" not in text and "val_adj" not in text


def test_irs_technical_note_section_3_2_explains_base_years():
    """Section 3.2 explains the 2021=100 vs 2015=100 base reference periods behind the planned conflicts."""
    text = _by_id("IRS-TN-EUR-DIFF")
    body = (KB / text["file"]).read_text(encoding="utf-8")
    section = body.split("### 3.2", 1)[1].split("\n### ", 1)[0]
    assert "2021=100" in section and "2015=100" in section
    assert "rebas" in section.lower()


def test_nuts_regulation_lists_every_romanian_region_code():
    """Every Romanian NUTS 1/2/3 code of regions.csv appears with its name in the NUTS regulation."""
    text = _text(_by_id("ESMB-REG-NUTS"))
    with REGIONS_CSV.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["level"].startswith("nuts")]
    assert len(rows) == 4 + 8 + 42
    for r in rows:
        assert re.search(rf"\|\s*{r['code']}\s*\|\s*{re.escape(r['name'])}\s*\|", text), r["code"]


def test_hicp_regulation_states_index_reference_period():
    """The HICP regulation fixes 2015=100 as the index reference period (base year)."""
    text = _text(_by_id("ESMB-REG-2023-14"))
    assert "2015=100" in text and "base year" in text.lower()
    assert 4 <= len(PdfReader(KB / _by_id("ESMB-REG-2023-14")["file"]).pages) <= 6


def test_commentary_guidelines_cover_wording_rules():
    """The commentary guidelines cover causal claims, rounding and percentage points vs percent."""
    text = _text(_by_id("ESMB-GUIDE-COMMENTARY")).lower()
    assert "percentage point" in text and "causal" in text and "round" in text


def test_committed_pdfs_match_their_markdown_sources(tmp_path):
    """Rebuilding the PDFs from knowledge_base/src reproduces the committed files byte for byte."""
    pytest.importorskip("reportlab")
    spec = importlib.util.spec_from_file_location("build_kb", ROOT / "scripts" / "build_knowledge_base.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built = module.build_all(KB, tmp_path)
    pdfs = [e for e in _manifest() if e["format"] == "pdf"]
    assert sorted(p.name for p in built) == sorted(e["file"] for e in pdfs)
    for e in pdfs:
        assert (tmp_path / e["file"]).read_bytes() == (KB / e["file"]).read_bytes(), e["file"]


@pytest.mark.parametrize("doc_id", [e["doc_id"] for e in _manifest()])
def test_hicp_base_is_consistently_2015(doc_id):
    """No document states an HICP base other than 2015=100 (the planned 2021=100 vs 2015=100 conflict)."""
    text = _text(_by_id(doc_id)).replace("\n", " ")
    bases = set(re.findall(r"\b(\d{4})=100\b", text))
    assert bases <= {"2015", "2021"}, bases
    assert not re.search(r"HICP[^.]{0,80}\b2021=100", text.replace("CPI, 2021=100", "")), doc_id


def test_hicp_regulation_change_of_base_is_undated():
    """Article 12 leaves a future change of the index reference period undated; 2015=100 applies until then."""
    src = (KB / _by_id("ESMB-REG-2023-14")["source"]).read_text(encoding="utf-8")
    article = src.split("### Article 12", 1)[1].split("\n### ", 1)[0]
    assert "2015=100" in article and not re.search(r"\b20\d\d=100", article.replace("2015=100", ""))
    assert not re.search(r"(January|February|March) 20\d\d", article)


def test_technical_note_states_relative_base_gap():
    """IRS TN 3.2 quantifies the base-year gap relative to the 2021=100 level (118.30 / 100 -> about 18 %)."""
    text = (KB / _by_id("IRS-TN-EUR-DIFF")["file"]).read_text(encoding="utf-8")
    assert "about 18 % relative to the 2021=100 level" in text
