"""Branded one-page PDF rendering with ReportLab, in memory.

The QR code on the page encodes only the activation code (LPA string). The
activation code, matching ID and confirmation code are never printed as text.
When a confirmation code exists, the page only says that it will be sent
separately.

Fonts are bundled under ``app/core/assets/fonts`` (DejaVu Sans, see
LICENSE_DEJAVU) so Greek and other non-ASCII names render correctly. This
module resolves the font path locally and never imports ``app.web``.

Performance notes:

* The QR code is drawn as vector rectangles straight from the module matrix
  (no PNG encode/decode, no image XObject).
* ``prepare_branding`` decodes and compresses the logo once; ``render_pdf``
  then reuses it for every PDF of the batch.
* ASCII85 stream encoding is switched off process-wide (``rl_config.useA85``).
  It only makes PDFs 7-bit clean, costs ~25% in size, and without ReportLab's
  optional C accelerator it was the single largest cost per PDF.
"""

from __future__ import annotations

import copy
import io
import re
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from reportlab import rl_config
from reportlab.lib.colors import Color, HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfdoc, pdfmetrics
from reportlab.pdfbase.ttfonts import (
    FF_NONSYMBOLIC,
    FF_SYMBOLIC,
    SUBSETN,
    TTFont,
    makeToUnicodeCMap,
)
from reportlab.pdfgen.canvas import Canvas

from app.core import qr
from app.core.models import Branding, EsimRecord

rl_config.useA85 = 0  # write binary (Flate-only) streams; see module docstring

FONT_DIR: Path = Path(__file__).parent / "assets" / "fonts"
FONT_REGULAR = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"

QR_SIZE = 6 * cm  # including the quiet zone
QR_BORDER = 2  # quiet zone, in modules
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
_DARK_RUN = re.compile(rb"\x01+")
_LOGO_NAME = "Logo"
_LOGO_MASK_NAME = "LogoMask"
_SUBSET_CACHE_SIZE = 64  # per font; one entry is ~20 KB
_font_lock = threading.Lock()
_fonts_ready = False


@dataclass(frozen=True, slots=True)
class _Logo:
    """Logo decoded and compressed once; copied (cheaply) into each PDF."""

    image: pdfdoc.PDFImageXObject
    smask: pdfdoc.PDFImageXObject | None
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class PreparedBranding:
    """Per-batch branding state from ``prepare_branding``. Read-only, thread-safe."""

    branding: Branding
    primary: Color
    logo: _Logo | None


class _Preformatted(pdfdoc.PDFObject):
    """A PDF value whose bytes were formatted once and are reused verbatim."""

    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data

    def format(self, document) -> bytes:
        return self.data


@dataclass(frozen=True, slots=True)
class _SubsetPdf:
    """The parts of one embedded font subset that are the same in every PDF."""

    widths: _Preformatted
    to_unicode: bytes  # Flate-compressed ToUnicode CMap
    font_file: bytes  # Flate-compressed TrueType subset program
    font_file_length: int  # uncompressed length (/Length1)


def _flate_stream(packed: bytes) -> pdfdoc.PDFStream:
    """A stream whose content is already Flate-compressed (preset /Filter skips encoding)."""
    stream = pdfdoc.PDFStream(content=packed)
    stream.dictionary["Filter"] = pdfdoc.PDFArray([pdfdoc.PDFName("FlateDecode")])
    return stream


class _CachingTTFont(TTFont):
    """``TTFont`` that builds each distinct embedded subset once and reuses it.

    Every PDF embeds its own font subsets. Building one (``makeSubset``),
    compressing it and formatting its widths costs several ms, and the result
    depends only on the subset's character list. That list repeats across
    PDFs: ReportLab always puts ASCII in subset 0, so only non-ASCII characters
    (in first-use order) make subsets differ. ``addObjects`` mirrors ReportLab
    5.1 ``TTFont.addObjects``/``TTFontFace.addSubsetObjects`` and produces
    byte-identical PDFs. Entries hold glyph data only, in a bounded LRU cache.
    """

    def __init__(self, name: str, filename: str) -> None:
        super().__init__(name, filename)
        self._subsets: OrderedDict[tuple[int, tuple[int, ...]], _SubsetPdf] = OrderedDict()
        self._subsets_lock = threading.Lock()

    def _subset_pdf(self, n: int, base_font_name: str, subset: list[int], doc) -> _SubsetPdf:
        key = (n, tuple(subset))  # n fixes base_font_name (subset prefix + face name)
        with self._subsets_lock:
            hit = self._subsets.get(key)
            if hit is not None:
                self._subsets.move_to_end(key)
                return hit
        face = self.face
        widths = pdfdoc.PDFArray(list(map(face.getCharWidth, subset))).format(doc)
        program = face.makeSubset(subset)
        hit = _SubsetPdf(
            widths=_Preformatted(widths),
            # zlib.compress with default level, as ReportLab's PDFZCompress does.
            to_unicode=zlib.compress(makeToUnicodeCMap(base_font_name, subset).encode("utf8")),
            font_file=zlib.compress(program),
            font_file_length=len(program),
        )
        with self._subsets_lock:
            self._subsets[key] = hit
            while len(self._subsets) > _SUBSET_CACHE_SIZE:
                self._subsets.popitem(last=False)
        return hit

    def addObjects(self, doc) -> None:  # noqa: N802 (ReportLab API)
        if not doc.compression:
            return super().addObjects(doc)
        state = self.state.get(doc) or TTFont.State(self._asciiReadable)
        self.state[doc] = state
        state.frozen = 1
        face = self.face
        for n, subset in enumerate(state.subsets):
            internal_name = self.getSubsetInternalName(n, doc)[1:]
            base_font_name = b"".join((SUBSETN(n), b"+", face.name, face.subfontNameX)).decode(
                "pdfdoc"
            )
            cached = self._subset_pdf(n, base_font_name, subset, doc)
            # Register objects in ReportLab's order, so object numbers match.
            to_unicode_ref = doc.Reference(
                _flate_stream(cached.to_unicode), "toUnicodeCMap:" + base_font_name
            )
            font_file = _flate_stream(cached.font_file)
            font_file.dictionary["Length1"] = cached.font_file_length
            font_file_ref = doc.Reference(font_file, f"fontFile:{face.filename}({base_font_name})")
            descriptor = pdfdoc.PDFDictionary(
                {
                    "Type": "/FontDescriptor",
                    "Ascent": face.ascent,
                    "CapHeight": face.capHeight,
                    "Descent": face.descent,
                    "Flags": (face.flags & ~FF_NONSYMBOLIC) | FF_SYMBOLIC,
                    "FontBBox": pdfdoc.PDFArray(face.bbox),
                    "FontName": pdfdoc.PDFName(base_font_name),
                    "ItalicAngle": face.italicAngle,
                    "StemV": face.stemV,
                    "FontFile2": font_file_ref,
                    "MissingWidth": face.defaultWidth,
                }
            )

            pdf_font = pdfdoc.PDFTrueTypeFont()
            pdf_font.__Comment__ = f"Font {self.fontName} subset {n}"
            pdf_font.Name = internal_name
            pdf_font.BaseFont = base_font_name
            pdf_font.FirstChar = 0
            pdf_font.LastChar = len(subset) - 1
            pdf_font.Widths = cached.widths
            pdf_font.ToUnicode = to_unicode_ref
            pdf_font.FontDescriptor = doc.Reference(descriptor, "fontDescriptor:" + base_font_name)
            doc.Reference(pdf_font, internal_name)
            doc.idToObject["BasicFonts"].dict[internal_name] = pdf_font
        del self.state[doc]


def register_fonts() -> None:
    """Register the bundled DejaVu TTFs with ReportLab. Safe to call more than once."""
    global _fonts_ready
    if _fonts_ready:
        return
    with _font_lock:
        registered = set(pdfmetrics.getRegisteredFontNames())
        for name, filename in (
            (FONT_REGULAR, "DejaVuSans.ttf"),
            (FONT_BOLD, "DejaVuSans-Bold.ttf"),
        ):
            if name not in registered:
                pdfmetrics.registerFont(_CachingTTFont(name, str(FONT_DIR / filename)))
        _fonts_ready = True


def primary_color(value: str | None) -> Color:
    """Parse ``#RRGGBB``/``#RGB`` (``#`` optional); anything else falls back to black."""
    if not value or not _HEX.fullmatch(value.strip()):
        return black
    digits = value.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return HexColor(f"#{digits}")


def prepare_branding(branding: Branding) -> PreparedBranding:
    """Do the per-batch work once: register fonts, parse the colour, decode the logo."""
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
    _draw_qr(canvas, qr.make_qr_matrix(record.activation_code), (width - QR_SIZE) / 2, y - QR_SIZE)
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
    """Draw the prepared logo, as ``Canvas.drawImage`` would, without re-encoding it.

    ReportLab stamps an object with its name in the document it is registered
    in, so each PDF gets a shallow copy (the compressed image bytes are shared).
    """
    doc = canvas._doc
    for obj in (logo.image, logo.smask):
        if obj is not None:
            doc.Reference(copy.copy(obj), doc.getXObjectName(obj.name))
    canvas.saveState()
    canvas.transform(logo.width, 0, 0, logo.height, x, y)
    canvas.doForm(logo.image.name)
    canvas.restoreState()
    canvas._currentPageHasImages = 1


def _logo(data: bytes | None) -> _Logo | None:
    """Decode the logo once and scale it to the header box; ``None`` if absent or unreadable."""
    if not data:
        return None
    try:
        reader = ImageReader(io.BytesIO(data))
        px_w, px_h = reader.getSize()
        if px_w <= 0 or px_h <= 0:
            return None
        # Same encoding (and transparency handling) as Canvas.drawImage(mask="auto").
        image = pdfdoc.PDFImageXObject(_LOGO_NAME, reader, mask="auto")
    except Exception:
        return None
    smask = getattr(image, "_smask", None)
    if smask is not None:
        del image._smask
        smask.name = _LOGO_MASK_NAME
        image.smask = pdfdoc.PDFObjectReference(pdfdoc.xObjectName(_LOGO_MASK_NAME))
    scale = min(LOGO_MAX_WIDTH / px_w, LOGO_MAX_HEIGHT / px_h)
    return _Logo(image, smask, px_w * scale, px_h * scale)
