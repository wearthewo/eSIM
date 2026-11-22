"""Web fixtures: the app on the test database, users, and a logged-in client.

All data is synthetic. Web tests need ``TEST_DATABASE_URL`` (they skip without it).
The app uses a real pooled engine (requests may run in parallel), so tables are
truncated before each test instead of rolling back a transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import accounts
from app.auth.passwords import hash_password
from app.auth.tokens import csrf_for
from app.db.base import Base
from app.db.models import Organization, Role, User
from app.mail import FakeMailer
from app.settings import Secret, Settings
from app.web.auth import CSRF_HEADER, SESSION_COOKIE
from app.web.main import create_app

PASSWORD = "correct horse battery staple"  # synthetic test password
# Not example.invalid: route tests check that customer emails (example.invalid) never appear.
EMAIL = "owner@org.test"
TEST_SECRET_KEY = "test-secret-key-for-signing-forms-only-0123456789"
TEST_MFA_KEY = "test-mfa-key-for-encrypting-totp-secrets-0123456789"
# Stands in for an encrypted TOTP secret of superadmins made by ``Accounts.user``: 2FA
# counts as on (so they are not sent to enrolment), but no code ever matches it.
PLACEHOLDER_MFA_SECRET = b"not-a-real-encrypted-secret-placeholder"


@pytest.fixture(scope="session")
def web_settings(db_engine: Engine) -> Settings:
    """Development settings on the test database (never the real ``.env`` values)."""
    return Settings(
        env="development",
        database_url=Secret(db_engine.url.render_as_string(hide_password=False)),
        secret_key=Secret(TEST_SECRET_KEY),
        fingerprint_key=Secret("test-fingerprint-key-0123456789abcdef"),
        mfa_key=Secret(TEST_MFA_KEY),
        public_base_url="http://testserver",
    )


@pytest.fixture(scope="session")
def web_sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def password_hash() -> str:
    """argon2id hash of ``PASSWORD``, computed once (hashing is deliberately slow)."""
    return hash_password(PASSWORD)


@pytest.fixture
def clean_db(db_engine: Engine) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def make_app(
    clean_db: None, web_settings: Settings, web_sessions: sessionmaker[Session]
) -> Callable[..., FastAPI]:
    def _make(
        settings: Settings | None = None,
        *,
        run_retention: bool = False,
        mailer: FakeMailer | None = None,
    ) -> FastAPI:
        # The daily purge is off by default so tests that count audit rows stay exact.
        # Email is off unless a FakeMailer is given: tests never build a real mailer.
        return create_app(
            settings or web_settings,
            sessions=web_sessions,
            run_retention=run_retention,
            mailer=mailer,
        )

    return _make


@pytest.fixture
def web_app(make_app: Callable[..., FastAPI]) -> FastAPI:
    return make_app()


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Accounts:
    """Create synthetic organizations, users and sessions directly in the database."""

    sessions: sessionmaker[Session]
    password_hash: str

    def org(self, name: str = "Example Org (test)", *, disabled: bool = False) -> int:
        with self.sessions.begin() as db:
            org = Organization(name=name, disabled=disabled)
            db.add(org)
            db.flush()
            return org.id

    def user(
        self,
        email: str = EMAIL,
        *,
        org_id: int | None = None,
        role: Role = Role.OWNER,
        superadmin: bool = False,
        disabled: bool = False,
        password_hash: str | None | bool = True,
        mfa: bool | None = None,
    ) -> int:
        """``password_hash=True`` uses the hash of ``PASSWORD``; ``None`` means no password.

        ``mfa`` (default: on for superadmins, who must have it) marks two-factor
        authentication as on with a placeholder secret that no code matches. Tests of
        2FA itself enrol through /account/2fa instead (``mfa=False``)."""
        if mfa is None:
            mfa = superadmin
        if org_id is None and not superadmin:
            org_id = self.org()
        with self.sessions.begin() as db:
            user = User(
                email=email,
                organization_id=org_id,
                role=role,
                is_superadmin=superadmin,
                disabled=disabled,
                password_hash=self.password_hash if password_hash is True else password_hash,
                mfa_secret=PLACEHOLDER_MFA_SECRET if mfa else None,
                mfa_enabled_at=now() if mfa else None,
            )
            db.add(user)
            db.flush()
            return user.id

    def session(self, user_id: int) -> str:
        with self.sessions.begin() as db:
            return accounts.create_session(db, user_id, now=now())

    def run(self, fn: Callable[[Session], Any]) -> Any:
        with self.sessions.begin() as db:
            return fn(db)

    def sql(self, statement: str, **params: Any) -> Any:
        with self.sessions.begin() as db:
            return db.execute(text(statement), params).all()


@pytest.fixture
def accts(clean_db: None, web_sessions: sessionmaker[Session], password_hash: str) -> Accounts:
    return Accounts(web_sessions, password_hash)


@dataclass
class LoggedIn:
    """A user with a live session on ``app``."""

    app: FastAPI
    user_id: int
    token: str

    @property
    def csrf(self) -> str:
        return csrf_for(self.token)

    @property
    def cookies(self) -> dict[str, str]:
        return {SESSION_COOKIE: self.token}

    @property
    def headers(self) -> dict[str, str]:
        return {CSRF_HEADER: self.csrf}

    def client(self, **kwargs: Any) -> TestClient:
        client = TestClient(self.app, **kwargs)
        client.cookies.set(SESSION_COOKIE, self.token)
        client.headers[CSRF_HEADER] = self.csrf
        return client


@pytest.fixture
def logged_in(web_app: FastAPI, accts: Accounts) -> LoggedIn:
    user_id = accts.user()
    return LoggedIn(web_app, user_id, accts.session(user_id))


@pytest.fixture
def client(logged_in: LoggedIn) -> Iterator[TestClient]:
    """A TestClient logged in as an organization owner (cookie + CSRF header)."""
    with logged_in.client() as c:
        yield c


ANON_IP = "203.0.113.7"  # TEST-NET-3, never a real client


@pytest.fixture
def browser(web_app: FastAPI) -> Callable[..., TestClient]:
    """``browser(ip=...)``: a new client, not logged in, that does not follow redirects."""

    def _make(ip: str = ANON_IP, app: FastAPI | None = None, **kwargs: Any) -> TestClient:
        return TestClient(app or web_app, follow_redirects=False, client=(ip, 50000), **kwargs)

    return _make


@pytest.fixture
def anon(browser: Callable[..., TestClient]) -> TestClient:
    return browser()
