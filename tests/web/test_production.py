"""Production behaviour, as deployed on Render (render.yaml): the app refuses to start
without its keys, sends HSTS, uses ``__Host-`` Secure cookies, shows no debug pages,
and /healthz answers without a database or a login.

These tests need no database: ``DATABASE_URL`` points at a closed port. The session
cookie after a real login is checked in tests/web/test_auth.py
(``test_production_cookies``). All values are synthetic.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.settings import Settings, SettingsError, load_settings
from app.web.auth import PREAUTH_COOKIE
from app.web.main import HSTS_HEADERS, SECURITY_HEADERS, create_app

# Nothing listens on port 1; the timeout keeps a connection attempt short.
UNREACHABLE_DB = "postgresql://esim:synthetic-password@127.0.0.1:1/esim?connect_timeout=2"
BASE_URL = "https://esim.example.invalid"
HSTS = "max-age=63072000; includeSubDomains"
PROD_ENV = {
    "ENV": "production",
    "DATABASE_URL": UNREACHABLE_DB,
    "SECRET_KEY": "s" * 44,
    "FINGERPRINT_KEY": "f" * 44,
    "MFA_KEY": "m" * 44,
    "PUBLIC_BASE_URL": BASE_URL,
    "OPERATOR_NAME": "Synthetic Operator Ltd (test)",
    "CONTACT_EMAIL": "support@example.invalid",
}


def _prod_settings() -> Settings:
    return load_settings(PROD_ENV)


def _app(settings: Settings | None = None, *, run_retention: bool = False) -> FastAPI:
    return create_app(settings or _prod_settings(), run_retention=run_retention)


def _client(app: FastAPI, **kwargs) -> TestClient:
    return TestClient(app, base_url=BASE_URL, follow_redirects=False, **kwargs)


# --- Refuses to start ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "SECRET_KEY",
        "FINGERPRINT_KEY",
        "MFA_KEY",
        "DATABASE_URL",
        "PUBLIC_BASE_URL",
        "OPERATOR_NAME",
        "CONTACT_EMAIL",
    ],
)
def test_refuses_to_start_with_a_missing_setting(monkeypatch, name: str) -> None:
    """``uvicorn app.web.main:app`` builds the app from the environment (get_settings)."""
    for key in (*PROD_ENV, "TEST_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in PROD_ENV.items():
        if key != name:
            monkeypatch.setenv(key, value)
    settings_module.get_settings.cache_clear()
    try:
        with pytest.raises(SettingsError) as info:
            create_app()
    finally:
        settings_module.get_settings.cache_clear()
    message = str(info.value)
    assert message.startswith("Refusing to start in production")
    assert f"{name} is missing" in message
    assert "synthetic-password" not in message
    assert "s" * 44 not in message


# --- HSTS -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/login", "/static/app.css", "/no-such-page"])
def test_hsts_and_security_headers_in_production(path: str) -> None:
    response = _client(_app()).get(path)
    assert response.headers["Strict-Transport-Security"] == HSTS
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_no_hsts_in_development() -> None:
    dev = load_settings({**PROD_ENV, "ENV": "development"})
    response = TestClient(create_app(dev, run_retention=False)).get("/healthz")
    assert response.status_code == 200
    assert "Strict-Transport-Security" not in response.headers
    assert set(HSTS_HEADERS).isdisjoint(SECURITY_HEADERS)


# --- Cookies --------------------------------------------------------------------------------


def test_preauth_cookie_is_host_prefixed_and_secure() -> None:
    response = _client(_app()).get("/login")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"__Host-{PREAUTH_COOKIE}=")
    for part in ("Secure", "HttpOnly", "Path=/", "SameSite=strict"):
        assert part in cookie
    assert "Domain" not in cookie


# --- No debug pages ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_no_api_docs_in_production(path: str) -> None:
    assert _client(_app()).get(path).status_code == 404


def test_errors_are_generic_in_production() -> None:
    app = _app()
    assert app.debug is False

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("row 2 LPA:1$smdp.example.invalid$TEST-0001-AAAA-BBBB")

    response = _client(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["Strict-Transport-Security"] == HSTS


# --- /healthz -----------------------------------------------------------------------------------


def test_healthz_needs_no_database_and_no_login(caplog: pytest.LogCaptureFixture) -> None:
    """Render's health check: the app starts (lifespan included) and answers even though
    the database cannot be reached; the failed startup purge is logged by class only."""
    caplog.set_level(logging.WARNING, logger="app.web.main")
    with _client(_app(run_retention=True)) as c:
        response = c.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "retention purge failed (OperationalError)" in caplog.text
    assert "synthetic-password" not in caplog.text
    assert "127.0.0.1" not in caplog.text
