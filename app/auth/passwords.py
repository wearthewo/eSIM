"""Password policy and argon2id hashing (argon2-cffi library defaults).

Each hash or verify uses about 64 MiB with the defaults, so at most
``MAX_CONCURRENT_HASHES`` run at once per process: a burst of login attempts
waits instead of exhausting memory. Passwords are never logged or put in
exception messages.
"""

from __future__ import annotations

import secrets
import threading
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.numbers import thousands

MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 128
MAX_CONCURRENT_HASHES = 2

MSG_TOO_SHORT = f"The password must be at least {thousands(MIN_PASSWORD_CHARS)} characters."
MSG_TOO_LONG = f"The password can be at most {thousands(MAX_PASSWORD_CHARS)} characters."
MSG_IS_EMAIL = "The password must not be your email address."

_hasher = PasswordHasher()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)


def password_problem(password: str, email: str) -> str | None:
    """A message to show if ``password`` breaks the policy, else ``None``."""
    if len(password) < MIN_PASSWORD_CHARS:
        return MSG_TOO_SHORT
    if len(password) > MAX_PASSWORD_CHARS:
        return MSG_TOO_LONG
    if password.strip().casefold() == email.strip().casefold():
        return MSG_IS_EMAIL
    return None


def hash_password(password: str) -> str:
    with _slots:
        return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, bool]:
    """``(matches, needs_rehash)``. ``needs_rehash`` is only ``True`` for a match whose
    hash was made with other parameters than the current defaults."""
    with _slots:
        try:
            _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False, False
        return True, _hasher.check_needs_rehash(password_hash)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


def verify_dummy(password: str) -> None:
    """Spend the time of a real verify, for unknown users (equal login timing)."""
    verify_password(_dummy_hash(), password)
