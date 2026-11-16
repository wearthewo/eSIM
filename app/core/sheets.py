"""Excel (.xlsx) files built in memory with openpyxl.

Used for ``manifest.xlsx`` and ``mailmerge.xlsx`` in the ZIP and for the
downloadable upload template. They are XLSX rather than CSV because Excel 2007
ignores the UTF-8 BOM of a CSV (Greek turns into mojibake) and turns ICCIDs
into numbers such as 8.999E+18, losing digits.

Every cell is an explicit string cell (``data_type "s"``) with the text number
format ``@``, so Excel never treats a value as a number, a date or a formula. A
value such as ``=cmd|...`` is stored as text and shown literally; it is never a
formula, so the CSV-era ``'`` prefix is not needed (in an XLSX it would show up
as a visible character and change the value).

Output is deterministic: fixed document timestamps and fixed timestamps on the
entries of the XLSX archive. Nothing touches the disk: openpyxl's
``ExcelWriter`` stages each worksheet in a temporary file, so worksheets are
written to memory instead (``_MemoryExcelWriter``).
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from collections.abc import Iterable, Sequence

from openpyxl import Workbook
from openpyxl.drawing.spreadsheet_drawing import SpreadsheetDrawing
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._writer import WorksheetWriter
from openpyxl.writer.excel import ExcelWriter

# Earliest timestamp a ZIP entry can hold; fixed so identical input gives identical bytes.
FIXED_DATE_TIME: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)
FIXED_TIMESTAMP = datetime.datetime(*FIXED_DATE_TIME)  # workbook created/modified
TEXT_FORMAT = "@"
MIN_COLUMN_WIDTH = 10
MAX_COLUMN_WIDTH = 60

TEMPLATE_SHEET = "eSIMs"
# Every column the parser understands. Use either activation_code or the
# smdp_address + matching_id pair; when both are filled, activation_code wins.
TEMPLATE_COLUMNS: tuple[str, ...] = (
    "iccid",
    "activation_code",
    "smdp_address",
    "matching_id",
    "confirmation_code",
    "imsi",
    "msisdn",
    "customer_name",
    "plan_name",
    "email",
)
# Synthetic example row: fake 8999 ICCID (valid Luhn digit), example.invalid hosts.
TEMPLATE_EXAMPLE: tuple[str, ...] = (
    "8999000000000000013",
    "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB",
    "",
    "",
    "",
    "001010000000001",
    "+306900000001",
    "Test Customer",
    "Test Plan 5GB",
    "customer@example.invalid",
)

# Characters XML 1.0 cannot hold (openpyxl refuses them); removed from cell text.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class _FixedTimeZipFile(zipfile.ZipFile):
    """ZipFile whose entries written by name get a fixed timestamp."""

    def writestr(self, zinfo_or_arcname, data, compress_type=None, compresslevel=None):
        if not isinstance(zinfo_or_arcname, zipfile.ZipInfo):
            zinfo_or_arcname = zipfile.ZipInfo(zinfo_or_arcname, date_time=FIXED_DATE_TIME)
            zinfo_or_arcname.compress_type = self.compression
        super().writestr(zinfo_or_arcname, data, compress_type, compresslevel)


class _MemoryExcelWriter(ExcelWriter):
    """``ExcelWriter`` that serialises worksheets into memory, never a temporary file.

    Mirrors ``ExcelWriter.write_worksheet`` (openpyxl 3.1) for normal (not
    write-only) workbooks; ``WorksheetWriter`` supports a ``BytesIO`` target.
    """

    def write_worksheet(self, ws) -> None:
        ws._drawing = SpreadsheetDrawing()
        ws._drawing.charts = ws._charts
        ws._drawing.images = ws._images
        writer = WorksheetWriter(ws, out=io.BytesIO())
        writer.write()
        ws._rels = writer._rels
        self._archive.writestr(ws.path[1:], writer.read())
        self.manifest.append(ws)


def text_cell_value(value: object) -> str:
    """The text written to a cell: ``str(value)`` (``None`` -> ``""``) without XML-illegal
    control characters."""
    return _ILLEGAL_XML.sub("", "" if value is None else str(value))


def text_xlsx(header: Sequence[str], rows: Iterable[Sequence[object]], *, title: str) -> bytes:
    """Return XLSX bytes with one sheet: a bold, frozen header row, then ``rows``.

    Every cell is a string cell formatted as text (``@``), and so is every
    column, so values typed in later stay text too. Empty values are left as
    blank cells (still formatted as text). Column widths fit the
    content (between ``MIN_COLUMN_WIDTH`` and ``MAX_COLUMN_WIDTH``).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = title
    widths = [len(name) for name in header]
    bold = Font(bold=True)
    for r, row in enumerate([header, *rows], start=1):
        for c, value in enumerate(row, start=1):
            text = text_cell_value(value)
            cell = ws.cell(row=r, column=c)
            if text:  # an empty value stays a blank (text-formatted) cell
                cell.value = text
                cell.data_type = "s"  # set after the value: openpyxl infers "f" for "=..."
            cell.number_format = TEXT_FORMAT
            if r == 1:
                cell.font = bold
            if c <= len(widths):
                widths[c - 1] = max(widths[c - 1], len(text))
    for c, width in enumerate(widths, start=1):
        column = ws.column_dimensions[get_column_letter(c)]
        column.number_format = TEXT_FORMAT
        column.width = min(MAX_COLUMN_WIDTH, max(MIN_COLUMN_WIDTH, width + 2))
    ws.freeze_panes = "A2"
    return _save(wb)


def build_template() -> bytes:
    """The upload template: every supported column and one synthetic example row."""
    return text_xlsx(TEMPLATE_COLUMNS, [TEMPLATE_EXAMPLE], title=TEMPLATE_SHEET)


def _save(wb: Workbook) -> bytes:
    """Serialise ``wb`` in memory with fixed timestamps (``Workbook.save`` stamps now)."""
    wb.properties.created = FIXED_TIMESTAMP
    wb.properties.modified = FIXED_TIMESTAMP
    buf = io.BytesIO()
    archive = _FixedTimeZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    _MemoryExcelWriter(wb, archive).save()  # closes the archive
    return buf.getvalue()
