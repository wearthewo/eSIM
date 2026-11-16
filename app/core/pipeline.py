"""End-to-end orchestration: parse -> validate -> render -> zip, all in memory.

Never log activation codes, confirmation codes, or file contents.
"""

from __future__ import annotations

import threading

from app.core.models import Branding, ValidationReport
from app.core.parser import parse_upload
from app.core.render import pdf_filename, prepare_branding, register_fonts, render_pdf
from app.core.validator import validate
from app.core.zipper import build_manifest, build_zip, sanitize_filename

_fonts_ready = False
_fonts_lock = threading.Lock()


def _ensure_fonts() -> None:
    """Register the PDF fonts once per process."""
    global _fonts_ready
    if _fonts_ready:
        return
    with _fonts_lock:
        if not _fonts_ready:
            register_fonts()
            _fonts_ready = True


def process_upload(
    data: bytes, filename: str, branding: Branding
) -> tuple[ValidationReport, bytes | None]:
    """Process one upload.

    Returns the validation report and the ZIP bytes. The ZIP is ``None`` when
    the report has any error-severity issue (all-or-nothing; warnings allowed).
    ``ParseError`` propagates to the caller.
    """
    report = validate(parse_upload(data, filename))
    if not report.ok:
        return report, None

    _ensure_fonts()
    prepared = prepare_branding(branding)  # colour and decoded logo, once per batch
    files: list[tuple[str, bytes]] = []
    manifest_rows: list[tuple[int, str, str]] = []
    for record in report.valid:
        name = sanitize_filename(pdf_filename(record))
        files.append((name, render_pdf(record, prepared)))
        manifest_rows.append((record.row_number, record.iccid, name))

    return report, build_zip(files, build_manifest(manifest_rows))
