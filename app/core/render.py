"""Branded one-page PDF rendering with ReportLab, in memory.

The QR code on the page encodes only the activation code (LPA string). The
confirmation code is never embedded in the QR payload. If it is printed at
all, it appears as separate text.

Fonts are bundled under ``app/core/assets/fonts`` (DejaVu Sans, see
LICENSE_DEJAVU) so Greek and other non-ASCII names render correctly. This
module resolves the font path locally and never imports ``app.web``.
"""

from __future__ import annotations

from pathlib import Path

from app.core.models import Branding, EsimRecord

FONT_DIR: Path = Path(__file__).parent / "assets" / "fonts"
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"


def register_fonts() -> None:
    """Register the bundled DejaVu TTFs with ReportLab. Safe to call more than once."""
    raise NotImplementedError


def render_pdf(record: EsimRecord, branding: Branding) -> bytes:
    """Build a one-page branded PDF for ``record`` and return its bytes.

    Layout: logo, company name, customer/plan, QR (activation code only),
    ICCID, install instructions, footer.
    """
    raise NotImplementedError


def pdf_filename(record: EsimRecord) -> str:
    """Return a deterministic filename such as ``esim_<iccid>.pdf``.

    It is never derived from the activation or confirmation code.
    """
    raise NotImplementedError
