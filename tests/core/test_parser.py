from __future__ import annotations

from app.core import parser
from tests.conftest import not_implemented


@not_implemented
def test_parse_upload(fixture_bytes) -> None:
    parser.parse_upload(fixture_bytes("valid_minimal.csv"), "valid_minimal.csv")


@not_implemented
def test_parse_csv(fixture_bytes) -> None:
    parser.parse_csv(fixture_bytes("valid_full.csv"))


@not_implemented
def test_parse_xlsx() -> None:
    parser.parse_xlsx(b"")


@not_implemented
def test_normalize_headers() -> None:
    parser.normalize_headers(["ICCID", "activation_code"])


@not_implemented
def test_build_activation_code() -> None:
    parser.build_activation_code("smdp.example.invalid", "TEST-0001")
