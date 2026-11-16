"""Login, sessions, CSRF, one-time links and route protection. All data is synthetic."""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from collections.abc import Callable

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import accounts
from app.auth.accounts import ACCOUNT_MAX_FAILURES, FAILURE_WINDOW, IP_MAX_FAILURES
from app.auth.passwords import MSG_IS_EMAIL, MSG_TOO_SHORT
from app.auth.tokens import FORM_TOKEN_MAX_AGE_SECONDS, csrf_for, new_token, sign_form_token
from app.db.models import Role
from app.settings import Settings
from app.web import auth
from app.web.admin import require_superadmin
from app.web.auth import (
    CSRF_HEADER,
    MSG_FORBIDDEN,
    MSG_LINK_INVALID,
    MSG_LOGIN_FAILED,
    MSG_PASSWORDS_DIFFER,
    PREAUTH_COOKIE,
    SESSION_COOKIE,
)
from app.web.main import SECURITY_HEADERS
from app.web.team import require_owner
from tests.web.conftest import (
    ANON_IP,
    EMAIL,
    PASSWORD,
    TEST_SECRET_KEY,
    Accounts,
    LoggedIn,
    now,
)
from tests.web.management_routes import MANAGEMENT_PATHS, MANAGEMENT_POSTS

Browser = Callable[..., TestClient]
NEW_PASSWORD = "a brand new synthetic passphrase"
_FORM_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def _form_token(html: str) -> str:
    match = _FORM_TOKEN.search(html)
    assert match, "no form token on the page"
    return match.group(1)


def _login(
    c: TestClient, email: str = EMAIL, password: str = PASSWORD, form_token: str | None = None
):
    if form_token is None:
        form_token = _form_token(c.get("/login").text)
    return c.post("/login", data={"form_token": form_token, "email": email, "password": password})


def _sessions_of(accts: Accounts, user_id: int) -> list:
    return accts.sql("SELECT token_hash FROM sessions WHERE user_id = :u", u=user_id)


def _audit(accts: Accounts) -> list[tuple]:
    return [
        tuple(row)
        for row in accts.sql(
            "SELECT action, outcome, user_id, organization_id, host(ip_prefix) || '/' "
            "|| masklen(ip_prefix) FROM audit_log ORDER BY id"
        )
    ]


def _age_audit_log(accts: Accounts, action: str | None = None) -> None:
    """Move audit rows (all, or one action's) back past the failure window."""
    accts.sql(
        "UPDATE audit_log SET created_at = created_at - make_interval(secs => :s) "
        "WHERE CAST(:action AS text) IS NULL OR action = :action RETURNING id",
        s=FAILURE_WINDOW.total_seconds() + 60,
        action=action,
    )


def _invite(accts: Accounts, org_id: int, email: str = "new.member@org.test") -> str:
    return accts.run(lambda db: accounts.invite(db, org_id, email, Role.MEMBER, now=now()))


def _set_password(
    c: TestClient, token: str, password: str = NEW_PASSWORD, confirm: str | None = None
):
    page = c.get(f"/invite/{token}")
    assert page.status_code == 200, page.text
    return c.post(
        f"/invite/{token}",
        data={
            "form_token": _form_token(page.text),
            "password": password,
            "password_confirm": password if confirm is None else confirm,
        },
    )


# --- Login ----------------------------------------------------------------------------


def test_login_page_is_public_and_csp_clean(anon: TestClient) -> None:
    response = anon.get("/login")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    html = response.text
    assert 'action="/login"' in html and 'name="form_token"' in html
    assert "hx-headers" not in html and "Log out" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and " style=" not in html
    assert PREAUTH_COOKIE in response.cookies


def test_login_success(anon: TestClient, accts: Accounts) -> None:
    user_id = accts.user()
    response = _login(anon)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie  # development: plain http (see app.web.auth)
    assert len(_sessions_of(accts, user_id)) == 1

    page = anon.get("/")
    assert page.status_code == 200
    assert EMAIL in page.text and 'action="/logout"' in page.text
    token = anon.cookies[SESSION_COOKIE]
    assert f'hx-headers=\'{{"X-CSRF-Token": "{csrf_for(token)}"}}\'' in page.text
    assert f'name="csrf_token" value="{csrf_for(token)}"' in page.text
    [(org_id,)] = accts.sql("SELECT organization_id FROM users WHERE id = :u", u=user_id)
    assert _audit(accts) == [("login", "success", user_id, org_id, "203.0.113.0/24")]


def test_login_email_is_case_insensitive(anon: TestClient, accts: Accounts) -> None:
    accts.user()
    assert _login(anon, email="  OWNER@Org.Test ").status_code == 303


def test_superadmin_without_org_can_log_in(anon: TestClient, accts: Accounts) -> None:
    accts.user("admin@org.test", superadmin=True)
    assert _login(anon, email="admin@org.test").status_code == 303
    assert anon.get("/").status_code == 200


def test_failures_look_identical(
    anon: TestClient, accts: Accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    accts.user()
    accts.user("disabled@org.test", disabled=True)
    accts.user("orgoff@org.test", org_id=accts.org("Off (test)", disabled=True))
    accts.user("pending@org.test", password_hash=None)

    verifies: list[str] = []
    real_verify, real_dummy = accounts.verify_password, accounts.verify_dummy
    monkeypatch.setattr(
        accounts, "verify_password", lambda h, p: (verifies.append("real"), real_verify(h, p))[1]
    )
    monkeypatch.setattr(
        accounts, "verify_dummy", lambda p: (verifies.append("dummy"), real_dummy(p))[1]
    )

    form_token = _form_token(anon.get("/login").text)
    cases = {
        "unknown email": ("nobody@org.test", PASSWORD),
        "wrong password": (EMAIL, "wrong password, synthetic"),
        "disabled user": ("disabled@org.test", PASSWORD),
        "disabled org": ("orgoff@org.test", PASSWORD),
        "no password yet": ("pending@org.test", PASSWORD),
    }
    responses = {}
    for name, (email, password) in cases.items():
        verifies.clear()
        responses[name] = _login(anon, email, password, form_token)
        assert len(verifies) == 1, name  # one argon2 verify in every case: similar timing

    first = next(iter(responses.values()))
    assert first.status_code == 401
    assert MSG_LOGIN_FAILED in first.text
    for name, response in responses.items():
        assert response.status_code == first.status_code, name
        assert response.text == first.text, name
        assert response.headers.get("set-cookie") == first.headers.get("set-cookie"), name
        assert SESSION_COOKIE not in response.cookies, name
    assert all(action == "login" and outcome == "failure" for action, outcome, *_ in _audit(accts))


def test_failed_login_page_does_not_echo_the_email(anon: TestClient, accts: Accounts) -> None:
    response = _login(anon, "someone.secret@org.test", "wrong password, synthetic")
    assert "someone.secret" not in response.text


def test_rehash_on_login(anon: TestClient, accts: Accounts) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    user_id = accts.user(password_hash=weak)
    assert _login(anon).status_code == 303
    [(stored,)] = accts.sql("SELECT password_hash FROM users WHERE id = :u", u=user_id)
    assert stored != weak
    assert not PasswordHasher().check_needs_rehash(stored)
    assert _login(anon).status_code == 303  # the new hash works


# --- Rate limits -------------------------------------------------------------------------


def test_limit_constants() -> None:
    assert ACCOUNT_MAX_FAILURES == 5
    assert IP_MAX_FAILURES == 20
    assert FAILURE_WINDOW.total_seconds() == 15 * 60


def test_account_limit_locks_and_unlocks(browser: Browser, accts: Accounts) -> None:
    user_id = accts.user()
    # Failures from different networks still count against the one account.
    for i in range(ACCOUNT_MAX_FAILURES - 1):
        assert _login(browser(f"10.{i}.0.1"), password="wrong password, synth").status_code == 401
    assert _login(browser("10.99.0.1")).status_code == 303  # 4 failures: still allowed

    assert _login(browser("10.98.0.1"), password="wrong password, synth").status_code == 401
    locked = _login(browser("10.97.0.1"))  # the right password, but 5 failures in 15 minutes
    assert locked.status_code == 401
    assert MSG_LOGIN_FAILED in locked.text
    assert SESSION_COOKIE not in locked.cookies
    assert _audit(accts)[-1][:3] == ("login_blocked", "failure", user_id)

    _age_audit_log(accts)
    assert _login(browser("10.96.0.1")).status_code == 303


def test_blocked_attempts_do_not_extend_the_lock(browser: Browser, accts: Accounts) -> None:
    accts.user()
    for _ in range(ACCOUNT_MAX_FAILURES):
        assert _login(browser(), password="wrong password, synth").status_code == 401
    for _ in range(3):
        assert _login(browser()).status_code == 401  # blocked
    assert [r[0] for r in _audit(accts)].count("login_blocked") == 3
    # Only the real failures age out of the window; the recent blocked attempts don't count.
    _age_audit_log(accts, action="login")
    assert _login(browser()).status_code == 303


def test_ip_limit_locks_and_unlocks(browser: Browser, accts: Accounts) -> None:
    accts.user()
    office = "198.51.100.10"
    for i in range(IP_MAX_FAILURES - 1):
        response = _login(browser(office), email=f"typo{i}@org.test")
        assert response.status_code == 401
    assert _login(browser(office)).status_code == 303  # 19 failures: not tripped

    assert _login(browser(office), email="typo-last@org.test").status_code == 401
    assert _login(browser("198.51.100.200")).status_code == 401  # same /24, right password
    assert _login(browser("192.0.2.1")).status_code == 303  # another network is unaffected

    _age_audit_log(accts)
    assert _login(browser(office)).status_code == 303


# --- Sessions ------------------------------------------------------------------------------


def test_session_row_lifetimes(logged_in: LoggedIn, accts: Accounts) -> None:
    [(created, expires, seen)] = accts.sql(
        "SELECT created_at, expires_at, last_seen_at FROM sessions WHERE user_id = :u",
        u=logged_in.user_id,
    )
    assert expires - created == accounts.SESSION_ABSOLUTE_TTL
    assert seen == created


def test_idle_timeout(client: TestClient, logged_in: LoggedIn, accts: Accounts) -> None:
    accts.sql(
        "UPDATE sessions SET last_seen_at = now() - interval '11 hours 59 minutes' RETURNING id"
    )
    assert client.get("/").status_code == 200
    accts.sql(
        "UPDATE sessions SET last_seen_at = now() - interval '12 hours 1 minute' RETURNING id"
    )
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert _sessions_of(accts, logged_in.user_id) == []  # expired rows are deleted


def test_absolute_timeout(client: TestClient, logged_in: LoggedIn, accts: Accounts) -> None:
    accts.sql(
        "UPDATE sessions SET created_at = now() - interval '7 days', "
        "expires_at = now() - interval '1 second', last_seen_at = now() RETURNING id"
    )
    assert client.get("/", follow_redirects=False).status_code == 303
    assert _sessions_of(accts, logged_in.user_id) == []


def test_last_seen_written_at_most_every_5_minutes(client: TestClient, accts: Accounts) -> None:
    accts.sql("UPDATE sessions SET last_seen_at = now() - interval '4 minutes' RETURNING id")
    [(before,)] = accts.sql("SELECT last_seen_at FROM sessions")
    assert client.get("/").status_code == 200
    assert accts.sql("SELECT last_seen_at FROM sessions") == [(before,)]

    accts.sql("UPDATE sessions SET last_seen_at = now() - interval '6 minutes' RETURNING id")
    assert client.get("/").status_code == 200
    [(after,)] = accts.sql("SELECT last_seen_at FROM sessions")
    assert (now() - after).total_seconds() < 60


def test_logout(client: TestClient, logged_in: LoggedIn, accts: Accounts) -> None:
    del client.headers[CSRF_HEADER]  # a plain form post: the token is in the form field
    response = client.post("/logout", data={"csrf_token": logged_in.csrf}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert f'{SESSION_COOKIE}=""' in response.headers["set-cookie"]
    assert _sessions_of(accts, logged_in.user_id) == []
    assert _audit(accts)[-1][:3] == ("logout", "success", logged_in.user_id)

    stale = logged_in.client(follow_redirects=False)  # the old cookie no longer works
    assert stale.get("/").status_code == 303


def _login_with_cookie(c: TestClient, session_token: str):
    """Log in while the browser sends ``session_token`` as its session cookie."""
    form_token = _form_token(c.get("/login").text)
    cookie = f"{PREAUTH_COOKIE}={c.cookies[PREAUTH_COOKIE]}; {SESSION_COOKIE}={session_token}"
    return c.post(
        "/login",
        data={"form_token": form_token, "email": EMAIL, "password": PASSWORD},
        headers={"cookie": cookie},
    )


@pytest.mark.parametrize("planted", ["unknown", "known"])
def test_login_replaces_the_session(browser: Browser, accts: Accounts, planted: str) -> None:
    """No fixation: a login never keeps the token the browser sent, it issues a new one.

    ``known``: a live session token for the account (e.g. planted by an attacker who
    has one) is deleted, not reused. ``unknown``: a made-up token is simply replaced.
    """
    user_id = accts.user()
    old = accts.session(user_id) if planted == "known" else new_token()
    response = _login_with_cookie(browser(), old)
    assert response.status_code == 303
    new = response.cookies[SESSION_COOKIE]
    assert new != old
    assert len(_sessions_of(accts, user_id)) == 1  # only the new session exists
    stale = browser()
    stale.cookies.set(SESSION_COOKIE, old)
    assert stale.get("/").status_code == 303
    fresh = browser()
    fresh.cookies.set(SESSION_COOKIE, new)
    assert fresh.get("/").status_code == 200


def test_disabled_user_loses_existing_sessions(client: TestClient, accts: Accounts) -> None:
    assert client.get("/").status_code == 200
    accts.sql("UPDATE users SET disabled = true RETURNING id")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_disabled_org_loses_existing_sessions(client: TestClient, accts: Accounts) -> None:
    assert client.get("/").status_code == 200
    accts.sql("UPDATE organizations SET disabled = true RETURNING id")
    assert client.get("/", follow_redirects=False).status_code == 303
    accts.sql("UPDATE organizations SET disabled = false RETURNING id")
    assert client.get("/").status_code == 200


# --- Route protection -------------------------------------------------------------------

PROTECTED = [("GET", "/"), ("GET", "/template.xlsx"), ("POST", "/validate"), ("POST", "/generate")]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_routes_redirect_to_login(anon: TestClient, method: str, path: str) -> None:
    response = anon.request(method, path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_routes_htmx_get_401(anon: TestClient, method: str, path: str) -> None:
    response = anon.request(method, path, headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


def test_invalid_session_cookie_redirects_and_is_cleared(anon: TestClient) -> None:
    anon.cookies.set(SESSION_COOKIE, new_token())
    response = anon.get("/")
    assert response.status_code == 303
    assert f'{SESSION_COOKIE}=""' in response.headers["set-cookie"]


def _routes(app: FastAPI) -> list:
    """Every route of the app, with routers included via ``include_router`` flattened.

    FastAPI 0.16x keeps included routers as one entry; its effective route contexts
    carry each route's path, methods and resolved dependencies.
    """
    flat: list = []
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        flat.extend(contexts() if callable(contexts) else [route])
    return flat


def test_every_app_route_is_protected_or_known_public(web_app: FastAPI) -> None:
    public = {"/healthz", "/login", "/invite/{token}", "/request-access", "/static"}
    checked = set()
    for route in _routes(web_app):
        if route.path in public:
            continue
        dependencies = [d.call for d in route.dependant.dependencies]
        assert auth.require_user in dependencies, route.path
        if route.path.startswith("/admin"):
            assert require_superadmin in dependencies, route.path
        if route.path.startswith("/team"):
            assert require_owner in dependencies, route.path
        checked.add(route.path)
    core = {"/", "/template.xlsx", "/validate", "/generate", "/logout"}
    assert checked == core | MANAGEMENT_PATHS


@pytest.mark.parametrize("path", ["/healthz", "/login", "/static/app.css", "/static/app.js"])
def test_public_routes(anon: TestClient, path: str) -> None:
    assert anon.get(path).status_code == 200


def test_healthz(anon: TestClient) -> None:
    response = anon.get("/healthz")
    assert response.text == "ok"


# --- CSRF ---------------------------------------------------------------------------------


def _post_routes(app: FastAPI) -> list[str]:
    return sorted(
        route.path
        for route in _routes(app)
        if "POST" in (getattr(route, "methods", None) or set())
        and route.path not in {"/login", "/invite/{token}", "/request-access"}
    )


def test_post_routes_are_known(web_app: FastAPI) -> None:
    expected = ["/generate", "/logout", "/validate", *MANAGEMENT_POSTS]
    assert _post_routes(web_app) == sorted(expected)


@pytest.mark.parametrize("token", [None, "", "wrong", "other-session"])
def test_csrf_required_on_every_post(
    web_app: FastAPI, logged_in: LoggedIn, accts: Accounts, token: str | None
) -> None:
    if token == "other-session":
        token = csrf_for(accts.session(accts.user("other@org.test")))
    c = logged_in.client(follow_redirects=False)
    del c.headers[CSRF_HEADER]
    headers = {} if token is None else {CSRF_HEADER: token}
    for path in _post_routes(web_app):
        multipart = c.post(path, headers=headers, files={"file": ("v.csv", b"iccid\n")})
        assert multipart.status_code == 403, path
        assert multipart.text == MSG_FORBIDDEN
        form = c.post(path, headers=headers, data={"csrf_token": token or ""})
        assert form.status_code == 403, path
    assert len(_sessions_of(accts, logged_in.user_id)) == 1  # logout did not happen


def test_csrf_header_accepted(client: TestClient) -> None:
    response = client.post("/validate", files={"file": ("v.csv", b"iccid,activation_code\n")})
    assert response.status_code == 200  # the header from the fixture is enough


def test_login_requires_form_token(browser: Browser, accts: Accounts) -> None:
    accts.user()
    c = browser()
    token = _form_token(c.get("/login").text)
    data = {"email": EMAIL, "password": PASSWORD}

    assert c.post("/login", data=data).status_code == 403  # no form token
    assert c.post("/login", data={**data, "form_token": token + "x"}).status_code == 403

    other = browser()  # a token fetched by someone else: the nonce cookie differs
    other.get("/login")
    assert other.post("/login", data={**data, "form_token": token}).status_code == 403

    nonce = c.cookies[PREAUTH_COOKIE]
    expired = sign_form_token(
        TEST_SECRET_KEY.encode(), "login", nonce, now=time.time() - FORM_TOKEN_MAX_AGE_SECONDS - 1
    )
    assert c.post("/login", data={**data, "form_token": expired}).status_code == 403

    assert c.post("/login", data={**data, "form_token": token}).status_code == 303
    assert accts.sql("SELECT count(*) FROM audit_log WHERE action = 'login'") == [(1,)]


def test_login_form_body_is_capped(anon: TestClient) -> None:
    response = anon.post("/login", data={"email": "x" * (auth.MAX_FORM_BYTES + 1)})
    assert response.status_code == 413


# --- Invites and reset links ---------------------------------------------------------------


def test_invite_sets_password_and_logs_in(browser: Browser, accts: Accounts) -> None:
    org_id = accts.org()
    token = _invite(accts, org_id)
    c = browser()
    page = c.get(f"/invite/{token}")
    assert page.status_code == 200
    assert "new.member@org.test" in page.text and page.headers["cache-control"] == "no-store"

    response = _set_password(c, token)
    assert response.status_code == 303 and response.headers["location"] == "/"
    home = c.get("/")
    assert home.status_code == 200 and "new.member@org.test" in home.text
    [(user_id,)] = accts.sql("SELECT id FROM users WHERE email = 'new.member@org.test'")
    actions = [row[:4] for row in _audit(accts)]
    assert actions == [
        ("invite_accepted", "success", user_id, org_id),
        ("password_set", "success", user_id, org_id),
    ]
    # The new password works for a normal login too.
    assert _login(browser(), "new.member@org.test", NEW_PASSWORD).status_code == 303


def test_invite_is_single_use(browser: Browser, accts: Accounts) -> None:
    token = _invite(accts, accts.org())
    c = browser()
    page = c.get(f"/invite/{token}")
    form_token, nonce = _form_token(page.text), c.cookies[PREAUTH_COOKIE]
    assert _set_password(c, token).status_code == 303

    again = browser()
    used = again.get(f"/invite/{token}")
    assert used.status_code == 404 and MSG_LINK_INVALID in used.text
    # Replaying the old form with a still-valid form token and its nonce cookie
    # gets past the CSRF check, and the link still cannot be used again.
    replay = browser()
    replay.cookies.set(PREAUTH_COOKIE, nonce)
    response = replay.post(
        f"/invite/{token}",
        data={"form_token": form_token, "password": "x" * 20, "password_confirm": "x" * 20},
    )
    assert response.status_code == 404 and MSG_LINK_INVALID in response.text
    assert _login(browser(), "new.member@org.test", NEW_PASSWORD).status_code == 303


def test_invalid_used_and_expired_links_look_the_same(browser: Browser, accts: Accounts) -> None:
    org_id = accts.org()
    used = _invite(accts, org_id, "used@org.test")
    _set_password(browser(), used)
    expired = _invite(accts, org_id, "expired@org.test")
    accts.sql("UPDATE invites SET expires_at = now() - interval '1 second' RETURNING id")
    reset_expired = accts.run(lambda db: accounts.reset_link(db, "used@org.test", now=now()))
    accts.sql("UPDATE password_tokens SET expires_at = now() - interval '1 second' RETURNING id")

    pages = [
        browser().get(f"/invite/{t}")
        for t in (used, expired, reset_expired, new_token(), "short", "x" * 300)
    ]
    assert {p.status_code for p in pages} == {404}
    assert len({p.text for p in pages}) == 1
    assert MSG_LINK_INVALID in pages[0].text


def test_invite_expires_after_48_hours(accts: Accounts) -> None:
    _invite(accts, accts.org())
    [(created, expires)] = accts.sql("SELECT created_at, expires_at FROM invites")
    assert abs((expires - created) - accounts.LINK_TTL).total_seconds() < 5


def test_set_password_policy(browser: Browser, accts: Accounts) -> None:
    token = _invite(accts, accts.org())
    c = browser()
    for password, confirm, message in [
        ("short", None, MSG_TOO_SHORT),
        (NEW_PASSWORD, NEW_PASSWORD + "!", MSG_PASSWORDS_DIFFER),
        ("New.Member@org.test", None, MSG_IS_EMAIL),
    ]:
        response = _set_password(c, token, password, confirm)
        assert response.status_code == 400
        assert message in response.text
        assert SESSION_COOKIE not in response.cookies
    assert _set_password(c, token).status_code == 303  # the link still works afterwards


def test_set_password_requires_form_token(browser: Browser, accts: Accounts) -> None:
    org_id = accts.org()
    token, other = _invite(accts, org_id), _invite(accts, org_id, "second@org.test")
    c = browser()
    other_form = _form_token(c.get(f"/invite/{other}").text)
    data = {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}
    assert c.post(f"/invite/{token}", data=data).status_code == 403
    # A form token for another link is not accepted.
    assert c.post(f"/invite/{token}", data={**data, "form_token": other_form}).status_code == 403
    assert _set_password(c, token).status_code == 303


def test_reset_link_ends_other_sessions(
    client: TestClient, logged_in: LoggedIn, browser: Browser, accts: Accounts
) -> None:
    assert client.get("/").status_code == 200
    token = accts.run(lambda db: accounts.reset_link(db, EMAIL, now=now()))
    c = browser()
    assert _set_password(c, token).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303  # the old session ended
    assert c.get("/").status_code == 200
    assert _login(browser()).status_code == 401  # the old password no longer works
    assert _login(browser(), password=NEW_PASSWORD).status_code == 303
    assert [r[0] for r in _audit(accts)][:1] == ["password_set"]


def test_disabled_user_cannot_use_a_link(browser: Browser, accts: Accounts) -> None:
    token = _invite(accts, accts.org())
    accts.sql("UPDATE users SET disabled = true RETURNING id")
    assert browser().get(f"/invite/{token}").status_code == 404


def test_new_link_replaces_older_ones(browser: Browser, accts: Accounts) -> None:
    org_id = accts.org()
    first = _invite(accts, org_id)
    second = _invite(accts, org_id)
    assert browser().get(f"/invite/{first}").status_code == 404
    assert browser().get(f"/invite/{second}").status_code == 200


# --- Production cookies -------------------------------------------------------------------


def test_production_cookies(
    make_app: Callable[..., FastAPI], web_settings: Settings, browser: Browser, accts: Accounts
) -> None:
    accts.user()
    prod = dataclasses.replace(web_settings, env="production", public_base_url="https://testserver")
    c = browser(app=make_app(prod), base_url="https://testserver")
    page = c.get("/login")
    preauth = page.headers["set-cookie"]
    assert preauth.startswith(f"__Host-{PREAUTH_COOKIE}=")
    for part in ("HttpOnly", "Secure", "Path=/", "SameSite=strict"):
        assert part in preauth
    assert "Domain" not in preauth

    response = _login(c, form_token=_form_token(page.text))
    assert response.status_code == 303
    session = next(
        v
        for k, v in response.headers.multi_items()
        if k == "set-cookie" and v.startswith(f"__Host-{SESSION_COOKIE}=")
    )
    for part in ("HttpOnly", "Secure", "Path=/", "SameSite=lax", "Max-Age=604800"):
        assert part in session
    assert "Domain" not in session
    assert c.get("/").status_code == 200


# --- Nothing secret in logs -----------------------------------------------------------------


def test_no_password_token_or_email_in_logs(
    caplog: pytest.LogCaptureFixture, browser: Browser, accts: Accounts
) -> None:
    caplog.set_level(logging.DEBUG)
    # SQLAlchemy keeps its loggers at WARN; turn on the SQL statement log (what echo=True
    # shows) to check that bound values stay hidden. DEBUG would add result rows, which
    # contain account data by design, so production must never run it at DEBUG.
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    org_id = accts.org()
    accts.user()
    c = browser()
    _login(c, password="wrong password, synthetic")
    assert _login(c).status_code == 303
    session_token = c.cookies[SESSION_COOKIE]
    c.post("/logout", data={"csrf_token": csrf_for(session_token)})
    invite_token = _invite(accts, org_id, "invited.person@org.test")
    assert _set_password(browser(), invite_token).status_code == 303

    # httpx logs the test client's own requests (URLs included); that is the test's
    # client, not the server. The server's access log is uvicorn's (test below).
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = "\n".join(f"{r.getMessage()} {r.args!s}" for r in records)
    secrets = [
        PASSWORD,
        NEW_PASSWORD,
        "wrong password, synthetic",
        session_token,
        csrf_for(session_token),
        invite_token,
        EMAIL,
        "invited.person",
    ]
    assert any(r.name.startswith("sqlalchemy") for r in records), "SQL log not captured"
    leaked = [i for i, s in enumerate(secrets) if s in logged]
    assert leaked == []


def test_access_log_redacts_link_tokens(web_app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    token = new_token()
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    # The call uvicorn's access logger makes for every request.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', f"{ANON_IP}:50000", "GET", f"/invite/{token}", "1.1", 200
    )
    assert token not in caplog.text
    assert "/invite/[redacted]" in caplog.text


def test_session_resolution_ignores_malformed_tokens(accts: Accounts) -> None:
    def resolve(db: Session) -> list:
        return [
            accounts.resolve_session(db, value, now=now())
            for value in (None, "", "short", "x" * 500, "'; DROP TABLE users; --")
        ]

    assert accts.run(resolve) == [None] * 5
