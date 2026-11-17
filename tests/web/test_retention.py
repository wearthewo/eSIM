"""Retention: ``purge`` deletes exactly the rows past their keeping period. Synthetic data."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import admin
from app.auth import retention
from app.auth.tokens import digest, new_token
from app.db.models import (
    AccessRequest,
    AccessRequestStatus,
    AuditLog,
    AuditOutcome,
    Batch,
    BatchStatus,
    CodeFingerprint,
    FingerprintKind,
    Invite,
    PasswordToken,
    UserSession,
    VolumeBand,
)
from app.settings import Settings
from tests.web.conftest import Accounts
from tests.web.management_routes import count

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
DAY = timedelta(days=1)


def _ids(accts: Accounts, table: str) -> list[int]:
    return [r[0] for r in accts.sql(f"SELECT id FROM {table} ORDER BY id")]  # noqa: S608


def seed(accts: Accounts) -> dict[str, list[int]]:
    """Rows on both sides of every limit; returns the ids that must survive, per table."""
    org_id = accts.org()
    user_id = accts.user("user@org.test", org_id=org_id)
    keep: dict[str, list[int]] = {}

    def _add(db: Session) -> None:
        def add(row: object) -> int:
            db.add(row)
            db.flush()
            return row.id  # type: ignore[attr-defined]

        def request(age: timedelta, status: AccessRequestStatus) -> int:
            return add(
                AccessRequest(
                    company_name="Old Co (test)",
                    contact_name="Someone",
                    email="old@org.test",
                    volume_band=VolumeBand.UNDER_1K,
                    status=status,
                    created_at=NOW - age,
                )
            )

        request(30 * DAY + MIN, AccessRequestStatus.PENDING)
        request(31 * DAY, AccessRequestStatus.APPROVED)
        keep["access_requests"] = [
            request(30 * DAY - MIN, AccessRequestStatus.PENDING),
            request(MIN, AccessRequestStatus.REJECTED),
        ]

        def audit(age: timedelta) -> int:
            return add(AuditLog(action="login", outcome=AuditOutcome.SUCCESS, created_at=NOW - age))

        audit(90 * DAY + MIN)
        keep["audit_log"] = [audit(90 * DAY - MIN), audit(MIN)]

        def session(expires: timedelta, last_seen: timedelta) -> int:
            return add(
                UserSession(
                    token_hash=digest(new_token()),
                    csrf_token_hash=digest(new_token()),
                    user_id=user_id,
                    created_at=NOW - 2 * DAY,
                    expires_at=NOW + expires,
                    last_seen_at=NOW - last_seen,
                )
            )

        session(-MIN, MIN)  # past the absolute limit
        session(DAY, timedelta(hours=12) + MIN)  # idle too long
        keep["sessions"] = [session(DAY, timedelta(hours=11))]

        def link(table: type, created: timedelta, *, used: bool, expires: timedelta) -> int:
            extra = (
                {"email": "user@org.test", "organization_id": org_id}
                if table is Invite
                else {"user_id": user_id}
            )
            return add(
                table(
                    token_hash=digest(new_token()),
                    created_at=NOW - created,
                    used_at=NOW - created + MIN if used else None,
                    expires_at=NOW + expires,
                    **extra,
                )
            )

        for name, table in (("invites", Invite), ("password_tokens", PasswordToken)):
            link(table, 7 * DAY + MIN, used=True, expires=-5 * DAY)  # used, old
            link(table, 8 * DAY, used=False, expires=-6 * DAY)  # expired, old
            keep[name] = [
                link(table, 7 * DAY - MIN, used=True, expires=-5 * DAY),  # used, recent
                link(table, 6 * DAY, used=False, expires=-4 * DAY),  # expired, recent
                link(table, MIN, used=False, expires=2 * DAY),  # live
                # Old but neither used nor expired (only if the link lifetime grows): kept.
                link(table, 8 * DAY, used=False, expires=DAY),
            ]

    accts.run(_add)
    return keep


def test_purge_deletes_exactly_the_old_rows(accts: Accounts) -> None:
    keep = seed(accts)
    counts = accts.run(lambda db: retention.purge(db, now=NOW))
    assert counts == {
        "access_requests": 2,
        "audit_log": 1,
        "sessions": 2,
        "invites": 2,
        "password_tokens": 2,
        # Batches and fingerprints: see test_purge_ages_batches_and_fingerprints.
        "code_fingerprints": 0,
        "batches_incomplete": 0,
        "batches_unused": 0,
    }
    for table, ids in keep.items():
        survivors = _ids(accts, table)
        if table == "audit_log":
            survivors = survivors[:-1]  # the purge's own row comes last
        assert survivors == ids, table
    assert accts.sql("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1")[0][0] == (
        "retention_purge"
    )

    # Nothing left to delete.
    again = accts.run(lambda db: retention.purge(db, now=NOW))
    assert set(again.values()) == {0}


def test_purge_ages_batches_and_fingerprints(accts: Accounts) -> None:
    """Fingerprints: 12 months. Batches: partial -> incomplete and validated -> deleted
    24 hours after they were created; completed, failed and incomplete ones stay."""
    org_id = accts.org()
    user_id = accts.user("user@org.test", org_id=org_id)

    def _add(db: Session) -> dict[str, int]:
        def batch(status: BatchStatus, age: timedelta, parts_downloaded: int = 0) -> int:
            row = Batch(
                organization_id=org_id,
                user_id=user_id,
                created_at=NOW - age,
                file_name="orders.csv",
                row_count=10,
                parts=2,
                parts_downloaded=parts_downloaded,
                status=status,
            )
            db.add(row)
            db.flush()
            return row.id

        ids = {
            "partial_old": batch(BatchStatus.PARTIAL, DAY + MIN, 1),
            "partial_new": batch(BatchStatus.PARTIAL, DAY - MIN, 1),
            "validated_old": batch(BatchStatus.VALIDATED, DAY + MIN),
            "validated_new": batch(BatchStatus.VALIDATED, DAY - MIN),
            "completed_old": batch(BatchStatus.COMPLETED, 400 * DAY, 2),
            "failed_old": batch(BatchStatus.FAILED, 30 * DAY),
            "incomplete_old": batch(BatchStatus.INCOMPLETE, 30 * DAY, 1),
        }
        for n, age in enumerate((365 * DAY + MIN, 365 * DAY - MIN)):
            db.add(
                CodeFingerprint(
                    organization_id=org_id,
                    kind=FingerprintKind.ICCID,
                    fingerprint=bytes([n + 1]) * 32,
                    batch_id=ids["completed_old"],
                    created_at=NOW - age,
                )
            )
        return ids

    ids = accts.run(_add)
    counts = accts.run(lambda db: retention.purge(db, now=NOW))
    assert (counts["code_fingerprints"], counts["batches_incomplete"]) == (1, 1)
    assert counts["batches_unused"] == 1
    status = dict(accts.sql("SELECT id, status FROM batches"))
    assert ids["validated_old"] not in status
    assert {name: status[i] for name, i in ids.items() if i in status} == {
        "partial_old": "incomplete",
        "partial_new": "partial",
        "validated_new": "validated",
        "completed_old": "completed",
        "failed_old": "failed",
        "incomplete_old": "incomplete",
    }
    remaining = accts.sql("SELECT created_at FROM code_fingerprints")
    assert [row[0] for row in remaining] == [NOW - (365 * DAY - MIN)]


def test_purge_if_due_runs_once_per_utc_day(accts: Accounts) -> None:
    def run(now: datetime):
        return accts.run(lambda db: retention.purge_if_due(db, now=now))

    assert run(NOW) is not None
    assert run(NOW + timedelta(hours=11)) is None  # 23:00 the same UTC day
    assert run(NOW + timedelta(hours=12, minutes=1)) is not None  # 00:01 the next day
    assert count(accts, "audit_log", "action = 'retention_purge'") == 2


def test_cli_purge_prints_counts_only(
    accts: Accounts, web_settings: Settings, web_sessions: sessionmaker[Session]
) -> None:
    seed(accts)
    out, err = io.StringIO(), io.StringIO()
    code = admin.main(
        ["purge"], session_scope=web_sessions.begin, settings=web_settings, out=out, err=err
    )
    assert code == 0
    assert err.getvalue() == ""
    lines = out.getvalue().strip().splitlines()
    assert all(re.fullmatch(r"[a-z_]+: \d+", line) for line in lines), lines
    assert [line.split(":")[0] for line in lines] == [
        "access_requests",
        "audit_log",
        "sessions",
        "invites",
        "password_tokens",
        "code_fingerprints",
        "batches_incomplete",
        "batches_unused",
    ]


def test_app_startup_purges_once_per_day(make_app: Callable, accts: Accounts) -> None:
    for _ in range(2):  # a restart on the same day does not purge again
        with TestClient(make_app(run_retention=True)) as c:
            assert c.get("/healthz").status_code == 200
    assert count(accts, "audit_log", "action = 'retention_purge'") == 1
