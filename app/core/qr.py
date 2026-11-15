"""QR code generation with segno, entirely in memory.

The payload is only the activation code (LPA string). The confirmation code is
never embedded in the QR payload. It is entered separately by the end user.
"""

from __future__ import annotations


def make_qr_png(payload: str, *, scale: int = 8, border: int = 4) -> bytes:
    """Encode ``payload`` (the LPA activation string) and return PNG bytes.

    Uses error-correction level M. Nothing is written to disk.
    """
    raise NotImplementedError


def make_qr_svg(payload: str, *, scale: int = 8, border: int = 4) -> bytes:
    """Same as ``make_qr_png`` but returns SVG bytes for vector embedding."""
    raise NotImplementedError
