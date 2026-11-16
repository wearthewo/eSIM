"""Database fixtures. All data is synthetic.

Tests that use ``db_engine`` or ``db`` run against ``TEST_DATABASE_URL`` (from the
environment or ``.env``) and are skipped when it is not set. The schema is built
once per test session with Alembic; each test runs in a transaction that is
rolled back afterwards, so tests never see each other's rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.engine import make_engine
from app.settings import get_settings

ROOT = Path(__file__).resolve().parents[2]
SKIP_MESSAGE = "TEST_DATABASE_URL is not set; skipping database tests"


def alembic_config(connection: Connection) -> Config:
    """Alembic config that migrates ``connection`` (not DATABASE_URL)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False  # leave pytest's logging alone
    return cfg


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    settings = get_settings()
    if settings.test_database_url is None:
        pytest.skip(SKIP_MESSAGE)
    test_url = make_url(settings.test_database_url.reveal())
    if settings.database_url is not None:
        dev_url = make_url(settings.database_url.reveal())
        same = (test_url.host, test_url.port, test_url.database) == (
            dev_url.host,
            dev_url.port,
            dev_url.database,
        )
        if same:
            pytest.fail("TEST_DATABASE_URL points at the DATABASE_URL database; refusing to run")

    engine = make_engine(settings.test_database_url.reveal())
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    """A session inside an outer transaction that is rolled back after the test.

    ``session.commit()`` / ``rollback()`` only act on a savepoint. Use
    ``session.begin_nested()`` around a statement that is expected to fail.
    """
    with db_engine.connect() as conn:
        outer = conn.begin()
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            outer.rollback()
