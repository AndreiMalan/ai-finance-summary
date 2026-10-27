"""Render the PDF documents of the RAG corpus from their Markdown sources (knowledge_base/src/*.md).

Usage: python scripts/build_knowledge_base.py [--out DIR]

Reads knowledge_base/manifest.json; every entry with format "pdf" names its Markdown `source`.
Output is byte-for-byte reproducible (reportlab invariant mode), so the committed PDFs can be
checked against their sources by the test suite. Requires reportlab (requirements-dev.txt only).

Supported Markdown subset: `#`/`##`/`###` headings, paragraphs, `- ` bullet lists,
pipe tables (with a `|---|` separator row) and `**bold**`.
"""
import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = REPO_ROOT / "knowledge_base"
PAGE_MARGIN = 2.2 * cm
BODY_FONT_SIZE = 10
TABLE_FONT_SIZE = 8.5
RULE_COLOR = colors.HexColor("#1F3A5F")
TABLE_HEADER_BG = colors.HexColor("#E3E9F1")


@dataclass
class Block:
    """One Markdown block: a heading (level 1-3), a paragraph, a bullet list or a table."""

    kind: str  # "h1" | "h2" | "h3" | "para" | "bullets" | "table"
    text: str = ""
    items: list = field(default_factory=list)


def _inline(text: str) -> str:
    """Escape XML special characters and convert **bold** to reportlab markup."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text))


def _cells(line: str) -> list[str]:
    """Split one pipe-table row into stripped cell texts."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_markdown(md: str) -> list[Block]:
    """Parse the supported Markdown subset into blocks (blank lines separate paragraphs)."""
    blocks: list[Block] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            blocks.append(Block(kind=f"h{len(heading.group(1))}", text=heading.group(2).strip()))
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.fullmatch(r"\|[\s\-|:]+\|?", lines[i].strip()):
                    rows.append(_cells(lines[i]))
                i += 1
            blocks.append(Block(kind="table", items=rows))
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:].strip())
                i += 1
            blocks.append(Block(kind="bullets", items=items))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,3}\s|\||- )", lines[i]):
                para.append(lines[i].strip())
                i += 1
            blocks.append(Block(kind="para", text=" ".join(para)))
    return blocks


def _styles() -> dict[str, ParagraphStyle]:
    """Paragraph styles for the official-document look."""
    base = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica", fontSize=BODY_FONT_SIZE,
                          leading=BODY_FONT_SIZE * 1.4, alignment=TA_JUSTIFY, spaceAfter=6)
    return {
        "h1": ParagraphStyle("H1", parent=base["Title"], fontName="Helvetica-Bold", fontSize=16, leading=20,
                             textColor=RULE_COLOR, spaceAfter=12),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12.5,
                             leading=16, textColor=RULE_COLOR, spaceBefore=12, spaceAfter=6),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10.5,
                             leading=14, spaceBefore=8, spaceAfter=4),
        "para": body,
        "cell": ParagraphStyle("Cell", parent=body, fontSize=TABLE_FONT_SIZE, leading=TABLE_FONT_SIZE * 1.3,
                               alignment=0, spaceAfter=0),
        "meta": ParagraphStyle("Meta", parent=body, fontSize=9, textColor=colors.HexColor("#444444")),
    }


def _table(rows: list[list[str]], styles: dict[str, ParagraphStyle], width: float) -> Table:
    """A full-width grid table whose first row is the header."""
    data = [[Paragraph(("<b>%s</b>" if r == 0 else "%s") % _inline(c), styles["cell"]) for c in row]
            for r, row in enumerate(rows)]
    table = Table(data, colWidths=[width / len(rows[0])] * len(rows[0]), repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8C9AAB")),
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _flowables(blocks: list[Block], entry: dict, width: float) -> list:
    """Turn parsed blocks into reportlab flowables, with a metadata line under the title."""
    styles = _styles()
    story: list = []
    for block in blocks:
        if block.kind in ("h1", "h2", "h3"):
            story.append(Paragraph(_inline(block.text), styles[block.kind]))
            if block.kind == "h1":
                story.append(Paragraph(
                    _inline(f"{entry['issuer_name']} ({entry['issuer']}) · Document {entry['doc_id']} · "
                            f"Version {entry['version']} · Published {entry['published']}"), styles["meta"]))
                story.append(Spacer(1, 8))
        elif block.kind == "para":
            story.append(Paragraph(_inline(block.text), styles["para"]))
        elif block.kind == "bullets":
            story.append(ListFlowable(
                [ListItem(Paragraph(_inline(t), styles["para"]), leftIndent=14) for t in block.items],
                bulletType="bullet", start="•", leftIndent=14))
        elif block.kind == "table":
            story.append(_table(block.items, styles, width))
            story.append(Spacer(1, 8))
    return story


def _page_decorator(entry: dict):
    """Return an onPage callback drawing the running header, rule and page-numbered footer."""

    def draw(canvas, doc) -> None:
        """Draw header and footer on one page."""
        width, height = A4
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(RULE_COLOR)
        canvas.drawString(PAGE_MARGIN, height - 1.3 * cm, entry["issuer_name"])
        canvas.drawRightString(width - PAGE_MARGIN, height - 1.3 * cm, entry["doc_id"])
        canvas.setStrokeColor(RULE_COLOR)
        canvas.setLineWidth(0.6)
        canvas.line(PAGE_MARGIN, height - 1.45 * cm, width - PAGE_MARGIN, height - 1.45 * cm)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(PAGE_MARGIN, 1.2 * cm, f"Version {entry['version']} - {entry['published']}")
        canvas.drawRightString(width - PAGE_MARGIN, 1.2 * cm, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def render_pdf(md_text: str, entry: dict, out_path: Path) -> Path:
    """Render one Markdown source to a reproducible PDF at `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4, leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN, bottomMargin=PAGE_MARGIN, title=entry["title"], author=entry["issuer_name"],
        subject=entry["doc_id"], creator=entry["issuer_name"], invariant=1,
    )
    decorate = _page_decorator(entry)
    doc.build(_flowables(parse_markdown(md_text), entry, doc.width), onFirstPage=decorate, onLaterPages=decorate)
    return out_path


def build_all(kb_dir: Path = KB_DIR, out_dir: Path | None = None) -> list[Path]:
    """Render every PDF entry of `kb_dir/manifest.json` into `out_dir` (default: kb_dir); return the paths."""
    out_dir = out_dir or kb_dir
    manifest = json.loads((kb_dir / "manifest.json").read_text(encoding="utf-8"))
    built = []
    for entry in manifest["documents"]:
        if entry["format"] != "pdf":
            continue
        md_text = (kb_dir / entry["source"]).read_text(encoding="utf-8")
        built.append(render_pdf(md_text, entry, out_dir / entry["file"]))
    return built


def main() -> None:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=None, help="output directory (default: knowledge_base/)")
    args = ap.parse_args()
    for path in build_all(KB_DIR, args.out):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
