from __future__ import annotations

import io
import random

import pytest
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


# --- module matrix (used by the PDF renderer) ---------------------------------------


def _reference_matrix(payload: str) -> tuple[bytes, ...]:
    code = segno.make_qr(payload, error="m", boost_error=False)
    return tuple(bytes(row) for row in code.matrix)


def test_make_qr_matrix_shape_and_values() -> None:
    matrix = qr.make_qr_matrix(PAYLOAD)
    assert all(isinstance(row, bytes) and len(row) == len(matrix) for row in matrix)
    assert {b for row in matrix for b in row} == {0, 1}


def test_make_qr_matrix_matches_segno() -> None:
    assert qr.make_qr_matrix(PAYLOAD) == _reference_matrix(PAYLOAD)


def test_make_qr_matrix_matches_segno_for_many_payloads() -> None:
    # Exercises most mask choices and versions 1-19 (alignment and version areas).
    rng = random.Random(20240501)  # noqa: S311 (test data, not crypto)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$-.:abcxyz"
    payloads = [f"LPA:1$smdp.example.invalid$TEST-{i:04d}-AAAA-BBBB" for i in range(40)]
    payloads += ["".join(rng.choice(alphabet) for _ in range(n)) for n in range(1, 160, 3)]
    payloads += ["".join(rng.choice(alphabet) for _ in range(n)) for n in (250, 400, 600)]
    masks, versions = set(), set()
    for payload in payloads:
        reference = segno.make_qr(payload, error="m", boost_error=False)
        masks.add(reference.mask)
        versions.add(reference.version)
        assert qr.make_qr_matrix(payload) == tuple(bytes(r) for r in reference.matrix), payload
    assert len(masks) >= 6
    assert min(versions) == 1 and max(versions) >= 7  # 7+ carries version information


def test_make_qr_matrix_payload_and_error_level(monkeypatch) -> None:
    spy = _spy(monkeypatch)
    qr.make_qr_matrix(PAYLOAD)
    assert spy.payloads == [PAYLOAD]
    assert CONFIRMATION not in spy.payloads[0]
    assert [code.error for code in spy.codes] == ["M"]
    assert spy.codes[0].is_micro is False


def _segno_n3(line: str) -> int:
    """N3 for one line, written the way segno's mask_scores counts it."""
    size, count, idx = len(line), 0, line.find("1011101")
    while idx != -1:
        offset = idx + 7
        if (
            idx in (0, size - 7)
            or "1" not in line[max(idx - 4, 0) : idx]
            or "1" not in line[offset : offset + 4]
        ):
            count += 40
        else:
            offset = idx + 4
        idx = line.find("1011101", offset)
    return count


@pytest.mark.parametrize(
    "line",
    [
        "1011101",
        "00001011101",
        "10111010000",
        "1111011101111",
        "000010111011101",  # overlap 4 apart: first counted, second skipped
        "1011101110111010000",  # overlap 4 apart: first not counted, second counted
        "0000101110101110100000",  # overlap 6 apart
        "10111011011101",
    ],
)
def test_n3_penalty_matches_segno_rules(line) -> None:
    assert qr._n3_penalty(int(line, 2)) == _segno_n3(line)


def test_n3_penalty_matches_segno_rules_random() -> None:
    rng = random.Random(7)  # noqa: S311 (test data, not crypto)
    pieces = ["1011101", "0000", "1", "0", "11", "01"]
    for _ in range(2000):
        line = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        assert qr._n3_penalty(int(line, 2)) == _segno_n3(line), line


def test_output_matches_reference_encoding_of_payload_only() -> None:
    reference = segno.make_qr(PAYLOAD, error="m", boost_error=False)
    buf = io.BytesIO()
    reference.save(buf, kind="png", scale=8, border=4)
    assert qr.make_qr_png(PAYLOAD) == buf.getvalue()
