"""Sync engine and session factory.

The web layer will call these from the threadpool. ``hide_parameters=True`` keeps
bound values (emails, hashes) out of SQLAlchemy error messages and logs; the
engine's ``repr`` already hides the password.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.settings import SettingsError, get_settings


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine for ``url`` with the project defaults."""
    options: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True, "echo": False}
    options.update(kwargs)
    return create_engine(url, **options)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine for ``DATABASE_URL`` (created on first use)."""
    url = get_settings().database_url
    if url is None:
        raise SettingsError("DATABASE_URL is not set")
    return make_engine(url.reveal())


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session in a transaction: committed on success, rolled back on error, closed."""
    with get_sessionmaker().begin() as session:
        yield session
