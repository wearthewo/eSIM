"""Accounts in the database: login with rate limits, sessions, one-time links, admin tasks.

Every function takes an open SQLAlchemy ``Session`` and leaves committing to the
caller. Times are passed in as ``now`` (timezone-aware UTC) so that the expiry
rules are easy to test.

Rules:

- Login failures are counted from ``audit_log`` over ``FAILURE_WINDOW``: at most
  ``ACCOUNT_MAX_FAILURES`` per account (by user_id) and ``IP_MAX_FAILURES`` per IP
  prefix. Past either limit every attempt fails, even with the right password, and
  is logged as ``login_blocked`` (which does not count, so the lock ends when the
  window passes).
- Audit rows hold user and organization IDs, the action, the outcome and the IP
  prefix. Never an email address, password or token.
- Sessions: 7 days absolute, 12 hours idle; ``last_seen_at`` is written at most
  once per ``LAST_SEEN_RESOLUTION``.
- Links (invites, password tokens) are single use and expire after ``LINK_TTL``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, verify_dummy, verify_password
from app.auth.tokens import csrf_for, digest, is_token_shaped, new_token
from app.db.models import (
    MAX_EMAIL_CHARS,
    MAX_NAME_CHARS,
    AuditLog,
    AuditOutcome,
    Invite,
    Organization,
    PasswordToken,
    Role,
    User,
    UserSession,
)

IpPrefix = IPv4Network | IPv6Network

ACCOUNT_MAX_FAILURES = 5
IP_MAX_FAILURES = 20
FAILURE_WINDOW = dt.timedelta(minutes=15)

SESSION_ABSOLUTE_TTL = dt.timedelta(days=7)
SESSION_IDLE_TTL = dt.timedelta(hours=12)
LAST_SEEN_RESOLUTION = dt.timedelta(minutes=5)

LINK_TTL = dt.timedelta(hours=48)

# audit_log.action values
LOGIN = "login"
LOGIN_BLOCKED = "login_blocked"
LOGOUT = "logout"
INVITE_ACCEPTED = "invite_accepted"
PASSWORD_SET = "password_set"  # noqa: S105  (an audit action name, not a password)


class AdminError(Exception):
    """An admin task cannot be done. The message is safe to print (no secrets)."""


# --- Helpers ---------------------------------------------------------------------


def normalize_email(raw: str) -> str:
    """Stripped and lowercased, with a basic shape check. Raises ``AdminError``."""
    email = raw.strip().lower()
    local, at, domain = email.partition("@")
    if (
        not at
        or not local
        or "@" in domain
        or "." not in domain.strip(".")
        or len(email) > MAX_EMAIL_CHARS
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in email)
    ):
        raise AdminError("not a valid email address")
    return email


def user_by_email(db: Session, email: str) -> User | None:
    return db.scalars(select(User).where(func.lower(User.email) == email.strip().lower())).first()


def audit(
    db: Session,
    action: str,
    outcome: AuditOutcome,
    *,
    now: dt.datetime,
    user_id: int | None = None,
    organization_id: int | None = None,
    ip: IpPrefix | None = None,
    target_user_id: int | None = None,
    target_batch_id: int | None = None,
) -> None:
    """Add an audit row. ``user_id`` is who acted, ``target_user_id`` who it was done to,
    ``target_batch_id`` the batch it was done to."""
    db.add(
        AuditLog(
            created_at=now,
            user_id=user_id,
            target_user_id=target_user_id,
            target_batch_id=target_batch_id,
            organization_id=organization_id,
            action=action,
            outcome=outcome,
            ip_prefix=ip,
        )
    )


def _organization_ok(db: Session, user: User) -> bool:
    """The user's organization is enabled, or the user is a superadmin with none."""
    if user.organization_id is None:
        return user.is_superadmin
    org = db.get(Organization, user.organization_id)
    return org is not None and not org.disabled


def _recent_failures(db: Session, since: dt.datetime, *conditions: ColumnElement[bool]) -> int:
    query = select(func.count(AuditLog.id)).where(
        AuditLog.action == LOGIN,
        AuditLog.outcome == AuditOutcome.FAILURE,
        AuditLog.created_at > since,
        *conditions,
    )
    return db.scalar(query) or 0


# --- Login -----------------------------------------------------------------------


def attempt_login(
    db: Session, email: str, password: str, *, ip: IpPrefix | None, now: dt.datetime
) -> User | None:
    """The user if ``email`` and ``password`` are right and login is allowed, else ``None``.

    Unknown emails, wrong passwords, disabled users or organizations, users without
    a password yet and rate-limited attempts all return ``None`` after the same
    work (one argon2 verify), so callers can answer them identically.
    """
    user = user_by_email(db, email) if len(email) <= MAX_EMAIL_CHARS else None

    since = now - FAILURE_WINDOW
    locked = False
    if user is not None:
        locked = _recent_failures(db, since, AuditLog.user_id == user.id) >= ACCOUNT_MAX_FAILURES
    if not locked and ip is not None:
        locked = _recent_failures(db, since, AuditLog.ip_prefix == ip) >= IP_MAX_FAILURES

    matches = needs_rehash = False
    if user is not None and user.password_hash is not None:
        matches, needs_rehash = verify_password(user.password_hash, password)
    else:
        verify_dummy(password)

    who = {
        "now": now,
        "user_id": user.id if user is not None else None,
        "organization_id": user.organization_id if user is not None else None,
        "ip": ip,
    }
    if locked:
        audit(db, LOGIN_BLOCKED, AuditOutcome.FAILURE, **who)
        return None
    if user is None or not matches or user.disabled or not _organization_ok(db, user):
        audit(db, LOGIN, AuditOutcome.FAILURE, **who)
        return None

    if needs_rehash:
        user.password_hash = hash_password(password)
    user.last_login_at = now
    audit(db, LOGIN, AuditOutcome.SUCCESS, **who)
    return user


# --- Sessions ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The logged-in user of a request."""

    user_id: int
    email: str
    organization_id: int | None
    role: Role
    is_superadmin: bool
    session_id: int
    csrf_token_hash: bytes


def create_session(db: Session, user_id: int, *, now: dt.datetime) -> str:
    """Start a new session for ``user_id``; returns the raw token (for the cookie only).

    Also removes the user's sessions that have already expired.
    """
    db.execute(
        delete(UserSession).where(
            UserSession.user_id == user_id,
            (UserSession.expires_at <= now) | (UserSession.last_seen_at <= now - SESSION_IDLE_TTL),
        )
    )
    token = new_token()
    db.add(
        UserSession(
            token_hash=digest(token),
            user_id=user_id,
            csrf_token_hash=digest(csrf_for(token)),
            created_at=now,
            expires_at=now + SESSION_ABSOLUTE_TTL,
            last_seen_at=now,
        )
    )
    return token


def resolve_session(db: Session, token: str | None, *, now: dt.datetime) -> CurrentUser | None:
    """The user of a valid session, else ``None``. Expired sessions are deleted."""
    if not token or not is_token_shaped(token):
        return None
    row = db.execute(
        select(UserSession, User, Organization)
        .join(User, UserSession.user_id == User.id)
        .outerjoin(Organization, User.organization_id == Organization.id)
        .where(UserSession.token_hash == digest(token))
    ).first()
    if row is None:
        return None
    session, user, org = row
    if session.expires_at <= now or session.last_seen_at <= now - SESSION_IDLE_TTL:
        db.delete(session)
        return None
    if user.disabled or user.password_hash is None:
        return None
    if user.organization_id is None:
        if not user.is_superadmin:
            return None
    elif org is None or org.disabled:
        return None
    if now - session.last_seen_at >= LAST_SEEN_RESOLUTION:
        session.last_seen_at = now
    return CurrentUser(
        user_id=user.id,
        email=user.email,
        organization_id=user.organization_id,
        role=user.role,
        is_superadmin=user.is_superadmin,
        session_id=session.id,
        csrf_token_hash=session.csrf_token_hash,
    )


def delete_session(db: Session, token: str | None) -> None:
    if token and is_token_shaped(token):
        db.execute(delete(UserSession).where(UserSession.token_hash == digest(token)))


def delete_user_sessions(db: Session, user_id: int) -> None:
    db.execute(delete(UserSession).where(UserSession.user_id == user_id))


# --- One-time links ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """Whose password a valid link sets."""

    is_invite: bool
    user_id: int
    email: str
    organization_id: int | None


def _usable_invite(db: Session, token_hash: bytes, now: dt.datetime) -> LinkTarget | None:
    invite = db.scalars(
        select(Invite).where(
            Invite.token_hash == token_hash, Invite.used_at.is_(None), Invite.expires_at > now
        )
    ).first()
    if invite is None:
        return None
    user = user_by_email(db, invite.email)
    if (
        user is None
        or user.organization_id != invite.organization_id
        or user.password_hash is not None
        or user.disabled
        or not _organization_ok(db, user)
    ):
        return None
    return LinkTarget(True, user.id, user.email, user.organization_id)


def _usable_password_token(db: Session, token_hash: bytes, now: dt.datetime) -> LinkTarget | None:
    row = db.execute(
        select(PasswordToken, User)
        .join(User, PasswordToken.user_id == User.id)
        .where(
            PasswordToken.token_hash == token_hash,
            PasswordToken.used_at.is_(None),
            PasswordToken.expires_at > now,
        )
    ).first()
    if row is None:
        return None
    _, user = row
    if user.disabled or not _organization_ok(db, user):
        return None
    return LinkTarget(False, user.id, user.email, user.organization_id)


def find_link(db: Session, token: str, *, now: dt.datetime) -> LinkTarget | None:
    """The target of an unused, unexpired invite or password link, else ``None``."""
    if not is_token_shaped(token):
        return None
    token_hash = digest(token)
    return _usable_invite(db, token_hash, now) or _usable_password_token(db, token_hash, now)


def consume_link(
    db: Session, token: str, password_hash: str, *, ip: IpPrefix | None, now: dt.datetime
) -> LinkTarget | None:
    """Use the link once: set the password, end the user's other sessions, audit.

    Returns the target, or ``None`` if the link is invalid, used or expired (also
    when another request used it first: the ``UPDATE`` only matches unused links).
    """
    target = find_link(db, token, now=now)
    if target is None:
        return None
    token_hash = digest(token)
    table = Invite if target.is_invite else PasswordToken
    used = db.execute(
        update(table)
        .where(table.token_hash == token_hash, table.used_at.is_(None), table.expires_at > now)
        .values(used_at=now)
        .returning(table.id)
    ).first()
    if used is None:
        return None
    user = db.get(User, target.user_id)
    if user is None:  # found by find_link in this transaction, so not expected
        return None
    user.password_hash = password_hash
    user.last_login_at = now
    delete_user_sessions(db, user.id)
    kw = {"now": now, "user_id": user.id, "organization_id": user.organization_id, "ip": ip}
    if target.is_invite:
        audit(db, INVITE_ACCEPTED, AuditOutcome.SUCCESS, **kw)
    audit(db, PASSWORD_SET, AuditOutcome.SUCCESS, **kw)
    return target


# --- Admin tasks (python -m app.admin) -------------------------------------------------


def create_org(db: Session, name: str) -> int:
    name = name.strip()
    if not name or len(name) > MAX_NAME_CHARS:
        raise AdminError(f"the organization name must be 1 to {MAX_NAME_CHARS} characters")
    org = Organization(name=name)
    db.add(org)
    db.flush()
    return org.id


def new_password_token(db: Session, user_id: int, now: dt.datetime) -> str:
    """A new set-password link for the user; earlier unused ones stop working."""
    db.execute(
        update(PasswordToken)
        .where(PasswordToken.user_id == user_id, PasswordToken.used_at.is_(None))
        .values(used_at=now)
    )
    token = new_token()
    db.add(PasswordToken(token_hash=digest(token), user_id=user_id, expires_at=now + LINK_TTL))
    return token


def create_superadmin(db: Session, email: str, *, now: dt.datetime) -> str:
    """A superadmin without organization; returns the token of a set-password link."""
    email = normalize_email(email)
    if user_by_email(db, email) is not None:
        raise AdminError("a user with this email already exists (use reset-link)")
    user = User(email=email, is_superadmin=True, organization_id=None, role=Role.MEMBER)
    db.add(user)
    db.flush()
    return new_password_token(db, user.id, now)


def invite(
    db: Session,
    organization_id: int,
    email: str,
    role: Role,
    *,
    now: dt.datetime,
    created_by: int | None = None,
) -> str:
    """Create the user (no password yet) and an invite; returns the invite token.

    Inviting the same email again (still without a password, same organization)
    replaces the earlier invite and updates the role.
    """
    email = normalize_email(email)
    org = db.get(Organization, organization_id)
    if org is None:
        raise AdminError("no organization with this id")
    if org.disabled:
        raise AdminError("the organization is disabled")
    user = user_by_email(db, email)
    if user is None:
        user = User(email=email, organization_id=organization_id, role=role)
        db.add(user)
    elif user.password_hash is not None:
        raise AdminError("this user already has a password (use reset-link)")
    elif user.organization_id != organization_id or user.is_superadmin:
        raise AdminError("this email belongs to another account")
    else:
        user.role = role
    db.execute(
        update(Invite)
        .where(func.lower(Invite.email) == email, Invite.used_at.is_(None))
        .values(used_at=now)
    )
    token = new_token()
    db.add(
        Invite(
            token_hash=digest(token),
            email=email,
            organization_id=organization_id,
            role=role,
            expires_at=now + LINK_TTL,
            created_by=created_by,
        )
    )
    db.flush()
    return token


def reset_link(db: Session, email: str, *, now: dt.datetime) -> str:
    """A set-password link for an existing, enabled user; returns the token."""
    user = user_by_email(db, email)
    if user is None:
        raise AdminError("no user with this email")
    if user.disabled:
        raise AdminError("the user is disabled (enable-user first)")
    return new_password_token(db, user.id, now)


def set_user_disabled(db: Session, email: str, disabled: bool) -> None:
    """Disable (and log out) or enable a user."""
    user = user_by_email(db, email)
    if user is None:
        raise AdminError("no user with this email")
    user.disabled = disabled
    if disabled:
        delete_user_sessions(db, user.id)
