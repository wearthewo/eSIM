"""End-to-end orchestration: parse -> validate -> render -> zip, all in memory.

Large uploads are generated in parts of ``GENERATE_CHUNK_ROWS`` rows, one ZIP per
part, so no single archive gets too big to build and download. Nothing is kept
between calls: every part call gets the whole upload, validates all of it (so
duplicates are caught across parts) and renders only its own rows.

Never log activation codes, confirmation codes, or file contents.
"""

from __future__ import annotations

import threading

from app.core.models import Branding, ValidationReport
from app.core.parser import ParsedUpload, parse_table
from app.core.render import pdf_filename, prepare_branding, register_fonts, render_pdf
from app.core.validator import validate
from app.core.zipper import build_mailmerge, build_manifest, build_zip, sanitize_filename

GENERATE_CHUNK_ROWS = 500  # rows (PDFs) per part, i.e. per ZIP

_fonts_ready = False
_fonts_lock = threading.Lock()


class PartError(ValueError):
    """The requested part does not exist for this upload. The message has no cell values."""


def part_count(rows: int) -> int:
    """How many parts an upload with ``rows`` data rows is generated in (at least 1)."""
    return max(1, -(-rows // GENERATE_CHUNK_ROWS))


def _ensure_fonts() -> None:
    """Register the PDF fonts once per process."""
    global _fonts_ready
    if _fonts_ready:
        return
    with _fonts_lock:
        if not _fonts_ready:
            register_fonts()
            _fonts_ready = True


def check_upload(data: bytes, filename: str) -> tuple[ParsedUpload, ValidationReport]:
    """Parse and validate only (no rendering). ``ParseError`` propagates to the caller."""
    parsed = parse_table(data, filename)
    return parsed, validate(parsed.records, has_email_column=parsed.has_email)


def process_upload(
    data: bytes, filename: str, branding: Branding, *, part: int = 1, parts: int = 1
) -> tuple[ValidationReport, bytes | None]:
    """Process one part (1-based) of one upload.

    Returns the validation report for the whole upload and the ZIP bytes for
    rows ``(part - 1) * GENERATE_CHUNK_ROWS`` onwards, at most
    ``GENERATE_CHUNK_ROWS`` of them. The ZIP is ``None`` when the report has any
    error-severity issue anywhere in the upload (all-or-nothing; warnings
    allowed). Its ``manifest.csv`` covers only this part's rows, and so does
    ``mailmerge.csv``, which is added when the upload has an ``email`` column.

    Raises ``PartError`` when ``part`` is out of range or ``parts`` is not
    ``part_count`` of the upload's rows. ``ParseError`` propagates to the caller.
    """
    if not 1 <= part <= parts:
        raise PartError(f"Part {part} of {parts} does not exist.")
    parsed, report = check_upload(data, filename)
    expected = part_count(len(parsed.records))
    if parts != expected:
        raise PartError(f"This upload has {expected} part(s), not {parts}.")
    if not report.ok:
        return report, None

    _ensure_fonts()
    prepared = prepare_branding(branding)  # colour and decoded logo, once per batch
    files: list[tuple[str, bytes]] = []
    manifest_rows: list[tuple[int, str, str]] = []
    mailmerge_rows: list[tuple[str, str, str, str, str]] = []
    start = (part - 1) * GENERATE_CHUNK_ROWS
    for record in report.valid[start : start + GENERATE_CHUNK_ROWS]:
        name = sanitize_filename(pdf_filename(record))
        files.append((name, render_pdf(record, prepared)))
        manifest_rows.append((record.row_number, record.iccid, name))
        mailmerge_rows.append(
            (
                record.email or "",
                record.customer_name or "",
                record.plan_name or "",
                record.iccid,
                name,
            )
        )

    mailmerge = build_mailmerge(mailmerge_rows) if parsed.has_email else None
    return report, build_zip(files, build_manifest(manifest_rows), mailmerge)
