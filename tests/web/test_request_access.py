"""The public "Request access" form. All data is synthetic."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.auth.access import RATE_LIMIT
from app.web.auth import MSG_FORBIDDEN, MSG_TIMED_OUT, PREAUTH_COOKIE
from app.web.public import (
    MSG_COMPANY,
    MSG_CONTROL,
    MSG_EMAIL,
    MSG_NAME,
    MSG_NOTE,
    MSG_VOLUME,
    REQUEST_ACCESS_PURPOSE,
)
from tests.web.conftest import Accounts
from tests.web.management_routes import STALE_WAYS, count, form_token, stale_form_token

Browser = Callable[..., TestClient]

VALID = {
    "company_name": "Example Telecom (test)",
    "contact_name": "Test Applicant",
    "email": "applicant@org.test",
    "volume_band": "1k_10k",
    "note": "We send about 2,000 eSIMs a month.",
    "website": "",
}


def submit(c: TestClient, **fields: str):
    token = form_token(c.get("/request-access").text)
    return c.post("/request-access", data={"form_token": token, **VALID, **fields})


def stored(accts: Accounts) -> list[tuple]:
    rows = accts.sql(
        "SELECT company_name, contact_name, email, volume_band, note, status "
        "FROM access_requests ORDER BY id"
    )
    return [tuple(r) for r in rows]


def test_valid_request_is_stored(anon: TestClient, accts: Accounts) -> None:
    response = submit(anon, email="  Applicant@ORG.test ")
    assert response.status_code == 200
    assert "We have received your request" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert stored(accts) == [
        (
            "Example Telecom (test)",
            "Test Applicant",
            "applicant@org.test",
            "1k_10k",
            "We send about 2,000 eSIMs a month.",
            "pending",
        )
    ]
    audit = accts.sql(
        "SELECT action, outcome, user_id, target_user_id, organization_id, "
        "host(ip_prefix) || '/' || masklen(ip_prefix) FROM audit_log"
    )
    assert [tuple(r) for r in audit] == [
        ("access_request_created", "success", None, None, None, "203.0.113.0/24")
    ]


def test_empty_note_is_stored_as_null(anon: TestClient, accts: Accounts) -> None:
    assert submit(anon, note="   ").status_code == 200
    assert accts.sql("SELECT note FROM access_requests")[0][0] is None


def test_long_multibyte_note_is_accepted(anon: TestClient, accts: Accounts) -> None:
    note = "€" * 2000  # 6000 bytes in UTF-8, about 18 KB percent-encoded
    assert submit(anon, note=note).status_code == 200
    assert accts.sql("SELECT note FROM access_requests")[0][0] == note


def test_form_token_required(browser: Browser, accts: Accounts) -> None:
    c = browser()
    assert c.post("/request-access", data=VALID).status_code == 403
    assert c.post("/request-access", data={**VALID, "form_token": "x.y.z"}).text == MSG_FORBIDDEN
    token = form_token(c.get("/request-access").text)
    other = browser()  # another browser's token: its pre-auth cookie differs
    other.get("/request-access")
    assert other.post("/request-access", data={**VALID, "form_token": token}).status_code == 403
    assert stored(accts) == []


@pytest.mark.parametrize("how", STALE_WAYS)
def test_timed_out_form_is_shown_again_with_only_the_email(
    browser: Browser, accts: Accounts, how: str
) -> None:
    c = browser()
    page = c.get("/request-access")
    stale = stale_form_token(c, page.text, REQUEST_ACCESS_PURPOSE, how)
    response = c.post("/request-access", data={"form_token": stale, **VALID})

    # Still refused: nothing stored.
    assert response.status_code == 403
    assert stored(accts) == []
    # The same form again, with only the email re-filled.
    assert response.headers["cache-control"] == "no-store"
    assert MSG_TIMED_OUT in response.text and MSG_FORBIDDEN not in response.text
    assert f'autocomplete="email" value="{VALID["email"]}"' in response.text
    assert 'autocomplete="organization" value=""' in response.text
    assert 'autocomplete="name" value=""' in response.text
    assert "selected" not in response.text
    assert 'maxlength="2000"></textarea>' in response.text
    for field in ("company_name", "contact_name", "note"):
        assert VALID[field] not in response.text
    # Its fresh token works.
    fresh = form_token(response.text)
    assert fresh != stale
    done = c.post("/request-access", data={"form_token": fresh, **VALID})
    assert done.status_code == 200 and "We have received your request" in done.text
    assert len(stored(accts)) == 1


def test_timed_out_cross_site_post_places_no_text_on_the_page(
    browser: Browser, accts: Accounts
) -> None:
    """Another site's post never carries the SameSite=Strict pre-auth cookie, so it is
    always timed out. Apart from the escaped email, none of its text reaches the page."""
    c = browser()
    page = c.get("/request-access")
    stale = stale_form_token(c, page.text, REQUEST_ACCESS_PURPOSE, "cookie_gone")
    planted = {
        "company_name": "Call 555-0100 to verify (planted)",
        "contact_name": "Planted Name",
        "volume_band": "over_100k",
        "note": "Your account is locked, visit http://phish.test",
        "website": "http://spam.test",
        "email": '"><b>planted</b>@org.test',
    }
    response = c.post("/request-access", data={"form_token": stale, **planted})
    assert response.status_code == 403 and MSG_TIMED_OUT in response.text
    for field in ("company_name", "contact_name", "note", "website"):
        assert planted[field] not in response.text
    assert "phish.test" not in response.text and "spam.test" not in response.text
    assert "selected" not in response.text
    assert "<b>planted</b>" not in response.text
    assert 'value="&#34;&gt;&lt;b&gt;planted&lt;/b&gt;@org.test"' in response.text
    assert stored(accts) == []


def test_timed_out_form_caps_the_email(browser: Browser) -> None:
    c = browser()
    page = c.get("/request-access")
    stale = stale_form_token(c, page.text, REQUEST_ACCESS_PURPOSE, "cookie_gone")
    email = "a" * 300 + "@org.test"
    response = c.post("/request-access", data={"form_token": stale, **VALID, "email": email})
    assert response.status_code == 403
    assert f'value="{"a" * 254}"' in response.text and "a" * 255 not in response.text


def test_preauth_cookie_is_strict(anon: TestClient) -> None:
    response = anon.get("/request-access")
    assert PREAUTH_COOKIE in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_honeypot_looks_like_success_and_stores_nothing(browser: Browser, accts: Accounts) -> None:
    real = submit(browser("198.51.100.1"), email="real@org.test")
    bot = submit(browser("198.51.100.2"), email="bot@org.test", website="http://spam.test")
    assert bot.status_code == real.status_code == 200
    assert bot.content == real.content
    assert bot.headers.get("set-cookie") == real.headers.get("set-cookie")
    assert [r[2] for r in stored(accts)] == ["real@org.test"]
    assert count(accts, "audit_log") == 1  # only the real one


def test_honeypot_wins_over_invalid_fields(anon: TestClient, accts: Accounts) -> None:
    response = submit(anon, email="not an email", website="filled")
    assert response.status_code == 200
    assert "We have received your request" in response.text
    assert stored(accts) == []


def test_rate_limit_per_ip_prefix(browser: Browser, accts: Accounts) -> None:
    c = browser("203.0.113.10")
    first = submit(c, email="first@org.test")
    for i in range(1, RATE_LIMIT):
        assert submit(c, email=f"user{i}@org.test").status_code == 200
    assert count(accts, "access_requests") == RATE_LIMIT

    same_prefix = browser("203.0.113.99")  # same /24
    over = submit(same_prefix, email="over@org.test")
    assert over.status_code == 200
    assert over.content == first.content
    assert count(accts, "access_requests") == RATE_LIMIT
    assert count(accts, "access_requests", "email = 'over@org.test'") == 0

    assert submit(browser("198.51.100.7"), email="other@org.test").status_code == 200
    assert count(accts, "access_requests") == RATE_LIMIT + 1

    # An hour later the prefix may send again.
    accts.sql("UPDATE audit_log SET created_at = created_at - interval '61 minutes' RETURNING id")
    assert submit(same_prefix, email="later@org.test").status_code == 200
    assert count(accts, "access_requests", "email = 'later@org.test'") == 1


def test_same_answer_for_existing_account_new_email_and_duplicate(
    browser: Browser, accts: Accounts
) -> None:
    accts.user("existing@org.test")
    existing = submit(browser("198.51.100.1"), email="existing@org.test")
    new = submit(browser("198.51.100.2"), email="new@org.test")
    duplicate = submit(browser("198.51.100.3"), email="NEW@org.test")
    for response in (existing, new, duplicate):
        assert response.status_code == 200
        assert response.content == new.content
        assert response.headers.get("set-cookie") == new.headers.get("set-cookie")
    # Stored: the existing account's request (the admin sees it) and one for the new email.
    assert [r[2] for r in stored(accts)] == ["existing@org.test", "new@org.test"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"company_name": "  "}, MSG_COMPANY),
        ({"company_name": "x" * 201}, MSG_COMPANY),
        ({"contact_name": ""}, MSG_NAME),
        ({"contact_name": "y" * 201}, MSG_NAME),
        ({"email": "not-an-email"}, MSG_EMAIL),
        ({"email": "a@b"}, MSG_EMAIL),
        ({"volume_band": "lots"}, MSG_VOLUME),
        ({"volume_band": ""}, MSG_VOLUME),
        ({"note": "n" * 2001}, MSG_NOTE),
        ({"company_name": "Bad\x00Name"}, MSG_CONTROL),
        ({"note": "bell\x07"}, MSG_CONTROL),
    ],
)
def test_invalid_input_is_shown_and_not_stored(
    anon: TestClient, accts: Accounts, fields: dict[str, str], message: str
) -> None:
    response = submit(anon, **fields)
    assert response.status_code == 400
    assert message in response.text
    assert stored(accts) == []


def test_boundary_lengths_are_accepted(anon: TestClient, accts: Accounts) -> None:
    response = submit(anon, company_name="c" * 200, contact_name="n" * 200, note="z" * 2000)
    assert response.status_code == 200
    assert count(accts, "access_requests") == 1


def test_login_page_links_to_request_access(anon: TestClient) -> None:
    assert 'href="/request-access"' in anon.get("/login").text


def test_request_page_is_csp_clean(anon: TestClient) -> None:
    html = anon.get("/request-access").text
    assert "style=" not in html
    assert html.count("<script") == html.count('<script src="/static/')
    assert 'class="hp-field"' in html
    assert 'name="website"' in html
