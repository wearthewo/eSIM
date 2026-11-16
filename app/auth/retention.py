"""Data retention: delete what is past its keeping period.

- access_requests: 30 days after they were made (whatever their status).
- audit_log: 90 days.
- sessions: as soon as they have expired (absolute or idle limit).
- invites and password_tokens: used or expired ones, 7 days after they were created.

``purge`` always runs (``python -m app.admin purge``). ``purge_if_due`` runs it at
most once per UTC day across all processes: it holds a transaction-scoped advisory
lock and skips if a ``retention_purge`` audit row exists since midnight UTC. The web
app calls it at startup and then every hour (``app.web.main``).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Delete, delete, func, or_, select
from sqlalchemy.orm import Session

from app.auth import accounts
from app.auth.accounts import SESSION_IDLE_TTL
from app.db.models import (
    AccessRequest,
    AuditLog,
    AuditOutcome,
    Invite,
    PasswordToken,
    UserSession,
)

ACCESS_REQUEST_TTL = dt.timedelta(days=30)
AUDIT_LOG_TTL = dt.timedelta(days=90)
SPENT_LINK_TTL = dt.timedelta(days=7)

RETENTION_PURGE = "retention_purge"
_PURGE_LOCK_KEY = 0x6553_0002  # pg_try_advisory_xact_lock key, unique to this job


def _delete(db: Session, statement: Delete) -> int:
    result = db.execute(statement.execution_options(synchronize_session=False))
    return int(getattr(result, "rowcount", 0) or 0)


def purge(db: Session, *, now: dt.datetime) -> dict[str, int]:
    """Delete everything past its retention; returns the number of rows deleted per table."""
    counts = {
        "access_requests": _delete(
            db, delete(AccessRequest).where(AccessRequest.created_at < now - ACCESS_REQUEST_TTL)
        ),
        "audit_log": _delete(db, delete(AuditLog).where(AuditLog.created_at < now - AUDIT_LOG_TTL)),
        "sessions": _delete(
            db,
            delete(UserSession).where(
                or_(
                    UserSession.expires_at <= now,
                    UserSession.last_seen_at <= now - SESSION_IDLE_TTL,
                )
            ),
        ),
    }
    for name, table in (("invites", Invite), ("password_tokens", PasswordToken)):
        counts[name] = _delete(
            db,
            delete(table).where(
                table.created_at < now - SPENT_LINK_TTL,
                or_(table.used_at.is_not(None), table.expires_at <= now),
            ),
        )
    accounts.audit(db, RETENTION_PURGE, AuditOutcome.SUCCESS, now=now)
    return counts


def _utc_midnight(now: dt.datetime) -> dt.datetime:
    return dt.datetime.combine(now.astimezone(dt.UTC).date(), dt.time(), tzinfo=dt.UTC)


def purge_if_due(db: Session, *, now: dt.datetime) -> dict[str, int] | None:
    """``purge`` unless it already ran today (UTC) or is running elsewhere; ``None`` if skipped."""
    if not db.scalar(select(func.pg_try_advisory_xact_lock(_PURGE_LOCK_KEY))):
        return None
    done_today = db.scalar(
        select(AuditLog.id)
        .where(AuditLog.action == RETENTION_PURGE, AuditLog.created_at >= _utc_midnight(now))
        .limit(1)
    )
    if done_today is not None:
        return None
    return purge(db, now=now)
