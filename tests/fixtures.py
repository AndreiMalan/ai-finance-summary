"""Builders for upload test files (in memory) and paths to the few committed binary samples."""
import csv
import io
import re
import zipfile
from pathlib import Path

from openpyxl import Workbook


def xlsx_bytes(sheets: dict[str, list[list]], hidden: frozenset[str] = frozenset()) -> bytes:
    """Build an .xlsx workbook with the given sheets (first sheet active)."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
        if name in hidden:
            ws.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_with_formula_no_cache() -> bytes:
    """Workbook whose B2 is a formula saved by openpyxl (which stores no cached value)."""
    return xlsx_bytes({"S": [["luna", "valoare"], ["2024-01", "=1+1"], ["2024-02", 7.1]]})


def add_zip_member(data: bytes, name: str, content: bytes) -> bytes:
    """Return a copy of a zip/xlsx with an extra member (e.g. a fake vbaProject.bin)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            z.writestr(item, src.read(item.filename))
        z.writestr(name, content)
    return out.getvalue()


def edit_xlsx_sheet(data: bytes, dimension: str | None = None, extra_rows_xml: bytes = b"") -> bytes:
    """Rewrite the first worksheet's XML: replace its declared <dimension> and/or append raw <row>s."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                if dimension is not None:
                    body = re.sub(rb'<dimension ref="[^"]*"', b'<dimension ref="' + dimension.encode() + b'"', body)
                body = body.replace(b"</sheetData>", extra_rows_xml + b"</sheetData>")
            dst.writestr(item, body)
    return out.getvalue()


def csv_bytes(rows: list[list], delimiter: str = ",", encoding: str = "utf-8") -> bytes:
    """Serialise rows as CSV in the given encoding."""
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode(encoding)


# Committed legacy .xls samples, saved once with Microsoft Excel (BIFF8, xlExcel8 format):
#   sample.xls          - visible sheet "Date" (luna/valoare/calc: two dates, numbers, "=1+1" cached as 2)
#                         plus a hidden sheet "Ascuns" placed first;
#   xlm_macro_sheet.xls - one worksheet plus an empty Excel 4.0 (XLM) macro sheet;
#   encrypted.xls       - password-protected (FILEPASS record).
DATA_DIR = Path(__file__).parent / "data"
SAMPLE_XLS = DATA_DIR / "sample.xls"
XLM_MACRO_XLS = DATA_DIR / "xlm_macro_sheet.xls"
ENCRYPTED_XLS = DATA_DIR / "encrypted.xls"
