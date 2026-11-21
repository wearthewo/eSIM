"""TOTP codes, the encrypted secret and recovery codes (``app.auth.mfa``), without a database.

All keys and secrets here are synthetic.
"""

from __future__ import annotations

import base64
import datetime as dt
import pickle
from urllib.parse import parse_qs, urlsplit

import pytest

from app.auth import mfa

# RFC 6238 appendix B, HMAC-SHA1: the ASCII secret "12345678901234567890" and the 8-digit
# codes at the given times. Six digits are the last six of those.
RFC_SECRET = b"12345678901234567890"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]
KEYS = mfa.keys_from("synthetic-mfa-key-for-tests-0123456789abcdef")
OTHER_KEYS = mfa.keys_from("another-synthetic-mfa-key-0123456789abcdef")


def at(seconds: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(seconds, dt.UTC)


# --- TOTP ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "eight_digits"), RFC_VECTORS)
def test_rfc_6238_vectors(seconds: int, eight_digits: str) -> None:
    step = mfa.step_of(at(seconds))
    assert step == seconds // 30
    assert mfa.code_at(RFC_SECRET, step) == eight_digits[-6:]


def test_current_and_previous_step_only() -> None:
    now = at(1_800_000_015)  # 15 s into a step
    step = mfa.step_of(now)
    for offset, accepted in [(0, True), (-1, True), (-2, False), (1, False), (-10, False)]:
        code = mfa.code_at(RFC_SECRET, step + offset)
        matched = mfa.check_code(RFC_SECRET, code, now, None)
        assert (matched == step + offset) if accepted else matched is None, offset


def test_a_used_step_and_older_ones_are_refused() -> None:
    now = at(1_800_000_015)
    step = mfa.step_of(now)
    current, previous = mfa.code_at(RFC_SECRET, step), mfa.code_at(RFC_SECRET, step - 1)
    assert mfa.check_code(RFC_SECRET, current, now, last_step=step) is None  # replay
    assert mfa.check_code(RFC_SECRET, previous, now, last_step=step) is None  # older
    assert mfa.check_code(RFC_SECRET, current, now, last_step=step - 1) == step


@pytest.mark.parametrize(
    "code", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", "12 34 5x", "1" * 100]
)
def test_malformed_codes_never_match(code: str) -> None:
    assert mfa.check_code(RFC_SECRET, code, at(59), None) is None


def test_spaces_in_a_code_are_ignored() -> None:
    now = at(59)
    code = mfa.code_at(RFC_SECRET, mfa.step_of(now))
    assert mfa.check_code(RFC_SECRET, f" {code[:3]} {code[3:]} ", now, None) is not None


def test_new_secrets_are_random_and_160_bits() -> None:
    first, second = mfa.new_secret(), mfa.new_secret()
    assert len(first) == 20 and first != second


def test_text_key_and_uri() -> None:
    key = mfa.text_key(RFC_SECRET)
    assert key == "GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ"
    assert base64.b32decode(key.replace(" ", "")) == RFC_SECRET
    uri = mfa.otpauth_uri("owner+test@org.test", RFC_SECRET)
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp"
    assert parts.path == "/eSIM%20PDF%20generator:owner%2Btest@org.test"
    query = parse_qs(parts.query)
    assert query == {
        "secret": ["GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"],
        "issuer": ["eSIM PDF generator"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


# --- The stored secret -----------------------------------------------------------------------


def test_secret_round_trip_and_never_plaintext() -> None:
    secret = mfa.new_secret()
    blob = mfa.encrypt_secret(KEYS, 7, secret)
    assert mfa.decrypt_secret(KEYS, 7, blob) == secret
    assert secret not in blob
    assert base64.b32encode(secret).rstrip(b"=") not in blob
    assert blob[:1] == b"\x01" and len(blob) == 1 + 12 + 20 + 16
    assert mfa.encrypt_secret(KEYS, 7, secret) != blob  # a new nonce every time


@pytest.mark.parametrize(
    "case", ["wrong key", "other user", "flipped byte", "truncated", "bad version", "empty"]
)
def test_secret_does_not_decrypt_when_tampered(case: str) -> None:
    blob = mfa.encrypt_secret(KEYS, 7, mfa.new_secret())
    keys, user_id = KEYS, 7
    if case == "wrong key":
        keys = OTHER_KEYS
    elif case == "other user":
        user_id = 8  # the user id is the associated data
    elif case == "flipped byte":
        blob = blob[:-1] + bytes([blob[-1] ^ 1])
    elif case == "truncated":
        blob = blob[:20]
    elif case == "bad version":
        blob = b"\x02" + blob[1:]
    else:
        blob = b""
    with pytest.raises(mfa.SecretUnreadable):
        mfa.decrypt_secret(keys, user_id, blob)


def test_keys_never_show_or_pickle() -> None:
    assert repr(KEYS) == "MfaKeys(***)"
    assert KEYS.secret_key != KEYS.recovery_key  # separate subkeys
    with pytest.raises(TypeError):
        pickle.dumps(KEYS)


# --- Recovery codes --------------------------------------------------------------------------


def test_recovery_codes_shape() -> None:
    codes = mfa.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    for code in codes:
        assert len(code) == 11 and code[5] == "-"
        assert all(ch in mfa.RECOVERY_ALPHABET for ch in code.replace("-", ""))


@pytest.mark.parametrize(
    ("typed", "normalized"),
    [
        ("ABCDE-12345", "ABCDE12345"),
        ("abcde 12345", "ABCDE12345"),
        (" abcde-12345 ", "ABCDE12345"),
        ("OOOOO-IIIII", "0000011111"),  # O reads as 0, I and L as 1
        ("lllll-00000", "1111100000"),
        ("ABCDE-1234", None),
        ("ABCDE-12345-6", None),
        ("ABCDU-12345", None),  # U is not in the alphabet
        ("123456", None),
        ("x" * 100, None),
    ],
)
def test_recovery_code_normalization(typed: str, normalized: str | None) -> None:
    assert mfa.normalize_recovery_code(typed) == normalized


def test_recovery_hash_is_keyed_and_per_user() -> None:
    digest = mfa.recovery_hash(KEYS, 7, "ABCDE12345")
    assert len(digest) == 32
    assert digest == mfa.recovery_hash(KEYS, 7, "ABCDE12345")
    assert digest != mfa.recovery_hash(KEYS, 8, "ABCDE12345")
    assert digest != mfa.recovery_hash(OTHER_KEYS, 7, "ABCDE12345")
    assert digest != mfa.recovery_hash(KEYS, 7, "ABCDE12346")
