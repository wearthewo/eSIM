"""Keyed fingerprints of ICCIDs and activation codes.

A fingerprint is HMAC-SHA256 keyed with ``FINGERPRINT_KEY`` over the organization
ID, the kind and the value. It lets the app recognise a code it has seen before
without storing the code: without the key a fingerprint cannot be checked against
guessed values, and the same code gives unrelated fingerprints in different
organizations. Never store an unkeyed hash of a code.

The message is a domain tag followed by each field as a 4-byte big-endian length
and its UTF-8 bytes, so field boundaries are unambiguous. Values are used exactly
as given: callers pass the validated, normalised value.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

from app.db.models import FingerprintKind
from app.settings import get_settings

DOMAIN_TAG = b"esim-fp-v1"


class FingerprintKeyError(RuntimeError):
    """``FINGERPRINT_KEY`` is not set."""


def _field(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + data


def _settings_key() -> bytes:
    key = get_settings().fingerprint_key
    if key is None:
        raise FingerprintKeyError("FINGERPRINT_KEY is not set")
    return key.reveal().encode("utf-8")


def fingerprint(
    organization_id: int,
    kind: FingerprintKind | str,
    value: str,
    *,
    key: bytes | None = None,
) -> bytes:
    """32-byte HMAC-SHA256 of ``value`` for ``organization_id`` and ``kind``.

    ``key`` defaults to ``FINGERPRINT_KEY`` from the settings. Error messages never
    include ``value`` or the key.
    """
    if isinstance(organization_id, bool) or not isinstance(organization_id, int):
        raise TypeError("organization_id must be an int")
    if not isinstance(value, str):
        raise TypeError("value must be a str")
    kind = FingerprintKind(kind)
    if key is None:
        key = _settings_key()
    if not key:
        raise FingerprintKeyError("the fingerprint key is empty")
    message = (
        DOMAIN_TAG
        + _field(str(organization_id).encode("ascii"))
        + _field(kind.value.encode("ascii"))
        + _field(value.encode("utf-8"))
    )
    return hmac.new(key, message, hashlib.sha256).digest()


def fingerprinter(
    organization_id: int, kind: FingerprintKind | str, *, key: bytes
) -> Callable[[str], bytes]:
    """``fingerprint`` for many values of one organization and kind, faster.

    The keyed hash of the message prefix is computed once and copied for each value,
    so ``fingerprinter(o, k, key=key)(v) == fingerprint(o, k, v, key=key)``.
    """
    if isinstance(organization_id, bool) or not isinstance(organization_id, int):
        raise TypeError("organization_id must be an int")
    kind = FingerprintKind(kind)
    if not key:
        raise FingerprintKeyError("the fingerprint key is empty")
    prefix = hmac.new(
        key,
        DOMAIN_TAG
        + _field(str(organization_id).encode("ascii"))
        + _field(kind.value.encode("ascii")),
        hashlib.sha256,
    )

    def one(value: str) -> bytes:
        if not isinstance(value, str):
            raise TypeError("value must be a str")
        state = prefix.copy()
        state.update(_field(value.encode("utf-8")))
        return state.digest()

    return one
