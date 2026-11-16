"""Access requests: the public "Request access" form and the superadmin's answers.

Every function takes an open SQLAlchemy ``Session`` and leaves committing to the
caller; ``now`` is timezone-aware UTC.

Rules:

- ``submit_request`` never tells the caller why a request was not stored, so the
  web page can answer every submission the same way (no account enumeration).
- At most ``RATE_LIMIT`` stored requests per IP prefix per ``RATE_WINDOW``, counted
  from the ``access_request_created`` audit rows (they carry the prefix; the
  request rows do not). Checking and storing are serialized per prefix with a
  transaction-scoped advisory lock.
- A second request for an email that already has a pending one is not stored.
- Approving creates the organization and an owner invite, once: the request row
  is locked (``FOR UPDATE``) and must still be pending. If the email already
  belongs to a user, nothing is created and the request stays pending.
- Audit rows never contain the email, names or note.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.auth import accounts
from app.auth.accounts import IpPrefix
from app.auth.management import INVITE_CREATED
from app.db.models import (
    AccessRequest,
    AccessRequestStatus,
    AuditLog,
    AuditOutcome,
    Role,
    User,
    VolumeBand,
)

RATE_LIMIT = 3
RATE_WINDOW = dt.timedelta(hours=1)
MAX_LISTED = 500

# pg_advisory_xact_lock(class, key): the class keeps these locks apart from others.
_RATE_LOCK_CLASS = 0x65530001

# audit_log.action values
ACCESS_REQUEST_CREATED = "access_request_created"
ACCESS_REQUEST_APPROVED = "access_request_approved"
ACCESS_REQUEST_REJECTED = "access_request_rejected"


@dataclass(frozen=True, slots=True)
class RequestFields:
    """A validated submission. ``email`` is already normalized (``normalize_email``)."""

    company_name: str
    contact_name: str
    email: str
    volume_band: VolumeBand
    note: str | None


@dataclass(frozen=True, slots=True)
class RequestRow:
    """One request as the admin page shows it."""

    id: int
    created_at: dt.datetime
    company_name: str
    contact_name: str
    email: str
    volume_band: VolumeBand
    note: str | None
    status: AccessRequestStatus
    handled_at: dt.datetime | None
    handled_by_email: str | None


class ApproveOutcome(enum.StrEnum):
    APPROVED = "approved"
    ALREADY_HANDLED = "already_handled"
    EXISTING_USER = "existing_user"


@dataclass(frozen=True, slots=True)
class ApproveResult:
    outcome: ApproveOutcome
    email: str = ""
    company_name: str = ""
    organization_id: int | None = None
    token: str | None = None  # the owner invite token, only when approved


# --- The public form ------------------------------------------------------------------


def _same_prefix(ip: IpPrefix | None) -> ColumnElement[bool]:
    return AuditLog.ip_prefix == ip if ip is not None else AuditLog.ip_prefix.is_(None)


def submit_request(
    db: Session, fields: RequestFields, *, ip: IpPrefix | None, now: dt.datetime
) -> bool:
    """Store the request unless rate-limited or a duplicate. True if stored.

    The result is for tests and logging of counts only: callers must answer the
    submitter the same way either way.
    """
    db.execute(select(func.pg_advisory_xact_lock(_RATE_LOCK_CLASS, func.hashtext(str(ip)))))
    recent = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.action == ACCESS_REQUEST_CREATED,
            AuditLog.created_at > now - RATE_WINDOW,
            _same_prefix(ip),
        )
    )
    if (recent or 0) >= RATE_LIMIT:
        return False
    duplicate = db.scalar(
        select(AccessRequest.id)
        .where(
            func.lower(AccessRequest.email) == fields.email,
            AccessRequest.status == AccessRequestStatus.PENDING,
        )
        .limit(1)
    )
    if duplicate is not None:
        return False
    db.add(
        AccessRequest(
            company_name=fields.company_name,
            contact_name=fields.contact_name,
            email=fields.email,
            volume_band=fields.volume_band,
            note=fields.note,
            status=AccessRequestStatus.PENDING,
            created_at=now,
        )
    )
    accounts.audit(db, ACCESS_REQUEST_CREATED, AuditOutcome.SUCCESS, now=now, ip=ip)
    return True


# --- Superadmin: list, approve, reject -------------------------------------------------------


def pending_count(db: Session) -> int:
    query = select(func.count(AccessRequest.id)).where(
        AccessRequest.status == AccessRequestStatus.PENDING
    )
    return db.scalar(query) or 0


def list_requests(db: Session, *, handled: bool) -> list[RequestRow]:
    """Pending requests newest first, or handled ones by most recently handled."""
    query = select(AccessRequest, User.email).outerjoin(User, AccessRequest.handled_by == User.id)
    if handled:
        query = query.where(AccessRequest.status != AccessRequestStatus.PENDING).order_by(
            AccessRequest.handled_at.desc(), AccessRequest.id.desc()
        )
    else:
        query = query.where(AccessRequest.status == AccessRequestStatus.PENDING).order_by(
            AccessRequest.created_at.desc(), AccessRequest.id.desc()
        )
    rows = db.execute(query.limit(MAX_LISTED)).all()
    return [
        RequestRow(
            id=r.id,
            created_at=r.created_at,
            company_name=r.company_name,
            contact_name=r.contact_name,
            email=r.email,
            volume_band=r.volume_band,
            note=r.note,
            status=r.status,
            handled_at=r.handled_at,
            handled_by_email=handler,
        )
        for r, handler in rows
    ]


def _locked_request(db: Session, request_id: int) -> AccessRequest | None:
    query = select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
    return db.scalars(query).first()


def approve(
    db: Session, request_id: int, *, admin_id: int, ip: IpPrefix | None, now: dt.datetime
) -> ApproveResult | None:
    """Create the organization and an owner invite; ``None`` if there is no such request.

    Raises ``AdminError`` if the invite cannot be made (the caller rolls back).
    """
    request = _locked_request(db, request_id)
    if request is None:
        return None
    result = {"email": request.email, "company_name": request.company_name}
    if request.status != AccessRequestStatus.PENDING:
        return ApproveResult(ApproveOutcome.ALREADY_HANDLED, **result)

    existing = accounts.user_by_email(db, request.email)
    if existing is not None:
        accounts.audit(
            db,
            ACCESS_REQUEST_APPROVED,
            AuditOutcome.FAILURE,
            now=now,
            user_id=admin_id,
            target_user_id=existing.id,
            ip=ip,
        )
        return ApproveResult(ApproveOutcome.EXISTING_USER, **result)

    org_id = accounts.create_org(db, request.company_name)
    token = accounts.invite(db, org_id, request.email, Role.OWNER, now=now, created_by=admin_id)
    owner = accounts.user_by_email(db, request.email)
    request.status = AccessRequestStatus.APPROVED
    request.handled_at = now
    request.handled_by = admin_id
    who = {
        "now": now,
        "user_id": admin_id,
        "organization_id": org_id,
        "target_user_id": owner.id if owner is not None else None,
        "ip": ip,
    }
    accounts.audit(db, ACCESS_REQUEST_APPROVED, AuditOutcome.SUCCESS, **who)
    accounts.audit(db, INVITE_CREATED, AuditOutcome.SUCCESS, **who)
    return ApproveResult(ApproveOutcome.APPROVED, organization_id=org_id, token=token, **result)


def reject(
    db: Session, request_id: int, *, admin_id: int, ip: IpPrefix | None, now: dt.datetime
) -> bool | None:
    """Mark the request rejected. ``None`` if it does not exist, False if already handled."""
    request = _locked_request(db, request_id)
    if request is None:
        return None
    if request.status != AccessRequestStatus.PENDING:
        return False
    request.status = AccessRequestStatus.REJECTED
    request.handled_at = now
    request.handled_by = admin_id
    accounts.audit(
        db, ACCESS_REQUEST_REJECTED, AuditOutcome.SUCCESS, now=now, user_id=admin_id, ip=ip
    )
    return True
