from __future__ import annotations

from app.core import render
from app.core.models import Branding, EsimRecord
from tests.conftest import not_implemented

RECORD = EsimRecord(
    row_number=2,
    iccid="8999000000000000013",
    activation_code="LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB",
    customer_name="Γιώργος Παπαδόπουλος",
)
BRANDING = Branding(company_name="Example Telecom (test)")


def test_bundled_fonts_present() -> None:
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "LICENSE_DEJAVU"):
        assert (render.FONT_DIR / name).is_file()


@not_implemented
def test_register_fonts() -> None:
    render.register_fonts()


@not_implemented
def test_render_pdf() -> None:
    render.render_pdf(RECORD, BRANDING)


@not_implemented
def test_pdf_filename() -> None:
    render.pdf_filename(RECORD)
