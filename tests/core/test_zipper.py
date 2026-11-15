from __future__ import annotations

from app.core import zipper
from tests.conftest import not_implemented


@not_implemented
def test_sanitize_filename() -> None:
    zipper.sanitize_filename("../evil.pdf")


@not_implemented
def test_escape_csv_cell() -> None:
    zipper.escape_csv_cell("=1+1")


@not_implemented
def test_build_manifest() -> None:
    zipper.build_manifest([(2, "8999000000000000013", "esim_8999000000000000013.pdf")])


@not_implemented
def test_build_zip() -> None:
    zipper.build_zip([("a.pdf", b"%PDF-")])
