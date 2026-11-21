from __future__ import annotations

import base64
import io
import random
import re
import struct
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
import segno
from PIL import Image, ImageDraw
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics

from app.core import qr, render
from app.core.models import Branding, EsimRecord

ACTIVATION = "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"
MATCHING_ID = "TEST-0001-AAAA-BBBB"
CONFIRMATION = "CONF0001"
GREEK_NAME = "Γιώργος Παπαδόπουλος"
GREEK_PLAN = "Ταξίδι 10GB"

RECORD = EsimRecord(
    row_number=2,
    iccid="8999000000000000013",
    activation_code=ACTIVATION,
    customer_name=GREEK_NAME,
    plan_name=GREEK_PLAN,
)
RECORD_WITH_CONFIRMATION = EsimRecord(
    row_number=3,
    iccid="8999000000000000021",
    activation_code=ACTIVATION,
    confirmation_code=CONFIRMATION,
    customer_name="Test Customer 1",
    plan_name="Test Plan 5GB",
)
BRANDING = Branding(
    company_name="Example Telecom (test)",
    primary_hex="#1A73E8",
    footer_text="Support: help@example.invalid",
)
CONFIRMATION_NOTE = "A confirmation code will be sent separately."


# --- minimal PDF reader (no extra dependencies) -----------------------------------
#
# ReportLab encodes page content streams (ASCII85 + Flate) and subsets TrueType
# fonts, so non-ASCII text is written as subset glyph codes, not UTF-8. A plain
# byte search on the PDF would therefore miss text that is really there. These
# helpers decode every stream's filter chain and decode shown text through each
# font's ToUnicode CMap, so the assertions below see what a PDF viewer extracts.

_OBJ = re.compile(rb"(\d+) 0 obj(.*?)endobj", re.DOTALL)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.DOTALL)
_STREAM_START = re.compile(rb"stream\r?\n")
_LENGTH = re.compile(rb"/Length (\d+)\b")
_FILTERS = re.compile(rb"/Filter\s*(\[[^\]]*\]|/\w+)")
_TEXT_TOKEN = re.compile(rb"/([^\s/\[\]<>()]+)\s+[\d.]+\s+Tf|\(((?:\\.|[^\\)])*)\)", re.DOTALL)
_BFCHAR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _decode_stream(head: bytes, stream: bytes) -> bytes:
    """Apply the stream's /Filter chain (ASCII85Decode and FlateDecode, in order)."""
    match = _FILTERS.search(head)
    for name in re.findall(rb"/(\w+)", match.group(1)) if match else []:
        if name == b"ASCII85Decode":
            stream = base64.a85decode(stream.strip().removesuffix(b"~>"))
        elif name == b"FlateDecode":
            stream = zlib.decompress(stream)
        else:
            break  # e.g. DCTDecode image data: leave as is
    return stream


def _objects(pdf: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    """Map object number -> (dictionary bytes, decoded stream bytes or None)."""
    out: dict[int, tuple[bytes, bytes | None]] = {}
    for match in _OBJ.finditer(pdf):
        body = match.group(2)
        stream_match = _STREAM.search(body)
        stream = None
        if stream_match:
            head = body[: stream_match.start()]
            # Streams are binary (Flate only): slice by /Length, not by delimiters.
            length = _LENGTH.search(head)
            start = _STREAM_START.match(body, stream_match.start()).end()
            stream = body[start : start + int(length.group(1))] if length else stream_match.group(1)
            stream = _decode_stream(head, stream)
            body = head
        out[int(match.group(1))] = (body, stream)
    return out


def decompressed(pdf: bytes) -> bytes:
    """All dictionaries and decoded streams concatenated, for raw byte searches."""
    return b"\n".join(head + b"\n" + (stream or b"") for head, stream in _objects(pdf).values())


def _unescape(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i : i + 1]
        if ch != b"\\":
            out += ch
            i += 1
            continue
        nxt = raw[i + 1 : i + 2]
        octal = re.match(rb"[0-7]{1,3}", raw[i + 1 : i + 4])
        if octal:
            out.append(int(octal.group(0), 8) & 0xFF)
            i += 1 + len(octal.group(0))
        else:
            out += _ESCAPES.get(nxt, nxt)
            i += 2
    return bytes(out)


def extract_text(pdf: bytes) -> str:
    """Decode every string shown with Tj/TJ, one string per line."""
    objects = _objects(pdf)
    cmaps: dict[bytes, dict[int, str]] = {}
    contents: list[bytes] = []
    for head, _stream in objects.values():
        name = re.search(rb"/Name\s*/([^\s/\[\]<>()]+)", head)
        to_unicode = re.search(rb"/ToUnicode\s+(\d+) 0 R", head)
        if b"/Type /Font" in head and name and to_unicode:
            cmap_stream = objects[int(to_unicode.group(1))][1] or b""
            cmaps[name.group(1)] = {
                int(src, 16): chr(int(dst, 16)) for src, dst in _BFCHAR.findall(cmap_stream)
            }
        if b"/Type /Page" in head and b"/Contents" in head:
            ref = re.search(rb"/Contents\s+(\d+) 0 R", head)
            if ref:
                contents.append(objects[int(ref.group(1))][1] or b"")

    lines: list[str] = []
    for content in contents:
        font: bytes | None = None
        for match in _TEXT_TOKEN.finditer(content):
            if match.group(1) is not None:
                font = match.group(1)
                continue
            raw = _unescape(match.group(2))
            cmap = cmaps.get(font or b"")
            if cmap is None:
                lines.append(raw.decode("latin-1"))
            else:
                lines.append("".join(cmap.get(b, "�") for b in raw))
    return "\n".join(lines)


def page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", pdf))


def image_draws(pdf: bytes) -> list[tuple[float, float]]:
    """(width, height) in points of every image XObject drawn on the page."""
    content = decompressed(pdf)
    draws = re.findall(rb"([\d.]+) 0 0 ([\d.]+) [\d.-]+ [\d.-]+ cm\s*/(\S+) Do", content)
    return [(float(w), float(h)) for w, h, _ in draws]


_QR_PATH = re.compile(
    rb"([\d.]+) 0 0 ([\d.]+) [\d.-]+ [\d.-]+ cm\s+0 0 0 rg\s+((?:\d+ \d+ \d+ 1 re\s+)+)f\b"
)


def qr_drawings(pdf: bytes) -> list[tuple[float, tuple[bytes, ...]]]:
    """Every vector QR on the page: (box side in points, module matrix without quiet zone).

    The QR is one filled path of 1-module-high rectangles in module units,
    drawn under a uniform ``module 0 0 module x y cm`` transform.
    """
    found = []
    for sx, sy, rects in _QR_PATH.findall(decompressed(pdf)):
        assert sx == sy  # square modules
        module = float(sx)
        total = round(render.QR_SIZE / module)  # modules including the quiet zone
        size = total - 2 * render.QR_BORDER
        grid = [bytearray(size) for _ in range(size)]
        for x, y, w in re.findall(rb"(\d+) (\d+) (\d+) 1 re", rects):
            row = size - 1 - (int(y) - render.QR_BORDER)
            col = int(x) - render.QR_BORDER
            for j in range(col, col + int(w)):
                grid[row][j] = 1
        found.append((module * total, tuple(bytes(r) for r in grid)))
    return found


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return render.render_pdf(RECORD, BRANDING)


@pytest.fixture(scope="module")
def pdf_text(pdf) -> str:
    return extract_text(pdf)


# --- fonts ----------------------------------------------------------------------


def test_bundled_fonts_present() -> None:
    for name in ("DejaVuSans.ttf", "LICENSE_DEJAVU"):
        assert (render.FONT_DIR / name).is_file()


def test_register_fonts_is_idempotent() -> None:
    render.register_fonts()
    first = pdfmetrics.getFont(render.UNICODE_FONT)
    render.register_fonts()
    assert pdfmetrics.getFont(render.UNICODE_FONT) is first
    assert render.UNICODE_FONT in pdfmetrics.getRegisteredFontNames()


def base_fonts(pdf: bytes) -> set[str]:
    """``/BaseFont`` names, without the 6-letter subset tag."""
    return {name.decode() for name in re.findall(rb"/BaseFont /(?:[A-Z]{6}\+)?([\w-]+)", pdf)}


def test_user_font_is_helvetica_only_for_pure_ascii() -> None:
    assert render.user_font("Example Telecom (test)") == "Helvetica"
    assert render.user_font("") == "Helvetica"
    assert render.user_font(GREEK_NAME) == render.UNICODE_FONT
    assert render.user_font("Café") == render.UNICODE_FONT  # Latin-1 is not ASCII


# --- basic structure --------------------------------------------------------------


def test_render_pdf_is_pdf(pdf) -> None:
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_render_pdf_is_one_a4_page(pdf) -> None:
    assert page_count(pdf) == 1
    assert b"/MediaBox [ 0 0 595.2756 841.8898 ]" in pdf


def test_render_pdf_is_deterministic() -> None:
    assert render.render_pdf(RECORD, BRANDING) == render.render_pdf(RECORD, BRANDING)


def test_text_extractor_sanity(pdf_text) -> None:
    # Guards the secrets tests below: if extraction silently found nothing they
    # would pass vacuously.
    assert "Example Telecom (test)" in pdf_text
    assert "8999000000000000013" in pdf_text


# --- content ----------------------------------------------------------------------


def test_render_pdf_greek_names_use_embedded_dejavu(pdf, pdf_text) -> None:
    assert re.search(rb"/BaseFont /[A-Z]{6}\+DejaVuSans\b", pdf)
    assert b"/FontFile2" in pdf  # the TrueType program itself is embedded
    # Regular DejaVu only; fixed text stays in the (not embedded) standard fonts.
    assert base_fonts(pdf) == {"Helvetica", "Helvetica-Bold", "DejaVuSans"}
    assert pdf.count(b"/FontFile2") == 1
    assert GREEK_NAME in pdf_text
    assert GREEK_PLAN in pdf_text


def test_render_pdf_ascii_only_embeds_no_font() -> None:
    data = render.render_pdf(RECORD_WITH_CONFIRMATION, BRANDING)
    assert base_fonts(data) == {"Helvetica", "Helvetica-Bold"}
    assert b"/FontFile" not in data
    text = extract_text(data)
    for expected in ("Test Customer 1", "Test Plan 5GB", "Example Telecom (test)", "iPhone"):
        assert expected in text


def test_render_pdf_each_user_string_picks_its_own_font() -> None:
    # Greek company name only: DejaVu for it, Helvetica for the ASCII customer/plan.
    branding = Branding(company_name="Εταιρεία Δοκιμής", footer_text="Support: x@example.invalid")
    data = render.render_pdf(RECORD_WITH_CONFIRMATION, branding)
    assert base_fonts(data) == {"Helvetica", "Helvetica-Bold", "DejaVuSans"}
    text = extract_text(data)
    assert "Εταιρεία Δοκιμής" in text
    assert "Test Customer 1" in text
    assert "Support: x@example.invalid" in text


def test_render_pdf_concurrent_greek_renders_match_sequential() -> None:
    # ReportLab's TrueType parser has one shared read position, used while
    # building each PDF's embedded subset; renders on several threads must not
    # interfere with each other.
    records = [
        EsimRecord(
            row_number=n,
            iccid="8999000000000000013",
            activation_code=ACTIVATION,
            customer_name=f"{GREEK_NAME} {'άέήίόύώ'[: n % 8]}",
            plan_name=GREEK_PLAN,
        )
        for n in range(2, 26)
    ]
    prepared = render.prepare_branding(BRANDING)
    expected = [render.render_pdf(r, prepared) for r in records]
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads often, so a race shows up reliably
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for _ in range(3):
                assert list(pool.map(lambda r: render.render_pdf(r, prepared), records)) == expected
    finally:
        sys.setswitchinterval(interval)


def test_render_pdf_contains_required_text(pdf_text) -> None:
    for expected in (
        "Example Telecom (test)",
        GREEK_NAME,
        GREEK_PLAN,
        "8999000000000000013",
        "iPhone",
        "Android",
        "Support: help@example.invalid",
    ):
        assert expected in pdf_text


def test_render_pdf_without_optional_fields() -> None:
    record = EsimRecord(row_number=2, iccid="8999000000000000013", activation_code=ACTIVATION)
    data = render.render_pdf(record, Branding(company_name="Example Telecom (test)"))
    assert data.startswith(b"%PDF")
    assert page_count(data) == 1


def test_render_pdf_qr_is_vector_and_at_least_4cm(pdf) -> None:
    assert image_draws(pdf) == []  # no logo, and the QR is not an image
    [(side, _matrix)] = qr_drawings(pdf)
    assert side >= 4 * cm
    assert side == pytest.approx(render.QR_SIZE)


@pytest.mark.parametrize("record", [RECORD, RECORD_WITH_CONFIRMATION], ids=["plain", "confirm"])
def test_render_pdf_qr_encodes_exactly_the_activation_code(record) -> None:
    # Decode the drawn modules and compare with segno's own encoding of the
    # activation code alone (level M, no error boost).
    [(_side, matrix)] = qr_drawings(render.render_pdf(record, BRANDING))
    reference = segno.make_qr(ACTIVATION, error="m", boost_error=False)
    assert matrix == tuple(bytes(row) for row in reference.matrix)


def test_render_pdf_qr_payload_is_activation_code_only(monkeypatch) -> None:
    calls: list[str] = []
    real = qr.make_qr_matrix

    def spy(payload: str) -> tuple[bytes, ...]:
        calls.append(payload)
        return real(payload)

    monkeypatch.setattr(qr, "make_qr_matrix", spy)
    render.render_pdf(RECORD_WITH_CONFIRMATION, BRANDING)
    assert calls == [ACTIVATION]
    assert CONFIRMATION not in calls[0]


def test_render_pdf_with_logo() -> None:
    logo = qr.make_qr_png("logo", scale=4)  # any valid PNG
    branding = Branding(company_name="Example Telecom (test)", logo_png=logo)
    data = render.render_pdf(RECORD, branding)
    [(width, height)] = image_draws(data)
    assert 0 < width <= render.LOGO_MAX_WIDTH
    assert 0 < height <= render.LOGO_MAX_HEIGHT
    assert len(qr_drawings(data)) == 1


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = struct.pack(">I", zlib.crc32(kind + data))
    return struct.pack(">I", len(data)) + kind + data + crc


def _rgba_png(width: int, height: int) -> bytes:
    """Minimal RGBA PNG: opaque blue box on a transparent background."""
    chunk = _png_chunk
    rows = b"".join(
        b"\x00"
        + b"".join(
            bytes((26, 115, 232, 255 if 2 <= x < width - 2 and 2 <= y < height - 2 else 0))
            for x in range(width)
        )
        for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_render_pdf_transparent_logo_keeps_soft_mask() -> None:
    # Same alpha handling as Canvas.drawImage(mask="auto"): the alpha channel becomes an /SMask.
    branding = Branding(company_name="Example Telecom (test)", logo_png=_rgba_png(40, 20))
    data = render.render_pdf(RECORD, branding)
    assert len(image_draws(data)) == 1
    assert b"/SMask" in decompressed(data)


def _pil_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def _logo_box(size: tuple[int, int], mode: str = "RGBA") -> bytes:
    """Flat-colour logo: blue box with a 10% transparent (or white) border."""
    image = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else "white")
    w, h = size
    ImageDraw.Draw(image).rectangle((w // 10, h // 10, w - w // 10, h - h // 10), (26, 115, 232))
    return _pil_png(image)


def _photo_logo(size: tuple[int, int]) -> bytes:
    """Photo-like logo (smooth random texture) with a soft alpha edge; deterministic."""
    w, h = size
    rng = random.Random(0)  # noqa: S311 (test pixels, not crypto)
    channels = [
        Image.frombytes("L", (w // 8, h // 8), rng.randbytes(w * h // 64)).resize(
            size, Image.Resampling.BICUBIC
        )
        for _ in range(3)
    ]
    image = Image.merge("RGB", channels).convert("RGBA")
    image.putalpha(Image.linear_gradient("L").rotate(90).resize(size))
    return _pil_png(image)


def logo_images(pdf: bytes) -> dict[str, tuple[bytes, bytes]]:
    """Image XObjects by role ("colour" or "mask"): (dictionary, decoded stream)."""
    images = [(h, s) for h, s in _objects(pdf).values() if b"/Subtype /Image" in h]
    out = {}
    for head, stream in images:
        out["colour" if b"/SMask" in head or len(images) == 1 else "mask"] = (head, stream)
    return out


def _dict_int(head: bytes, key: bytes) -> int:
    return int(re.search(rb"/" + key + rb" (\d+)", head).group(1))


def flate_image(head: bytes, filtered: bytes) -> Image.Image:
    """Rebuild a PNG from a Flate+PNG-predictor image stream and decode it with Pillow."""
    assert b"/FlateDecode" in head and b"/Predictor 15" in head
    width, height = _dict_int(head, b"Width"), _dict_int(head, b"Height")
    assert _dict_int(head, b"Columns") == width
    colour_type = {3: 2, 1: 0}[_dict_int(head, b"Colors")]
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(filtered))
        + _png_chunk(b"IEND", b"")
    )
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def _needed_px(points: float) -> int:
    return round(points / 72 * render.LOGO_DPI)


def test_render_pdf_large_logo_is_downscaled_to_logo_dpi_and_keeps_transparency() -> None:
    branding = Branding(company_name="Example", logo_png=_logo_box((2000, 800)))
    data = render.render_pdf(RECORD, branding)
    [(width, height)] = image_draws(data)  # drawn size is unchanged by the downscale
    assert (width, height) == pytest.approx((render.LOGO_MAX_WIDTH, render.LOGO_MAX_HEIGHT))
    images = logo_images(data)
    for head, _ in images.values():
        assert _dict_int(head, b"Width") == _needed_px(render.LOGO_MAX_WIDTH)  # 394
        assert _dict_int(head, b"Height") == _needed_px(render.LOGO_MAX_HEIGHT)  # 157
    colour_head, colour = images["colour"]
    mask_head, mask = images["mask"]
    assert b"/ColorSpace /DeviceRGB" in colour_head
    assert b"/ColorSpace /DeviceGray" in mask_head
    # Flat colours: PNG is smaller than JPEG. Decode both and check the pixels.
    rgb, alpha = flate_image(colour_head, colour), flate_image(mask_head, mask)
    centre = (rgb.width // 2, rgb.height // 2)
    assert rgb.getpixel(centre) == (26, 115, 232)
    assert alpha.getpixel(centre) == 255
    assert alpha.getpixel((0, 0)) == 0


def test_render_pdf_small_logo_is_not_enlarged() -> None:
    data = render.render_pdf(RECORD, Branding(company_name="Example", logo_png=_rgba_png(40, 20)))
    for head, _ in logo_images(data).values():
        assert (_dict_int(head, b"Width"), _dict_int(head, b"Height")) == (40, 20)


def test_render_pdf_photo_logo_uses_jpeg_when_smaller() -> None:
    branding = Branding(company_name="Example", logo_png=_photo_logo((1600, 640)))
    data = render.render_pdf(RECORD, branding)
    images = logo_images(data)
    colour_head, _ = images["colour"]
    mask_head, mask = images["mask"]
    assert b"/DCTDecode" in colour_head
    assert b"/SMask" in colour_head
    assert b"/FlateDecode" in mask_head  # the soft mask is always lossless
    alpha = flate_image(mask_head, mask)
    assert alpha.size == (_needed_px(render.LOGO_MAX_WIDTH), _needed_px(render.LOGO_MAX_HEIGHT))


def test_logo_resolution_is_capped_at_200_dpi_of_its_printed_size() -> None:
    # 1600 x 600 drawn at 5 cm x 1.875 cm: 394 x 148 px at 200 dpi (591 x 221 at 300).
    assert render.LOGO_DPI == 200
    data = render.render_pdf(
        RECORD, Branding(company_name="Example", logo_png=_logo_box((1600, 600)))
    )
    [(width, height)] = image_draws(data)  # the layout is unchanged
    assert width == pytest.approx(render.LOGO_MAX_WIDTH)
    assert height == pytest.approx(render.LOGO_MAX_WIDTH * 600 / 1600)
    for head, _ in logo_images(data).values():
        assert (_dict_int(head, b"Width"), _dict_int(head, b"Height")) == (394, 148)


def test_detailed_jpeg_logo_is_kept_when_the_optimizer_buffer_is_too_small() -> None:
    # Pillow's optimizing JPEG encoder fails ("Suspension not allowed here") when the
    # image compresses to more than its ~width x height byte buffer; the logo must
    # still be embedded (as a non-optimized JPEG), never silently dropped.
    branding = Branding(company_name="Example", logo_png=_photo_logo((1600, 600)))
    prepared = render.prepare_branding(branding)
    assert prepared.logo is not None
    image = prepared.logo.image
    assert image.filter == "DCTDecode" and (image.width, image.height) == (394, 148)
    assert len(image.data) > image.width * image.height  # past the optimizer's buffer
    decoded = Image.open(io.BytesIO(image.data))
    assert decoded.format == "JPEG" and decoded.size == (394, 148)
    data = render.render_pdf(RECORD, prepared)
    assert len(image_draws(data)) == 1


def test_render_pdf_opaque_logo_has_no_soft_mask() -> None:
    opaque = Image.new("RGBA", (300, 100), (26, 115, 232, 255))
    data = render.render_pdf(RECORD, Branding(company_name="Example", logo_png=_pil_png(opaque)))
    [(head, _)] = logo_images(data).values()
    assert b"/SMask" not in head


def test_render_pdf_greyscale_jpeg_logo() -> None:
    buf = io.BytesIO()
    Image.new("L", (64, 16), 90).save(buf, "JPEG")
    data = render.render_pdf(RECORD, Branding(company_name="Example", logo_png=buf.getvalue()))
    [(head, _)] = logo_images(data).values()
    assert b"/ColorSpace /DeviceGray" in head


def test_prepared_branding_is_reused_across_pdfs() -> None:
    logo = qr.make_qr_png("logo", scale=4)
    branding = Branding(company_name="Example Telecom (test)", logo_png=logo, footer_text="x")
    prepared = render.prepare_branding(branding)
    first = render.render_pdf(RECORD, prepared)
    second = render.render_pdf(RECORD_WITH_CONFIRMATION, prepared)
    again = render.render_pdf(RECORD, prepared)
    assert first == again == render.render_pdf(RECORD, branding)
    for data in (first, second):
        assert len(image_draws(data)) == 1


def test_render_pdf_invalid_logo_is_skipped() -> None:
    branding = Branding(company_name="Example Telecom (test)", logo_png=b"not a png")
    data = render.render_pdf(RECORD, branding)
    assert page_count(data) == 1
    assert image_draws(data) == []


# --- secrets never printed -------------------------------------------------------


@pytest.mark.parametrize("record", [RECORD, RECORD_WITH_CONFIRMATION], ids=["plain", "confirm"])
def test_render_pdf_never_prints_secrets(record) -> None:
    data = render.render_pdf(record, BRANDING)
    raw = data + decompressed(data)
    text = extract_text(data)
    for secret in (ACTIVATION, MATCHING_ID, CONFIRMATION):
        assert secret not in text
        assert secret.encode() not in raw
    assert "LPA:1$" not in text


def test_render_pdf_confirmation_note_only_when_code_exists(pdf_text) -> None:
    with_code = extract_text(render.render_pdf(RECORD_WITH_CONFIRMATION, BRANDING))
    assert CONFIRMATION_NOTE in with_code
    assert CONFIRMATION_NOTE not in pdf_text


# --- colour ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "red", "#12345", "#GGGGGG", "1A73E8FF", "#1A73E8 x"])
def test_invalid_primary_hex_falls_back_to_black(bad) -> None:
    black = render.render_pdf(RECORD, Branding(company_name="Example", primary_hex="#000000"))
    fallback = render.render_pdf(RECORD, Branding(company_name="Example", primary_hex=bad))
    assert fallback == black


def test_valid_primary_hex_is_used_for_headings() -> None:
    blue = render.render_pdf(RECORD, Branding(company_name="Example", primary_hex="#1A73E8"))
    no_hash = render.render_pdf(RECORD, Branding(company_name="Example", primary_hex="1a73e8"))
    black = render.render_pdf(RECORD, Branding(company_name="Example", primary_hex="#000000"))
    assert blue == no_hash
    assert blue != black
    # 0x1A/255, 0x73/255, 0xE8/255 as a fill colour.
    assert re.search(rb"\.10196\d* \.45098\d* \.9098\d* rg", decompressed(blue))


# --- filename ---------------------------------------------------------------------


def test_pdf_filename() -> None:
    assert render.pdf_filename(RECORD) == "esim_8999000000000000013.pdf"


def test_pdf_filename_ignores_secrets() -> None:
    name = render.pdf_filename(RECORD_WITH_CONFIRMATION)
    assert name == "esim_8999000000000000021.pdf"
    assert CONFIRMATION not in name
    assert MATCHING_ID not in name


def test_pdf_filename_is_path_safe() -> None:
    record = EsimRecord(row_number=2, iccid="../89 99/x", activation_code=ACTIVATION)
    assert render.pdf_filename(record) == "esim____89_99_x.pdf"
