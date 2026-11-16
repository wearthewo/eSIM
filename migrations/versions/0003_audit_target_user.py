"""Audit log: the user an action was done to (``target_user_id``), next to the actor.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("target_user_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_log_target_user_id_users"),
        "audit_log",
        "users",
        ["target_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_audit_log_target_user_id"), "audit_log", ["target_user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_target_user_id"), table_name="audit_log")
    op.drop_constraint(op.f("fk_audit_log_target_user_id_users"), "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "target_user_id")
