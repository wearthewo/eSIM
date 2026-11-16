"""Organizations and users, managed from the web: superadmin pages and the owner's team page.

Every function takes an open SQLAlchemy ``Session`` and leaves committing to the
caller; ``now`` is timezone-aware UTC. Errors are ``AdminError`` with a message
meant for the web page.

Who may act on whom is decided in one place, ``scoped_user``:

- as superadmin (``admin=True``, the /admin pages): any user except oneself;
- as owner (``admin=False``, the /team page): only members of the owner's own
  organization. Owners (oneself included), superadmins and users of other
  organizations are out of scope. Routes answer out-of-scope IDs with 404.

Audit rows: ``user_id`` is the actor, ``target_user_id`` the user acted on.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import accounts
from app.auth.accounts import AdminError, CurrentUser, IpPrefix
from app.db.models import (
    AuditOutcome,
    Organization,
    Role,
    Usage,
    User,
    UserSession,
)

MAX_DAILY_ROW_QUOTA = 10_000_000

# audit_log.action values
INVITE_CREATED = "invite_created"
PASSWORD_LINK_CREATED = "password_link_created"  # noqa: S105  (an audit action name)
USER_DISABLED = "user_disabled"
USER_ENABLED = "user_enabled"
ORG_DISABLED = "org_disabled"
ORG_ENABLED = "org_enabled"
QUOTA_CHANGED = "quota_changed"

MSG_BAD_EMAIL = "Enter a valid email address."
MSG_OTHER_ACCOUNT = "This email belongs to another account and cannot be invited here."
MSG_HAS_ACCOUNT = "This person already has an account here. Create a reset link instead."
MSG_OWNER_ONLY_ADMIN = "Only an administrator can invite owners."
MSG_PENDING_OWNER = "This person was invited as an owner. Only an administrator can change that."
MSG_USER_DISABLED = "This user is disabled. Enable them first."
MSG_OWN_ORG = "You cannot disable your own organization."
MSG_ORG_DISABLED = "This organization is disabled. Enable it first."


@dataclass(frozen=True, slots=True)
class OrgSummary:
    id: int
    name: str
    user_count: int
    rows_today: int
    daily_row_quota: int
    disabled: bool


@dataclass(frozen=True, slots=True)
class Member:
    id: int
    email: str
    role: Role
    is_superadmin: bool
    disabled: bool
    has_password: bool
    last_login_at: dt.datetime | None


# --- Reading --------------------------------------------------------------------------


def list_orgs(db: Session, *, today: dt.date) -> list[OrgSummary]:
    """Every organization with its user count and the rows generated on ``today`` (UTC)."""
    counts = (
        select(User.organization_id, func.count(User.id).label("n"))
        .group_by(User.organization_id)
        .subquery()
    )
    query = (
        select(
            Organization,
            func.coalesce(counts.c.n, 0),
            func.coalesce(Usage.rows_generated, 0),
        )
        .outerjoin(counts, counts.c.organization_id == Organization.id)
        .outerjoin(Usage, (Usage.organization_id == Organization.id) & (Usage.day == today))
        .order_by(func.lower(Organization.name), Organization.id)
    )
    return [
        OrgSummary(org.id, org.name, users, rows, org.daily_row_quota, org.disabled)
        for org, users, rows in db.execute(query).all()
    ]


def get_org(db: Session, org_id: int) -> Organization | None:
    return db.get(Organization, org_id)


def members(db: Session, org_id: int) -> list[Member]:
    """The organization's users: owners first, then by email."""
    users = db.scalars(
        select(User)
        .where(User.organization_id == org_id)
        .order_by((User.role == Role.OWNER).desc(), func.lower(User.email))
    ).all()
    return [
        Member(
            u.id,
            u.email,
            u.role,
            u.is_superadmin,
            u.disabled,
            u.password_hash is not None,
            u.last_login_at,
        )
        for u in users
    ]


def scoped_user(db: Session, actor: CurrentUser, user_id: int, *, admin: bool) -> User | None:
    """The user ``actor`` may manage, else ``None`` (see the module docstring)."""
    user = db.get(User, user_id)
    if user is None or user.id == actor.user_id:
        return None
    if admin:
        return user if actor.is_superadmin else None
    if (
        actor.role == Role.OWNER
        and actor.organization_id is not None
        and user.organization_id == actor.organization_id
        and user.role == Role.MEMBER
        and not user.is_superadmin
    ):
        return user
    return None


# --- Users --------------------------------------------------------------------------------


def set_user_disabled(
    db: Session,
    actor: CurrentUser,
    user: User,
    disabled: bool,
    *,
    ip: IpPrefix | None,
    now: dt.datetime,
) -> None:
    """Disable (ending the user's sessions at once) or enable a user from ``scoped_user``."""
    user.disabled = disabled
    if disabled:
        accounts.delete_user_sessions(db, user.id)
    accounts.audit(
        db,
        USER_DISABLED if disabled else USER_ENABLED,
        AuditOutcome.SUCCESS,
        now=now,
        user_id=actor.user_id,
        target_user_id=user.id,
        organization_id=user.organization_id,
        ip=ip,
    )


def password_link(
    db: Session, actor: CurrentUser, user: User, *, ip: IpPrefix | None, now: dt.datetime
) -> str:
    """A one-time set-password link for a user from ``scoped_user``; returns the token."""
    if user.disabled:
        raise AdminError(MSG_USER_DISABLED)
    token = accounts.new_password_token(db, user.id, now)
    accounts.audit(
        db,
        PASSWORD_LINK_CREATED,
        AuditOutcome.SUCCESS,
        now=now,
        user_id=actor.user_id,
        target_user_id=user.id,
        organization_id=user.organization_id,
        ip=ip,
    )
    return token


def invite_user(
    db: Session,
    actor: CurrentUser,
    org_id: int,
    email: str,
    role: Role,
    *,
    admin: bool,
    ip: IpPrefix | None,
    now: dt.datetime,
) -> str:
    """Invite ``email`` to the organization; returns the invite token.

    Owners (``admin=False``) can invite members only, and cannot re-invite a
    pending owner (``accounts.invite`` would change that user's role).
    """
    if not admin and role != Role.MEMBER:
        raise AdminError(MSG_OWNER_ONLY_ADMIN)
    try:
        email = accounts.normalize_email(email)
    except AdminError:
        raise AdminError(MSG_BAD_EMAIL) from None
    org = db.get(Organization, org_id)
    if org is None:
        raise AdminError("No such organization.")
    if org.disabled:
        raise AdminError(MSG_ORG_DISABLED)
    existing = accounts.user_by_email(db, email)
    if existing is not None:
        if existing.organization_id != org_id or existing.is_superadmin:
            raise AdminError(MSG_OTHER_ACCOUNT)
        if existing.password_hash is not None:
            raise AdminError(MSG_HAS_ACCOUNT)
        if not admin and existing.role != Role.MEMBER:
            raise AdminError(MSG_PENDING_OWNER)
    token = accounts.invite(db, org_id, email, role, now=now, created_by=actor.user_id)
    invited = accounts.user_by_email(db, email)
    accounts.audit(
        db,
        INVITE_CREATED,
        AuditOutcome.SUCCESS,
        now=now,
        user_id=actor.user_id,
        target_user_id=invited.id if invited is not None else None,
        organization_id=org_id,
        ip=ip,
    )
    return token


# --- Organizations ------------------------------------------------------------------------


def set_org_disabled(
    db: Session,
    actor: CurrentUser,
    org_id: int,
    disabled: bool,
    *,
    ip: IpPrefix | None,
    now: dt.datetime,
) -> bool:
    """Disable (ending its users' sessions) or enable an organization. False if not found."""
    org = db.get(Organization, org_id)
    if org is None:
        return False
    if disabled and actor.organization_id == org_id:
        raise AdminError(MSG_OWN_ORG)
    org.disabled = disabled
    if disabled:
        org_users = select(User.id).where(User.organization_id == org_id)
        db.execute(
            delete(UserSession)
            .where(UserSession.user_id.in_(org_users))
            .execution_options(synchronize_session=False)
        )
    accounts.audit(
        db,
        ORG_DISABLED if disabled else ORG_ENABLED,
        AuditOutcome.SUCCESS,
        now=now,
        user_id=actor.user_id,
        organization_id=org_id,
        ip=ip,
    )
    return True


def parse_quota(raw: str) -> int:
    """A daily row quota from a form field. Raises ``AdminError``."""
    value = raw.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise AdminError(f"The quota must be a whole number from 0 to {MAX_DAILY_ROW_QUOTA:,}.")
    quota = int(value)
    if quota > MAX_DAILY_ROW_QUOTA:
        raise AdminError(f"The quota must be a whole number from 0 to {MAX_DAILY_ROW_QUOTA:,}.")
    return quota


def set_quota(
    db: Session,
    actor: CurrentUser,
    org_id: int,
    quota: int,
    *,
    ip: IpPrefix | None,
    now: dt.datetime,
) -> bool:
    """Change the organization's daily row quota. False if not found."""
    org = db.get(Organization, org_id)
    if org is None:
        return False
    org.daily_row_quota = quota
    accounts.audit(
        db,
        QUOTA_CHANGED,
        AuditOutcome.SUCCESS,
        now=now,
        user_id=actor.user_id,
        organization_id=org_id,
        ip=ip,
    )
    return True
