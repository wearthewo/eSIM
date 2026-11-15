from __future__ import annotations

import io

import segno

from app.core import qr

PAYLOAD = "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"
CONFIRMATION = "CONF0001"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _MakeSpy:
    """Wraps ``segno.make`` (``segno.make_qr`` delegates to it); records payloads and codes."""

    def __init__(self) -> None:
        self.payloads: list[object] = []
        self.codes: list[segno.QRCode] = []
        self._real = segno.make

    def __call__(self, content, *args, **kwargs):
        self.payloads.append(content)
        code = self._real(content, *args, **kwargs)
        self.codes.append(code)
        return code


def _spy(monkeypatch) -> _MakeSpy:
    spy = _MakeSpy()
    monkeypatch.setattr(segno, "make", spy)
    return spy


# --- PNG ------------------------------------------------------------------------


def test_make_qr_png_signature() -> None:
    data = qr.make_qr_png(PAYLOAD)
    assert isinstance(data, bytes)
    assert data.startswith(PNG_SIGNATURE)


def test_make_qr_png_is_deterministic() -> None:
    assert qr.make_qr_png(PAYLOAD) == qr.make_qr_png(PAYLOAD)


def test_make_qr_png_differs_per_payload() -> None:
    other = "LPA:1$smdp.example.invalid$TEST-0002-AAAA-BBBB"
    assert qr.make_qr_png(PAYLOAD) != qr.make_qr_png(other)


def test_make_qr_png_scale_changes_size() -> None:
    assert len(qr.make_qr_png(PAYLOAD, scale=2)) < len(qr.make_qr_png(PAYLOAD, scale=10))


def test_make_qr_png_writes_nothing_to_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    qr.make_qr_png(PAYLOAD)
    qr.make_qr_svg(PAYLOAD)
    assert list(tmp_path.iterdir()) == []


# --- SVG ------------------------------------------------------------------------


def test_make_qr_svg_is_svg_bytes() -> None:
    data = qr.make_qr_svg(PAYLOAD)
    assert isinstance(data, bytes)
    assert b"<svg" in data
    assert data.rstrip().endswith(b"</svg>")


def test_make_qr_svg_is_deterministic() -> None:
    assert qr.make_qr_svg(PAYLOAD) == qr.make_qr_svg(PAYLOAD)


# --- payload and error level --------------------------------------------------------


def test_payload_is_exactly_the_activation_code(monkeypatch) -> None:
    spy = _spy(monkeypatch)
    qr.make_qr_png(PAYLOAD)
    qr.make_qr_svg(PAYLOAD)
    assert spy.payloads == [PAYLOAD, PAYLOAD]
    for payload in spy.payloads:
        assert CONFIRMATION not in payload


def test_error_correction_level_is_m(monkeypatch) -> None:
    spy = _spy(monkeypatch)
    qr.make_qr_png(PAYLOAD)
    qr.make_qr_svg(PAYLOAD)
    assert [code.error for code in spy.codes] == ["M", "M"]
    assert all(code.is_micro is False for code in spy.codes)


def test_output_matches_reference_encoding_of_payload_only() -> None:
    reference = segno.make_qr(PAYLOAD, error="m", boost_error=False)
    buf = io.BytesIO()
    reference.save(buf, kind="png", scale=8, border=4)
    assert qr.make_qr_png(PAYLOAD) == buf.getvalue()
