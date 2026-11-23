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

Between the password and the two-factor code (``/login/2fa``) there is no session yet,
only a signed state ``<user_id>.<timestamp>.<mac>`` (HMAC-SHA256 under ``SECRET_KEY`` over
the user id, the timestamp and the pre-auth cookie's nonce). It is valid for
``MFA_STATE_MAX_AGE_SECONDS`` and only together with that browser's pre-auth cookie.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import re
import secrets
import time

TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")  # 32 bytes, URL-safe base64, no padding
FORM_TOKEN_MAX_AGE_SECONDS = 60 * 60
MFA_STATE_MAX_AGE_SECONDS = 5 * 60
_CSRF_TAG = b"esim-csrf-v1"
_FORM_TAG = b"esim-form-v1"
_MFA_TAG = b"esim-mfa-state-v1"
_MFA_STATE = re.compile(r"([0-9]{1,19})\.([0-9]{1,12})\.([A-Za-z0-9_-]{43})")


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


class FormTokenStatus(enum.Enum):
    OK = "ok"
    # Signed by us for this purpose, but too old (or from the future), or its nonce is not
    # this browser's pre-auth cookie (the cookie expired, was replaced, or is missing).
    STALE = "stale"
    # Missing, malformed, not signed by us, or signed for another purpose.
    INVALID = "invalid"


def form_token_status(
    key: bytes,
    purpose: str,
    token: str | None,
    cookie_nonce: str | None,
    *,
    now: float | None = None,
    max_age: int = FORM_TOKEN_MAX_AGE_SECONDS,
) -> FormTokenStatus:
    """Why ``token`` is or is not acceptable; only ``OK`` is. ``STALE`` lets a page show the
    form again with a fresh token instead of an error; it never lets the request through."""
    if not token:
        return FormTokenStatus.INVALID
    parts = token.split(".")
    if len(parts) != 3:
        return FormTokenStatus.INVALID
    timestamp, nonce, mac = parts
    if not timestamp.isascii() or not timestamp.isdigit():
        return FormTokenStatus.INVALID
    try:
        expected = _form_mac(key, purpose, timestamp, nonce)
    except UnicodeEncodeError:  # lone surrogates
        return FormTokenStatus.INVALID
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return FormTokenStatus.INVALID
    if not cookie_nonce or not hmac.compare_digest(nonce.encode(), cookie_nonce.encode()):
        return FormTokenStatus.STALE
    age = (time.time() if now is None else now) - int(timestamp)
    return FormTokenStatus.OK if 0 <= age <= max_age else FormTokenStatus.STALE


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
    status = form_token_status(key, purpose, token, cookie_nonce, now=now, max_age=max_age)
    return status is FormTokenStatus.OK


def _mfa_mac(key: bytes, user_id: str, timestamp: str, nonce: str) -> str:
    message = b"|".join((_MFA_TAG, user_id.encode(), timestamp.encode(), nonce.encode()))
    return (
        base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def sign_mfa_state(key: bytes, user_id: int, nonce: str, *, now: float | None = None) -> str:
    """The pre-auth state for ``user_id`` after a correct password, tied to ``nonce``."""
    timestamp = str(int(time.time() if now is None else now))
    return f"{user_id}.{timestamp}.{_mfa_mac(key, str(user_id), timestamp, nonce)}"


def check_mfa_state(
    key: bytes,
    value: str | None,
    cookie_nonce: str | None,
    *,
    now: float | None = None,
    max_age: int = MFA_STATE_MAX_AGE_SECONDS,
) -> int | None:
    """The user id of a valid, fresh state signed for this browser's nonce, else ``None``."""
    if not value or not cookie_nonce:
        return None
    match = _MFA_STATE.fullmatch(value)
    if match is None:
        return None
    user_id, timestamp, mac = match.groups()
    try:
        expected = _mfa_mac(key, user_id, timestamp, cookie_nonce)
    except UnicodeEncodeError:  # lone surrogates in the cookie
        return None
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return None
    age = (time.time() if now is None else now) - int(timestamp)
    if not 0 <= age <= max_age:
        return None
    return int(user_id)
