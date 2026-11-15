from __future__ import annotations

from app.core import validator
from tests.conftest import not_implemented


@not_implemented
def test_validate() -> None:
    validator.validate([])


@not_implemented
def test_validate_iccid() -> None:
    validator.validate_iccid("8999000000000000013")


@not_implemented
def test_validate_imsi() -> None:
    validator.validate_imsi("001010000000030")


@not_implemented
def test_validate_msisdn() -> None:
    validator.validate_msisdn("+306900000030")


@not_implemented
def test_validate_activation_code() -> None:
    validator.validate_activation_code("LPA:1$smdp.example.invalid$TEST-0001")


@not_implemented
def test_luhn_ok() -> None:
    validator.luhn_ok("8999000000000000013")


@not_implemented
def test_find_duplicates() -> None:
    validator.find_duplicates([])
