"""Two-factor authentication (TOTP).

- users: ``mfa_secret`` (the TOTP secret, AES-GCM encrypted with MFA_KEY, never
  plaintext), ``mfa_enabled_at`` (NULL: 2FA off) and ``mfa_last_step`` (the last accepted
  30-second step, against replays).
- mfa_recovery_codes: one-time recovery codes, only their HMAC.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_secret", sa.LargeBinary(), nullable=True))
    op.add_column("users", sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_mfa_enabled_needs_secret"),
        "users",
        "mfa_enabled_at IS NULL OR mfa_secret IS NOT NULL",
    )

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "octet_length(code_hash) = 32", name=op.f("ck_mfa_recovery_codes_code_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mfa_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_recovery_codes")),
    )
    op.create_index(
        op.f("ix_mfa_recovery_codes_user_id"), "mfa_recovery_codes", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_mfa_recovery_codes_user_id"), table_name="mfa_recovery_codes")
    op.drop_table("mfa_recovery_codes")
    op.drop_constraint(op.f("ck_users_mfa_enabled_needs_secret"), "users", type_="check")
    op.drop_column("users", "mfa_last_step")
    op.drop_column("users", "mfa_enabled_at")
    op.drop_column("users", "mfa_secret")
