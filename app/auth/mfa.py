"""Two-factor authentication: TOTP codes from an authenticator app, plus recovery codes.

TOTP is RFC 6238 with the defaults every authenticator app uses: HMAC-SHA1, 6 digits,
30-second steps. A code is accepted for the current step and the previous one only, and
only if that step is later than the last accepted one (``users.mfa_last_step``), so a
code cannot be used twice and no code older than a used one works.

Storage (never plaintext, never logged, never shown again after enrolment):

- the TOTP secret is AES-256-GCM encrypted (``0x01 | nonce | ciphertext+tag``) with a
  subkey of ``MFA_KEY``; the user id is the associated data, so a value copied to another
  user's row does not decrypt;
- the 10 recovery codes are kept only as HMAC-SHA256 under another subkey of ``MFA_KEY``;
  each works once.

Wrong codes are counted from ``audit_log`` like wrong passwords (``app.auth.accounts``):
past ``ACCOUNT_MAX_FAILURES`` in ``FAILURE_WINDOW`` every code is refused, even the right
one, as ``mfa_blocked`` (which does not count, so the lock ends when the window passes).

Every function that takes a ``Session`` leaves committing to the caller. This module
imports nothing from ``app.web``.
"""

from __future__ import annotations

import base64
import datetime as dt
import enum
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.auth import accounts
from app.auth.accounts import ACCOUNT_MAX_FAILURES, FAILURE_WINDOW, AdminError, IpPrefix
from app.db.models import AuditLog, AuditOutcome, MfaRecoveryCode, User

STEP_SECONDS = 30
DIGITS = 6
SECRET_BYTES = 20  # 160 bits, as RFC 4226 recommends for HMAC-SHA1
ISSUER = "eSIM PDF generator"
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_CHARS = 10  # 50 bits each
# Crockford's base32: no I, L, O or U. On input O reads as 0 and I, L as 1.
RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RECOVERY_INPUT = str.maketrans({"O": "0", "I": "1", "L": "1"})
MAX_CODE_CHARS = 32  # longer input is never a code

_SECRET_INFO = b"esim-mfa-secret-v1"
_RECOVERY_INFO = b"esim-mfa-recovery-v1"
_BLOB_VERSION = b"\x01"
_NONCE_BYTES = 12
_TAG_BYTES = 16

# audit_log.action values
MFA_ENABLED = "mfa_enabled"
MFA_DISABLED = "mfa_disabled"
MFA_LOGIN = "mfa_login"
MFA_BLOCKED = "mfa_blocked"
MFA_RECOVERY_USED = "mfa_recovery_used"
MFA_RESET = "mfa_reset"
MFA_RECOVERY_REGENERATED = "mfa_recovery_regenerated"
# Wrong codes of these actions count towards the lock.
_COUNTED = (MFA_LOGIN, MFA_DISABLED, MFA_RECOVERY_REGENERATED)


class SecretUnreadable(Exception):
    """A stored secret does not decrypt: tampered, another user's, or another MFA_KEY."""


@dataclass(frozen=True, slots=True, repr=False)
class MfaKeys:
    """The subkeys derived from ``MFA_KEY``. Never shown by ``repr`` and never pickled."""

    secret_key: bytes
    recovery_key: bytes

    def __repr__(self) -> str:
        return "MfaKeys(***)"

    def __reduce__(self) -> object:
        raise TypeError("MfaKeys cannot be pickled")


def _subkey(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


def keys_from(mfa_key: str) -> MfaKeys:
    master = mfa_key.encode("utf-8")
    return MfaKeys(_subkey(master, _SECRET_INFO), _subkey(master, _RECOVERY_INFO))


# --- TOTP ----------------------------------------------------------------------------------


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def _base32(secret: bytes) -> str:
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def text_key(secret: bytes) -> str:
    """The secret for typing into an app: base32 in groups of four."""
    key = _base32(secret)
    return " ".join(key[i : i + 4] for i in range(0, len(key), 4))


def otpauth_uri(email: str, secret: bytes) -> str:
    """The ``otpauth://totp/...`` URI the QR code holds."""
    label = quote(ISSUER, safe="") + ":" + quote(email, safe="@")
    query = urlencode(
        {
            "secret": _base32(secret),
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": str(DIGITS),
            "period": str(STEP_SECONDS),
        },
        quote_via=quote,
    )
    return f"otpauth://totp/{label}?{query}"


def step_of(now: dt.datetime) -> int:
    return int(now.timestamp()) // STEP_SECONDS


def code_at(secret: bytes, step: int) -> str:
    """The code for a 30-second step (RFC 4226 HOTP with the step as counter)."""
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**DIGITS:0{DIGITS}d}"


def _clean_code(code: str) -> str | None:
    """Six ASCII digits (spaces ignored), else ``None``."""
    if len(code) > MAX_CODE_CHARS:
        return None
    digits = "".join(code.split())
    if len(digits) != DIGITS or not digits.isascii() or not digits.isdigit():
        return None
    return digits


def check_code(secret: bytes, code: str, now: dt.datetime, last_step: int | None) -> int | None:
    """The step ``code`` belongs to, if it is the current or the previous one and later
    than ``last_step``; else ``None``."""
    digits = _clean_code(code)
    if digits is None:
        return None
    current = step_of(now)
    for candidate in (current, current - 1):
        if last_step is not None and candidate <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), digits):
            return candidate
    return None


# --- The stored secret ---------------------------------------------------------------------


def _aad(user_id: int) -> bytes:
    return b"esim-mfa-secret|" + str(user_id).encode("ascii")


def encrypt_secret(keys: MfaKeys, user_id: int, secret: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_BYTES)
    return _BLOB_VERSION + nonce + AESGCM(keys.secret_key).encrypt(nonce, secret, _aad(user_id))


def decrypt_secret(keys: MfaKeys, user_id: int, blob: bytes) -> bytes:
    """Raises ``SecretUnreadable``."""
    if len(blob) < 1 + _NONCE_BYTES + _TAG_BYTES or blob[:1] != _BLOB_VERSION:
        raise SecretUnreadable
    nonce, sealed = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
    try:
        return AESGCM(keys.secret_key).decrypt(nonce, sealed, _aad(user_id))
    except InvalidTag:
        raise SecretUnreadable from None


# --- Recovery codes ------------------------------------------------------------------------


def new_recovery_codes() -> list[str]:
    """``RECOVERY_CODE_COUNT`` codes shaped ``ABCDE-12345``."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_CHARS))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str | None:
    """Upper case without spaces and dashes (O read as 0, I and L as 1); ``None`` if it
    cannot be a recovery code."""
    if len(code) > MAX_CODE_CHARS:
        return None
    text = "".join(code.split()).replace("-", "").upper().translate(_RECOVERY_INPUT)
    if len(text) != RECOVERY_CODE_CHARS or any(ch not in RECOVERY_ALPHABET for ch in text):
        return None
    return text


def recovery_hash(keys: MfaKeys, user_id: int, normalized: str) -> bytes:
    message = f"{user_id}|{normalized}".encode("ascii")
    return hmac.new(keys.recovery_key, message, hashlib.sha256).digest()


# --- Accounts ------------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    OK = "ok"  # a valid TOTP code
    RECOVERY = "recovery"  # a valid, unused recovery code (now used)
    WRONG = "wrong"
    BLOCKED = "blocked"  # too many wrong codes recently
    FORBIDDEN = "forbidden"  # superadmins cannot switch 2FA off
    UNAVAILABLE = "unavailable"  # no such user, or 2FA is off


@dataclass(frozen=True, slots=True)
class MfaStatus:
    enabled: bool
    enabled_at: dt.datetime | None
    recovery_codes_left: int


def _locked_user(db: Session, user_id: int) -> User | None:
    """The user row, locked until the transaction ends (two requests never both use a code)."""
    return db.scalars(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _who(user: User, now: dt.datetime, ip: IpPrefix | None) -> dict:
    return {"now": now, "user_id": user.id, "organization_id": user.organization_id, "ip": ip}


def is_locked(db: Session, user_id: int, *, now: dt.datetime) -> bool:
    """Too many wrong codes for the account in the last ``FAILURE_WINDOW``."""
    failures = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.action.in_(_COUNTED),
            AuditLog.outcome == AuditOutcome.FAILURE,
            AuditLog.created_at > now - FAILURE_WINDOW,
            AuditLog.user_id == user_id,
        )
    )
    return (failures or 0) >= ACCOUNT_MAX_FAILURES


def status(db: Session, user_id: int) -> MfaStatus:
    user = db.get(User, user_id)
    if user is None or user.mfa_enabled_at is None:
        return MfaStatus(False, None, 0)
    left = db.scalar(
        select(func.count(MfaRecoveryCode.id)).where(
            MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None)
        )
    )
    return MfaStatus(True, user.mfa_enabled_at, left or 0)


def start_enrolment(db: Session, user_id: int, keys: MfaKeys) -> bytes:
    """The secret to show for enrolment: the pending one, else a new one (stored encrypted).

    Raises ``AdminError`` if 2FA is already on.
    """
    user = _locked_user(db, user_id)
    if user is None:
        raise AdminError("no such user")
    if user.mfa_enabled_at is not None:
        raise AdminError("two-factor authentication is already on")
    if user.mfa_secret is not None:
        try:
            return decrypt_secret(keys, user.id, user.mfa_secret)
        except SecretUnreadable:
            pass  # e.g. MFA_KEY changed: start over with a new secret
    secret = new_secret()
    user.mfa_secret = encrypt_secret(keys, user.id, secret)
    user.mfa_last_step = None
    return secret


def _replace_recovery_codes(
    db: Session, user_id: int, keys: MfaKeys, now: dt.datetime
) -> list[str]:
    db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))
    codes = new_recovery_codes()
    db.add_all(
        MfaRecoveryCode(
            user_id=user_id,
            code_hash=recovery_hash(keys, user_id, normalize_recovery_code(code) or ""),
            created_at=now,
        )
        for code in codes
    )
    return codes


def confirm_enrolment(
    db: Session,
    user_id: int,
    code: str,
    keys: MfaKeys,
    *,
    now: dt.datetime,
    ip: IpPrefix | None,
) -> list[str] | None:
    """Switch 2FA on if ``code`` matches the pending secret; returns the new recovery codes
    (to show once), or ``None`` for a wrong code (audited as an ``mfa_enabled`` failure)."""
    user = _locked_user(db, user_id)
    if user is None or user.mfa_enabled_at is not None or user.mfa_secret is None:
        return None
    try:
        secret = decrypt_secret(keys, user.id, user.mfa_secret)
    except SecretUnreadable:
        return None
    matched = check_code(secret, code, now, user.mfa_last_step)
    if matched is None:
        accounts.audit(db, MFA_ENABLED, AuditOutcome.FAILURE, **_who(user, now, ip))
        return None
    user.mfa_enabled_at = now
    user.mfa_last_step = matched
    codes = _replace_recovery_codes(db, user.id, keys, now)
    accounts.audit(db, MFA_ENABLED, AuditOutcome.SUCCESS, **_who(user, now, ip))
    return codes


def _use_recovery_code(db: Session, user: User, code: str, keys: MfaKeys, now: dt.datetime) -> bool:
    normalized = normalize_recovery_code(code)
    if normalized is None:
        return False
    used = db.execute(
        update(MfaRecoveryCode)
        .where(
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.code_hash == recovery_hash(keys, user.id, normalized),
            MfaRecoveryCode.used_at.is_(None),
        )
        .values(used_at=now)
        .returning(MfaRecoveryCode.id)
    ).first()
    return used is not None


def _second_factor(db: Session, user: User, code: str, keys: MfaKeys, now: dt.datetime) -> Verdict:
    """Check a TOTP code (recording its step) or else use a recovery code."""
    try:
        secret = decrypt_secret(keys, user.id, user.mfa_secret or b"")
    except SecretUnreadable:
        secret = None  # only recovery codes can still work (or reset-2fa)
    if secret is not None:
        matched = check_code(secret, code, now, user.mfa_last_step)
        if matched is not None:
            user.mfa_last_step = matched
            return Verdict.OK
    if _use_recovery_code(db, user, code, keys, now):
        return Verdict.RECOVERY
    return Verdict.WRONG


def verify_login(
    db: Session,
    user_id: int,
    code: str,
    keys: MfaKeys,
    *,
    now: dt.datetime,
    ip: IpPrefix | None,
) -> Verdict:
    """The second login step: ``OK``/``RECOVERY`` (log in), ``WRONG``, ``BLOCKED`` or
    ``UNAVAILABLE`` (the user is gone, disabled, or has 2FA off: start over)."""
    user = _locked_user(db, user_id)
    if user is None or user.mfa_enabled_at is None or not accounts.login_allowed(db, user):
        return Verdict.UNAVAILABLE
    who = _who(user, now, ip)
    if is_locked(db, user.id, now=now):
        accounts.audit(db, MFA_BLOCKED, AuditOutcome.FAILURE, **who)
        return Verdict.BLOCKED
    verdict = _second_factor(db, user, code, keys, now)
    if verdict == Verdict.WRONG:
        accounts.audit(db, MFA_LOGIN, AuditOutcome.FAILURE, **who)
        return verdict
    if verdict == Verdict.RECOVERY:
        accounts.audit(db, MFA_RECOVERY_USED, AuditOutcome.SUCCESS, **who)
    accounts.audit(db, MFA_LOGIN, AuditOutcome.SUCCESS, **who)
    user.last_login_at = now
    return verdict


def regenerate_recovery_codes(
    db: Session,
    user_id: int,
    code: str,
    keys: MfaKeys,
    *,
    now: dt.datetime,
    ip: IpPrefix | None,
) -> tuple[Verdict, list[str] | None]:
    """New recovery codes for a user with 2FA on, after a current TOTP code (a recovery
    code is not enough). Every old code stops working; the new ones are returned to show
    once. ``(OK, codes)``, else ``(WRONG | BLOCKED | UNAVAILABLE, None)``.

    Audited as ``mfa_recovery_regenerated``; wrong codes count towards the lock.
    """
    user = _locked_user(db, user_id)
    if user is None or user.mfa_enabled_at is None:
        return Verdict.UNAVAILABLE, None
    who = _who(user, now, ip)
    if is_locked(db, user.id, now=now):
        accounts.audit(db, MFA_BLOCKED, AuditOutcome.FAILURE, **who)
        return Verdict.BLOCKED, None
    try:
        secret: bytes | None = decrypt_secret(keys, user.id, user.mfa_secret or b"")
    except SecretUnreadable:
        secret = None
    matched = check_code(secret, code, now, user.mfa_last_step) if secret is not None else None
    if matched is None:
        accounts.audit(db, MFA_RECOVERY_REGENERATED, AuditOutcome.FAILURE, **who)
        return Verdict.WRONG, None
    user.mfa_last_step = matched
    codes = _replace_recovery_codes(db, user.id, keys, now)
    accounts.audit(db, MFA_RECOVERY_REGENERATED, AuditOutcome.SUCCESS, **who)
    return Verdict.OK, codes


def _switch_off(db: Session, user: User) -> None:
    user.mfa_secret = None
    user.mfa_enabled_at = None
    user.mfa_last_step = None
    db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))


def disable(
    db: Session,
    user_id: int,
    code: str,
    keys: MfaKeys,
    *,
    now: dt.datetime,
    ip: IpPrefix | None,
) -> Verdict:
    """Switch 2FA off with a current code (or a recovery code). Superadmins: ``FORBIDDEN``.

    Wrong codes count towards the same lock as the login step.
    """
    user = _locked_user(db, user_id)
    if user is None or user.mfa_enabled_at is None:
        return Verdict.UNAVAILABLE
    if user.is_superadmin:
        return Verdict.FORBIDDEN
    who = _who(user, now, ip)
    if is_locked(db, user.id, now=now):
        accounts.audit(db, MFA_BLOCKED, AuditOutcome.FAILURE, **who)
        return Verdict.BLOCKED
    verdict = _second_factor(db, user, code, keys, now)
    if verdict == Verdict.WRONG:
        accounts.audit(db, MFA_DISABLED, AuditOutcome.FAILURE, **who)
        return verdict
    if verdict == Verdict.RECOVERY:
        accounts.audit(db, MFA_RECOVERY_USED, AuditOutcome.SUCCESS, **who)
    _switch_off(db, user)
    accounts.audit(db, MFA_DISABLED, AuditOutcome.SUCCESS, **who)
    return verdict


def reset(db: Session, email: str, *, now: dt.datetime) -> None:
    """Break-glass (``python -m app.admin reset-2fa``): switch 2FA off for the user and end
    their sessions. A superadmin must enrol again at the next login. Raises ``AdminError``."""
    found = accounts.user_by_email(db, email)
    if found is None:
        raise AdminError("no user with this email")
    user = _locked_user(db, found.id)
    if user is None:  # deleted meanwhile
        raise AdminError("no user with this email")
    _switch_off(db, user)
    accounts.delete_user_sessions(db, user.id)
    accounts.audit(
        db,
        MFA_RESET,
        AuditOutcome.SUCCESS,
        now=now,
        organization_id=user.organization_id,
        target_user_id=user.id,
    )
