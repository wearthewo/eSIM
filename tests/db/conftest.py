"""Database fixtures. All data is synthetic.

``db_engine`` (in tests/conftest.py) runs against ``TEST_DATABASE_URL`` and skips
when it is not set. Each test here runs in a transaction that is rolled back
afterwards, so tests never see each other's rows.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tests.conftest import ROOT, alembic_config

__all__ = ["ROOT", "alembic_config"]


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
