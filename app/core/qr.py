"""QR code generation with segno, entirely in memory.

The payload is only the activation code (LPA string). The confirmation code is
never embedded in the QR payload. It is entered separately by the end user.

``make_qr_matrix`` returns exactly the module matrix of
``segno.make_qr(payload, error="m", boost_error=False)`` but several times
faster. segno spends most of its time choosing the data mask: it scores all
eight masks with per-module Python loops. Here the symbol is encoded once with
mask 0, the mask is undone, and the eight candidates are scored with the same
ISO/IEC 18004 penalty rules (including segno's handling of edge and
overlapping N3 patterns) using big-integer bit operations. The winning mask is
then applied and its format information written. Tests compare the result
with segno for many payloads across versions 1-40.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import cache
from itertools import product

import segno

ERROR_LEVEL = "m"

_TO_DIGITS = bytes.maketrans(b"\x00\x01", b"01")
_FROM_DIGITS = bytes.maketrans(b"01", b"\x00\x01")
_ERROR_BITS_M = 0b00  # format-information indicator for error level M
_GAP = 4  # zero (light) bits between packed lines; see _Layout

# ISO/IEC 18004 Table 10. i = row, j = column.
_MASKS = (
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
)


def _make(payload: str, mask: int | None = None) -> segno.QRCode:
    # boost_error=False keeps the level at exactly M instead of letting segno raise it.
    return segno.make_qr(payload, error=ERROR_LEVEL, boost_error=False, mask=mask)


def make_qr_png(payload: str, *, scale: int = 8, border: int = 4) -> bytes:
    """Encode ``payload`` (the LPA activation string) and return PNG bytes.

    Uses error-correction level M. Nothing is written to disk.
    """
    buf = io.BytesIO()
    _make(payload).save(buf, kind="png", scale=scale, border=border)
    return buf.getvalue()


def make_qr_svg(payload: str, *, scale: int = 8, border: int = 4) -> bytes:
    """Same as ``make_qr_png`` but returns SVG bytes for vector embedding."""
    buf = io.BytesIO()
    _make(payload).save(buf, kind="svg", scale=scale, border=border)
    return buf.getvalue()


def make_qr_matrix(payload: str) -> tuple[bytes, ...]:
    """Encode ``payload`` (the LPA activation string) at error level M.

    Returns the module matrix without quiet zone, top row first; each row is
    ``bytes`` with 0x01 for dark and 0x00 for light modules. Identical to
    ``segno.make_qr(payload, error="m", boost_error=False).matrix``.
    """
    code = _make(payload, mask=0)
    size = len(code.matrix)
    layout = _layout(size)
    pattern0 = layout.patterns[0]
    final0 = [int(bytes(row).translate(_TO_DIGITS), 2) for row in code.matrix]
    # Undo mask 0 and clear format/version areas, as segno has them while scoring.
    base = [(row ^ pattern0[i]) & layout.keep[i] for i, row in enumerate(final0)]
    packed = _pack(base + _columns(base, size), layout.stride)

    best_mask, best_score = 0, None
    for mask, packed_pattern in enumerate(layout.packed_patterns):
        score = _penalty(packed ^ packed_pattern, layout)
        if best_score is None or score < best_score:
            best_mask, best_score = mask, score

    fmt = _format_rows(size, best_mask)
    change = [a ^ b for a, b in zip(pattern0, layout.patterns[best_mask], strict=True)]
    out = []
    for i, row in enumerate(final0):
        bits = ((row ^ change[i]) & layout.fmt_clear[i]) | fmt[i]
        out.append(format(bits, f"0{size}b").encode("ascii").translate(_FROM_DIGITS))
    return tuple(out)


# --- internals ------------------------------------------------------------------
#
# A "line" is a row or column as an int: module j (string index j, as segno
# scans it) is bit ``size - 1 - j``. For scoring, all rows and then all
# columns are packed into one big int, line t at bit offset ``t * stride``,
# with ``_GAP`` zero bits between lines. Zero bits read as light modules,
# which is how segno treats positions past a line's edge, and no run of 5 or
# 1:1:3:1:1 pattern can span a gap, so whole-matrix bit operations give the
# same counts as segno's per-line loops.


@dataclass(frozen=True, slots=True)
class _Layout:
    stride: int
    patterns: tuple[tuple[int, ...], ...]  # per mask: row ints, data region only
    packed_patterns: tuple[int, ...]  # per mask: rows + columns, packed
    keep: tuple[int, ...]  # rows without format, dark-module and version areas
    fmt_clear: tuple[int, ...]  # rows without format area and dark module
    lines: int  # packed: ones on every module of every line
    rows: int  # packed: ones on every module of the row lines
    pairs: int  # packed: 2x2-block anchors (row lines but the last, all bits but the top)


def _bit(size: int, j: int) -> int:
    return 1 << (size - 1 - j)


def _pack(lines: list[int], stride: int) -> int:
    packed = 0
    for t, line in enumerate(lines):
        packed |= line << (t * stride)
    return packed


def _columns(rows: list[int], size: int) -> list[int]:
    text = [format(r, f"0{size}b") for r in rows]
    return [int("".join(col), 2) for col in zip(*text, strict=True)]


def _alignment_centres(version: int) -> list[int]:
    if version < 2:
        return []
    size = version * 4 + 17
    count = version // 7 + 2
    step = 26 if version == 32 else (version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
    return [6] + [size - 7 - k * step for k in range(count - 2, -1, -1)]


@cache
def _layout(size: int) -> _Layout:
    version = (size - 17) // 4
    function = [[False] * size for _ in range(size)]
    fmt = [[False] * size for _ in range(size)]
    ver = [[False] * size for _ in range(size)]
    for i, j in product(range(size), repeat=2):
        in_finder = (i < 8 and j < 8) or (i < 8 and j >= size - 8) or (i >= size - 8 and j < 8)
        function[i][j] = in_finder or i == 6 or j == 6
    centres = _alignment_centres(version)
    first, last = (centres[0], centres[-1]) if centres else (0, 0)
    corners = {(first, first), (first, last), (last, first)}
    for x, y in product(centres, repeat=2):
        if (x, y) in corners:
            continue
        for i, j in product(range(x - 2, x + 3), range(y - 2, y + 3)):
            function[i][j] = True
    for k in (*range(6), 7, 8, *range(size - 8, size)):
        fmt[8][k] = True  # row 8; includes (8, 8); (8, 6) is timing
        fmt[k][8] = True  # column 8; includes the dark module at (size - 8, 8)
    if version >= 7:
        for a, b in product(range(6), range(size - 11, size - 8)):
            ver[a][b] = ver[b][a] = True

    def row_bits(cells: list[bool]) -> int:
        return sum(_bit(size, j) for j, on in enumerate(cells) if on)

    full = (1 << size) - 1
    stride = size + _GAP
    data = [
        [not (function[i][j] or fmt[i][j] or ver[i][j]) for j in range(size)] for i in range(size)
    ]
    patterns = tuple(
        tuple(row_bits([data[i][j] and fn(i, j) for j in range(size)]) for i in range(size))
        for fn in _MASKS
    )
    packed_patterns = tuple(_pack(list(p) + _columns(list(p), size), stride) for p in patterns)
    keep = tuple(
        full & ~row_bits([fmt[i][j] or ver[i][j] for j in range(size)]) for i in range(size)
    )
    fmt_clear = tuple(full & ~row_bits(fmt[i]) for i in range(size))
    return _Layout(
        stride=stride,
        patterns=patterns,
        packed_patterns=packed_patterns,
        keep=keep,
        fmt_clear=fmt_clear,
        lines=_pack([full] * (2 * size), stride),
        rows=_pack([full] * size, stride),
        pairs=_pack([full >> 1] * (size - 1), stride),
    )


@cache
def _format_rows(size: int, mask: int) -> tuple[int, ...]:
    """Format information bits (and the dark module) for error level M, as row ints."""
    data = (_ERROR_BITS_M << 3) | mask
    rem = data << 10
    for shift in range(4, -1, -1):  # BCH(15, 5), generator 0x537
        if rem & (1 << (shift + 10)):
            rem ^= 0x537 << shift
    info = ((data << 10) | rem) ^ 0x5412
    rows = [0] * size
    for k in range(8):
        vbit = (info >> k) & 1
        hbit = (info >> (14 - k)) & 1
        pos = k if k < 6 else k + 1  # skip the timing pattern at index 6
        if vbit:
            rows[pos] |= _bit(size, 8)  # column 8, top left
            rows[8] |= _bit(size, size - 1 - k)  # row 8, top right
        if hbit:
            rows[8] |= _bit(size, pos)  # row 8, top left
            rows[size - 1 - k] |= _bit(size, 8)  # column 8, bottom left
    rows[size - 8] |= _bit(size, 8)  # dark module
    return tuple(rows)


def _runs_penalty(x: int) -> int:
    """N1 for runs of set bits: each run of length L >= 5 scores L - 2."""
    windows = x & (x >> 1) & (x >> 2) & (x >> 3) & (x >> 4)  # bit q: q..q+4 all set
    runs = windows & ~(windows >> 1)  # one bit per run
    return windows.bit_count() + 2 * runs.bit_count()  # sum of (L - 4) + 2 per run


def _n3_penalty(m: int) -> int:
    """N3: 1:1:3:1:1 (1011101) patterns with 4 light modules before or after.

    Bit q marks a pattern on bits q..q+6. "Before" in scan order is the higher
    bits q+7..q+10, "after" the lower bits q-4..q-1. Like segno, after a
    counted pattern the scan resumes 7 modules on, otherwise 4 on; this only
    matters when two patterns overlap (4 or 6 apart), which is rare.
    """
    found = m & ~(m >> 1) & (m >> 2) & (m >> 3) & (m >> 4) & ~(m >> 5) & (m >> 6)
    if not found:
        return 0
    before = (m >> 7) | (m >> 8) | (m >> 9) | (m >> 10)  # bit q: any dark in q+7..q+10
    after = (m << 1) | (m << 2) | (m << 3) | (m << 4)  # bit q: any dark in q-4..q-1 (>= 0)
    counted = found & (~before | ~after)
    if not found & ((found >> 4) | (found >> 6)):
        return 40 * counted.bit_count()
    score, limit, rest = 0, None, found
    while rest:
        q = rest.bit_length() - 1
        rest ^= 1 << q
        if limit is not None and q > limit:
            continue
        if (counted >> q) & 1:
            score += 40
            limit = q - 7
        else:
            limit = q - 4
    return score


def _penalty(m: int, layout: _Layout) -> int:
    """ISO/IEC 18004 7.8.3.1 penalty (N1 + N2 + N3 + N4) of packed lines ``m``, as segno."""
    light = ~m & layout.lines
    score = _runs_penalty(m) + _runs_penalty(light) + _n3_penalty(m)
    rows = m & layout.rows
    same = ~(rows ^ (rows >> layout.stride))  # row t vs row t + 1
    blocks = same & (same >> 1) & ~(rows ^ (rows >> 1)) & layout.pairs
    score += 3 * blocks.bit_count()
    size = layout.stride - _GAP
    dark = rows.bit_count()
    score += 10 * int(abs(float(dark) / (size**2) * 100 - 50) / 5)
    return score
