"""Branded one-page PDF rendering with ReportLab, in memory.

The QR code on the page encodes only the activation code (LPA string). The
activation code, matching ID and confirmation code are never printed as text.
When a confirmation code exists, the page only says that it will be sent
separately.

Fonts are bundled under ``app/core/assets/fonts`` (DejaVu Sans, see
LICENSE_DEJAVU) so Greek and other non-ASCII names render correctly. This
module resolves the font path locally and never imports ``app.web``.
"""

from __future__ import annotations

import io
import re
import threading
from pathlib import Path

from reportlab.lib.colors import Color, HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.core import qr
from app.core.models import Branding, EsimRecord

FONT_DIR: Path = Path(__file__).parent / "assets" / "fonts"
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"

QR_SIZE = 6 * cm
MARGIN = 2 * cm
LOGO_MAX_WIDTH = 5 * cm
LOGO_MAX_HEIGHT = 2 * cm
TEXT_GREY = HexColor("#444444")
CONFIRMATION_NOTE = "A confirmation code will be sent separately."

INSTALL_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "iPhone",
        (
            "Connect to Wi-Fi and open Settings > Mobile Service (or Cellular).",
            "Tap Add eSIM, then Use QR Code.",
            "Scan the QR code above and follow the on-screen steps.",
        ),
    ),
    (
        "Android",
        (
            "Connect to Wi-Fi and open Settings > Network & internet > SIMs.",
            "Tap Add eSIM (or Download a SIM) and choose to scan a QR code.",
            "Scan the QR code above and follow the on-screen steps.",
        ),
    ),
)

_HEX = re.compile(r"#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
_UNSAFE_FILENAME = re.compile(r"[^0-9A-Za-z]")
_font_lock = threading.Lock()


def register_fonts() -> None:
    """Register the bundled DejaVu TTFs with ReportLab. Safe to call more than once."""
    with _font_lock:
        registered = set(pdfmetrics.getRegisteredFontNames())
        for name, filename in (
            (FONT_REGULAR, "DejaVuSans.ttf"),
            (FONT_BOLD, "DejaVuSans-Bold.ttf"),
        ):
            if name not in registered:
                pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))


def primary_color(value: str | None) -> Color:
    """Parse ``#RRGGBB``/``#RGB`` (``#`` optional); anything else falls back to black."""
    if not value or not _HEX.fullmatch(value.strip()):
        return black
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return HexColor(f"#{digits}")


def render_pdf(record: EsimRecord, branding: Branding) -> bytes:
    """Build a one-page branded A4 PDF for ``record`` and return its bytes.

    Layout: logo, company name, customer/plan/ICCID, QR (activation code only),
    install instructions, footer. Output is deterministic for the same input.
    """
    register_fonts()
    primary = primary_color(branding.primary_hex)
    width, height = A4
    content_width = width - 2 * MARGIN

    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=A4, invariant=True)
    canvas.setTitle(f"eSIM {record.iccid}")
    canvas.setAuthor(branding.company_name)
    canvas.setCreator(branding.company_name)

    # --- header: logo + company name ---
    top = height - MARGIN
    header_bottom = top
    text_x = MARGIN
    logo = _logo(branding.logo_png)
    if logo is not None:
        image, logo_w, logo_h = logo
        canvas.drawImage(image, MARGIN, top - logo_h, logo_w, logo_h, mask="auto")
        text_x += logo_w + 0.6 * cm
        header_bottom = top - logo_h
    canvas.setFillColor(primary)
    canvas.setFont(FONT_BOLD, 18)
    baseline = top - 18
    for line in simpleSplit(branding.company_name, FONT_BOLD, 18, width - MARGIN - text_x)[:2]:
        canvas.drawString(text_x, baseline, line)
        header_bottom = min(header_bottom, baseline - 6)
        baseline -= 22
    y = header_bottom - 0.4 * cm

    canvas.setStrokeColor(primary)
    canvas.setLineWidth(1.5)
    canvas.line(MARGIN, y, width - MARGIN, y)
    y -= 1 * cm

    # --- title and details ---
    canvas.setFont(FONT_BOLD, 16)
    canvas.drawString(MARGIN, y, "Your eSIM")
    y -= 0.8 * cm
    details = [
        ("Customer", record.customer_name),
        ("Plan", record.plan_name),
        ("ICCID", record.iccid),
    ]
    label_width = 2.6 * cm
    for label, value in details:
        if not value:
            continue
        canvas.setFillColor(TEXT_GREY)
        canvas.setFont(FONT_REGULAR, 10)
        canvas.drawString(MARGIN, y, label)
        canvas.setFillColor(black)
        canvas.setFont(FONT_BOLD, 12)
        for line in simpleSplit(value, FONT_BOLD, 12, content_width - label_width):
            canvas.drawString(MARGIN + label_width, y, line)
            y -= 16
        y -= 4

    # --- QR code (activation code only) ---
    y -= 0.4 * cm
    qr_png = qr.make_qr_png(record.activation_code, scale=10, border=2)
    canvas.drawImage(
        ImageReader(io.BytesIO(qr_png)), (width - QR_SIZE) / 2, y - QR_SIZE, QR_SIZE, QR_SIZE
    )
    y -= QR_SIZE + 0.6 * cm
    canvas.setFillColor(black)
    canvas.setFont(FONT_REGULAR, 10)
    canvas.drawCentredString(width / 2, y, "Scan this QR code with the phone you want to use.")
    if record.confirmation_code:
        y -= 0.55 * cm
        canvas.setFont(FONT_BOLD, 10)
        canvas.drawCentredString(width / 2, y, CONFIRMATION_NOTE)
    y -= 1.1 * cm

    # --- install steps ---
    canvas.setFillColor(primary)
    canvas.setFont(FONT_BOLD, 14)
    canvas.drawString(MARGIN, y, "How to install")
    y -= 0.7 * cm
    for platform, steps in INSTALL_STEPS:
        canvas.setFillColor(primary)
        canvas.setFont(FONT_BOLD, 11)
        canvas.drawString(MARGIN, y, platform)
        y -= 15
        canvas.setFillColor(black)
        canvas.setFont(FONT_REGULAR, 10)
        for number, step in enumerate(steps, start=1):
            canvas.drawString(MARGIN + 0.3 * cm, y, f"{number}.")
            for line in simpleSplit(step, FONT_REGULAR, 10, content_width - 1 * cm):
                canvas.drawString(MARGIN + 1 * cm, y, line)
                y -= 13
        y -= 8

    # --- footer ---
    if branding.footer_text:
        lines = simpleSplit(branding.footer_text, FONT_REGULAR, 8, content_width)[:4]
        footer_y = MARGIN + (len(lines) - 1) * 10
        canvas.setStrokeColor(TEXT_GREY)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, footer_y + 14, width - MARGIN, footer_y + 14)
        canvas.setFillColor(TEXT_GREY)
        canvas.setFont(FONT_REGULAR, 8)
        for line in lines:
            canvas.drawCentredString(width / 2, footer_y, line)
            footer_y -= 10

    canvas.showPage()
    canvas.save()
    return buf.getvalue()


def pdf_filename(record: EsimRecord) -> str:
    """Return a deterministic filename such as ``esim_<iccid>.pdf``.

    It is never derived from the activation or confirmation code. Characters
    other than ASCII letters and digits are replaced with ``_``.
    """
    return f"esim_{_UNSAFE_FILENAME.sub('_', record.iccid)}.pdf"


def _logo(data: bytes | None) -> tuple[ImageReader, float, float] | None:
    """Return the logo scaled to fit the header box, or ``None`` if absent or unreadable."""
    if not data:
        return None
    try:
        image = ImageReader(io.BytesIO(data))
        px_w, px_h = image.getSize()
    except Exception:
        return None
    if px_w <= 0 or px_h <= 0:
        return None
    scale = min(LOGO_MAX_WIDTH / px_w, LOGO_MAX_HEIGHT / px_h)
    return image, px_w * scale, px_h * scale
