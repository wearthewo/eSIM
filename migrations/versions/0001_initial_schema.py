"""Initial schema: metadata-only tables for accounts, organizations and usage.

No table has a column for activation codes, matching IDs, confirmation codes,
uploaded files, PDFs, customer names or customer emails. See app/db/models.py
for the ON DELETE rules.

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("daily_row_quota", sa.Integer(), server_default="20000", nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "daily_row_quota >= 0", name=op.f("ck_organizations_daily_row_quota_nonnegative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.create_table(
        "branding",
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("company_name", sa.String(length=100), nullable=False),
        sa.Column("primary_hex", sa.String(length=7), server_default="#000000", nullable=False),
        sa.Column("footer_text", sa.String(length=200), server_default="", nullable=False),
        sa.Column("logo_png", sa.LargeBinary(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "primary_hex ~ '^#[0-9A-Fa-f]{6}$'", name=op.f("ck_branding_primary_hex_format")
        ),
        sa.CheckConstraint(
            "substring(logo_png FROM 1 FOR 8) = decode('89504e470d0a1a0a', 'hex')",
            name=op.f("ck_branding_logo_png_signature"),
        ),
        sa.CheckConstraint(
            "octet_length(logo_png) <= 512000", name=op.f("ck_branding_logo_png_size")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_branding_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", name=op.f("pk_branding")),
    )
    op.create_table(
        "usage",
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("rows_generated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("rows_generated >= 0", name=op.f("ck_usage_rows_generated_nonnegative")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_usage_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", "day", name=op.f("pk_usage")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "member", name="role", native_enum=False, create_constraint=True, length=16
            ),
            nullable=False,
        ),
        sa.Column("is_superadmin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "is_superadmin OR organization_id IS NOT NULL",
            name=op.f("ck_users_organization_required_unless_superadmin"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_users_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_organization_id"), "users", ["organization_id"], unique=False)
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "access_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("contact_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column(
            "volume_band",
            sa.Enum(
                "under_1k",
                "1k_10k",
                "10k_100k",
                "over_100k",
                name="volume_band",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "char_length(note) <= 2000", name=op.f("ck_access_requests_note_length")
        ),
        sa.ForeignKeyConstraint(
            ["handled_by"],
            ["users.id"],
            name=op.f("fk_access_requests_handled_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_requests")),
    )
    op.create_index(
        op.f("ix_access_requests_created_at"), "access_requests", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_access_requests_status"), "access_requests", ["status"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("organization_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "success",
                "failure",
                name="outcome",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("ip_prefix", postgresql.CIDR(), nullable=True),
        sa.CheckConstraint(
            "(family(ip_prefix) = 4 AND masklen(ip_prefix) = 24)"
            " OR (family(ip_prefix) = 6 AND masklen(ip_prefix) = 48)",
            name=op.f("ck_audit_log_ip_prefix_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_audit_log_organization_id_organizations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_log_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_log_organization_id_created_at",
        "audit_log",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_audit_log_user_id"), "audit_log", ["user_id"], unique=False)
    op.create_table(
        "batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("parts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("warning_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "completed",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.CheckConstraint(
            "row_count >= 0 AND parts >= 0 AND error_count >= 0 AND warning_count >= 0",
            name=op.f("ck_batches_counts_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_batches_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_batches_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_batches")),
    )
    op.create_index(
        "ix_batches_organization_id_created_at",
        "batches",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_batches_user_id"), "batches", ["user_id"], unique=False)
    op.create_table(
        "invites",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner", "member", name="role", native_enum=False, create_constraint=True, length=16
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_invites_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_invites_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invites_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
    )
    op.create_index(
        op.f("ix_invites_organization_id"), "invites", ["organization_id"], unique=False
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("csrf_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(csrf_token_hash) = 32", name=op.f("ck_sessions_csrf_token_hash_length")
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_sessions_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_expires_at"), "sessions", ["expires_at"], unique=False)
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "code_fingerprints",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("organization_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "iccid",
                "activation",
                name="kind",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(fingerprint) = 32", name=op.f("ck_code_fingerprints_fingerprint_length")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batches.id"],
            name=op.f("fk_code_fingerprints_batch_id_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_code_fingerprints_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_fingerprints")),
        sa.UniqueConstraint(
            "organization_id",
            "kind",
            "fingerprint",
            name=op.f("uq_code_fingerprints_organization_id_kind_fingerprint"),
        ),
    )
    op.create_index(
        op.f("ix_code_fingerprints_batch_id"), "code_fingerprints", ["batch_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_code_fingerprints_batch_id"), table_name="code_fingerprints")
    op.drop_table("code_fingerprints")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_expires_at"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_invites_organization_id"), table_name="invites")
    op.drop_table("invites")
    op.drop_index(op.f("ix_batches_user_id"), table_name="batches")
    op.drop_index("ix_batches_organization_id_created_at", table_name="batches")
    op.drop_table("batches")
    op.drop_index(op.f("ix_audit_log_user_id"), table_name="audit_log")
    op.drop_index("ix_audit_log_organization_id_created_at", table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_created_at"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(op.f("ix_access_requests_status"), table_name="access_requests")
    op.drop_index(op.f("ix_access_requests_created_at"), table_name="access_requests")
    op.drop_table("access_requests")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_index(op.f("ix_users_organization_id"), table_name="users")
    op.drop_table("users")
    op.drop_table("usage")
    op.drop_table("branding")
    op.drop_table("organizations")
