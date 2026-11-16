"""Alembic environment.

The database URL comes from ``app.settings`` (``DATABASE_URL``), never from
alembic.ini, and is never logged. Callers (the tests) may pass an open connection
in ``config.attributes["connection"]`` to migrate another database.
Online mode only: migrations always run against a live database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.db.engine import make_engine
from app.settings import SettingsError, get_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = get_settings().database_url
    if url is None:
        raise SettingsError("DATABASE_URL is not set")
    engine = make_engine(url.reveal(), pool_pre_ping=False)
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported; run against a database.")
run_migrations_online()
