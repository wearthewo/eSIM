"""End-to-end orchestration: parse -> validate -> render -> zip, all in memory.

Never log activation codes, confirmation codes, or file contents.
"""

from __future__ import annotations

from app.core.models import Branding, ValidationReport


def process_upload(
    data: bytes, filename: str, branding: Branding
) -> tuple[ValidationReport, bytes | None]:
    """Process one upload.

    Returns the validation report and the ZIP bytes. The ZIP is ``None`` when
    the report has any error-severity issue (all-or-nothing; warnings allowed).
    ``ParseError`` propagates to the caller.
    """
    raise NotImplementedError
