"""Constraints, defaults and ON DELETE behaviour, checked against PostgreSQL.

Every test runs inside a rolled-back transaction (``db`` fixture). Statements
expected to fail run in a savepoint (``_fails``) so the test can carry on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from ipaddress import IPv4Network, IPv6Network

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.fingerprints import fingerprint
from app.db.ip import ip_prefix
from app.db.models import (
    AccessRequest,
    AccessRequestStatus,
    AuditLog,
    AuditOutcome,
    Batch,
    BatchPart,
    BatchStatus,
    Branding,
    CodeFingerprint,
    FingerprintKind,
    Invite,
    Organization,
    Role,
    Usage,
    User,
    UserSession,
    VolumeBand,
)

KEY = b"k" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16  # only the signature is checked
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
LATER = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
TODAY = dt.date(2026, 1, 1)


def _digest(n: int) -> bytes:
    return bytes([n]) * 32


def _constraint(error: Exception) -> str | None:
    return error.orig.diag.constraint_name  # type: ignore[attr-defined]


def _fails(db: Session, action: Callable[[], object], error: type[Exception] = IntegrityError):
    """Run ``action`` and a flush in a savepoint; assert it raises ``error``."""
    with pytest.raises(error) as info:
        with db.begin_nested():
            action()
            db.flush()
    return info.value


def _add_fails(db: Session, *objects: object) -> str | None:
    """Add ``objects``, assert the flush violates a constraint, return its name."""
    return _constraint(_fails(db, lambda: db.add_all(objects)))


def _sql_fails(db: Session, sql: str, error: type[Exception] = IntegrityError, **params):
    return _fails(db, lambda: db.execute(text(sql), params), error)


@pytest.fixture
def org(db: Session) -> Organization:
    o = Organization(name="Test Org A")
    db.add(o)
    db.flush()
    return o


@pytest.fixture
def org_b(db: Session) -> Organization:
    o = Organization(name="Test Org B")
    db.add(o)
    db.flush()
    return o


@pytest.fixture
def user(db: Session, org: Organization) -> User:
    u = User(email="owner@example.test", organization_id=org.id, role=Role.OWNER)
    db.add(u)
    db.flush()
    return u


def _batch(db: Session, org: Organization, user: User | None = None) -> Batch:
    b = Batch(organization_id=org.id, user_id=user.id if user else None, row_count=3)
    db.add(b)
    db.flush()
    return b


# --- defaults ---------------------------------------------------------------------


def test_defaults(db: Session, org: Organization, user: User) -> None:
    batch = _batch(db, org, user)
    request = AccessRequest(
        company_name="Example Co",
        contact_name="Test Contact",
        email="contact@example.test",
        volume_band=VolumeBand.UNDER_1K,
    )
    db.add(request)
    db.flush()
    for obj in (org, user, batch, request):
        db.refresh(obj)

    assert org.daily_row_quota == 20_000
    assert org.disabled is False
    assert org.created_at.tzinfo is not None
    assert user.password_hash is None
    assert user.is_superadmin is False
    assert user.disabled is False
    assert user.last_login_at is None
    assert batch.status is BatchStatus.VALIDATED
    assert (batch.parts, batch.parts_downloaded, batch.error_count, batch.warning_count) == (
        0,
        0,
        0,
        0,
    )
    assert batch.file_name == ""
    assert request.status is AccessRequestStatus.PENDING


def test_server_defaults_apply_to_raw_inserts(db: Session) -> None:
    row = db.execute(
        text("INSERT INTO organizations (name) VALUES ('Raw Org') RETURNING daily_row_quota")
    ).one()
    assert row.daily_row_quota == 20_000


def test_enums_are_stored_as_their_values(db: Session, user: User) -> None:
    assert db.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user.id}).scalar() == (
        "owner"
    )


# --- users ------------------------------------------------------------------------


def test_email_unique_case_insensitive(db: Session, org: Organization, user: User) -> None:
    clash = User(email="OWNER@Example.TEST", organization_id=org.id)
    assert _add_fails(db, clash) == "uq_users_email_lower"


def test_email_unique_across_organizations(db: Session, org_b: Organization, user: User) -> None:
    clash = User(email="owner@example.test", organization_id=org_b.id)
    assert _add_fails(db, clash) == "uq_users_email_lower"


def test_user_needs_an_organization_unless_superadmin(db: Session) -> None:
    db.add(User(email="admin@example.test", is_superadmin=True))
    db.flush()
    orphan = User(email="member@example.test")
    assert _add_fails(db, orphan) == "ck_users_organization_required_unless_superadmin"


def test_role_check(db: Session, org: Organization) -> None:
    error = _sql_fails(
        db,
        "INSERT INTO users (email, organization_id, role) VALUES ('x@example.test', :org, 'admin')",
        org=org.id,
    )
    assert _constraint(error) == "ck_users_role"


def test_unknown_enum_value_is_rejected_before_the_database(db: Session) -> None:
    request = AccessRequest(
        company_name="Example Co",
        contact_name="Test Contact",
        email="contact@example.test",
        volume_band="lots",
    )
    _fails(db, lambda: db.add(request), StatementError)


# --- invites and sessions ---------------------------------------------------------


def test_invite_token_hash_unique(db: Session, org: Organization) -> None:
    db.add(
        Invite(
            token_hash=_digest(1), email="a@example.test", organization_id=org.id, expires_at=LATER
        )
    )
    db.flush()
    clash = Invite(
        token_hash=_digest(1), email="b@example.test", organization_id=org.id, expires_at=LATER
    )
    assert _add_fails(db, clash) == "uq_invites_token_hash"


def test_session_token_hash_unique(db: Session, user: User) -> None:
    db.add(
        UserSession(
            token_hash=_digest(1), user_id=user.id, csrf_token_hash=_digest(2), expires_at=LATER
        )
    )
    db.flush()
    clash = UserSession(
        token_hash=_digest(1), user_id=user.id, csrf_token_hash=_digest(3), expires_at=LATER
    )
    assert _add_fails(db, clash) == "uq_sessions_token_hash"


@pytest.mark.parametrize("length", [0, 16, 31, 33, 64])
def test_token_hashes_must_be_32_bytes(db: Session, user: User, length: int) -> None:
    bad = UserSession(
        token_hash=b"t" * length, user_id=user.id, csrf_token_hash=_digest(2), expires_at=LATER
    )
    assert _add_fails(db, bad) == "ck_sessions_token_hash_length"
    bad = UserSession(
        token_hash=_digest(1), user_id=user.id, csrf_token_hash=b"c" * length, expires_at=LATER
    )
    assert _add_fails(db, bad) == "ck_sessions_csrf_token_hash_length"


# --- branding ---------------------------------------------------------------------


def test_one_branding_per_organization(db: Session, org: Organization) -> None:
    db.add(Branding(organization_id=org.id, company_name="Example Co"))
    db.flush()
    clash = Branding(organization_id=org.id, company_name="Example Co 2")
    assert _add_fails(db, clash) == "pk_branding"


def test_branding_logo_must_be_png(db: Session, org: Organization, org_b: Organization) -> None:
    db.add(Branding(organization_id=org.id, company_name="Example Co", logo_png=PNG))
    db.flush()
    jpeg = Branding(organization_id=org_b.id, company_name="Example Co", logo_png=JPEG)
    assert _add_fails(db, jpeg) == "ck_branding_logo_png_signature"
    huge = Branding(
        organization_id=org_b.id, company_name="Example Co", logo_png=PNG + b"\0" * 512_000
    )
    assert _add_fails(db, huge) == "ck_branding_logo_png_size"


@pytest.mark.parametrize("value", ["#12345", "123456", "#12345G", "#1234567", "red"])
def test_branding_primary_hex_format(db: Session, org: Organization, value: str) -> None:
    bad = Branding(organization_id=org.id, company_name="Example Co", primary_hex=value)
    constraint = _constraint(_fails(db, lambda: db.add(bad), (IntegrityError, DataError)))
    # "#1234567" is too long for varchar(7): a DataError without a constraint name.
    assert constraint in ("ck_branding_primary_hex_format", None)


# --- fingerprints -----------------------------------------------------------------


def test_fingerprint_unique_per_organization_and_kind(
    db: Session, org: Organization, org_b: Organization
) -> None:
    batch_a, batch_b = _batch(db, org), _batch(db, org_b)
    value = "8999000000000000013"
    fp_a = fingerprint(org.id, FingerprintKind.ICCID, value, key=KEY)
    db.add(
        CodeFingerprint(
            organization_id=org.id,
            kind=FingerprintKind.ICCID,
            fingerprint=fp_a,
            batch_id=batch_a.id,
        )
    )
    db.flush()

    clash = CodeFingerprint(
        organization_id=org.id, kind=FingerprintKind.ICCID, fingerprint=fp_a, batch_id=batch_a.id
    )
    assert _add_fails(db, clash) == "uq_code_fingerprints_organization_id_kind_fingerprint"

    # The same bytes are allowed for another kind or another organization.
    db.add_all(
        [
            CodeFingerprint(
                organization_id=org.id,
                kind=FingerprintKind.ACTIVATION,
                fingerprint=fp_a,
                batch_id=batch_a.id,
            ),
            CodeFingerprint(
                organization_id=org_b.id,
                kind=FingerprintKind.ICCID,
                fingerprint=fp_a,
                batch_id=batch_b.id,
            ),
        ]
    )
    db.flush()


def test_fingerprint_must_be_32_bytes(db: Session, org: Organization) -> None:
    batch = _batch(db, org)
    bad = CodeFingerprint(
        organization_id=org.id, kind=FingerprintKind.ICCID, fingerprint=b"short", batch_id=batch.id
    )
    assert _add_fails(db, bad) == "ck_code_fingerprints_fingerprint_length"


# --- usage ------------------------------------------------------------------------


def test_usage_unique_per_organization_and_day(
    db: Session, org: Organization, org_b: Organization
) -> None:
    db.add(Usage(organization_id=org.id, day=TODAY, rows_generated=10))
    db.flush()
    assert _add_fails(db, Usage(organization_id=org.id, day=TODAY)) == "pk_usage"
    db.add_all(
        [
            Usage(organization_id=org.id, day=TODAY + dt.timedelta(days=1)),
            Usage(organization_id=org_b.id, day=TODAY),
        ]
    )
    db.flush()


def test_usage_cannot_go_negative(db: Session, org: Organization) -> None:
    bad = Usage(organization_id=org.id, day=TODAY, rows_generated=-1)
    assert _add_fails(db, bad) == "ck_usage_rows_generated_nonnegative"


def test_counters_cannot_go_negative(db: Session, org: Organization) -> None:
    assert _add_fails(db, Organization(name="X", daily_row_quota=-1)) == (
        "ck_organizations_daily_row_quota_nonnegative"
    )
    assert _add_fails(db, Batch(organization_id=org.id, error_count=-1)) == (
        "ck_batches_counts_nonnegative"
    )


@pytest.mark.parametrize("downloaded", [-1, 3])
def test_parts_downloaded_stays_within_parts(
    db: Session, org: Organization, downloaded: int
) -> None:
    bad = Batch(organization_id=org.id, parts=2, parts_downloaded=downloaded)
    assert _add_fails(db, bad) == "ck_batches_parts_downloaded_range"


def test_batch_status_is_checked(db: Session, org: Organization) -> None:
    db.add(Batch(organization_id=org.id))
    db.flush()
    error = _sql_fails(db, "UPDATE batches SET status = 'running'")
    assert _constraint(error) == "ck_batches_status"


def test_batch_part_is_once_per_part_and_positive(db: Session, org: Organization) -> None:
    batch = _batch(db, org)
    db.add(BatchPart(batch_id=batch.id, part=1))
    db.flush()
    assert _add_fails(db, BatchPart(batch_id=batch.id, part=1)) == "pk_batch_parts"
    assert _add_fails(db, BatchPart(batch_id=batch.id, part=0)) == "ck_batch_parts_part_positive"


# --- audit log --------------------------------------------------------------------


def test_audit_log_stores_prefixes(db: Session, org: Organization, user: User) -> None:
    for address in ("203.0.113.77", "2001:db8:1234:5678::1", "::ffff:198.51.100.9"):
        db.add(
            AuditLog(
                user_id=user.id,
                organization_id=org.id,
                action="login",
                outcome=AuditOutcome.SUCCESS,
                ip_prefix=ip_prefix(address),
            )
        )
    db.add(AuditLog(action="login", outcome=AuditOutcome.FAILURE, ip_prefix=None))
    db.flush()
    stored = set(db.scalars(select(AuditLog.ip_prefix).where(AuditLog.ip_prefix.is_not(None))))
    assert stored == {
        IPv4Network("203.0.113.0/24"),
        IPv6Network("2001:db8:1234::/48"),
        IPv4Network("198.51.100.0/24"),
    }


@pytest.mark.parametrize(
    "network",
    [
        IPv4Network("203.0.113.77/32"),
        IPv4Network("203.0.0.0/16"),
        IPv6Network("2001:db8:1234:5678::1/128"),
        IPv6Network("2001:db8:1234:5678::/64"),
    ],
    ids=str,
)
def test_audit_log_rejects_full_addresses_and_other_prefixes(
    db: Session, network: IPv4Network | IPv6Network
) -> None:
    bad = AuditLog(action="login", outcome=AuditOutcome.FAILURE, ip_prefix=network)
    assert _add_fails(db, bad) == "ck_audit_log_ip_prefix_length"


def test_audit_log_rejects_host_bits(db: Session) -> None:
    # A CIDR value cannot carry a host address: 203.0.113.77/24 is not a valid cidr.
    _sql_fails(
        db,
        "INSERT INTO audit_log (action, outcome, ip_prefix)"
        " VALUES ('login', 'failure', CAST(:ip AS cidr))",
        DataError,
        ip="203.0.113.77/24",
    )


# --- ON DELETE --------------------------------------------------------------------


def test_deleting_a_user(db: Session, org: Organization, user: User) -> None:
    db.add_all(
        [
            UserSession(
                token_hash=_digest(1), user_id=user.id, csrf_token_hash=_digest(2), expires_at=LATER
            ),
            Invite(
                token_hash=_digest(3),
                email="new@example.test",
                organization_id=org.id,
                expires_at=LATER,
                created_by=user.id,
            ),
            AuditLog(
                user_id=user.id,
                organization_id=org.id,
                action="login",
                outcome=AuditOutcome.SUCCESS,
            ),
            AccessRequest(
                company_name="Example Co",
                contact_name="Test Contact",
                email="contact@example.test",
                volume_band=VolumeBand.OVER_100K,
                handled_by=user.id,
            ),
        ]
    )
    batch = _batch(db, org, user)
    db.flush()

    db.delete(user)
    db.flush()
    db.expire_all()

    assert db.scalars(select(UserSession)).all() == []  # CASCADE
    assert db.scalar(select(Invite.created_by)) is None  # SET NULL
    assert db.scalar(select(AuditLog.user_id)) is None  # SET NULL
    assert db.scalar(select(AccessRequest.handled_by)) is None  # SET NULL
    db.refresh(batch)
    assert batch.user_id is None  # SET NULL: the organization keeps its history


def test_deleting_an_organization(db: Session, org: Organization, user: User) -> None:
    batch = _batch(db, org, user)
    db.add_all(
        [
            Branding(organization_id=org.id, company_name="Example Co"),
            Usage(organization_id=org.id, day=TODAY, rows_generated=3),
            Invite(
                token_hash=_digest(3),
                email="new@example.test",
                organization_id=org.id,
                expires_at=LATER,
            ),
            CodeFingerprint(
                organization_id=org.id,
                kind=FingerprintKind.ICCID,
                fingerprint=_digest(4),
                batch_id=batch.id,
            ),
            AuditLog(organization_id=org.id, action="upload", outcome=AuditOutcome.SUCCESS),
        ]
    )
    db.flush()

    db.delete(org)
    db.flush()
    db.expire_all()

    for model in (User, Batch, Branding, Usage, Invite, CodeFingerprint):
        assert db.scalars(select(model)).all() == [], model.__name__  # CASCADE
    log = db.scalars(select(AuditLog)).one()
    assert log.organization_id is None  # SET NULL: the audit trail outlives it


def test_deleting_a_batch_deletes_its_fingerprints(db: Session, org: Organization) -> None:
    batch = _batch(db, org)
    db.add(
        CodeFingerprint(
            organization_id=org.id,
            kind=FingerprintKind.ICCID,
            fingerprint=_digest(4),
            batch_id=batch.id,
        )
    )
    db.add(BatchPart(batch_id=batch.id, part=1))
    db.flush()
    db.delete(batch)
    db.flush()
    assert db.scalars(select(CodeFingerprint)).all() == []
    assert db.scalars(select(BatchPart)).all() == []


def test_voided_at_is_set_exactly_when_voided(db: Session, org: Organization) -> None:
    now = dt.datetime(2026, 11, 17, tzinfo=dt.UTC)
    no_time = Batch(organization_id=org.id, status=BatchStatus.VOIDED)
    assert _add_fails(db, no_time) == "ck_batches_voided_at_iff_voided"
    stray = Batch(organization_id=org.id, status=BatchStatus.COMPLETED, voided_at=now)
    assert _add_fails(db, stray) == "ck_batches_voided_at_iff_voided"
    db.add(Batch(organization_id=org.id, status=BatchStatus.VOIDED, voided_at=now))
    db.flush()


def test_part_sizes_and_numbers_are_positive(db: Session, org: Organization) -> None:
    assert _add_fails(db, Batch(organization_id=org.id, chunk_rows=0)) == (
        "ck_batches_chunk_rows_positive"
    )
    batch = _batch(db, org)
    db.refresh(batch)
    assert batch.chunk_rows == 500  # the part size before it was stored per batch
    bad = CodeFingerprint(
        organization_id=org.id,
        kind=FingerprintKind.ICCID,
        fingerprint=_digest(9),
        batch_id=batch.id,
        part=0,
    )
    assert _add_fails(db, bad) == "ck_code_fingerprints_part_positive"
