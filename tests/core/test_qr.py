from __future__ import annotations

from app.core import qr
from tests.conftest import not_implemented

PAYLOAD = "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"


@not_implemented
def test_make_qr_png() -> None:
    qr.make_qr_png(PAYLOAD)


@not_implemented
def test_make_qr_svg() -> None:
    qr.make_qr_svg(PAYLOAD)
