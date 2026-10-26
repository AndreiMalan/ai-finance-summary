"""Downloadable data template (long format) in CSV and XLSX, values only (AC 17)."""
import csv
import io

from openpyxl import Workbook

TEMPLATE_COLUMNS = ["date", "region", "indicator", "value", "unit"]
DATA_SHEET = "Data"
README_SHEET = "README"
_EXAMPLE_ROWS = [
    ["2024-01", "RO113", "HICP_YOY", 7.4, "%"],
    ["2024-02", "RO113", "HICP_YOY", 7.1, "%"],
    ["2024-03", "RO113", "HICP_YOY", 6.6, "%"],
]
_README = [
    ["Column", "Meaning"],
    ["date", "Period: 2024 (year), 2024-Q1 (quarter), 2024-03 or 2024M03 (month)."],
    ["region", "Region code (RO, RO113, EU27_2020) or name (Cluj, Romania)."],
    ["indicator", "Your indicator code, e.g. HICP_YOY or UNEMP_RATE."],
    ["value", "Number; use a dot or comma as decimal separator; leave empty for a missing value."],
    ["unit", "Unit, e.g. %, pp, EUR, RON, thousand persons, Index 2015=100."],
]


def build_template_csv() -> bytes:
    """Template as UTF-8 CSV: header plus example rows."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TEMPLATE_COLUMNS)
    w.writerows(_EXAMPLE_ROWS)
    return buf.getvalue().encode("utf-8")


def build_template_xlsx() -> bytes:
    """Template as XLSX with a Data sheet (first, active) and a README sheet; values only, no formulas."""
    wb = Workbook()
    data = wb.active
    data.title = DATA_SHEET
    data.append(TEMPLATE_COLUMNS)
    for r in _EXAMPLE_ROWS:
        data.append(r)
    readme = wb.create_sheet(README_SHEET)
    for r in _README:
        readme.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
