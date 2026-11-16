"""HMAC fingerprints of codes (no database needed). All values are synthetic."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.db import fingerprints
from app.db.fingerprints import DOMAIN_TAG, FingerprintKeyError, fingerprint
from app.db.models import FingerprintKind
from app.settings import Secret, Settings

KEY = b"k" * 32
OTHER_KEY = b"q" * 32
ICCID = "8999000000000000013"
CODE = "LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB"


def test_deterministic_within_an_organization() -> None:
    first = fingerprint(1, FingerprintKind.ACTIVATION, CODE, key=KEY)
    assert first == fingerprint(1, FingerprintKind.ACTIVATION, CODE, key=KEY)
    assert fingerprint(1, "activation", CODE, key=KEY) == first  # kind as a string
    assert isinstance(first, bytes)
    assert len(first) == 32


def test_differs_across_organizations() -> None:
    assert fingerprint(1, "iccid", ICCID, key=KEY) != fingerprint(2, "iccid", ICCID, key=KEY)


def test_differs_across_kinds() -> None:
    value = "8999000000000000013"
    assert fingerprint(1, "iccid", value, key=KEY) != fingerprint(1, "activation", value, key=KEY)


def test_differs_across_values_and_keys() -> None:
    base = fingerprint(1, "iccid", ICCID, key=KEY)
    assert fingerprint(1, "iccid", "8999000000000000021", key=KEY) != base
    assert fingerprint(1, "iccid", ICCID, key=OTHER_KEY) != base


def test_is_keyed_hmac_sha256_not_a_plain_hash() -> None:
    message = (
        DOMAIN_TAG
        + (1).to_bytes(4, "big")
        + b"7"
        + (5).to_bytes(4, "big")
        + b"iccid"
        + len(ICCID).to_bytes(4, "big")
        + ICCID.encode()
    )
    expected = hmac.new(KEY, message, hashlib.sha256).digest()
    assert fingerprint(7, "iccid", ICCID, key=KEY) == expected
    assert expected != hashlib.sha256(ICCID.encode()).digest()
    assert expected != hashlib.sha256(message).digest()


def test_field_boundaries_are_unambiguous() -> None:
    # Organization 1 + value "1..." must not collide with organization 11 + value "...".
    assert fingerprint(1, "iccid", "1899", key=KEY) != fingerprint(11, "iccid", "899", key=KEY)


def test_uses_the_settings_key_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(fingerprint_key=Secret(KEY.decode()))
    monkeypatch.setattr(fingerprints, "get_settings", lambda: settings)
    assert fingerprint(1, "iccid", ICCID) == fingerprint(1, "iccid", ICCID, key=KEY)


def test_missing_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fingerprints, "get_settings", lambda: Settings())
    with pytest.raises(FingerprintKeyError, match="FINGERPRINT_KEY is not set"):
        fingerprint(1, "iccid", ICCID)
    with pytest.raises(FingerprintKeyError):
        fingerprint(1, "iccid", ICCID, key=b"")


def test_bad_arguments_do_not_echo_the_value() -> None:
    with pytest.raises(ValueError, match="matching_id"):
        fingerprint(1, "matching_id", CODE, key=KEY)
    with pytest.raises(TypeError) as info:
        fingerprint(True, "iccid", CODE, key=KEY)
    assert CODE not in str(info.value)
    with pytest.raises(TypeError):
        fingerprint("1", "iccid", CODE, key=KEY)  # type: ignore[arg-type]
    with pytest.raises(TypeError) as info:
        fingerprint(1, "iccid", 8999000000000000013, key=KEY)  # type: ignore[arg-type]
    assert "8999000000000000013" not in str(info.value)
