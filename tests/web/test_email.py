"""Email in the web flows, with the in-memory ``FakeMailer`` (never a real backend).

Invites (approve request, admin invite, team invite) and the admin/team reset buttons
email the link; "Forgot password?"; the access-request notice; the fallback when
sending fails; and that nothing changes with email off. All data is synthetic.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import access, accounts
from app.auth.access import RequestFields
from app.auth.accounts import RESET_MAX_PER_IP, RESET_MAX_PER_USER
from app.db.models import Role, VolumeBand
from app.mail import FakeMailer, Message
from app.settings import Secret, Settings
from app.web.auth import MSG_FORBIDDEN, MSG_TIMED_OUT, SESSION_COOKIE
from app.web.forgot import FORGOT_PURPOSE
from tests.web.conftest import PASSWORD, Accounts, LoggedIn, now
from tests.web.management_routes import (
    NEW_PASSWORD,
    STALE_WAYS,
    audit_actions,
    count,
    form_token,
    link_token,
    stale_form_token,
    use_link,
)

Browser = Callable[..., TestClient]

ADMIN_EMAIL = "platform.admin@org.test"
NOTIFY = "admin.notify@org.test"
API_KEY = "re_synthetic_test_key_0123456789abcdef"
LINK = re.compile(r"http://testserver/invite/([A-Za-z0-9_-]{43})")
SENT_PAGE = "If an account exists, we've sent a link."
FALLBACK = "We couldn't send the email; copy this link instead."
SPAM_HINT = "Didn't get it? Check your spam or junk folder."


def assert_sent_page(html: str, sent: str, email: str) -> None:
    """The "sent" message is one flex item (no gap before the full stop) and the spam
    hint follows it."""
    assert f'role="status"><span>{sent} <strong>{email}</strong>.</span></p>' in html
    assert SPAM_HINT in html


@pytest.fixture
def mail_settings(web_settings: Settings) -> Settings:
    return dataclasses.replace(
        web_settings,
        email_backend="resend",
        resend_api_key=Secret(API_KEY),
        email_from="eSIM Sheet <no-reply@mail.example.invalid>",
        admin_notify_email=NOTIFY,
    )


@pytest.fixture
def outbox() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def mail_app(make_app: Callable[..., FastAPI], mail_settings: Settings, outbox) -> FastAPI:
    return make_app(mail_settings, mailer=outbox)


def client(app: FastAPI, accts: Accounts, user_id: int) -> TestClient:
    return LoggedIn(app, user_id, accts.session(user_id)).client(follow_redirects=False)


def emailed_token(message: Message) -> str:
    """The one-time link's token, the same in the text and the HTML part."""
    [token] = set(LINK.findall(message.text))
    assert set(LINK.findall(message.html)) == {token}
    return token


@pytest.fixture
def admin(mail_app: FastAPI, accts: Accounts) -> TestClient:
    return client(mail_app, accts, accts.user(ADMIN_EMAIL, superadmin=True, role=Role.MEMBER))


def pending_request(accts: Accounts, email: str = "applicant@org.test") -> int:
    fields = RequestFields(
        "Example Telecom (test)", "Test Applicant", email, VolumeBand.FROM_1K_TO_10K, None
    )

    def _add(db: Session) -> int:
        access.submit_request(db, fields, ip=None, now=now())
        return db.execute(access.select(access.AccessRequest.id)).scalars().one()

    return accts.run(_add)


# --- Invites and reset buttons -----------------------------------------------------------------


def test_approve_emails_the_owner_invite(admin: TestClient, accts: Accounts, outbox) -> None:
    request_id = pending_request(accts)
    page = admin.post(f"/admin/requests/{request_id}/approve")
    assert page.status_code == 200
    assert_sent_page(page.text, "Invitation sent to", "applicant@org.test")
    [message] = outbox.outbox
    assert message.to == "applicant@org.test"
    assert message.subject == "Your invitation to eSIM Sheet"
    assert "join Example Telecom (test) on eSIM Sheet as an owner" in message.text
    assert emailed_token(message) == link_token(page.text)  # the page still offers it


def test_admin_invite_emails_the_link(
    admin: TestClient, accts: Accounts, outbox, browser: Browser
) -> None:
    org_id = accts.org("Invite Org (test)")
    page = admin.post(
        f"/admin/orgs/{org_id}/invite", data={"email": "New.Member@Org.Test", "role": "member"}
    )
    assert_sent_page(page.text, "Invitation sent to", "new.member@org.test")
    [message] = outbox.outbox
    assert message.to == "new.member@org.test"
    assert "join Invite Org (test) on eSIM Sheet as a member" in message.text
    token = emailed_token(message)
    assert token == link_token(page.text)
    assert use_link(browser(), token) == 303


def test_team_invite_emails_the_link(mail_app: FastAPI, accts: Accounts, outbox) -> None:
    org_id = accts.org("Team Org (test)")
    owner = client(mail_app, accts, accts.user("owner@org.test", org_id=org_id))
    page = owner.post("/team/invite", data={"email": "teammate@org.test", "role": "owner"})
    assert_sent_page(page.text, "Invitation sent to", "teammate@org.test")
    [message] = outbox.outbox
    assert "as a member" in message.text  # owners invite members only
    assert emailed_token(message) == link_token(page.text)


@pytest.mark.parametrize("who", ["admin", "team"])
def test_reset_buttons_email_the_link(
    who: str, mail_app: FastAPI, admin: TestClient, accts: Accounts, outbox, browser: Browser
) -> None:
    org_id = accts.org("Reset Org (test)")
    owner_id = accts.user("owner@org.test", org_id=org_id)
    member_id = accts.user("member@org.test", org_id=org_id, role=Role.MEMBER)
    if who == "admin":
        page = admin.post(f"/admin/users/{member_id}/reset-link")
    else:
        page = client(mail_app, accts, owner_id).post(f"/team/users/{member_id}/reset-link")
    assert_sent_page(page.text, "Reset link sent to", "member@org.test")
    [message] = outbox.outbox
    assert message.to == "member@org.test"
    assert message.subject == "Reset your eSIM Sheet password"
    assert "expires in 48 hours" in message.text
    token = emailed_token(message)
    assert token == link_token(page.text)
    assert use_link(browser(), token) == 303


def test_failed_email_falls_back_to_the_link(
    admin: TestClient, accts: Accounts, outbox, caplog, browser: Browser
) -> None:
    caplog.set_level(logging.DEBUG)
    outbox.fail = True
    org_id = accts.org("Invite Org (test)")
    page = admin.post(
        f"/admin/orgs/{org_id}/invite", data={"email": "new.member@org.test", "role": "owner"}
    )
    assert page.status_code == 200  # the invite was made all the same
    assert FALLBACK in page.text and "Invitation sent" not in page.text
    assert SPAM_HINT not in page.text  # nothing was sent
    token = link_token(page.text)
    assert use_link(browser(), token) == 303

    invited = accts.sql("SELECT id FROM users WHERE email = 'new.member@org.test'")[0][0]
    admin_id = accts.sql("SELECT id FROM users WHERE is_superadmin")[0][0]
    failed = [a for a in audit_actions(accts) if a[0] == "email_failed"]
    assert failed == [("email_failed", "failure", admin_id, invited, org_id)]
    assert "email not sent (fake outage)" in caplog.text
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = "\n".join(f"{r.getMessage()} {r.args!s}" for r in records)
    for secret in ("new.member@org.test", token, API_KEY, "Invite Org"):
        assert secret not in logged


def test_email_off_pages_are_unchanged(web_app: FastAPI, accts: Accounts) -> None:
    """With EMAIL_BACKEND=none (the web_app fixture): links only, no "sent" message."""
    superadmin = client(web_app, accts, accts.user(ADMIN_EMAIL, superadmin=True, role=Role.MEMBER))
    org_id = accts.org("Invite Org (test)")
    page = superadmin.post(
        f"/admin/orgs/{org_id}/invite", data={"email": "new.member@org.test", "role": "member"}
    )
    assert "Send this link to <strong>new.member@org.test</strong>." in page.text
    assert "sent to" not in page.text and FALLBACK not in page.text
    assert SPAM_HINT not in page.text
    link_token(page.text)
    login = TestClient(web_app).get("/login")
    assert "Forgot password?</a>" not in login.text
    assert "Ask your administrator for a reset link." in login.text
    assert TestClient(web_app).get("/forgot-password").status_code == 404
    assert TestClient(web_app).post("/forgot-password", data={}).status_code == 404


# --- Forgot password ---------------------------------------------------------------------------


def forgot(c: TestClient, email: str, **headers: str):
    token = form_token(c.get("/forgot-password").text)
    return c.post("/forgot-password", data={"form_token": token, "email": email}, headers=headers)


def test_login_page_links_to_forgot_password(mail_app: FastAPI) -> None:
    page = TestClient(mail_app).get("/login")
    assert '<a href="/forgot-password">Forgot password?</a>' in page.text
    assert "Ask your administrator" not in page.text


def test_forgot_password_emails_a_one_hour_link(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    user_id = accts.user("owner@org.test")
    response = forgot(browser(app=mail_app), "  OWNER@org.test ")
    assert response.status_code == 200 and SENT_PAGE in response.text
    [message] = outbox.outbox
    assert message.to == "owner@org.test"
    assert message.subject == "Reset your eSIM Sheet password"
    assert "expires in 1 hour." in message.text
    token = emailed_token(message)
    [(expires, created)] = accts.sql(
        "SELECT expires_at, created_at FROM password_tokens WHERE user_id = :u", u=user_id
    )
    assert timedelta(minutes=59) < expires - created <= timedelta(hours=1, seconds=5)
    assert ("password_reset_requested", "success", None, user_id, 1) in audit_actions(accts)

    c = browser(app=mail_app)
    assert use_link(c, token) == 303
    assert accts.sql("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == [(1,)]
    assert use_link(browser(app=mail_app), token) == 404  # single use
    assert accts.sql("SELECT password_hash IS NOT NULL FROM users WHERE id = :u", u=user_id)[0][0]


def test_forgot_password_link_expires_after_an_hour(
    mail_app: FastAPI, accts: Accounts, browser: Browser
) -> None:
    accts.user("owner@org.test")
    grant = accts.run(
        lambda db: accounts.request_password_reset(
            db, "owner@org.test", ip=None, now=now() - timedelta(minutes=61)
        )
    )
    assert grant is not None
    assert browser(app=mail_app).get(f"/invite/{grant.token}").status_code == 404


@pytest.mark.parametrize("case", ["unknown", "disabled", "no-password", "disabled-org"])
def test_forgot_password_same_answer_and_nothing_sent(
    case: str, mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    accts.user("owner@org.test")
    if case == "disabled":
        accts.user("target@org.test", disabled=True)
    elif case == "no-password":
        accts.user("target@org.test", password_hash=None)
    elif case == "disabled-org":
        accts.user("target@org.test", org_id=accts.org("Off Org (test)", disabled=True))
    known = forgot(browser(app=mail_app, ip="203.0.113.10"), "owner@org.test")
    other = forgot(browser(app=mail_app, ip="198.51.100.10"), "target@org.test")
    assert len(outbox.outbox) == 1 and outbox.outbox[0].to == "owner@org.test"
    assert (other.status_code, other.text) == (known.status_code, known.text)
    assert SPAM_HINT in known.text
    assert "set-cookie" not in other.headers and "set-cookie" not in known.headers
    assert other.headers["cache-control"] == "no-store"


def test_forgot_password_rate_limit_per_ip_prefix(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    for n in range(RESET_MAX_PER_IP):
        accts.user(f"user{n}@org.test")
    c = browser(app=mail_app, ip="203.0.113.20")
    forgot(c, "nobody@org.test")  # unknown addresses count too
    for n in range(RESET_MAX_PER_IP - 1):
        forgot(c, f"user{n}@org.test")
    assert len(outbox.outbox) == RESET_MAX_PER_IP - 1
    blocked = forgot(browser(app=mail_app, ip="203.0.113.21"), "user2@org.test")  # same /24
    assert SENT_PAGE in blocked.text
    assert len(outbox.outbox) == RESET_MAX_PER_IP - 1
    forgot(browser(app=mail_app, ip="198.51.100.20"), "user2@org.test")  # another prefix
    assert len(outbox.outbox) == RESET_MAX_PER_IP


def test_forgot_password_rate_limit_per_email(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    accts.user("owner@org.test")
    for n in range(RESET_MAX_PER_USER + 1):
        response = forgot(browser(app=mail_app, ip=f"198.51.{100 + n}.7"), "owner@org.test")
        assert SENT_PAGE in response.text
    assert len(outbox.outbox) == RESET_MAX_PER_USER
    # Only the newest link works: each one replaces the one before.
    tokens = [emailed_token(m) for m in outbox.outbox]
    assert use_link(browser(app=mail_app), tokens[0]) == 404
    assert use_link(browser(app=mail_app), tokens[-1]) == 303


def test_reset_keeps_two_factor_authentication(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    user_id = accts.user("owner@org.test", mfa=True)
    forgot(browser(app=mail_app), "owner@org.test")
    token = emailed_token(outbox.outbox[0])
    c = browser(app=mail_app)
    page = c.get(f"/invite/{token}")
    response = c.post(
        f"/invite/{token}",
        data={
            "form_token": form_token(page.text),
            "password": NEW_PASSWORD,
            "password_confirm": NEW_PASSWORD,
        },
    )
    assert response.status_code == 303 and response.headers["location"] == "/login/2fa"
    assert not any(v.startswith(SESSION_COOKIE) for v in response.headers.get_list("set-cookie"))
    assert accts.sql("SELECT mfa_enabled_at IS NOT NULL FROM users WHERE id = :u", u=user_id) == [
        (True,)
    ]
    assert count(accts, "sessions") == 0


def test_forgot_password_needs_its_form_token(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    accts.user("owner@org.test")
    c = browser(app=mail_app)
    token = form_token(c.get("/forgot-password").text)
    data = {"email": "owner@org.test"}
    assert c.post("/forgot-password", data=data).status_code == 403
    response = c.post("/forgot-password", data={**data, "form_token": token + "x"})
    assert response.status_code == 403 and response.text == MSG_FORBIDDEN
    login_token = form_token(c.get("/login").text)  # another form's token
    assert c.post("/forgot-password", data={**data, "form_token": login_token}).status_code == 403
    other = browser(app=mail_app, ip="198.51.100.30")  # someone else's token
    other.get("/forgot-password")
    assert other.post("/forgot-password", data={**data, "form_token": token}).status_code == 403
    assert outbox.outbox == []


@pytest.mark.parametrize("how", STALE_WAYS)
def test_forgot_password_timed_out_form_is_shown_again(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser, how: str
) -> None:
    accts.user("owner@org.test")
    c = browser(app=mail_app)
    page = c.get("/forgot-password")
    stale = stale_form_token(c, page.text, FORGOT_PURPOSE, how)
    response = c.post("/forgot-password", data={"form_token": stale, "email": "owner@org.test"})

    # Still refused: no link made or sent.
    assert response.status_code == 403
    assert outbox.outbox == []
    assert count(accts, "password_tokens") == 0
    # The same form again with the email kept.
    assert response.headers["cache-control"] == "no-store"
    assert MSG_TIMED_OUT in response.text and MSG_FORBIDDEN not in response.text
    assert 'action="/forgot-password"' in response.text
    assert 'value="owner@org.test"' in response.text
    # Its fresh token works.
    fresh = form_token(response.text)
    assert fresh != stale
    sent = c.post("/forgot-password", data={"form_token": fresh, "email": "owner@org.test"})
    assert sent.status_code == 200 and SENT_PAGE in sent.text
    assert [m.to for m in outbox.outbox] == ["owner@org.test"]


def test_forgot_password_malformed_email(mail_app: FastAPI, outbox, browser: Browser) -> None:
    response = forgot(browser(app=mail_app), "<b>not an email</b>")
    assert response.status_code == 400
    assert "Enter a valid email address." in response.text
    assert "<b>not an email</b>" not in response.text  # escaped
    assert outbox.outbox == []


def test_forgot_password_failure_is_audited(
    mail_app: FastAPI, accts: Accounts, outbox, caplog, browser: Browser
) -> None:
    caplog.set_level(logging.DEBUG)
    outbox.fail = True
    user_id = accts.user("owner@org.test")
    response = forgot(browser(app=mail_app), "owner@org.test")
    assert SENT_PAGE in response.text  # the same answer
    assert ("email_failed", "failure", None, user_id, 1) in audit_actions(accts)
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = "\n".join(f"{r.getMessage()} {r.args!s}" for r in records)
    assert "owner@org.test" not in logged and PASSWORD not in logged


def test_no_secrets_in_logs_for_forgot_password(
    mail_app: FastAPI, accts: Accounts, outbox, caplog, browser: Browser
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")  # SQL with hidden parameters
    accts.user("owner@org.test")
    forgot(browser(app=mail_app), "owner@org.test")
    forgot(browser(app=mail_app, ip="198.51.100.40"), "nobody@org.test")
    token = emailed_token(outbox.outbox[0])
    assert use_link(browser(app=mail_app), token) == 303
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = "\n".join(f"{r.getMessage()} {r.args!s}" for r in records)
    assert any(r.name.startswith("sqlalchemy") for r in records), "SQL log not captured"
    for secret in ("owner@org.test", "nobody@org.test", token, NEW_PASSWORD, API_KEY):
        assert secret not in logged


# --- Access requests -----------------------------------------------------------------------------

VALID = {
    "company_name": "Example Telecom (test)",
    "contact_name": "Test Applicant",
    "email": "applicant@org.test",
    "volume_band": "1k_10k",
    "note": "A synthetic note about volumes.",
    "website": "",
}


def submit(c: TestClient, **fields: str):
    token = form_token(c.get("/request-access").text)
    return c.post("/request-access", data={"form_token": token, **VALID, **fields})


def test_new_access_request_notifies_the_admin(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    response = submit(browser(app=mail_app))
    assert response.status_code == 200
    [message] = outbox.outbox
    assert message.to == NOTIFY
    assert message.subject == "New access request on eSIM Sheet"
    assert "Company: Example Telecom (test)" in message.text
    assert "Expected monthly volume: 1,000 to 10,000" in message.text
    assert "http://testserver/admin/requests" in message.text
    for private in ("Test Applicant", "applicant@org.test", "synthetic note"):
        assert private not in message.text and private not in message.html


def test_no_notice_for_honeypot_duplicate_or_rate_limited(
    mail_app: FastAPI, accts: Accounts, outbox, browser: Browser
) -> None:
    c = browser(app=mail_app, ip="203.0.113.50")
    first = submit(c)
    honeypot = submit(c, website="https://spam.example.invalid", email="bot@org.test")
    duplicate = submit(c)
    assert first.text == honeypot.text == duplicate.text
    assert len(outbox.outbox) == 1
    for n in range(access.RATE_LIMIT):
        submit(c, email=f"more{n}@org.test")
    assert count(accts, "access_requests") == access.RATE_LIMIT
    assert len(outbox.outbox) == access.RATE_LIMIT  # the rate-limited one sent nothing


def test_no_notice_without_admin_notify_email(
    make_app: Callable[..., FastAPI], mail_settings: Settings, outbox, browser: Browser
) -> None:
    app = make_app(dataclasses.replace(mail_settings, admin_notify_email=None), mailer=outbox)
    assert submit(browser(app=app)).status_code == 200
    assert outbox.outbox == []
