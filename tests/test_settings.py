"""Settings: production secret checks, URL rewrite, and no secrets in repr/str/errors.

All secrets and passwords here are synthetic.
"""

from __future__ import annotations

import base64
import pickle
import re
import secrets

import pytest

from app.settings import (
    DEFAULT_PUBLIC_BASE_URL,
    DRIVER_SCHEME,
    Secret,
    Settings,
    SettingsError,
    check_web_settings,
    load_settings,
    normalize_database_url,
    safe_url,
)

PASSWORD = "Sup3r-S3cret-Pa55word"
DB_URL = f"postgres://esim:{PASSWORD}@db.example.invalid:5432/esim"
TEST_DB_URL = f"postgresql://esim:{PASSWORD}@db.example.invalid:5432/esim_test"
SECRET_KEY = "s" * 40
FINGERPRINT_KEY = "f" * 40
MFA_KEY = "m" * 40
OPERATOR_NAME = "Synthetic Operator Ltd (test)"
CONTACT_EMAIL = "support@example.invalid"


def _prod(**overrides: str | None) -> dict[str, str]:
    env = {
        "ENV": "production",
        "DATABASE_URL": DB_URL,
        "SECRET_KEY": SECRET_KEY,
        "FINGERPRINT_KEY": FINGERPRINT_KEY,
        "MFA_KEY": MFA_KEY,
        "PUBLIC_BASE_URL": "https://esim.example.invalid",
        "OPERATOR_NAME": OPERATOR_NAME,
        "CONTACT_EMAIL": CONTACT_EMAIL,
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
    assert settings.mfa_key is not None
    assert settings.mfa_key.reveal() == MFA_KEY
    assert MFA_KEY not in repr(settings)


def test_env_is_case_insensitive() -> None:
    assert load_settings(_prod(ENV="Production")).is_production


# --- production refuses weak secrets ------------------------------------------------


@pytest.mark.parametrize("name", ["SECRET_KEY", "FINGERPRINT_KEY", "MFA_KEY"])
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


def test_render_generated_keys_pass_the_production_checks() -> None:
    """render.yaml uses ``generateValue: true`` for the three keys: Render sets each to a
    random 256-bit value, base64-encoded (44 characters, alphabet ``A-Za-z0-9+/=``), so
    it is long enough and can never start with ``dev-`` (``-`` is not base64)."""
    for _ in range(50):
        keys = {
            name: base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            for name in ("SECRET_KEY", "FINGERPRINT_KEY", "MFA_KEY")
        }
        assert {len(v) for v in keys.values()} == {44}
        settings = load_settings(_prod(**keys))
        assert settings.mfa_key == Secret(keys["MFA_KEY"])


RESEND_KEY = "re_synthetic_test_key_0123456789abcdef"
SENDER = "eSIM Sheet <no-reply@mail.example.invalid>"


def test_email_is_off_by_default() -> None:
    assert load_settings({}).email_backend == "none"
    assert not load_settings(_prod()).email_enabled


def test_resend_settings_are_read() -> None:
    settings = load_settings(
        _prod(
            EMAIL_BACKEND="Resend",
            RESEND_API_KEY=RESEND_KEY,
            EMAIL_FROM=SENDER,
            ADMIN_NOTIFY_EMAIL="admin.notify@org.test",
        )
    )
    assert settings.email_enabled and settings.email_backend == "resend"
    assert settings.resend_api_key == Secret(RESEND_KEY)
    assert settings.email_from == SENDER
    assert settings.admin_notify_email == "admin.notify@org.test"
    assert RESEND_KEY not in repr(settings)
    bare = load_settings({"EMAIL_BACKEND": "resend", "RESEND_API_KEY": "k", "EMAIL_FROM": "a@b.co"})
    assert bare.email_from == "a@b.co" and bare.admin_notify_email is None
    assert bare.email_reply_to is None


@pytest.mark.parametrize(
    "value", ["support@esimsheet.example", "eSIM Sheet Support <support@esimsheet.example>"]
)
def test_email_reply_to_is_read(value: str) -> None:
    settings = load_settings(_prod(EMAIL_REPLY_TO=f" {value} "))
    assert settings.email_reply_to == value
    assert load_settings({"EMAIL_REPLY_TO": "  "}).email_reply_to is None


@pytest.mark.parametrize("production", [True, False], ids=["production", "development"])
@pytest.mark.parametrize("missing", ["RESEND_API_KEY", "EMAIL_FROM"])
def test_resend_needs_key_and_sender(production: bool, missing: str) -> None:
    env = {"EMAIL_BACKEND": "resend", "RESEND_API_KEY": RESEND_KEY, "EMAIL_FROM": SENDER}
    del env[missing]
    with pytest.raises(SettingsError) as info:
        load_settings(_prod(**env) if production else env)
    message = str(info.value)
    assert f"{missing} is missing (EMAIL_BACKEND is resend)" in message
    if production:
        assert message.startswith("Refusing to start in production")
    assert RESEND_KEY not in message


def test_unknown_email_backend_is_refused() -> None:
    with pytest.raises(SettingsError, match="EMAIL_BACKEND must be one of none, resend"):
        load_settings({"EMAIL_BACKEND": "smtp"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EMAIL_FROM", "not an address"),
        ("EMAIL_FROM", 'Name "quoted" <a@b.co>'),
        ("EMAIL_FROM", "Name <a@b.co>\nBcc: x@y.co"),
        ("EMAIL_FROM", "Name <a@b>"),
        ("EMAIL_REPLY_TO", "support at example"),
        ("EMAIL_REPLY_TO", "Support <support@example.invalid>\r\nBcc: x@y.co"),
        ("EMAIL_REPLY_TO", 'Support "team" <support@example.invalid>'),
        ("ADMIN_NOTIFY_EMAIL", "nobody"),
        ("ADMIN_NOTIFY_EMAIL", "Name <a@b.co>"),
    ],
)
def test_malformed_addresses_are_refused(name: str, value: str) -> None:
    with pytest.raises(SettingsError) as info:
        load_settings({name: value})
    assert name in str(info.value)
    assert value not in str(info.value)


# --- operator and contact (/privacy, /terms) ---------------------------------------------


def test_operator_and_contact_are_read() -> None:
    settings = load_settings(_prod(OPERATOR_NAME=f"  {OPERATOR_NAME} "))
    assert settings.operator_name == OPERATOR_NAME
    assert settings.contact_email == CONTACT_EMAIL


def test_development_allows_operator_and_contact_unset() -> None:
    settings = load_settings({"OPERATOR_NAME": " ", "CONTACT_EMAIL": ""})
    assert settings.operator_name is None
    assert settings.contact_email is None


@pytest.mark.parametrize("name", ["OPERATOR_NAME", "CONTACT_EMAIL"])
@pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_production_requires_operator_and_contact(name: str, value: str | None) -> None:
    with pytest.raises(SettingsError) as info:
        load_settings(_prod(**{name: value}))
    message = str(info.value)
    assert message.startswith("Refusing to start in production")
    assert f"{name} is missing" in message


@pytest.mark.parametrize("production", [False, True], ids=["development", "production"])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CONTACT_EMAIL", "support at example"),
        ("CONTACT_EMAIL", "Support <support@example.invalid>"),
        ("CONTACT_EMAIL", "support@example"),
        ("OPERATOR_NAME", "Operator\tLtd"),
        ("OPERATOR_NAME", "x" * 201),
    ],
    ids=["no-at", "display-name", "no-tld", "control-char", "too-long"],
)
def test_malformed_operator_or_contact_is_refused(production: bool, name: str, value: str) -> None:
    env = _prod(**{name: value}) if production else {name: value}
    with pytest.raises(SettingsError) as info:
        load_settings(env)
    message = str(info.value)
    assert name in message
    assert value not in message


def test_bootstrap_superadmin_email_is_read_and_blank_is_unset() -> None:
    assert load_settings({}).bootstrap_superadmin_email is None
    assert load_settings({"BOOTSTRAP_SUPERADMIN_EMAIL": "  "}).bootstrap_superadmin_email is None
    settings = load_settings(_prod(BOOTSTRAP_SUPERADMIN_EMAIL=" first.admin@org.test "))
    assert settings.bootstrap_superadmin_email == "first.admin@org.test"


def test_production_errors_never_contain_the_database_password() -> None:
    with pytest.raises(SettingsError) as info:
        load_settings(_prod(SECRET_KEY="tiny-value"))
    assert PASSWORD not in str(info.value)
    assert "tiny-value" not in str(info.value)


# --- PUBLIC_BASE_URL and the web app ---------------------------------------------------


def test_public_base_url_defaults_to_local_in_development() -> None:
    assert load_settings({}).public_base_url == DEFAULT_PUBLIC_BASE_URL
    assert not load_settings({}).cookie_secure


def test_public_base_url_trailing_slash_is_stripped() -> None:
    settings = load_settings(_prod(PUBLIC_BASE_URL="https://esim.example.invalid/"))
    assert settings.public_base_url == "https://esim.example.invalid"
    assert settings.cookie_secure


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "PUBLIC_BASE_URL is missing"),
        ("http://esim.example.invalid", "PUBLIC_BASE_URL must be an https URL"),
        ("esim.example.invalid", "PUBLIC_BASE_URL must be an https URL"),
        ("https://", "PUBLIC_BASE_URL must be an https URL"),
    ],
)
def test_production_requires_https_public_base_url(value: str | None, reason: str) -> None:
    with pytest.raises(SettingsError, match=reason):
        load_settings(_prod(PUBLIC_BASE_URL=value))


@pytest.mark.parametrize("value", ["ftp://x.example.invalid", "localhost:8000", "http://"])
def test_development_rejects_malformed_public_base_url(value: str) -> None:
    with pytest.raises(SettingsError, match="PUBLIC_BASE_URL"):
        load_settings({"PUBLIC_BASE_URL": value})


def test_development_accepts_http_public_base_url() -> None:
    settings = load_settings({"PUBLIC_BASE_URL": "http://127.0.0.1:8000/"})
    assert settings.public_base_url == "http://127.0.0.1:8000"


def test_web_app_needs_database_and_secret_key_in_every_env() -> None:
    with pytest.raises(SettingsError) as info:
        check_web_settings(load_settings({}))
    assert "DATABASE_URL is missing" in str(info.value)
    assert "SECRET_KEY is missing" in str(info.value)
    assert "FINGERPRINT_KEY is missing" in str(info.value)
    assert "MFA_KEY is missing" in str(info.value)
    with pytest.raises(SettingsError, match="FINGERPRINT_KEY is missing"):
        check_web_settings(load_settings({"DATABASE_URL": DB_URL, "SECRET_KEY": "dev-short"}))
    dev = {"DATABASE_URL": DB_URL, "SECRET_KEY": "dev-short", "FINGERPRINT_KEY": "x"}
    with pytest.raises(SettingsError, match="MFA_KEY is missing"):  # superadmins need 2FA
        check_web_settings(load_settings(dev))
    check_web_settings(load_settings({**dev, "MFA_KEY": "dev-mfa"}))
    check_web_settings(load_settings(_prod()))


def test_create_app_refuses_bad_production_settings(monkeypatch) -> None:
    """create_app() reads get_settings(), so bad production settings stop the app."""
    from app import settings as settings_module
    from app.web.main import create_app

    for name, value in _prod(SECRET_KEY="dev-" + "x" * 40).items():
        monkeypatch.setenv(name, value)
    settings_module.get_settings.cache_clear()
    try:
        with pytest.raises(SettingsError, match="SECRET_KEY starts with 'dev-'"):
            create_app()
    finally:
        settings_module.get_settings.cache_clear()


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


# --- performance settings and limits -----------------------------------------------


def test_performance_defaults() -> None:
    settings = load_settings({})
    assert settings.generate_slots == 1
    assert settings.generate_wait_seconds == 60
    assert settings.upload_cache_mb == 100
    assert settings.upload_cache_ttl_minutes == 30
    assert settings.max_rows == 10_000
    assert settings.max_upload_mb == 5 and settings.max_upload_bytes == 5 * 1024 * 1024


def test_limits_default_to_the_product_constants() -> None:
    from app.core.parser import MAX_ROWS
    from app.web.routes import MAX_UPLOAD_BYTES

    assert Settings().max_rows == MAX_ROWS
    assert Settings().max_upload_bytes == MAX_UPLOAD_BYTES


@pytest.mark.parametrize("env", [_prod(), {}], ids=["production", "development"])
def test_performance_settings_are_read(env: dict[str, str]) -> None:
    settings = load_settings(
        {
            **env,
            "GENERATE_SLOTS": "2",
            "GENERATE_WAIT_SECONDS": "120",
            "UPLOAD_CACHE_MB": "0",
            "UPLOAD_CACHE_TTL_MINUTES": " 45 ",
            "MAX_ROWS": "100000",
            "MAX_UPLOAD_MB": "64",
        }
    )
    assert settings.generate_slots == 2
    assert settings.generate_wait_seconds == 120
    assert settings.upload_cache_mb == 0  # the cache is off
    assert settings.upload_cache_ttl_minutes == 45
    assert settings.max_rows == 100_000
    assert settings.max_upload_bytes == 64 * 1024 * 1024


def test_blank_performance_settings_use_the_defaults() -> None:
    assert load_settings({"GENERATE_SLOTS": "", "MAX_ROWS": "  "}) == Settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GENERATE_SLOTS", "0"),
        ("GENERATE_SLOTS", "17"),
        ("GENERATE_SLOTS", "-1"),
        ("GENERATE_SLOTS", "1.5"),
        ("GENERATE_SLOTS", "two"),
        ("GENERATE_SLOTS", "\u0662"),  # ARABIC-INDIC DIGIT TWO: not ASCII
        ("GENERATE_WAIT_SECONDS", "0"),
        ("GENERATE_WAIT_SECONDS", "601"),
        ("GENERATE_WAIT_SECONDS", "0.5"),
        ("UPLOAD_CACHE_MB", "-1"),
        ("UPLOAD_CACHE_MB", "5000"),
        ("UPLOAD_CACHE_TTL_MINUTES", "0"),
        ("MAX_ROWS", "0"),
        ("MAX_ROWS", "1000001"),
        ("MAX_UPLOAD_MB", "0"),
        ("MAX_UPLOAD_MB", "2048"),
    ],
)
def test_bad_performance_settings_are_rejected(name: str, value: str) -> None:
    with pytest.raises(SettingsError) as raised:
        load_settings({name: value})
    # The message names the variable and its range, never the value given.
    assert re.fullmatch(rf"{name} must be a whole number from \d+ to \d+", str(raised.value))
