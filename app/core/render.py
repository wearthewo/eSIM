"""Branded one-page PDF rendering with ReportLab, in memory.

The QR code on the page encodes only the activation code (LPA string). The
activation code, matching ID and confirmation code are never printed as text.
When a confirmation code exists, the page only says that it will be sent
separately.

Fonts:

* Fixed text (headings, labels, install steps, the confirmation note) uses the
  PDF standard fonts Helvetica and Helvetica-Bold. They are built into every
  PDF viewer, so nothing is embedded.
* Text that comes from the user (company name, customer name, plan name,
  footer) uses Helvetica when it is pure ASCII, and otherwise the bundled DejaVu
  Sans (regular only, see ``assets/fonts/LICENSE_DEJAVU``), which ReportLab
  embeds as a subset. So Greek and other non-ASCII names render correctly, and
  only PDFs that need it carry a font program. These strings are never bold: the
  hierarchy comes from size, so ASCII and non-ASCII pages look the same.
  Validated ICCIDs are ASCII digits, so they always use Helvetica.

Performance notes:

* The QR code is drawn as vector rectangles straight from the module matrix
  (no PNG encode/decode, no image XObject).
* ``prepare_branding`` decodes the logo once per batch, downscales it to the
  pixels needed at ``LOGO_DPI`` for its printed size and encodes it as PNG
  (Flate with PNG predictors) or JPEG (DCT), whichever is smaller. Transparency
  becomes a lossless soft mask. Every PDF of the batch reuses those bytes.
* ASCII85 stream encoding is switched off process-wide (``rl_config.useA85``).
  It only makes PDFs 7-bit clean, costs ~25% in size, and without ReportLab's
  optional C accelerator it was the single largest cost per PDF.

This module never imports ``app.web``.
"""

from __future__ import annotations

import functools
import io
import re
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image  # installed with ReportLab, which depends on Pillow
from reportlab import rl_config
from reportlab.lib.colors import Color, HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfdoc, pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.core import qr
from app.core.models import Branding, EsimRecord

rl_config.useA85 = 0  # write binary (Flate-only) streams; see module docstring

FONT_DIR: Path = Path(__file__).parent / "assets" / "fonts"
FIXED_FONT = "Helvetica"  # PDF standard font, never embedded
FIXED_FONT_BOLD = "Helvetica-Bold"
UNICODE_FONT = "DejaVuSans"  # embedded subset, only for non-ASCII user text

COMPANY_SIZE = 20  # user text sizes; user text is never bold
VALUE_SIZE = 13
FOOTER_SIZE = 8

QR_SIZE = 6 * cm  # including the quiet zone
QR_BORDER = 2  # quiet zone, in modules
MARGIN = 2 * cm
LOGO_MAX_WIDTH = 5 * cm
LOGO_MAX_HEIGHT = 2 * cm
# The logo is downscaled to this resolution at its printed size (at most 5 x 2 cm, so at
# most 394 x 157 px). 200 dpi is sharp on office and laser printers; 300 dpi made the
# logo about 50% larger in every PDF (README, "Size and speed").
LOGO_DPI = 200
LOGO_JPEG_QUALITY = 90
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
_DARK_RUN = re.compile(rb"\x01+")
_LOGO_NAME = "Logo"
_LOGO_MASK_NAME = "LogoMask"
_LOGO_FORMATS = ("PNG", "JPEG")
_font_lock = threading.Lock()
_fonts_ready = False
# ReportLab's TrueType face keeps one shared read position, which ``makeSubset``
# moves while it builds a PDF's embedded subset (in ``Canvas.save``). Saves of
# PDFs that embed the font are serialised so concurrent renders cannot interleave.
_embed_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class _EncodedImage:
    """One image stream, encoded once per batch (``prepare_branding``)."""

    name: str
    width: int  # pixels
    height: int
    colour_space: str  # DeviceRGB or DeviceGray
    filter: str  # FlateDecode (PNG data) or DCTDecode (JPEG data)
    data: bytes
    columns_colors: tuple[int, int] | None  # PNG predictor parameters, for FlateDecode
    smask: str | None = None  # name of the soft-mask image


class _ImageXObject(pdfdoc.PDFObject):
    """Writes a pre-encoded image. ReportLab stamps each registered object with its
    name in that document, so every PDF gets its own (tiny) instance."""

    def __init__(self, image: _EncodedImage) -> None:
        self.image = image

    def format(self, document) -> bytes:
        img = self.image
        entries = {
            "Type": pdfdoc.PDFName("XObject"),
            "Subtype": pdfdoc.PDFName("Image"),
            "Width": img.width,
            "Height": img.height,
            "BitsPerComponent": 8,
            "ColorSpace": pdfdoc.PDFName(img.colour_space),
            "Filter": pdfdoc.PDFName(img.filter),  # preset: PDFStream will not re-encode
        }
        if img.columns_colors is not None:
            columns, colors = img.columns_colors
            entries["DecodeParms"] = pdfdoc.PDFDictionary(
                {"Predictor": 15, "Colors": colors, "BitsPerComponent": 8, "Columns": columns}
            )
        if img.smask is not None:
            entries["SMask"] = pdfdoc.PDFObjectReference(pdfdoc.xObjectName(img.smask))
        return pdfdoc.PDFStream(pdfdoc.PDFDictionary(entries), img.data).format(document)


@dataclass(frozen=True, slots=True)
class _Logo:
    """Logo encoded once; its bytes are shared by every PDF of the batch."""

    image: _EncodedImage
    smask: _EncodedImage | None
    width: float  # points, as drawn
    height: float


@dataclass(frozen=True, slots=True)
class PreparedBranding:
    """Per-batch branding state from ``prepare_branding``. Read-only, thread-safe."""

    branding: Branding
    primary: Color
    logo: _Logo | None


def register_fonts() -> None:
    """Register the bundled DejaVu Sans with ReportLab. Safe to call more than once."""
    global _fonts_ready
    if _fonts_ready:
        return
    with _font_lock:
        if UNICODE_FONT not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(UNICODE_FONT, str(FONT_DIR / "DejaVuSans.ttf")))
        for name in (FIXED_FONT, FIXED_FONT_BOLD):
            pdfmetrics.getFont(name)  # load the standard font metrics up front
        _fonts_ready = True


def user_font(text: str) -> str:
    """Font for user-supplied text: Helvetica if pure ASCII, else embedded DejaVu Sans."""
    return FIXED_FONT if text.isascii() else UNICODE_FONT


def primary_color(value: str | None) -> Color:
    """Parse ``#RRGGBB``/``#RGB`` (``#`` optional); anything else falls back to black."""
    if not value or not _HEX.fullmatch(value.strip()):
        return black
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return HexColor(f"#{digits}")


def prepare_branding(branding: Branding) -> PreparedBranding:
    """Do the per-batch work once: register fonts, parse the colour, encode the logo."""
    register_fonts()
    return PreparedBranding(branding, primary_color(branding.primary_hex), _logo(branding.logo_png))


def render_pdf(record: EsimRecord, branding: Branding | PreparedBranding) -> bytes:
    """Build a one-page branded A4 PDF for ``record`` and return its bytes.

    Layout: logo, company name, customer/plan/ICCID, QR (activation code only),
    install instructions, footer. Output is deterministic for the same input.
    Pass a ``PreparedBranding`` when rendering many records with the same branding.
    """
    prepared = branding if isinstance(branding, PreparedBranding) else prepare_branding(branding)
    branding = prepared.branding
    primary = prepared.primary
    width, height = A4
    content_width = width - 2 * MARGIN
    embeds_font = False  # set when any user string needs DejaVu Sans

    def set_user_font(text: str, size: float) -> str:
        nonlocal embeds_font
        font = user_font(text)
        embeds_font = embeds_font or font == UNICODE_FONT
        canvas.setFont(font, size)
        return font

    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=A4, invariant=True)
    canvas.setTitle(f"eSIM {record.iccid}")
    canvas.setAuthor(branding.company_name)
    canvas.setCreator(branding.company_name)

    # --- header: logo + company name ---
    top = height - MARGIN
    header_bottom = top
    text_x = MARGIN
    logo = prepared.logo
    if logo is not None:
        _draw_logo(canvas, logo, MARGIN, top - logo.height)
        text_x += logo.width + 0.6 * cm
        header_bottom = top - logo.height
    canvas.setFillColor(primary)
    font = set_user_font(branding.company_name, COMPANY_SIZE)
    baseline = top - COMPANY_SIZE
    name_width = width - MARGIN - text_x
    for line in simpleSplit(branding.company_name, font, COMPANY_SIZE, name_width)[:2]:
        canvas.drawString(text_x, baseline, line)
        header_bottom = min(header_bottom, baseline - 6)
        baseline -= COMPANY_SIZE + 4
    y = header_bottom - 0.4 * cm

    canvas.setStrokeColor(primary)
    canvas.setLineWidth(1.5)
    canvas.line(MARGIN, y, width - MARGIN, y)
    y -= 1 * cm

    # --- title and details ---
    canvas.setFont(FIXED_FONT_BOLD, 16)
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
        canvas.setFont(FIXED_FONT, 10)
        canvas.drawString(MARGIN, y, label)
        canvas.setFillColor(black)
        font = set_user_font(value, VALUE_SIZE)
        for line in simpleSplit(value, font, VALUE_SIZE, content_width - label_width):
            canvas.drawString(MARGIN + label_width, y, line)
            y -= VALUE_SIZE + 4
        y -= 4

    # --- QR code (activation code only) ---
    y -= 0.4 * cm
    _draw_qr(canvas, qr.make_qr_matrix(record.activation_code), (width - QR_SIZE) / 2, y - QR_SIZE)
    y -= QR_SIZE + 0.6 * cm
    canvas.setFillColor(black)
    canvas.setFont(FIXED_FONT, 10)
    canvas.drawCentredString(width / 2, y, "Scan this QR code with the phone you want to use.")
    if record.confirmation_code:
        y -= 0.55 * cm
        canvas.setFont(FIXED_FONT_BOLD, 10)
        canvas.drawCentredString(width / 2, y, CONFIRMATION_NOTE)
    y -= 1.1 * cm

    # --- install steps ---
    canvas.setFillColor(primary)
    canvas.setFont(FIXED_FONT_BOLD, 14)
    canvas.drawString(MARGIN, y, "How to install")
    y -= 0.7 * cm
    for platform, steps in _wrapped_install_steps(content_width - 1 * cm):
        canvas.setFillColor(primary)
        canvas.setFont(FIXED_FONT_BOLD, 11)
        canvas.drawString(MARGIN, y, platform)
        y -= 15
        canvas.setFillColor(black)
        canvas.setFont(FIXED_FONT, 10)
        for number, lines in enumerate(steps, start=1):
            canvas.drawString(MARGIN + 0.3 * cm, y, f"{number}.")
            for line in lines:
                canvas.drawString(MARGIN + 1 * cm, y, line)
                y -= 13
        y -= 8

    # --- footer ---
    if branding.footer_text:
        font = set_user_font(branding.footer_text, FOOTER_SIZE)
        lines = simpleSplit(branding.footer_text, font, FOOTER_SIZE, content_width)[:4]
        footer_y = MARGIN + (len(lines) - 1) * 10
        canvas.setStrokeColor(TEXT_GREY)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, footer_y + 14, width - MARGIN, footer_y + 14)
        canvas.setFillColor(TEXT_GREY)
        for line in lines:
            canvas.drawCentredString(width / 2, footer_y, line)
            footer_y -= 10

    canvas.showPage()
    if embeds_font:
        with _embed_lock:
            canvas.save()
    else:
        canvas.save()
    return buf.getvalue()


def pdf_filename(record: EsimRecord) -> str:
    """Return a deterministic filename such as ``esim_<iccid>.pdf``.

    It is never derived from the activation or confirmation code. Characters
    other than ASCII letters and digits are replaced with ``_``.
    """
    return f"esim_{_UNSAFE_FILENAME.sub('_', record.iccid)}.pdf"


@functools.cache
def _wrapped_install_steps(
    max_width: float,
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    """``INSTALL_STEPS`` with each step wrapped to ``max_width``; fixed text, wrapped once."""
    return tuple(
        (platform, tuple(tuple(simpleSplit(step, FIXED_FONT, 10, max_width)) for step in steps))
        for platform, steps in INSTALL_STEPS
    )


def _draw_qr(canvas: Canvas, matrix: tuple[bytes, ...], x: float, y: float) -> None:
    """Fill the dark modules as one vector path in a ``QR_SIZE`` box at (x, y).

    The box includes a ``QR_BORDER``-module quiet zone. Horizontal runs of dark
    modules become one rectangle each, in module units (one ``cm`` transform),
    so the path is compact and has no seams inside a run.
    """
    size = len(matrix)
    module = QR_SIZE / (size + 2 * QR_BORDER)
    path: list[str] = []
    for i, row in enumerate(matrix):
        row_y = size - 1 - i + QR_BORDER  # PDF y grows upwards; row 0 is the top
        for run in _DARK_RUN.finditer(row):
            path.append(f"{run.start() + QR_BORDER} {row_y} {run.end() - run.start()} 1 re")
    path.append("f")
    canvas.saveState()
    canvas.transform(module, 0, 0, module, x, y)
    canvas.setFillColor(black)
    canvas.addLiteral("\n".join(path))
    canvas.restoreState()


def _draw_logo(canvas: Canvas, logo: _Logo, x: float, y: float) -> None:
    """Draw the prepared logo, as ``Canvas.drawImage`` would, without re-encoding it."""
    doc = canvas._doc
    for image in (logo.image, logo.smask):
        if image is not None:
            doc.Reference(_ImageXObject(image), doc.getXObjectName(image.name))
    canvas.saveState()
    canvas.transform(logo.width, 0, 0, logo.height, x, y)
    canvas.doForm(logo.image.name)
    canvas.restoreState()
    canvas._currentPageHasImages = 1


def _logo(data: bytes | None) -> _Logo | None:
    """Decode, downscale and encode the logo once; ``None`` if absent or unreadable.

    The drawn size fits the logo in ``LOGO_MAX_WIDTH`` x ``LOGO_MAX_HEIGHT``. The
    pixels are reduced (never enlarged) to what that size needs at ``LOGO_DPI``.
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data), formats=_LOGO_FORMATS) as source:
            px_w, px_h = source.size
            if px_w <= 0 or px_h <= 0:
                return None
            scale = min(LOGO_MAX_WIDTH / px_w, LOGO_MAX_HEIGHT / px_h)  # points per pixel
            colour, alpha = _logo_pixels(source, min(1.0, scale * LOGO_DPI / 72))
        smask = _encode_lossless(_LOGO_MASK_NAME, alpha) if alpha is not None else None
        image = _encode_smallest(colour, smask.name if smask is not None else None)
    except Exception:
        return None
    return _Logo(image, smask, px_w * scale, px_h * scale)


def _logo_pixels(source: Image.Image, ratio: float) -> tuple[Image.Image, Image.Image | None]:
    """Return the 8-bit colour (RGB or L) and alpha (L, or ``None`` if opaque) images,
    resized by ``ratio`` (<= 1)."""
    grey = source.mode in ("1", "L", "LA")
    has_alpha = source.mode in ("LA", "RGBA", "PA") or "transparency" in source.info
    if has_alpha:
        image = source.convert("RGBA")  # applies palette / colour-key transparency
        if grey:
            image = image.convert("LA")
    else:
        image = source.convert("L" if grey else "RGB")
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    if size != image.size:
        # Area averaging (box filter): no ringing at the hard edges logos have, so
        # it adds the fewest new colours and compresses far better than Lanczos.
        # Pillow premultiplies alpha while resizing RGBA/LA.
        image = image.resize(size, Image.Resampling.BOX)
    if not has_alpha:
        return image, None
    alpha = image.getchannel("A")
    colour = image.convert("L" if grey else "RGB")
    return colour, (None if alpha.getextrema() == (255, 255) else alpha)


def _encode_smallest(image: Image.Image, smask: str | None) -> _EncodedImage:
    """Encode ``image`` as PNG and as JPEG and keep the smaller (PNG on a tie)."""
    png = _encode_lossless(_LOGO_NAME, image, smask)
    jpeg = _jpeg(image)
    if len(jpeg) >= len(png.data):
        return png
    return _EncodedImage(
        name=_LOGO_NAME,
        width=image.width,
        height=image.height,
        colour_space=png.colour_space,
        filter="DCTDecode",
        data=jpeg,
        columns_colors=None,
        smask=smask,
    )


def _jpeg(image: Image.Image) -> bytes:
    """Baseline JPEG at ``LOGO_JPEG_QUALITY`` with optimized Huffman tables.

    Pillow gives the optimizing encoder an output buffer of about width x height bytes
    (at least 64 KB); a detailed image that compresses to more than that fails with
    "Suspension not allowed here". Such an image is encoded without the optimization
    (no buffer limit, a few percent larger) instead of losing the logo.
    """
    out = io.BytesIO()
    try:
        image.save(out, "JPEG", quality=LOGO_JPEG_QUALITY, optimize=True, subsampling=0)
    except OSError:
        out = io.BytesIO()
        image.save(out, "JPEG", quality=LOGO_JPEG_QUALITY, subsampling=0)
    return out.getvalue()


def _encode_lossless(name: str, image: Image.Image, smask: str | None = None) -> _EncodedImage:
    """Encode an 8-bit RGB or L image as PNG and embed its IDAT data.

    A PNG's IDAT data is a Flate stream of predictor-filtered rows, which a PDF
    reads directly with ``/DecodeParms << /Predictor 15 ... >>``.
    """
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)  # Pillow never interlaces on save
    colors = 3 if image.mode == "RGB" else 1
    return _EncodedImage(
        name=name,
        width=image.width,
        height=image.height,
        colour_space="DeviceRGB" if colors == 3 else "DeviceGray",
        filter="FlateDecode",
        data=_png_idat(out.getvalue()),
        columns_colors=(image.width, colors),
        smask=smask,
    )


def _png_idat(png: bytes) -> bytes:
    """Concatenated IDAT chunk data of a PNG file (8-byte signature, then chunks)."""
    parts: list[bytes] = []
    pos = 8
    while pos + 8 <= len(png):
        length, kind = struct.unpack(">I4s", png[pos : pos + 8])
        if kind == b"IDAT":
            parts.append(png[pos + 8 : pos + 8 + length])
        pos += 12 + length  # length, type, data, CRC
    return b"".join(parts)
