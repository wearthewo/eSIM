from __future__ import annotations

import base64
import re
import zlib

import pytest
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
            stream = stream_match.group(1)
            head = body[: stream_match.start()]
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


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return render.render_pdf(RECORD, BRANDING)


@pytest.fixture(scope="module")
def pdf_text(pdf) -> str:
    return extract_text(pdf)


# --- fonts ----------------------------------------------------------------------


def test_bundled_fonts_present() -> None:
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "LICENSE_DEJAVU"):
        assert (render.FONT_DIR / name).is_file()


def test_register_fonts_is_idempotent() -> None:
    render.register_fonts()
    first = pdfmetrics.getFont(render.FONT_REGULAR), pdfmetrics.getFont(render.FONT_BOLD)
    render.register_fonts()
    second = pdfmetrics.getFont(render.FONT_REGULAR), pdfmetrics.getFont(render.FONT_BOLD)
    assert first[0] is second[0]
    assert first[1] is second[1]
    assert {render.FONT_REGULAR, render.FONT_BOLD} <= set(pdfmetrics.getRegisteredFontNames())


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
    assert re.search(rb"/BaseFont /[A-Z]{6}\+DejaVuSans-Bold\b", pdf)
    assert b"/FontFile2" in pdf  # the TrueType program itself is embedded
    assert GREEK_NAME in pdf_text
    assert GREEK_PLAN in pdf_text


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


def test_render_pdf_qr_is_at_least_4cm(pdf) -> None:
    (width, height) = image_draws(pdf)[0]  # no logo, so the only image is the QR
    assert len(image_draws(pdf)) == 1
    assert width >= 4 * cm
    assert height == pytest.approx(width)


def test_render_pdf_qr_payload_is_activation_code_only(monkeypatch) -> None:
    calls: list[str] = []
    real = qr.make_qr_png

    def spy(payload: str, **kwargs) -> bytes:
        calls.append(payload)
        return real(payload, **kwargs)

    monkeypatch.setattr(qr, "make_qr_png", spy)
    render.render_pdf(RECORD_WITH_CONFIRMATION, BRANDING)
    assert calls == [ACTIVATION]
    assert CONFIRMATION not in calls[0]


def test_render_pdf_with_logo() -> None:
    logo = qr.make_qr_png("logo", scale=4)  # any valid PNG
    branding = Branding(company_name="Example Telecom (test)", logo_png=logo)
    data = render.render_pdf(RECORD, branding)
    draws = image_draws(data)
    assert len(draws) == 2
    assert max(w for w, _ in draws) >= 4 * cm


def test_render_pdf_invalid_logo_is_skipped() -> None:
    branding = Branding(company_name="Example Telecom (test)", logo_png=b"not a png")
    data = render.render_pdf(RECORD, branding)
    assert page_count(data) == 1
    assert len(image_draws(data)) == 1


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
