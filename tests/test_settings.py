"""Settings: production secret checks, URL rewrite, and no secrets in repr/str/errors.

All secrets and passwords here are synthetic.
"""

from __future__ import annotations

import pickle

import pytest

from app.settings import (
    DRIVER_SCHEME,
    Secret,
    Settings,
    SettingsError,
    load_settings,
    normalize_database_url,
    safe_url,
)

PASSWORD = "Sup3r-S3cret-Pa55word"
DB_URL = f"postgres://esim:{PASSWORD}@db.example.invalid:5432/esim"
TEST_DB_URL = f"postgresql://esim:{PASSWORD}@db.example.invalid:5432/esim_test"
SECRET_KEY = "s" * 40
FINGERPRINT_KEY = "f" * 40


def _prod(**overrides: str | None) -> dict[str, str]:
    env = {
        "ENV": "production",
        "DATABASE_URL": DB_URL,
        "SECRET_KEY": SECRET_KEY,
        "FINGERPRINT_KEY": FINGERPRINT_KEY,
    }
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


# --- environment ------------------------------------------------------------------


def test_defaults_to_development_with_nothing_set() -> None:
    settings = load_settings({})
    assert settings == Settings()
    assert settings.env == "development"
    assert not settings.is_production


def test_development_allows_missing_and_dev_secrets() -> None:
    settings = load_settings({"SECRET_KEY": "dev-short", "FINGERPRINT_KEY": "dev-x"})
    assert settings.secret_key == Secret("dev-short")


@pytest.mark.parametrize("value", ["staging", "prod", "test"])
def test_unknown_env_is_rejected(value: str) -> None:
    with pytest.raises(SettingsError, match="ENV must be one of"):
        load_settings({"ENV": value})


def test_valid_production_settings() -> None:
    settings = load_settings(_prod())
    assert settings.is_production
    assert settings.secret_key is not None
    assert settings.secret_key.reveal() == SECRET_KEY
    assert settings.fingerprint_key is not None
    assert settings.fingerprint_key.reveal() == FINGERPRINT_KEY


def test_env_is_case_insensitive() -> None:
    assert load_settings(_prod(ENV="Production")).is_production


# --- production refuses weak secrets ------------------------------------------------


@pytest.mark.parametrize("name", ["SECRET_KEY", "FINGERPRINT_KEY"])
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "is missing"),
        ("", "is missing"),
        ("   ", "is missing"),
        ("x" * 31, "is shorter than 32 characters"),
        ("dev-" + "x" * 40, "starts with 'dev-'"),
    ],
    ids=["unset", "empty", "blank", "31-chars", "dev-prefix"],
)
def test_production_refuses_weak_secrets(name: str, value: str | None, reason: str) -> None:
    with pytest.raises(SettingsError) as info:
        load_settings(_prod(**{name: value}))
    message = str(info.value)
    assert f"{name} {reason}" in message
    if value and value.strip():
        assert value not in message


def test_production_reports_every_problem_at_once() -> None:
    with pytest.raises(SettingsError) as info:
        load_settings(_prod(SECRET_KEY=None, FINGERPRINT_KEY="dev-" + "x" * 40, DATABASE_URL=None))
    message = str(info.value)
    assert "SECRET_KEY is missing" in message
    assert "FINGERPRINT_KEY starts with 'dev-'" in message
    assert "DATABASE_URL is missing" in message


def test_exactly_32_characters_is_enough() -> None:
    assert load_settings(_prod(SECRET_KEY="x" * 32)).secret_key == Secret("x" * 32)


def test_production_errors_never_contain_the_database_password() -> None:
    with pytest.raises(SettingsError) as info:
        load_settings(_prod(SECRET_KEY="tiny-value"))
    assert PASSWORD not in str(info.value)
    assert "tiny-value" not in str(info.value)


# --- database URL rewrite -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+psycopg2://u:p@h/d", "postgresql+psycopg2://u:p@h/d"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
        ("postgres://h/postgres://x", "postgresql+psycopg://h/postgres://x"),  # prefix only
    ],
)
def test_normalize_database_url(url: str, expected: str) -> None:
    assert normalize_database_url(url) == expected


def test_both_database_urls_are_rewritten() -> None:
    settings = load_settings({"DATABASE_URL": DB_URL, "TEST_DATABASE_URL": TEST_DB_URL})
    assert settings.database_url is not None
    assert settings.test_database_url is not None
    assert settings.database_url.reveal().startswith(DRIVER_SCHEME)
    assert settings.test_database_url.reveal().startswith(DRIVER_SCHEME)


# --- secrets never shown ------------------------------------------------------------


def test_settings_never_expose_secrets_in_repr_or_str() -> None:
    settings = load_settings(_prod(TEST_DATABASE_URL=TEST_DB_URL))
    for text in (repr(settings), str(settings), f"{settings}", f"{settings!r}"):
        assert PASSWORD not in text
        assert SECRET_KEY not in text
        assert FINGERPRINT_KEY not in text
        assert "db.example.invalid" not in text  # the URL itself is masked too
    for secret in (settings.database_url, settings.secret_key, settings.fingerprint_key):
        assert secret is not None
        assert "**" in repr(secret)
        assert PASSWORD not in repr(secret)
        assert PASSWORD not in str(secret)


def test_safe_database_url_hides_the_password() -> None:
    settings = load_settings({"DATABASE_URL": DB_URL, "TEST_DATABASE_URL": TEST_DB_URL})
    assert (
        settings.safe_database_url == "postgresql+psycopg://esim:***@db.example.invalid:5432/esim"
    )
    assert settings.safe_test_database_url is not None
    assert PASSWORD not in settings.safe_test_database_url
    assert safe_url(None) is None


def test_secret_cannot_be_pickled() -> None:
    with pytest.raises(TypeError):
        pickle.dumps(Secret(PASSWORD))
