"""Batches for the History page and the duplicate check across batches.

- batches: ``file_name`` (sanitized, max 200 chars), ``parts_downloaded``, and the
  statuses validated | partial | completed | incomplete | failed (was running |
  completed | failed; ``running`` rows become ``validated``).
- batch_parts: one row per generated part, so a part is counted once however often it
  is downloaded.
- code_fingerprints.created_at is indexed for the 12-month purge.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_STATUSES = ("validated", "partial", "completed", "incomplete", "failed")
OLD_STATUSES = ("running", "completed", "failed")


def _status_check(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column(
        "batches",
        sa.Column("file_name", sa.String(length=200), server_default="", nullable=False),
    )
    op.add_column(
        "batches",
        sa.Column("parts_downloaded", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_batches_parts_downloaded_range"),
        "batches",
        "parts_downloaded >= 0 AND parts_downloaded <= parts",
    )

    op.drop_constraint(op.f("ck_batches_status"), "batches", type_="check")
    op.execute("UPDATE batches SET status = 'validated' WHERE status = 'running'")
    op.create_check_constraint(op.f("ck_batches_status"), "batches", _status_check(NEW_STATUSES))
    op.alter_column("batches", "status", server_default="validated")

    op.create_table(
        "batch_parts",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("part", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("part >= 1", name=op.f("ck_batch_parts_part_positive")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batches.id"],
            name=op.f("fk_batch_parts_batch_id_batches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("batch_id", "part", name=op.f("pk_batch_parts")),
    )
    op.create_index(
        op.f("ix_code_fingerprints_created_at"), "code_fingerprints", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_code_fingerprints_created_at"), table_name="code_fingerprints")
    op.drop_table("batch_parts")

    op.drop_constraint(op.f("ck_batches_status"), "batches", type_="check")
    op.execute(
        "UPDATE batches SET status = CASE status"
        " WHEN 'completed' THEN 'completed' WHEN 'failed' THEN 'failed' ELSE 'running' END"
    )
    op.create_check_constraint(op.f("ck_batches_status"), "batches", _status_check(OLD_STATUSES))
    op.alter_column("batches", "status", server_default="running")

    op.drop_constraint(op.f("ck_batches_parts_downloaded_range"), "batches", type_="check")
    op.drop_column("batches", "parts_downloaded")
    op.drop_column("batches", "file_name")
