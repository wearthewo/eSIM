"""Continue an unfinished batch; void a batch.

- batches: ``chunk_rows`` (part size the batch was created with; existing rows 500),
  ``voided_at`` / ``voided_by`` and the status ``voided`` (``voided_at`` is set exactly
  when the status is voided).
- code_fingerprints: ``part`` (the part the row was generated in; NULL for older rows).
- audit_log: ``target_batch_id`` (the batch an action was done to).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = ("validated", "partial", "completed", "incomplete", "failed")
NEW_STATUSES = (*OLD_STATUSES, "voided")


def _status_check(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column(
        "batches",
        sa.Column("chunk_rows", sa.Integer(), server_default=sa.text("500"), nullable=False),
    )
    op.create_check_constraint(op.f("ck_batches_chunk_rows_positive"), "batches", "chunk_rows >= 1")
    op.add_column("batches", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("batches", sa.Column("voided_by", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_batches_voided_by_users"),
        "batches",
        "users",
        ["voided_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(op.f("ck_batches_status"), "batches", type_="check")
    op.create_check_constraint(op.f("ck_batches_status"), "batches", _status_check(NEW_STATUSES))
    op.create_check_constraint(
        op.f("ck_batches_voided_at_iff_voided"),
        "batches",
        "(status = 'voided') = (voided_at IS NOT NULL)",
    )

    op.add_column("code_fingerprints", sa.Column("part", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_code_fingerprints_part_positive"), "code_fingerprints", "part >= 1"
    )

    op.add_column("audit_log", sa.Column("target_batch_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_log_target_batch_id_batches"),
        "audit_log",
        "batches",
        ["target_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_audit_log_target_batch_id"), "audit_log", ["target_batch_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_target_batch_id"), table_name="audit_log")
    op.drop_constraint(
        op.f("fk_audit_log_target_batch_id_batches"), "audit_log", type_="foreignkey"
    )
    op.drop_column("audit_log", "target_batch_id")

    op.drop_constraint(
        op.f("ck_code_fingerprints_part_positive"), "code_fingerprints", type_="check"
    )
    op.drop_column("code_fingerprints", "part")

    op.drop_constraint(op.f("ck_batches_voided_at_iff_voided"), "batches", type_="check")
    op.drop_constraint(op.f("ck_batches_status"), "batches", type_="check")
    # A voided batch has no fingerprints left; before 0005 the nearest status is failed.
    op.execute("UPDATE batches SET status = 'failed' WHERE status = 'voided'")
    op.create_check_constraint(op.f("ck_batches_status"), "batches", _status_check(OLD_STATUSES))
    op.drop_constraint(op.f("fk_batches_voided_by_users"), "batches", type_="foreignkey")
    op.drop_column("batches", "voided_by")
    op.drop_column("batches", "voided_at")
    op.drop_constraint(op.f("ck_batches_chunk_rows_positive"), "batches", type_="check")
    op.drop_column("batches", "chunk_rows")
