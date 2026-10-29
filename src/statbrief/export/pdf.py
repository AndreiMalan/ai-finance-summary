"""PDF export of an approved report (reportlab platypus + reportlab.graphics charts, AC 47).

Layout: cover (title, period, regions, generated date, approver) · executive summary · one section per
indicator with its paragraphs and a line chart of the observations used · data-quality notes (validation
exclusions, gap fills, conflict resolutions, unit differences, caveats) · citations grouped Uploaded files /
Public APIs / Reference documents · calculation appendix (every claim: value, metric id, formula, input
observations → source).

Every piece of text from the draft or the data is XML-escaped before it reaches a reportlab Paragraph
(Paragraph parses markup, so unescaped LLM or file text could inject links or break the build). The
document is built with reportlab's `invariant` mode, so the same bundle always yields the same bytes; the
only timestamps printed are the bundle's own (`generated_at`, `approved_at`).
"""
import io
import threading
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from statbrief.analytics.combine import source_label
from statbrief.domain.observation import Observation
from statbrief.export.bundle import ReportBundle
from statbrief.export.format import (ExportOptions, claim_decimals, format_number, observation_source, unit_text)

# Section titles (protocol: tests and readers rely on them).
EXECUTIVE_SUMMARY = "Executive summary"
DATA_QUALITY = "Data-quality notes"
CITATIONS = "Citations"
APPENDIX = "Calculation appendix"

# Page and chart geometry in points (presentation layout, not tunables).
_MARGIN = 2 * cm
_CHART_W = 440
_PLOT_X, _PLOT_W, _PLOT_H = 45, 380, 120
_LEGEND_ROW_H = 9  # one legend line per series
_AXIS_LABEL_SPACE = 45  # room below the plot for the rotated period labels
_TOP_SPACE = 20  # room above the plot for the unit caption
_MAX_AXIS_LABELS = 12  # x-axis labels shown per chart; the others are left blank
_SERIES_COLORS = (colors.HexColor("#1f6feb"), colors.HexColor("#d1495b"), colors.HexColor("#2a9d8f"),
                  colors.HexColor("#e9a23b"), colors.HexColor("#6f42c1"), colors.HexColor("#6c757d"))
_APPENDIX_COL_WIDTHS = (2.2 * cm, 2.2 * cm, 4.6 * cm, 7.0 * cm)
_NONE = "None."
# Embedded fonts with full Latin Extended coverage (Romanian ș ț ă î â, incl. cedilla forms ş ţ). The
# built-in Type 1 Helvetica is WinAnsi-only and renders those as boxes. DejaVu Sans, vendored under
# export/fonts/ with its LICENSE (Bitstream Vera / DejaVu terms permit redistribution).
_FONT_DIR = Path(__file__).parent / "fonts"
FONT, FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
_FONT_FILES = {FONT: "DejaVuSans.ttf", FONT_BOLD: "DejaVuSans-Bold.ttf"}
_font_lock = threading.Lock()


def register_fonts() -> None:
    """Register the embedded TTF fonts with reportlab once per process (idempotent, thread-safe)."""
    with _font_lock:
        registered = set(pdfmetrics.getRegisteredFontNames())
        for name, filename in _FONT_FILES.items():
            if name not in registered:
                pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / filename)))
        pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    """Paragraph of plain (escaped) text."""
    return Paragraph(escape(text), style)


def _styles() -> dict[str, ParagraphStyle]:
    """Paragraph styles used by the document, all in the embedded fonts."""
    base = getSampleStyleSheet()

    def font(style: ParagraphStyle, bold: bool = False, **kw) -> ParagraphStyle:
        """A copy of `style` in the embedded (bold) font."""
        return ParagraphStyle(f"SB{style.name}{len(kw)}", parent=style, fontName=FONT_BOLD if bold else FONT,
                              bulletFontName=FONT, **kw)

    body = font(base["BodyText"])
    return {"title": font(base["Title"], bold=True), "h1": font(base["Heading1"], bold=True),
            "h2": font(base["Heading2"], bold=True), "h3": font(base["Heading3"], bold=True), "body": body,
            "small": font(base["BodyText"], fontSize=8, leading=10),
            "meta": font(base["BodyText"], fontSize=11, leading=15, spaceAfter=4)}


def _bullets(items: list[str], style: ParagraphStyle) -> list:
    """A bullet list of escaped texts, or a "None." paragraph."""
    if not items:
        return [_p(_NONE, style)]
    return [ListFlowable([ListItem(_p(t, style), leftIndent=12) for t in items], bulletType="bullet",
                         leftIndent=12)]


def _period_text(bundle: ReportBundle) -> str:
    """The report period as printed on the cover."""
    p = bundle.params
    if p.period_from and p.period_to:
        return f"{p.period_from} to {p.period_to}"
    return p.period_from or p.period_to or "all periods in the data"


def _cover(bundle: ReportBundle, st: dict[str, ParagraphStyle]) -> list:
    """Cover page: title, period, regions, generated date and approver."""
    p = bundle.params
    regions = ", ".join(p.regions) if p.regions else "all regions in the data"
    return [Spacer(1, 4 * cm), _p(bundle.draft.title, st["title"]), Spacer(1, 1 * cm),
            _p(f"Period: {_period_text(bundle)}", st["meta"]), _p(f"Regions: {regions}", st["meta"]),
            _p(f"Generated: {bundle.generated_at.strftime('%Y-%m-%d %H:%M %Z')}", st["meta"]),
            _p(f"Approved by: {bundle.approved_by} on {bundle.approved_at.strftime('%Y-%m-%d')}", st["meta"]),
            PageBreak()]


def _series(obs: list[Observation]) -> dict[tuple[str, int], dict[str, dict[str, float]]]:
    """(unit, multiplier) → series label → {period: value} (missing values skipped)."""
    out: dict[tuple[str, int], dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for o in obs:
        if o.value is not None:
            out[(o.unit, o.unit_multiplier)][f"{o.region_code} · {source_label(o)}"][o.period] = o.value
    return out


def _chart(unit: str, series: dict[str, dict[str, float]], max_series: int) -> Drawing:
    """A line chart of up to `max_series` series (most points first) over the union of their periods.

    Points carry markers so a series with a single observation is still visible; the legend lists one
    series per line below the period labels.
    """
    chosen = sorted(series.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max_series]
    periods = sorted({p for _, points in chosen for p in points})
    step = max(1, -(-len(periods) // _MAX_AXIS_LABELS))
    legend_top = 8 + _LEGEND_ROW_H * len(chosen)
    plot_y = legend_top + _AXIS_LABEL_SPACE
    d = Drawing(_CHART_W, plot_y + _PLOT_H + _TOP_SPACE)
    lc = HorizontalLineChart()
    lc.x, lc.y, lc.width, lc.height = _PLOT_X, plot_y, _PLOT_W, _PLOT_H
    lc.data = [[points.get(p) for p in periods] for _, points in chosen]
    lc.categoryAxis.categoryNames = [p if i % step == 0 else "" for i, p in enumerate(periods)]
    lc.categoryAxis.labels.angle = 45
    lc.categoryAxis.labels.boxAnchor = "ne"
    lc.categoryAxis.labels.fontSize = 6
    lc.categoryAxis.labels.fontName = FONT
    lc.valueAxis.labels.fontName = FONT
    lc.valueAxis.labels.fontSize = 7
    for i in range(len(chosen)):
        color = _SERIES_COLORS[i % len(_SERIES_COLORS)]
        lc.lines[i].strokeColor = color
        lc.lines[i].strokeWidth = 1.2
        lc.lines[i].symbol = makeMarker("FilledCircle", size=3, fillColor=color, strokeColor=color)
    d.add(lc)
    d.add(String(_PLOT_X, plot_y + _PLOT_H + 8, f"Unit: {unit}", fontSize=7, fontName=FONT))
    legend = Legend()
    legend.fontName = FONT
    legend.x, legend.y = _PLOT_X, legend_top
    legend.fontSize, legend.columnMaximum, legend.dx, legend.dy = 6, len(chosen), 6, 6
    legend.deltay = _LEGEND_ROW_H
    legend.colorNamePairs = [(_SERIES_COLORS[i % len(_SERIES_COLORS)], label)
                             for i, (label, _) in enumerate(chosen)]
    d.add(legend)
    return d


def _sections(bundle: ReportBundle, st: dict[str, ParagraphStyle], opts: ExportOptions) -> list:
    """One section per indicator: heading, paragraphs and one chart per unit of its observations."""
    out: list = []
    for n, section in enumerate(bundle.draft.sections, start=1):
        out.append(_p(f"{n}. {section.title}", st["h1"]))
        out += [_p(text, st["body"]) for text in section.paragraphs]
        obs = [o for o in bundle.observations if o.indicator_code == section.indicator_code]
        for (unit, mult), series in sorted(_series(obs).items()):
            out += [Spacer(1, 6), KeepTogether([_chart(unit_text(unit, mult), series, opts.chart_max_series)]),
                    Spacer(1, 6)]
    return out


def _data_quality(bundle: ReportBundle, st: dict[str, ParagraphStyle]) -> list:
    """Validation exclusions, gap fills, conflict resolutions, unit differences and caveats."""
    exclusions = [f"{e.filename}{f' (sheet {e.worksheet})' if e.worksheet else ''}: {e.issue_code}"
                  f"{f' in column {e.column}' if e.column else ''}, rows {', '.join(map(str, e.rows)) or '-'}: "
                  f"{e.message}" for e in bundle.validation_exclusions]
    fills = []
    if bundle.gap_fill is not None:
        fills = [f"{f.upload_indicator} {f.region} {f.period}: filled from {f.api_name} {f.series_id} "
                 f"(observation {f.source_obs_id})" for f in bundle.gap_fill.filled]
        fills += [f"{g.indicator} {g.region} {g.period}: not filled ({g.reason})" for g in bundle.gap_fill.not_filled]
    resolutions = []
    for note in bundle.conflicts:
        c, r = note.conflict, note.resolution
        values = ", ".join(f"{s}: {v!r}" for s, v in zip(c.sources, c.values, strict=True))
        decision = {"prefer": f"kept {r.preferred_obs_id}", "keep_both": "kept both values, labelled by source",
                    "exclude": "excluded all values"}[r.choice]
        resolutions.append(f"{c.indicator} {c.region} {c.period} ({unit_text(c.unit, c.unit_multiplier)}): "
                           f"{values}; {decision}.")
    resolutions += [f"{e.obs_id} excluded: {e.reason}" for e in bundle.conflict_exclusions]
    mismatches = [f"{m.indicator} {m.region} {m.period}: units {', '.join(m.units)} are not comparable; "
                  f"values kept separately" for m in bundle.unit_mismatches]
    out = [_p(DATA_QUALITY, st["h1"])]
    for heading, items in (("Validation exclusions", exclusions), ("Gap fills", fills),
                           ("Conflict resolutions", resolutions), ("Unit differences", mismatches),
                           ("Caveats", list(bundle.draft.caveats))):
        out += [_p(heading, st["h2"]), *_bullets(items, st["body"])]
    return out


def _citations(bundle: ReportBundle, st: dict[str, ParagraphStyle]) -> list:
    """Citations grouped Uploaded files / Public APIs / Reference documents (every group printed)."""
    out = [_p(CITATIONS, st["h1"])]
    for heading, cites in bundle.citations_by_group():
        items = [f"{c.label}: {c.source_label}" + (f", {c.locator}" if c.locator else "")
                 + (f" ({c.url})" if c.url else "") for c in cites]
        out += [_p(heading, st["h2"]), *_bullets(items, st["body"])]
    return out


def _appendix(bundle: ReportBundle, st: dict[str, ParagraphStyle], opts: ExportOptions) -> list:
    """Every claim: rounded value, metric id, formula and its input observations with their sources.

    One table row per input observation (claim, value and formula only on the claim's first row), so a
    claim citing hundreds of observations flows over pages instead of overflowing one; `splitInRow` also
    lets a single tall row split.
    """
    small = st["small"]
    rows = [[_p(h, small) for h in ("Claim", "Value", "Metric / formula", "Inputs (observation → source)")]]
    first_rows: list[int] = []
    for claim in bundle.draft.claims:
        decimals = claim_decimals(claim)
        value = format_number(claim.value, opts.default_decimals if decimals is None else decimals)
        metric = bundle.metric(claim.metric_id) if claim.metric_id else None
        formula = f"{claim.metric_id}\n{metric.formula}" if metric else "direct observation (no metric)"
        first_rows.append(len(rows))
        for i, oid in enumerate(claim.observation_ids):
            obs = bundle.observation(oid)
            source = _p(f"{oid} → {observation_source(obs)}" if obs else oid, small)
            if i == 0:
                rows.append([_p(claim.claim_id, small), _p(f"{value} {claim.unit}", small),
                             Paragraph(escape(formula).replace("\n", "<br/>"), small), source])
            else:
                rows.append(["", "", "", source])
    style = [("BOX", (0, 0), (-1, -1), 0.25, colors.grey), ("INNERGRID", (3, 0), (3, -1), 0.25, colors.lightgrey),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]
    style += [("LINEABOVE", (0, r), (-1, r), 0.25, colors.grey) for r in first_rows]
    table = Table(rows, colWidths=_APPENDIX_COL_WIDTHS, repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle(style))
    return [PageBreak(), _p(APPENDIX, st["h1"]), table]


def build_pdf(bundle: ReportBundle, options: ExportOptions | None = None) -> bytes:
    """Render the bundle as a PDF document (see module doc); deterministic for a given bundle."""
    opts = options or ExportOptions()
    register_fonts()
    st = _styles()
    story = _cover(bundle, st)
    story += [_p(EXECUTIVE_SUMMARY, st["h1"]), *_bullets([f.headline for f in bundle.draft.executive_summary],
                                                         st["body"])]
    story += _sections(bundle, st, opts)
    story += _data_quality(bundle, st)
    story += _citations(bundle, st)
    story += _appendix(bundle, st, opts)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN,
                            bottomMargin=_MARGIN, title=bundle.draft.title, author="StatBrief",
                            subject=f"Report {bundle.report_id}", creator="StatBrief", invariant=1)
    doc.build(story)
    return buf.getvalue()
