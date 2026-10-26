"""In-memory builders for upload test files (no files on disk)."""
import csv
import io
import zipfile

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


def csv_bytes(rows: list[list], delimiter: str = ",", encoding: str = "utf-8") -> bytes:
    """Serialise rows as CSV in the given encoding."""
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode(encoding)
