"""/privacy and /terms: public, CSP-clean, and in step with the code.

Every period on the pages is in a ``data-period`` span. These tests read each one back
into a ``timedelta`` and compare it with the constant or setting that enforces it, so the
pages cannot drift from the code. The page tests need no database (``DATABASE_URL``
points at a closed port); the footer and consent-line tests at the end use the test
database (they skip without ``TEST_DATABASE_URL``). All values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import accounts
from app.auth.accounts import LINK_TTL, RESET_LINK_TTL, SESSION_ABSOLUTE_TTL, SESSION_IDLE_TTL
from app.auth.retention import ACCESS_REQUEST_TTL, AUDIT_LOG_TTL, SPENT_LINK_TTL
from app.auth.tokens import FORM_TOKEN_MAX_AGE_SECONDS, MFA_STATE_MAX_AGE_SECONDS
from app.batches import DUPLICATE_WINDOW, STALE_AFTER
from app.db.ip import IPV4_PREFIX_LEN, IPV6_PREFIX_LEN
from app.db.models import DEFAULT_DAILY_ROW_QUOTA, Role
from app.mail import FakeMailer
from app.settings import DEFAULT_UPLOAD_CACHE_TTL_MINUTES, Settings, load_settings
from app.web.auth import MFA_COOKIE, PREAUTH_COOKIE, SESSION_COOKIE
from app.web.legal import LAST_UPDATED, duration
from app.web.main import create_app
from app.web.templating import TEMPLATES_DIR
from tests.web.conftest import Accounts, now
from tests.web.test_templates import _assert_local_urls, _assert_no_inline_code

UNREACHABLE_DB = "postgresql://esim:synthetic-password@127.0.0.1:1/esim?connect_timeout=2"
OPERATOR = "Synthetic Operator Ltd (test)"
CONTACT = "support@example.invalid"
DEV_ENV = {
    "DATABASE_URL": UNREACHABLE_DB,
    "SECRET_KEY": "s" * 44,
    "FINGERPRINT_KEY": "f" * 44,
    "MFA_KEY": "m" * 44,
}
PROD_ENV = {
    **DEV_ENV,
    "ENV": "production",
    "PUBLIC_BASE_URL": "https://esim.example.invalid",
    "OPERATOR_NAME": OPERATOR,
    "CONTACT_EMAIL": CONTACT,
}
PERIOD = re.compile(r'<span data-period="([a-z_]+)">([^<]*)</span>')
LIMIT = re.compile(r'<span data-limit="([a-z_]+)">([^<]*)</span>')
UNITS = {
    "minute": dt.timedelta(minutes=1),
    "hour": dt.timedelta(hours=1),
    "day": dt.timedelta(days=1),
}


def _client(env: dict[str, str]) -> TestClient:
    settings = load_settings(env)
    base_url = "https://esim.example.invalid" if settings.is_production else "http://testserver"
    return TestClient(create_app(settings, run_retention=False), base_url=base_url)


def _expected(settings: Settings) -> dict[str, dt.timedelta]:
    """What each ``data-period`` key must say: the code's own values."""
    return {
        "access_requests": ACCESS_REQUEST_TTL,
        "audit_log": AUDIT_LOG_TTL,
        "session_idle": SESSION_IDLE_TTL,
        "session_absolute": SESSION_ABSOLUTE_TTL,
        "invite_link": LINK_TTL,
        "reset_link": RESET_LINK_TTL,
        "spent_links": SPENT_LINK_TTL,
        "validated_batches": STALE_AFTER,
        "fingerprints": DUPLICATE_WINDOW,
        "upload_cache": dt.timedelta(minutes=settings.upload_cache_ttl_minutes),
        "preauth_cookie": dt.timedelta(seconds=FORM_TOKEN_MAX_AGE_SECONDS),
        "mfa_cookie": dt.timedelta(seconds=MFA_STATE_MAX_AGE_SECONDS),
    }


def _parse(text: str) -> dt.timedelta:
    """ "30 minutes", "1 hour", "7 days", "12 months" back into a timedelta."""
    match = re.fullmatch(r"(\d+) (minute|hour|day|month)s?", text)
    assert match, f"not a period: {text!r}"
    n, unit = int(match.group(1)), match.group(2)
    if unit == "month":
        assert n % 12 == 0, f"{text!r}: months are only used for whole years"
        return dt.timedelta(days=365 * n // 12)
    return n * UNITS[unit]


def _periods(html: str) -> dict[str, set[dt.timedelta]]:
    found: dict[str, set[dt.timedelta]] = {}
    for key, text in PERIOD.findall(html):
        found.setdefault(key, set()).add(_parse(text))
    return found


# --- Public and clean -------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/privacy", "/terms"])
def test_pages_are_public_and_clean(url: str) -> None:
    with _client(PROD_ENV) as c:
        response = c.get(url, follow_redirects=False)
    assert response.status_code == 200
    _assert_no_inline_code(response.text, url)
    _assert_local_urls(response.text, url)
    assert "set-cookie" not in response.headers


# --- The page matches the code ------------------------------------------------------------


@pytest.mark.parametrize("env", [DEV_ENV, PROD_ENV], ids=["development", "production"])
def test_privacy_periods_match_the_code(env: dict[str, str]) -> None:
    settings = load_settings(env)
    with _client(env) as c:
        html = c.get("/privacy").text
    found = _periods(html)
    expected = _expected(settings)
    assert set(found) == set(expected), "a period is missing from /privacy or unknown"
    for key, value in expected.items():
        assert found[key] == {value}, f"{key}: page says {found[key]}, code says {value}"


def test_cache_minutes_come_from_the_setting() -> None:
    assert DEFAULT_UPLOAD_CACHE_TTL_MINUTES != 45
    with _client({**DEV_ENV, "UPLOAD_CACHE_TTL_MINUTES": "45"}) as c:
        html = c.get("/privacy").text
    assert _periods(html)["upload_cache"] == {dt.timedelta(minutes=45)}
    assert '<span data-period="upload_cache">45 minutes</span>' in html


def test_privacy_shows_the_ip_prefix_lengths() -> None:
    with _client(DEV_ENV) as c:
        html = c.get("/privacy").text
    assert f"/{IPV4_PREFIX_LEN} for IPv4" in html
    assert f"/{IPV6_PREFIX_LEN} for IPv6" in html


@pytest.mark.parametrize(
    ("env", "prefix"), [(DEV_ENV, ""), (PROD_ENV, "__Host-")], ids=["development", "production"]
)
def test_cookie_table_names_the_cookies_this_server_sets(env: dict[str, str], prefix: str) -> None:
    with _client(env) as c:
        html = c.get("/privacy").text
    for base in (SESSION_COOKIE, PREAUTH_COOKIE, MFA_COOKIE):
        assert f"<code>{prefix}{base}</code>" in html
    if not prefix:
        assert "__Host-" not in html


def test_terms_limits_match_the_code() -> None:
    env = {**DEV_ENV, "MAX_ROWS": "1234", "MAX_UPLOAD_MB": "7"}
    with _client(env) as c:
        html = c.get("/terms").text
    limits = dict(LIMIT.findall(html))
    assert limits == {
        "daily_quota": f"{DEFAULT_DAILY_ROW_QUOTA:,}",
        "max_rows": "1,234",
        "max_upload_mb": "7",
    }


@pytest.mark.parametrize("name", ["privacy.html", "terms.html", "_legal.html"])
def test_templates_type_no_periods(name: str) -> None:
    """A period typed into the template (not from the code) would escape the tests above."""
    source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    source = re.sub(r"\{#.*?#\}|\{\{.*?\}\}|\{%.*?%\}", "", source, flags=re.S)
    typed = re.findall(r"\b\d+\s*(?:second|minute|hour|day|week|month|year)s?\b", source, re.I)
    assert typed == [], f"{name} types periods instead of rendering them: {typed}"


def test_duration_words() -> None:
    assert duration(dt.timedelta(minutes=5)) == "5 minutes"
    assert duration(dt.timedelta(hours=1)) == "1 hour"
    assert duration(dt.timedelta(hours=48)) == "48 hours"
    assert duration(dt.timedelta(days=7)) == "7 days"
    assert duration(dt.timedelta(days=365)) == "12 months"
    assert duration(dt.timedelta(minutes=90)) == "90 minutes"


# --- Operator, contact, date ------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/privacy", "/terms"])
def test_operator_contact_and_date_are_shown(url: str) -> None:
    with _client(PROD_ENV) as c:
        html = c.get(url).text
    assert OPERATOR in html
    assert f'<a href="mailto:{CONTACT}">{CONTACT}</a>' in html
    assert f'<time datetime="{LAST_UPDATED.isoformat()}">' in html
    assert "Last updated" in html
    assert "[OPERATOR_NAME]" not in html and "[CONTACT_EMAIL]" not in html


@pytest.mark.parametrize("url", ["/privacy", "/terms"])
def test_development_shows_placeholders(url: str) -> None:
    with _client(DEV_ENV) as c:
        html = c.get(url).text
    assert '<span class="placeholder">[OPERATOR_NAME]</span>' in html
    assert '<span class="placeholder">[CONTACT_EMAIL]</span>' in html
    assert "mailto:" not in html


def test_terms_name_the_governing_law() -> None:
    with _client(DEV_ENV) as c:
        html = c.get("/terms").text
    assert "These terms are governed by Greek law. Courts of Athens, Greece, have jurisdiction" in (
        html
    )


def _assert_footer(html: str, where: str) -> None:
    assert html.count('<footer class="site-footer">') == 1, where
    footer = html[html.index('<footer class="site-footer">') :]
    assert '<a href="/privacy">Privacy Policy</a>' in footer, where
    assert '<a href="/terms">Terms of Use</a>' in footer, where


CONSENT = (
    '<p class="form-note">By continuing you agree to the <a href="/terms">Terms</a> and '
    '<a href="/privacy">Privacy Policy</a>.</p>'
)


@pytest.mark.parametrize("url", ["/privacy", "/terms", "/login", "/no-such-page"])
def test_footer_links_to_both_pages(url: str) -> None:
    with _client(DEV_ENV) as c:
        response = c.get(url)
    if response.headers["content-type"].startswith("text/html"):
        _assert_footer(response.text, url)
    else:
        assert response.status_code == 404  # FastAPI's JSON 404: no page, no footer


# --- With the test database: logged-in pages and the forms -------------------------------------


def test_footer_on_logged_in_pages(client: TestClient) -> None:
    for url in ("/", "/history", "/team", "/account/2fa"):
        response = client.get(url)
        assert response.status_code == 200, url
        _assert_footer(response.text, url)


def test_consent_line_on_request_access(anon: TestClient) -> None:
    assert CONSENT in anon.get("/request-access").text


def test_consent_line_on_forgot_password(
    make_app: Callable[..., FastAPI], browser: Callable[..., TestClient]
) -> None:
    app = make_app(mailer=FakeMailer())
    assert CONSENT in browser(app=app).get("/forgot-password").text


def test_consent_line_on_the_invite_page(accts: Accounts, anon: TestClient) -> None:
    org_id = accts.org("Consent Org (test)")
    token = accts.run(
        lambda db: accounts.invite(db, org_id, "invited@org.test", Role.MEMBER, now=now())
    )
    page = anon.get(f"/invite/{token}")
    assert page.status_code == 200
    assert CONSENT in page.text


def test_login_page_has_no_consent_line(anon: TestClient) -> None:
    """Signing in is not agreeing anew: the line is only where data is given or an
    account is made."""
    assert CONSENT not in anon.get("/login").text
