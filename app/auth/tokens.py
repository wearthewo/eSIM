"""Random tokens, their digests, and signed form tokens.

Session tokens and link tokens are 32 random bytes, URL-safe base64 (43 chars).
Only ``digest(token)`` (SHA-256) is stored: the tokens are random, so a plain
hash is enough (unlike activation codes, which need a keyed HMAC).

The CSRF token of a session is derived from the session token, so the server can
render it on every page while storing only its hash.

Pages without a session (login, set password) use a signed form token instead:
``<timestamp>.<nonce>.<mac>`` with an HMAC-SHA256 under ``SECRET_KEY`` over a
purpose, the timestamp and a nonce. The nonce is also kept in a SameSite=Strict
cookie and must match it, so a token fetched by another site is useless.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")  # 32 bytes, URL-safe base64, no padding
FORM_TOKEN_MAX_AGE_SECONDS = 60 * 60
_CSRF_TAG = b"esim-csrf-v1"
_FORM_TAG = b"esim-form-v1"


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def is_token_shaped(value: str) -> bool:
    return TOKEN_PATTERN.fullmatch(value) is not None


def digest(token: str) -> bytes:
    """SHA-256 of a token (32 bytes): what the database stores."""
    return hashlib.sha256(token.encode("ascii", errors="replace")).digest()


def csrf_for(session_token: str) -> str:
    """The CSRF token for a session (stored as ``digest(csrf_for(token))``)."""
    mac = hmac.new(session_token.encode("ascii", errors="replace"), _CSRF_TAG, hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode("ascii")


def csrf_matches(submitted: str | None, stored_hash: bytes) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(digest(submitted), stored_hash)


def _form_mac(key: bytes, purpose: str, timestamp: str, nonce: str) -> str:
    message = b"|".join((_FORM_TAG, purpose.encode(), timestamp.encode(), nonce.encode()))
    return (
        base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def sign_form_token(key: bytes, purpose: str, nonce: str, *, now: float | None = None) -> str:
    timestamp = str(int(time.time() if now is None else now))
    return f"{timestamp}.{nonce}.{_form_mac(key, purpose, timestamp, nonce)}"


def check_form_token(
    key: bytes,
    purpose: str,
    token: str | None,
    cookie_nonce: str | None,
    *,
    now: float | None = None,
    max_age: int = FORM_TOKEN_MAX_AGE_SECONDS,
) -> bool:
    """True if ``token`` was signed for ``purpose``, is fresh, and carries the cookie's nonce."""
    if not token or not cookie_nonce:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    timestamp, nonce, mac = parts
    if not timestamp.isascii() or not timestamp.isdigit():
        return False
    try:
        expected = _form_mac(key, purpose, timestamp, nonce)
    except UnicodeEncodeError:  # lone surrogates
        return False
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return False
    if not hmac.compare_digest(nonce.encode(), cookie_nonce.encode()):
        return False
    age = (time.time() if now is None else now) - int(timestamp)
    return 0 <= age <= max_age
