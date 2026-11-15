"""QR code generation with segno, entirely in memory.

The payload is only the activation code (LPA string). The confirmation code is
never embedded in the QR payload. It is entered separately by the end user.
"""

from __future__ import annotations

import io

import segno

ERROR_LEVEL = "m"


def _make(payload: str) -> segno.QRCode:
    # boost_error=False keeps the level at exactly M instead of letting segno raise it.
    return segno.make_qr(payload, error=ERROR_LEVEL, boost_error=False)


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
