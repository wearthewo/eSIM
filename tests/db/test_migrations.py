"""The Alembic migrations build exactly the schema the models describe."""

from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect

from app.db.base import Base
from tests.db.conftest import ROOT, alembic_config

TABLES = {
    "organizations",
    "users",
    "invites",
    "password_tokens",
    "sessions",
    "access_requests",
    "branding",
    "batches",
    "batch_parts",
    "code_fingerprints",
    "audit_log",
    "usage",
}

# Column-name fragments that would mean storing data the database must never hold.
FORBIDDEN_COLUMN_FRAGMENTS = (
    "activation",
    "matching",
    "confirmation",
    "customer",
    "pdf",
    "upload",
    "file",
    "lpa",
)
# Columns allowed despite a forbidden fragment, each with the reason.
ALLOWED_COLUMNS = {
    # The uploaded file's sanitized name for the History page (requested), never its content.
    "batches.file_name",
}


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names()) - {"alembic_version"}


def _diff(conn: Connection) -> list:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return compare_metadata(ctx, Base.metadata)


def test_models_define_the_expected_tables() -> None:
    assert set(Base.metadata.tables) == TABLES


def test_upgrade_and_downgrade_cleanly(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        cfg = alembic_config(conn)

        command.downgrade(cfg, "base")
        assert _tables(conn) == set()

        command.upgrade(cfg, "head")
        assert _tables(conn) == TABLES
        assert _diff(conn) == []  # the migration matches the models exactly

        command.downgrade(cfg, "base")
        assert _tables(conn) == set()

        command.upgrade(cfg, "head")  # leave the schema in place for the other tests
        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert MigrationContext.configure(conn).get_current_revision() == head


def test_single_migration_head() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_no_column_could_hold_codes_files_or_customer_data() -> None:
    bad = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(fragment in column.name for fragment in FORBIDDEN_COLUMN_FRAGMENTS)
        and f"{table.name}.{column.name}" not in ALLOWED_COLUMNS
    ]
    assert bad == []
