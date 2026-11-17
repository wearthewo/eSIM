"""Application settings, read from environment variables.

In development, variables missing from the real environment are filled from a
``.env`` file (python-dotenv); real environment variables always win. In
production no ``.env`` is read, and weak secrets stop the app from starting.

Secret values and database URLs are wrapped in ``Secret``: they never appear in
``repr``/``str``, error messages or logs. Read them with ``.reveal()`` only where
they are used (the engine, the HMAC key).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

DOTENV_PATH: Path = Path(__file__).resolve().parents[1] / ".env"  # project root

Env = Literal["development", "production"]
ENVS: tuple[Env, ...] = ("development", "production")

MIN_SECRET_CHARS = 32
DEV_SECRET_PREFIX = "dev-"  # noqa: S105  (a prefix to reject, not a secret)
DRIVER_SCHEME = "postgresql+psycopg://"
_REWRITE_SCHEMES = ("postgresql://", "postgres://")
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8000"
_MASK = "**********"


class SettingsError(RuntimeError):
    """Settings are missing or invalid. Messages name variables, never their values."""


class Secret:
    """A string that never shows itself in ``repr``/``str``. Use ``reveal()``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"Secret('{_MASK}')"

    def __str__(self) -> str:
        return _MASK

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __reduce__(self) -> object:
        raise TypeError("Secret values cannot be pickled")


def normalize_database_url(url: str) -> str:
    """Point plain PostgreSQL URLs (as Render gives them) at the psycopg 3 driver.

    ``postgres://`` and ``postgresql://`` become ``postgresql+psycopg://``; any other
    URL (one that already names a driver, or not PostgreSQL) is returned unchanged.
    """
    for scheme in _REWRITE_SCHEMES:
        if url.startswith(scheme):
            return DRIVER_SCHEME + url[len(scheme) :]
    return url


def safe_url(url: Secret | None) -> str | None:
    """The URL with its password replaced by ``***``, for display. ``None`` stays ``None``."""
    if url is None:
        return None
    try:
        return make_url(url.reveal()).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable database URL>"


@dataclass(frozen=True, slots=True)
class Settings:
    env: Env = "development"
    database_url: Secret | None = None
    test_database_url: Secret | None = None
    secret_key: Secret | None = None
    fingerprint_key: Secret | None = None
    # Where users reach the app; the admin CLI builds invite and reset links from it.
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def cookie_secure(self) -> bool:
        """``Secure`` + ``__Host-`` cookies. Off in development, which runs on plain http."""
        return self.is_production

    @property
    def safe_database_url(self) -> str | None:
        return safe_url(self.database_url)

    @property
    def safe_test_database_url(self) -> str | None:
        return safe_url(self.test_database_url)


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    return value or None


def _secret(environ: Mapping[str, str], name: str) -> Secret | None:
    value = _optional(environ, name)
    return Secret(value) if value is not None else None


def _url(environ: Mapping[str, str], name: str) -> Secret | None:
    value = _optional(environ, name)
    return Secret(normalize_database_url(value)) if value is not None else None


def _secret_problem(name: str, value: Secret | None) -> str | None:
    if value is None:
        return f"{name} is missing"
    if len(value) < MIN_SECRET_CHARS:
        return f"{name} is shorter than {MIN_SECRET_CHARS} characters"
    if value.reveal().startswith(DEV_SECRET_PREFIX):
        return f"{name} starts with '{DEV_SECRET_PREFIX}' (a development value)"
    return None


def _base_url_problem(value: str | None, *, production: bool) -> str | None:
    """Production needs an ``https://`` URL; development also accepts ``http://``."""
    if value is None:
        return "PUBLIC_BASE_URL is missing" if production else None
    parts = urlsplit(value)
    schemes = ("https",) if production else ("http", "https")
    if parts.scheme not in schemes or not parts.netloc or parts.query or parts.fragment:
        return f"PUBLIC_BASE_URL must be an {' or '.join(schemes)} URL"
    return None


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build ``Settings`` from ``environ``. Raises ``SettingsError`` if they are unusable.

    Pure: reads nothing but ``environ`` (no ``.env``), so tests can pass a dict.
    """
    env = (_optional(environ, "ENV") or "development").lower()
    if env not in ENVS:
        raise SettingsError(f"ENV must be one of {', '.join(ENVS)}")
    production = env == "production"

    base_url = _optional(environ, "PUBLIC_BASE_URL")
    if base_url is not None:
        base_url = base_url.rstrip("/")
    base_url_problem = _base_url_problem(base_url, production=production)
    if base_url_problem is not None and not production:
        raise SettingsError(base_url_problem)

    settings = Settings(
        env=env,  # type: ignore[arg-type]  # checked against ENVS above
        database_url=_url(environ, "DATABASE_URL"),
        test_database_url=_url(environ, "TEST_DATABASE_URL"),
        secret_key=_secret(environ, "SECRET_KEY"),
        fingerprint_key=_secret(environ, "FINGERPRINT_KEY"),
        public_base_url=base_url or DEFAULT_PUBLIC_BASE_URL,
    )

    if production:
        problems = [
            p
            for p in (
                _secret_problem("SECRET_KEY", settings.secret_key),
                _secret_problem("FINGERPRINT_KEY", settings.fingerprint_key),
                "DATABASE_URL is missing" if settings.database_url is None else None,
                base_url_problem,
            )
            if p is not None
        ]
        if problems:
            raise SettingsError("Refusing to start in production: " + "; ".join(problems))
    return settings


def check_web_settings(settings: Settings) -> None:
    """Raise ``SettingsError`` unless the web app can run: it needs the database, a
    ``SECRET_KEY`` and a ``FINGERPRINT_KEY`` (the duplicate check) in every environment
    (production strength is checked by ``load_settings``)."""
    problems = [
        p
        for p in (
            "DATABASE_URL is missing" if settings.database_url is None else None,
            "SECRET_KEY is missing" if settings.secret_key is None else None,
            "FINGERPRINT_KEY is missing" if settings.fingerprint_key is None else None,
        )
        if p is not None
    ]
    if problems:
        raise SettingsError("Refusing to start: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings for this process (cached). Reads ``.env`` unless ``ENV=production``."""
    if os.environ.get("ENV", "").strip().lower() != "production":
        from dotenv import load_dotenv

        load_dotenv(DOTENV_PATH, override=False)
    return load_settings(os.environ)
